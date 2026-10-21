from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional

try:
    from src.data_loader import parse_timestamp
    from src.policy import PolicyEngine, PolicyResult
    from src.goals import load_goal_specs
except ImportError:
    from data_loader import parse_timestamp
    from policy import PolicyEngine, PolicyResult
    from goals import load_goal_specs


UNSUPPORTED_CLAIM_PATTERNS = (
    "confirmed in billing",
    "billing",
    "external",
    "verified by",
    "confirmed externally",
    "outside data",
    "not in our data",
)


@dataclass(frozen=True)
class ModelDecision:
    """Outcome of evaluating one model output."""
    allowed: bool
    reason: str
    action_id: str
    user_id: str
    action_type: str
    rule: str = ""
    proposed_action: Optional[Dict[str, Any]] = None
    model_output: Any = None


class ModelClient:
    def __init__(self, engine, *, goal_specs=None):
        self.engine = engine
        self.goal_specs = dict(goal_specs or {})
        self._outputs = {}
        self._faults = {}

    def register_output(self, trigger_event_id, output, *, fault=None):
        self._outputs[str(trigger_event_id).strip()] = output
        self._faults[str(trigger_event_id).strip()] = fault

    def complete(self, trigger_event_id, user_id, trigger_at,
                 *, action_type="push", action_id=None, title_id=None):
        trigger_event_id = str(trigger_event_id).strip()
        user_id = str(user_id).strip()
        action_id = (str(action_id) if action_id else f"A-{trigger_event_id[:8]}").strip()

        raw = self._outputs.get(trigger_event_id)
        fault = self._faults.get(trigger_event_id)

        if fault == "timeout":
            return ModelDecision(
                allowed=False,
                reason=f"model provider timed out for trigger {trigger_event_id}",
                action_id=action_id,
                user_id=user_id,
                action_type="no_action",
                rule="model_timeout",
                model_output=raw,
            )

        if raw is None:
            return ModelDecision(
                allowed=False,
                reason=f"no model output registered for trigger {trigger_event_id}",
                action_id=action_id,
                user_id=user_id,
                action_type="no_action",
                rule="missing_model_output",
            )

        try:
            parsed = self._parse(raw)
        except (ValueError, TypeError) as exc:
            return ModelDecision(
                allowed=False,
                reason=f"malformed model output: {exc}",
                action_id=action_id,
                user_id=user_id,
                action_type="no_action",
                rule="malformed_output",
                model_output=raw,
            )

        try:
            self._validate_structure(parsed)
        except ValueError as exc:
            return ModelDecision(
                allowed=False,
                reason=f"malformed model output: {exc}",
                action_id=action_id,
                user_id=user_id,
                action_type="no_action",
                rule="malformed_output",
                model_output=parsed,
            )

        claim_reason = self._check_unsupported_claim(parsed)
        if claim_reason is not None:
            return ModelDecision(
                allowed=False,
                reason=claim_reason,
                action_id=action_id,
                user_id=user_id,
                action_type="no_action",
                rule="unsupported_claim",
                model_output=parsed,
            )

        if not parsed.get("eligible"):
            goal_id = str(parsed.get("goal_id") or "").strip()
            return ModelDecision(
                allowed=False,
                reason=f"goal {goal_id} not eligible",
                action_id=action_id,
                user_id=user_id,
                action_type="no_action",
                rule="ineligible_goal",
                model_output=parsed,
            )

        proposed = self._build_proposed_action(
            parsed, action_id=action_id, user_id=user_id,
            trigger_event_id=trigger_event_id, trigger_at=trigger_at,
            action_type=action_type, title_id=title_id,
        )
        policy_result = self.engine.validate(proposed)
        if policy_result.rejected:
            return ModelDecision(
                allowed=False,
                reason=policy_result.reason,
                action_id=action_id,
                user_id=user_id,
                action_type=policy_result.action_type,
                rule=policy_result.rule,
                model_output=parsed,
                proposed_action=proposed,
            )

        return ModelDecision(
            allowed=True,
            reason="",
            action_id=action_id,
            user_id=user_id,
            action_type=policy_result.action_type,
            rule=policy_result.rule,
            proposed_action=proposed,
            model_output=parsed,
        )

    @staticmethod
    def _parse(raw):
        if isinstance(raw, dict):
            return dict(raw)
        if isinstance(raw, str):
            text = raw.strip()
            if not text:
                raise ValueError("empty model output")
            try:
                return json.loads(text)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSON: {exc}") from exc
        raise ValueError(f"unsupported model output type: {type(raw).__name__}")

    @staticmethod
    def _validate_structure(parsed):
        goal_id = str(parsed.get("goal_id") or "").strip()
        if not goal_id:
            raise ValueError("missing goal_id")
        if not isinstance(parsed.get("eligible"), bool):
            raise ValueError("eligible must be a boolean")
        confidence = parsed.get("confidence")
        if confidence is not None:
            try:
                conf = float(confidence)
            except (TypeError, ValueError):
                raise ValueError("confidence must be a number")
            if not 0.0 <= conf <= 1.0:
                raise ValueError("confidence must be between 0 and 1")
        reason = parsed.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("missing reason")

    def _check_unsupported_claim(self, parsed):
        goal_id = str(parsed.get("goal_id") or "").strip()
        if self.goal_specs and goal_id not in self.goal_specs:
            return f"goal {goal_id} is not a known goal"
        reason_text = str(parsed.get("reason") or "")
        lowered = reason_text.lower()
        for pattern in UNSUPPORTED_CLAIM_PATTERNS:
            if pattern in lowered:
                return f"unsupported claim in model reason: {reason_text!r}"
        return None

    def _build_proposed_action(self, parsed, *, action_id, user_id,
                                trigger_event_id, trigger_at,
                                action_type, title_id=None):
        action = {
            "action_id": action_id,
            "user_id": user_id,
            "type": action_type,
            "trigger_event_id": trigger_event_id,
            "send_at": trigger_at.isoformat(),
        }
        resolved_title_id = title_id or str(parsed.get("title_id") or "").strip()
        if not resolved_title_id:
            resolved_title_id = self._lookup_title_id(user_id)
        if resolved_title_id:
            action["title_id"] = resolved_title_id

        if action_type == "push":
            text = parsed.get("text") or {}
            title = str(text.get("title") or "").strip()
            body = str(text.get("body") or "").strip()
            if not title:
                title = f"New content for {resolved_title_id}" if resolved_title_id else "New content"
            if not body:
                body = "Open the app to see what's new."
            action["text"] = {"title": title, "body": body}
            deeplink = str(parsed.get("deeplink") or "").strip()
            if deeplink:
                action["deeplink"] = deeplink
        elif action_type == "whatsapp":
            template_id = str(parsed.get("template_id") or "").strip()
            if template_id:
                action["template_id"] = template_id
            variables = parsed.get("variables")
            if isinstance(variables, dict):
                action["variables"] = dict(variables)
        elif action_type == "home_page":
            dialect = str(parsed.get("dialect") or "").strip()
            if dialect:
                action["dialect"] = dialect
            rows = parsed.get("rows")
            if isinstance(rows, list):
                action["rows"] = rows
            hero = str(parsed.get("hero_title_id") or "").strip()
            if hero:
                action["hero_title_id"] = hero
        return action

    def _lookup_title_id(self, user_id):
        profile = self.engine.profile_for(user_id)
        dialect = getattr(profile, "preferred_dialect", None) if profile else None
        titles = self.engine.pack.titles if self.engine.pack else {}
        candidates = []
        for title in titles.values():
            if dialect and getattr(title, "dialect", None) == dialect:
                candidates.append(title)
        if not candidates:
            candidates = list(titles.values())
        if not candidates:
            return ""
        candidates.sort(key=lambda t: str(getattr(t, "title_id", "")))
        return str(getattr(candidates[0], "title_id", ""))


__all__ = [
    "ModelClient",
    "ModelDecision",
    "UNSUPPORTED_CLAIM_PATTERNS",
]