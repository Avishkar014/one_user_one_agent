# -*- coding: utf-8 -*-
"""Replay orchestration: the main agent loop for the One User, One Agent assignment.

``run_replay`` drives the scheduled replay over the replay day's triggers.  It
integrates with the real project APIs:

* :class:`src.data_loader.DataPack` -- the catalogue, profiles, arms, historical
  events, replay triggers and live events.
* :class:`src.goals.GoalManager` -- eligibility, measurable-goal selection and
  goal-outcome tracking (including the G03 rule correction loaded from the
  control events).
* :class:`src.model_client.ModelClient` -- mock/model-provider replay through
  :meth:`ModelClient.complete`, which delegates policy validation to
  :class:`src.policy.PolicyEngine` exactly once per proposed action.
* :class:`src.policy.PolicyEngine` -- consent, quiet-hours, frequency and
  home-page validation.

Actions are written to ``actions.jsonl`` in simulator-compatible form: the
action type is carried under the key ``type`` (never ``atype``) and home-page
actions preserve ``dialect``, ``hero_title_id`` and ``rows`` with no ``send_at``.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from src.data_loader import (
    DataPack,
    DataPackPaths,
    Event,
    LoadReport,
    load_data_pack,
    parse_timestamp,
)
from src.goals import GoalManager, ACTIVE, SUCCEEDED, FAILED
from src.model_client import ModelClient, ModelDecision
from src.policy import PolicyEngine

__all__ = [
    "DECISION_MIN_REQUIRED_FIELDS",
    "DEFAULT_OUTPUT_DIR",
    "ActionRecord",
    "DecisionRecord",
    "RunReport",
    "load_model_fault_mapping",
    "run_replay",
]

UTC = timezone.utc

DEFAULT_OUTPUT_DIR = "sample_run"

DECISION_MIN_REQUIRED_FIELDS = {
    "decision_id",
    "user_id",
    "trigger_event_id",
    "goal_id",
    "action",
    "reason",
    "published_at",
    "processing_ms",
}

FAULT_RULES = frozenset(
    {
        "model_timeout",
        "unsupported_claim",
        "malformed_output",
        "missing_model_output",
    }
)


def _ist_day(when: datetime) -> str:
    """The IST calendar date of a UTC instant."""
    return (
        when.astimezone(UTC)
        .astimezone(timezone(timedelta(hours=5, minutes=30)))
        .date()
        .isoformat()
    )


def _strip(value: Any) -> Optional[str]:
    """Normalise a loader/JSONL field for stable string keys."""
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    return str(value).strip()


def _is_writeable(path: str) -> bool:
    """True when the parent directory exists and is writable."""
    parent = os.path.dirname(os.path.abspath(path))
    return os.path.isdir(parent) and os.access(parent, os.W_OK)


def _dedup_preserving_order(items: Sequence[Event]) -> List[Event]:
    """Drop exact event_id duplicates, keeping the first occurrence."""
    seen: set = set()
    out: List[Event] = []
    for item in items:
        if item.event_id in seen:
            continue
        seen.add(item.event_id)
        out.append(item)
    return out


def load_model_fault_mapping(fault_jsonl: str) -> Dict[str, Dict[str, Any]]:
    """Load the model fault mapping from a JSONL file, keyed by ``trigger_event_id``.

    Each line of ``replay/model_faults.jsonl`` names one trigger and the fault
    (``unsupported_claim``, ``malformed_output`` or ``timeout``) the mock model
    provider must reproduce for it.
    """
    mapping: Dict[str, Dict[str, Any]] = {}
    with open(fault_jsonl, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            entry = json.loads(line)
            trigger_event_id = str(entry.get("trigger_event_id", "")).strip()
            if trigger_event_id:
                mapping[trigger_event_id] = entry
    return mapping


def load_control_events(control_jsonl: str) -> List[Dict[str, Any]]:
    """Load operational control events (withdrawals, outages, degradations)."""
    events: List[Dict[str, Any]] = []
    if not control_jsonl or not os.path.isfile(control_jsonl):
        return events
    with open(control_jsonl, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            events.append(json.loads(line))
    return events


@dataclass(frozen=True)
class ActionRecord:
    """One published executable agent action (simulator input).

    The simulator reads the action type from the ``type`` key.  Home-page
    actions carry ``dialect``, ``hero_title_id`` and ``rows`` and never
    ``send_at``; push/whatsapp actions carry ``send_at`` and ``published_at``.
    """

    action_id: Optional[str] = None
    decision_id: Optional[str] = None
    user_id: Optional[str] = None
    trigger_event_id: Optional[str] = None
    type: Optional[str] = None
    published_at: Optional[str] = None
    send_at: Optional[str] = None
    dialect: Optional[str] = None
    hero_title_id: Optional[str] = None
    rows: Optional[List[Dict[str, Any]]] = None
    title_id: Optional[str] = None
    text: Optional[Dict[str, Any]] = None
    deeplink: Optional[str] = None
    template_id: Optional[str] = None
    variables: Optional[Dict[str, Any]] = None


@dataclass(frozen=True)
class DecisionRecord:
    """One processed trigger, including intentional no-action decisions."""

    decision_id: Optional[str] = None
    user_id: Optional[str] = None
    trigger_event_id: Optional[str] = None
    goal_id: Optional[str] = None
    action: Optional[str] = None
    reason: str = ""
    rule: str = ""
    published_at: Optional[str] = None
    processing_ms: int = 0
    title_id: Optional[str] = None


@dataclass(frozen=True)
class RunReport:
    """Summary of one replay run."""

    decisions_total: int = None
    actions_published: int = None
    no_action_decisions: int = None
    fault_no_action_decisions: int = None
    policy_no_action_decisions: int = None
    holdout_decisions: int = None
    errors: List[str] = field(default_factory=list)


def _utcnow() -> datetime:
    """A stable deterministic wall-clock."""
    return datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)


def _is_title_withdrawn(
    proposed: Mapping[str, Any],
    trigger_at: datetime,
    withdrawn_titles: Sequence[Tuple[str, datetime]],
) -> bool:
    """True when ``proposed`` promotes a title withdrawn at or before ``trigger_at``."""
    for title_id, withdrawn_at in withdrawn_titles:
        if trigger_at < withdrawn_at:
            continue
        if proposed.get("title_id") == title_id:
            return True
        if proposed.get("hero_title_id") == title_id:
            return True
        for row in proposed.get("rows") or []:
            if not isinstance(row, Mapping):
                continue
            if title_id in row.get("title_ids", []):
                return True
    return False


def _prior_events_for_user(
    pack: DataPack,
    user_id: str,
    cutoff: datetime,
) -> List[Event]:
    """Historical and live events for ``user_id`` available at or before ``cutoff``.

    Events whose trustworthy ``received_at`` is after the trigger's delivery time
    are excluded, so no future-event leakage reaches eligibility or success checks.
    """
    prior: List[Event] = []
    for event in pack.events_for_user(user_id):
        received = parse_timestamp(event.received_at)
        if received is not None and received <= cutoff:
            prior.append(event)
    for event in pack.live_events:
        if event.user_id != user_id:
            continue
        received = parse_timestamp(event.received_at)
        if received is not None and received <= cutoff:
            prior.append(event)
    prior = _dedup_preserving_order(prior)
    prior.sort(
        key=lambda e: (
            parse_timestamp(e.received_at) or datetime.min.replace(tzinfo=UTC),
            e.event_id,
        )
    )
    return prior


def run_replay(
    data_pack: DataPack,
    model_client: ModelClient,
    policy_engine: PolicyEngine,
    output_dir: str = DEFAULT_OUTPUT_DIR,
    fault_mapping_path: Optional[str] = None,
    event_overrides: Optional[Mapping[str, Mapping[str, str]]] = None,
    input_dir: Optional[str] = None,
) -> Tuple[Path, Path, RunReport]:
    """Run the scheduled replay and return ``(actions_path, decisions_path, report)``.

    Triggers are processed in ``deliver_at`` order.  For every trigger a
    decision is recorded -- including holdout and no-action decisions -- so
    doing nothing is represented in the decision output, never as an action.
    """
    pack = data_pack
    root = str(getattr(pack.paths, "root", "") or "")

    # Goal manager: loads goals from config/goals.json and rule corrections
    # (including the G03 correction) from replay/control_events.jsonl.
    goal_manager = GoalManager(pack)

    # Register the supplied model faults keyed by trigger_event_id.
    fault_path = fault_mapping_path or os.path.join(root, "replay", "model_faults.jsonl")
    if os.path.isfile(fault_path):
        faults = load_model_fault_mapping(fault_path)
        for trigger_event_id, entry in faults.items():
            model_client.register_output(
                trigger_event_id,
                entry.get("model_output"),
                fault=entry.get("fault"),
            )

    # Control events: title withdrawals and dependency windows.
    control_path = os.path.join(root, "replay", "control_events.jsonl")
    control_events = load_control_events(control_path)
    outage_windows: List[Tuple[datetime, datetime]] = []
    withdrawn_titles: List[Tuple[str, datetime]] = []
    for ce in control_events:
        ctype = ce.get("type")
        if ctype == "dependency_unavailable" and ce.get("dependency") == "model_provider":
            start = parse_timestamp(ce.get("at"))
            until = parse_timestamp(ce.get("until"))
            if start is not None and until is not None:
                outage_windows.append((start, until))
        elif ctype == "title_withdrawn":
            withdrawn_at = parse_timestamp(ce.get("at"))
            title_id = ce.get("title_id")
            if withdrawn_at is not None and title_id:
                withdrawn_titles.append((str(title_id), withdrawn_at))

    # Triggers in delivery-time order, never historical events as triggers.
    triggers = sorted(
        pack.triggers,
        key=lambda t: (
            parse_timestamp(t.deliver_at) or parse_timestamp(t.received_at)
            or datetime.min.replace(tzinfo=UTC),
            t.event_id,
        ),
    )

    decisions: List[DecisionRecord] = []
    actions: List[ActionRecord] = []
    report_errors: List[str] = []
    active_goals: Dict[str, dict] = {}
    decision_counter = 0
    action_counter = 0

    for trigger in triggers:
        trigger_event_id = trigger.event_id
        user_id = trigger.user_id
        trigger_at = (
            parse_timestamp(trigger.deliver_at) or parse_timestamp(trigger.received_at)
        )
        if trigger_at is None:
            continue

        started = time.perf_counter()

        # Holdout users never receive an intervention.
        if pack.arm_for(user_id) == "holdout":
            elapsed_ms = (time.perf_counter() - started) * 1000
            decisions.append(
                DecisionRecord(
                    decision_id=f"D-{decision_counter:04d}",
                    user_id=user_id,
                    trigger_event_id=trigger_event_id,
                    goal_id=None,
                    action=None,
                    reason="HOLDOUT",
                    rule="HOLDOUT",
                    published_at=trigger_at.isoformat(),
                    processing_ms=int(elapsed_ms),
                )
            )
            decision_counter += 1
            continue

        # Evidence available by this trigger's delivery time (no future leakage).
        prior_events = _prior_events_for_user(pack, user_id, trigger_at)
        goal_context = goal_manager.context_for(
            user_id, trigger_at=trigger_at, events=prior_events
        )

        # Goal selection with active-goal tracking (one active goal per user).
        goal_spec = None
        goal_id = None
        active_info = active_goals.get(user_id)
        if active_info is not None and active_info.get("state") == ACTIVE:
            outcome = goal_manager.evaluate(
                active_info["goal_spec"],
                goal_context,
                goal_started_at=active_info["started_at"],
                evaluated_at=trigger_at,
            )
            if outcome.state in (SUCCEEDED, FAILED):
                del active_goals[user_id]
                active_info = None
            else:
                goal_spec = active_info["goal_spec"]
                goal_id = goal_spec.goal_id

        if goal_spec is None:
            goal_spec = goal_manager.select(goal_context)
            if goal_spec is None:
                elapsed_ms = (time.perf_counter() - started) * 1000
                decisions.append(
                    DecisionRecord(
                        decision_id=f"D-{decision_counter:04d}",
                        user_id=user_id,
                        trigger_event_id=trigger_event_id,
                        goal_id=None,
                        action=None,
                        reason="NO_ELIGIBLE_GOAL",
                        rule="NO_ELIGIBLE_GOAL",
                        published_at=trigger_at.isoformat(),
                        processing_ms=int(elapsed_ms),
                    )
                )
                decision_counter += 1
                continue
            goal_id = goal_spec.goal_id
            window_hours = goal_spec.window_hours
            window_end = (
                trigger_at + timedelta(hours=window_hours)
                if window_hours is not None
                else None
            )
            active_goals[user_id] = {
                "goal_spec": goal_spec,
                "started_at": trigger_at,
                "window_end": window_end,
                "state": ACTIVE,
                "reason": "selected",
            }

        # Model-provider outage window: every model call fails / times out.
        in_outage = any(start <= trigger_at <= until for start, until in outage_windows)
        if in_outage:
            model_client.register_output(trigger_event_id, None, fault="timeout")
                # Use deterministic mock output for ordinary triggers.
        # Never overwrite explicitly registered replay faults or outputs.
        if (
            model_client.registered_output(trigger_event_id) is None
            and model_client.registered_fault(trigger_event_id) is None
            and not in_outage
        ):
            model_client.register_output(
                trigger_event_id,
                model_client._normal_model_output(
                    user_id=user_id,
                    trigger_at=trigger_at,
                    action_type="push",
                    goal_id=goal_id,
                    app_dialect=trigger.app_dialect,
                ),
            )
        # Read the action type from the registered model output when present,
        # defaulting to push.  This lets the model choose push / whatsapp /
        # home_page per trigger.
        registered = model_client.registered_output(trigger_event_id) or {}
        if isinstance(registered, dict):
            action_type = str(registered.get("action_type") or "push")
        else:
            action_type = "push"

        # Evaluate through the real API.  complete() builds the proposed action and
        # delegates policy validation to PolicyEngine exactly once.
        decision = model_client.complete(
            trigger_event_id,
            user_id,
            trigger_at,
            action_type=action_type,
            title_id=None,
            published_at=trigger_at,
           goal_id=goal_id,
        )

        elapsed_ms = (time.perf_counter() - started) * 1000
        published_at = (
            trigger_at + timedelta(seconds=(time.perf_counter() - started))
        ).isoformat()

        proposed = decision.proposed_action
        published = False
        if decision.allowed and proposed:
            if not _is_title_withdrawn(proposed, trigger_at, withdrawn_titles):
                action_counter += 1
                actions.append(
                    ActionRecord(
                        action_id=f"A-{action_counter:04d}",
                        decision_id=f"D-{decision_counter:04d}",
                        user_id=user_id,
                        trigger_event_id=trigger_event_id,
                        type=proposed.get("type") or "push",
                        published_at=published_at,
                        send_at=proposed.get("send_at"),
                        dialect=proposed.get("dialect"),
                        hero_title_id=proposed.get("hero_title_id"),
                        rows=proposed.get("rows"),
                        title_id=proposed.get("title_id"),
                        text=proposed.get("text"),
                        deeplink=proposed.get("deeplink"),
                        template_id=proposed.get("template_id"),
                        variables=proposed.get("variables"),
                    )
                )
                decisions.append(
                    DecisionRecord(
                        decision_id=f"D-{decision_counter:04d}",
                        user_id=user_id,
                        trigger_event_id=trigger_event_id,
                        goal_id=goal_id,
                        action=decision.action_type,
                        reason="",
                        rule=decision.rule,
                        published_at=published_at,
                        processing_ms=int(elapsed_ms),
                        title_id=proposed.get("title_id"),
                    )
                )
                published = True
        if not published:
            if decision.allowed and proposed:
                reason = "TITLE_WITHDRAWN"
                rule = "title_withdrawn"
            else:
                if decision.rule in FAULT_RULES:
                    reason = decision.reason or "FAULT"
                elif decision.reason:
                    reason = "POLICY"
                else:
                    reason = "NO_ACTION"
                rule = decision.rule
            decisions.append(
                DecisionRecord(
                    decision_id=f"D-{decision_counter:04d}",
                    user_id=user_id,
                    trigger_event_id=trigger_event_id,
                    goal_id=goal_id,
                    action=None,
                    reason=reason,
                    rule=rule,
                    published_at=trigger_at.isoformat(),
                    processing_ms=int(elapsed_ms),
                )
            )
        decision_counter += 1

    os.makedirs(output_dir, exist_ok=True)
    decisions_path = Path(output_dir) / "decisions.jsonl"
    actions_path = Path(output_dir) / "actions.jsonl"

    with open(decisions_path, "w", encoding="utf-8") as fh:
        for decision in decisions:
            fh.write(json.dumps(decision.__dict__, ensure_ascii=True) + "\n")

    with open(actions_path, "w", encoding="utf-8") as fh:
        for action in actions:
            # Omit None-valued fields so home-page actions never carry send_at.
            payload = {k: v for k, v in action.__dict__.items() if v is not None}
            fh.write(json.dumps(payload, ensure_ascii=True) + "\n")

    report = RunReport(
        decisions_total=len(decisions),
        actions_published=len(actions),
        no_action_decisions=sum(
            1 for d in decisions if d.action is None and d.reason != ""
        ),
        fault_no_action_decisions=sum(
            1 for d in decisions if d.rule in FAULT_RULES
        ),
        policy_no_action_decisions=sum(
            1 for d in decisions if d.reason == "POLICY"
        ),
        holdout_decisions=sum(
            1 for d in decisions if d.reason == "HOLDOUT"
        ),
        errors=report_errors,
    )
    return actions_path, decisions_path, report
def main() -> None:
    project_root = Path(__file__).resolve().parents[1]
    pack_root = project_root / "data_pack" / "one_user_one_agent_pack"

    paths = DataPackPaths.from_root(str(pack_root))
    pack = load_data_pack(paths)

    pricing = json.loads(
        (pack_root / "config" / "pricing.json").read_text(encoding="utf-8")
    )
    interventions = json.loads(
        (pack_root / "config" / "interventions.json").read_text(encoding="utf-8")
    )

    policy_engine = PolicyEngine(pack, pricing, interventions)
    model_client = ModelClient(policy_engine)

    actions_path, decisions_path, report = run_replay(
        pack,
        model_client,
        policy_engine,
        output_dir=str(project_root / "sample_run"),
    )

    print("Replay completed")
    print(f"Triggers loaded: {len(pack.triggers)}")
    print(f"Historical events loaded: {len(pack.events)}")
    print(f"Actions published: {report.actions_published}")
    print(f"Decisions recorded: {report.decisions_total}")
    print(f"Holdout decisions: {report.holdout_decisions}")
    print(f"Errors: {report.errors}")
    print(f"Actions: {actions_path}")
    print(f"Decisions: {decisions_path}")


if __name__ == "__main__":
    main()