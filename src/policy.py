"""Policy enforcement for the One User, One Agent replay.

Pure, reusable validation of agent actions against the pack's messaging and
home-page policy.  Goal selection and model logic are deliberately not
involved.

Every validator returns a :class:`PolicyResult`.  ``allowed=True`` means the
action passes; ``allowed=False`` carries a ``reason`` and the ``rule`` that
was violated so the caller can record exactly why an action was blocked.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Mapping, Optional, Sequence, Set, Tuple

try:  # pragma: no cover - import style depends on how the package is loaded
    from src.data_loader import DataPack, Event, Title, parse_timestamp
except ImportError:  # pragma: no cover
    from data_loader import DataPack, Event, Title, parse_timestamp  # type: ignore

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))

# ---- quiet hours -----------------------------------------------------------

QUIET_HOURS_START_HOUR = 22  # 22:00 IST
QUIET_HOURS_END_HOUR = 8     # 08:00 IST

# ---- push copy limits ------------------------------------------------------

PUSH_TITLE_MAX_CHARS = 40
PUSH_BODY_MAX_CHARS = 120

PUSH_FORBIDDEN_PHRASES = (
    "last chance",
    "hurry",
    "only today",
    "free forever",
    "guaranteed",
)

# ---- deeplink --------------------------------------------------------------

DEEPLINK_RE = re.compile(r"^ott://title/([A-Za-z0-9_-]+)$")

# ---- home page -------------------------------------------------------------

MAX_HOMEPAGE_ROWS = 6
MIN_HOMEPAGE_TITLES_PER_ROW = 3
MAX_HOMEPAGE_TITLES_PER_ROW = 10

HOMEPAGE_ROW_TYPES = (
    "next_episode",
    "because_you_watched",
    "popular_in_dialect",
    "new_releases",
    "genre",
)

#: Row types the platform adds itself; the agent must not publish them.
PLATFORM_ROW_TYPES = ("continue_watching",)

# ---- action types ----------------------------------------------------------

ACTION_TYPES = ("push", "whatsapp", "home_page", "in_app_message")
MESSAGING_ACTIONS = ("push", "whatsapp")


def ist_date(when: datetime) -> date:
    """The IST calendar date of a UTC instant."""
    return when.astimezone(IST).date()


def _action_time(action: Mapping[str, Any]) -> Optional[str]:
    """The send/publish timestamp string carried by an action."""
    return action.get("send_at") or action.get("published_at")


@dataclass(frozen=True)
class PolicyResult:
    """The outcome of validating one action against the policy.

    ``allowed`` is ``True`` when every applicable rule passes.  ``reason``
    explains the rejection (empty when allowed) and ``rule`` names the rule
    that was violated so a decision log can record it.
    """

    allowed: bool
    reason: str
    action_id: str
    user_id: str
    action_type: str
    rule: str = ""

    @property
    def rejected(self) -> bool:
        return not self.allowed


def _allowed(action_id: str, user_id: str, action_type: str, rule: str = "") -> PolicyResult:
    return PolicyResult(True, "", action_id, user_id, action_type, rule)


def _rejected(
    reason: str,
    action_id: str,
    user_id: str,
    action_type: str,
    rule: str,
) -> PolicyResult:
    return PolicyResult(False, reason, action_id, user_id, action_type, rule)


# ---------------------------------------------------------------------------
# Pricing / approved claims
# ---------------------------------------------------------------------------


def load_pricing(path: str) -> Dict[str, Any]:
    """Load ``config/pricing.json`` and return the parsed document."""
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def approved_prices(pricing: Mapping[str, Any]) -> Set[int]:
    """Return the set of approved ``price_inr`` values from pricing config."""
    prices: Set[int] = set()
    for plan in pricing.get("plans", []) or []:
        price = plan.get("price_inr")
        if price is not None:
            try:
                prices.add(int(price))
            except (TypeError, ValueError):
                continue
    return prices


PRICE_RE = re.compile(
    r"(?:₹|rs\.?\s*|inr\s*)?(\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?)\b",
    re.IGNORECASE,
)


def price_claims_approved(text: Optional[str], pricing: Mapping[str, Any]) -> bool:
    """True when every INR amount mentioned in ``text`` is an approved price."""
    if not text:
        return True
    allowed = approved_prices(pricing)
    for match in PRICE_RE.finditer(str(text)):
        raw = match.group(1).replace(",", "")
        try:
            value = float(raw)
        except ValueError:
            return False
        if value not in {float(p) for p in allowed}:
            return False
    return True


# ---------------------------------------------------------------------------
# WhatsApp approved templates
# ---------------------------------------------------------------------------


def load_interventions(path: str) -> Dict[str, Any]:
    """Load ``config/interventions.json`` and return the parsed document."""
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def whatsapp_approved_templates(interventions: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Return ``template_id -> template`` for the approved WhatsApp templates."""
    templates: Dict[str, Dict[str, Any]] = {}
    for entry in (interventions.get("whatsapp", {}).get("approved_templates") or []):
        tid = str(entry.get("template_id") or "").strip()
        if tid:
            templates[tid] = entry
    return templates


# ---------------------------------------------------------------------------
# Consent / opt-outs derived from history
# ---------------------------------------------------------------------------


def channel_opt_outs(events: Sequence[Event]) -> Dict[str, Set[str]]:
    """Return ``user_id -> set of channels opted out`` from history events.

    A ``notification_opt_out`` event with ``properties.channel`` records that
    the user opted out of that specific channel only.
    """
    outs: Dict[str, Set[str]] = {}
    for event in events:
        if event.event != "notification_opt_out":
            continue
        channel = str(event.properties.get("channel") or "").strip().lower()
        if not channel:
            continue
        outs.setdefault(event.user_id, set()).add(channel)
    return outs


def profile_consent(profile: Optional[Any], channel: str) -> bool:
    """Snapshot consent for a channel from the user's profile."""
    if profile is None:
        return False
    field = "notifications_opt_in" if channel == "push" else "whatsapp_opt_in"
    value = getattr(profile, field, None)
    return bool(value)


def is_opted_out(opt_outs: Mapping[str, Set[str]], user_id: str, channel: str) -> bool:
    """True when the user has opted out of ``channel`` (per-channel)."""
    return channel in (opt_outs.get(user_id) or set())


# ---------------------------------------------------------------------------
# Policy engine
# ---------------------------------------------------------------------------


class PolicyEngine:
    """Validates agent actions against the messaging and home-page policy.

    The engine holds the mutable state that policy needs across actions
    (daily messaging counts) and the static reference data (catalogue,
    profiles, arms, pricing, templates).  It is independent of goal
    selection and model logic.

    Usage::

        engine = PolicyEngine(pack, pricing, interventions)
        result = engine.validate(action)
        if result.allowed:
            executable.append(action)
        else:
            decision_log.append(result)
    """

    def __init__(
        self,
        pack: DataPack,
        pricing: Mapping[str, Any],
        interventions: Mapping[str, Any],
        *,
        now: Optional[datetime] = None,
    ) -> None:
        self.pack = pack
        self.pricing = dict(pricing or {})
        self.interventions = dict(interventions or {})
        self.now = now
        self._opt_outs = channel_opt_outs(list(pack.events))
        # Daily requested messaging sends per (user, IST date).
        self._sent_today: Dict[Tuple[str, date], int] = {}
        # Daily in-app messages per (user, IST date).
        self._in_app_today: Dict[Tuple[str, date], int] = {}

    # -- reference lookups ---------------------------------------------------

    def title_for(self, title_id: Optional[str]) -> Optional[Title]:
        if title_id is None:
            return None
        return self.pack.title_by_id(str(title_id).strip())

    def profile_for(self, user_id: Optional[str]) -> Optional[Any]:
        if user_id is None:
            return None
        return self.pack.profile_for(str(user_id).strip())

    def arm_for(self, user_id: Optional[str]) -> Optional[str]:
        if user_id is None:
            return None
        return self.pack.arm_for(str(user_id).strip())

    def templates(self) -> Dict[str, Dict[str, Any]]:
        return whatsapp_approved_templates(self.interventions)

    # -- public API ----------------------------------------------------------

    def validate(self, action: Mapping[str, Any]) -> PolicyResult:
        """Validate one action, returning a :class:`PolicyResult`.

        The action is validated against every applicable rule in order:
        general structure, holdout, consent, quiet hours, frequency cap, and
        type-specific rules.  The first failing rule returns its result.
        """
        action_id = str(action.get("action_id") or "").strip()
        user_id = str(action.get("user_id") or "").strip()
        action_type = str(action.get("type") or "").strip()

        # --- no_action is always allowed (recorded separately by caller) ---
        if action_type == "no_action":
            return _allowed(action_id, user_id, action_type, "no_action")

        # --- general: unknown action type ----------------------------------
        if action_type not in ACTION_TYPES:
            return _rejected(
                f"unknown action type: {action_type!r}",
                action_id,
                user_id,
                action_type,
                "unknown_action_type",
            )

        # --- general: user / trigger references ---------------------------
        if not user_id:
            return _rejected(
                "missing user_id",
                action_id,
                user_id,
                action_type,
                "invalid_user_reference",
            )
        if self.profile_for(user_id) is None and self.arm_for(user_id) is None:
            return _rejected(
                f"unknown user: {user_id!r}",
                action_id,
                user_id,
                action_type,
                "invalid_user_reference",
            )
        trigger = str(action.get("trigger_event_id") or "").strip()
        if not trigger:
            return _rejected(
                "missing trigger_event_id",
                action_id,
                user_id,
                action_type,
                "invalid_trigger_reference",
            )

        # --- general: malformed timestamp ---------------------------------
        at = _action_time(action)
        when = None
        if at:
            try:
                when = parse_timestamp(at)
            except (ValueError, TypeError):
                return _rejected(
                    f"malformed timestamp: {at!r}",
                    action_id,
                    user_id,
                    action_type,
                    "malformed_timestamp",
                )

        # --- holdout --------------------------------------------------------
        if self.arm_for(user_id) == "holdout":
            return _rejected(
                "user is in holdout arm; no agent action permitted",
                action_id,
                user_id,
                action_type,
                "holdout",
            )

        # --- consent (per channel) -----------------------------------------
        if action_type in MESSAGING_ACTIONS:
            channel = action_type
            profile = self.profile_for(user_id)
            if not profile_consent(profile, channel):
                return _rejected(
                    f"no snapshot consent for {channel}",
                    action_id,
                    user_id,
                    action_type,
                    "no_consent",
                )
            if is_opted_out(self._opt_outs, user_id, channel):
                return _rejected(
                    f"user opted out of {channel}",
                    action_id,
                    user_id,
                    action_type,
                    "opted_out",
                )

        # --- quiet hours ----------------------------------------------------
        if action_type in MESSAGING_ACTIONS and when is not None:
            if _in_quiet_hours(when):
                return _rejected(
                    f"{channel} in quiet hours (22:00-08:00 IST)",
                    action_id,
                    user_id,
                    action_type,
                    "quiet_hours",
                )

        # --- type-specific validation --------------------------------------
        if action_type == "push":
            result = self._validate_push(action, when)
            if result.rejected:
                return result
        elif action_type == "whatsapp":
            result = self._validate_whatsapp(action)
            if result.rejected:
                return result
        elif action_type == "home_page":
            result = self._validate_home_page(action, when)
            if result.rejected:
                return result
        elif action_type == "in_app_message":
            result = self._validate_in_app(action, user_id, when)
            if result.rejected:
                return result

        # --- frequency cap (push + whatsapp combined) ----------------------
        if action_type in MESSAGING_ACTIONS and when is not None:
            result = self._check_frequency_cap(user_id, when, action_id, action_type)
            if result.rejected:
                return result

        # --- in-app daily cap ----------------------------------------------
        if action_type == "in_app_message" and when is not None:
            result = self._check_in_app_cap(user_id, when, action_id, action_type)
            if result.rejected:
                return result

        return _allowed(action_id, user_id, action_type)

    # -- type-specific validators -------------------------------------------

    def _validate_push(
        self, action: Mapping[str, Any], when: Optional[datetime]
    ) -> PolicyResult:
        action_id = str(action.get("action_id") or "").strip()
        user_id = str(action.get("user_id") or "").strip()
        text = action.get("text") or {}
        title = str(text.get("title") or "")
        body = str(text.get("body") or "")

        if len(title) > PUSH_TITLE_MAX_CHARS:
            return _rejected(
                f"push title exceeds {PUSH_TITLE_MAX_CHARS} characters",
                action_id,
                user_id,
                "push",
                "push_title_too_long",
            )
        if len(body) > PUSH_BODY_MAX_CHARS:
            return _rejected(
                f"push body exceeds {PUSH_BODY_MAX_CHARS} characters",
                action_id,
                user_id,
                "push",
                "push_body_too_long",
            )
        lowered = f"{title} {body}".lower()
        for phrase in PUSH_FORBIDDEN_PHRASES:
            if phrase in lowered:
                return _rejected(
                    f"push copy contains forbidden phrase: {phrase!r}",
                    action_id,
                    user_id,
                    "push",
                    "forbidden_phrase",
                )
        full_copy = f"{title} {body}"
        if not price_claims_approved(full_copy, self.pricing):
            return _rejected(
                "push copy contains a price claim not in config/pricing.json",
                action_id,
                user_id,
                "push",
                "unapproved_price_claim",
            )

        deeplink = str(action.get("deeplink") or "").strip()
        if deeplink and not DEEPLINK_RE.match(deeplink):
            return _rejected(
                f"push deeplink must match ott://title/<title_id>: {deeplink!r}",
                action_id,
                user_id,
                "push",
                "invalid_deeplink",
            )

        title_id = str(action.get("title_id") or "").strip()
        if title_id:
            title_obj = self.title_for(title_id)
            if title_obj is None:
                return _rejected(
                    f"push title_id not in catalogue: {title_id!r}",
                    action_id,
                    user_id,
                    "push",
                    "invalid_title",
                )
        return _allowed(action_id, user_id, "push")

    def _validate_whatsapp(self, action: Mapping[str, Any]) -> PolicyResult:
        action_id = str(action.get("action_id") or "").strip()
        user_id = str(action.get("user_id") or "").strip()
        templates = self.templates()
        template_id = str(action.get("template_id") or "").strip()

        if template_id not in templates:
            return _rejected(
                f"whatsapp template not approved: {template_id!r}",
                action_id,
                user_id,
                "whatsapp",
                "unapproved_template",
            )

        template = templates[template_id]
        approved_vars = [str(v).strip() for v in (template.get("variables") or [])]
        variables = action.get("variables") or {}
        if not isinstance(variables, dict):
            return _rejected(
                "whatsapp variables must be an object",
                action_id,
                user_id,
                "whatsapp",
                "invalid_variables",
            )
        provided_vars = set(str(k).strip() for k in variables.keys())
        expected_vars = set(approved_vars)
        if provided_vars != expected_vars:
            return _rejected(
                f"whatsapp variables must be exactly {sorted(expected_vars)}",
                action_id,
                user_id,
                "whatsapp",
                "invalid_variables",
            )

        # Validate variable values against catalogue or pricing data.
        title_id = str(action.get("title_id") or "").strip()
        for var_name, var_value in variables.items():
            if not self._whatsapp_value_approved(var_name, var_value, title_id):
                return _rejected(
                    f"whatsapp variable {var_name!r} value not approved: {var_value!r}",
                    action_id,
                    user_id,
                    "whatsapp",
                    "unapproved_variable_value",
                )
        return _allowed(action_id, user_id, "whatsapp")

    def _whatsapp_value_approved(
        self, var_name: str, value: Any, title_id: str
    ) -> bool:
        """Check a WhatsApp variable value against catalogue or pricing data."""
        text = "" if value is None else str(value).strip()
        if not text:
            return False
        title = self.title_for(title_id) if title_id else None

        if var_name == "title_name":
            return bool(title and title.title and text == title.title)
        if var_name == "episode_number":
            # Must be a positive integer referring to an episode of the title.
            try:
                episode = int(text)
            except (TypeError, ValueError):
                return False
            if episode <= 0 or not title:
                return False
            for ep in self.pack.episodes_for_slug(title.content_slug):
                if ep.episode == episode:
                    return True
            return False
        if var_name == "dialect_name":
            return bool(title and title.dialect and text == title.dialect)
        if var_name == "plan_price":
            return text in {str(p) for p in approved_prices(self.pricing)}
        # Unknown variable names are not approved.
        return False

    def _validate_home_page(
        self, action: Mapping[str, Any], when: Optional[datetime]
    ) -> PolicyResult:
        action_id = str(action.get("action_id") or "").strip()
        user_id = str(action.get("user_id") or "").strip()
        dialect = str(action.get("dialect") or "").strip()

        rows = action.get("rows") or []
        if not isinstance(rows, list):
            rows = []
        if len(rows) > MAX_HOMEPAGE_ROWS:
            return _rejected(
                f"home page exceeds {MAX_HOMEPAGE_ROWS} rows",
                action_id,
                user_id,
                "home_page",
                "rows_over_maximum",
            )

        # No agent-published Continue Watching row.
        for row in rows:
            row_type = str((row or {}).get("row_type") or "").strip()
            if row_type in PLATFORM_ROW_TYPES:
                return _rejected(
                    "agent must not publish a Continue Watching row",
                    action_id,
                    user_id,
                    "home_page",
                    "continue_watching_row",
                )

        hero_title_id = str(action.get("hero_title_id") or "").strip()
        hero_count = 1 if hero_title_id else 0
        if hero_count > 1:
            return _rejected(
                "home page may have at most one hero",
                action_id,
                user_id,
                "home_page",
                "multiple_heroes",
            )

        # Validate publish time for availability.
        publish_at = action.get("published_at")
        publish_when = None
        if publish_at:
            try:
                publish_when = parse_timestamp(publish_at)
            except (ValueError, TypeError):
                return _rejected(
                    f"malformed published_at: {publish_at!r}",
                    action_id,
                    user_id,
                    "home_page",
                    "malformed_timestamp",
                )

        seen_slugs: Set[str] = set()
        for row in rows:
            title_ids = (row or {}).get("title_ids") or []
            if not isinstance(title_ids, list):
                title_ids = []
            if len(title_ids) < MIN_HOMEPAGE_TITLES_PER_ROW:
                return _rejected(
                    f"row has fewer than {MIN_HOMEPAGE_TITLES_PER_ROW} titles",
                    action_id,
                    user_id,
                    "home_page",
                    "row_below_minimum",
                )
            if len(title_ids) > MAX_HOMEPAGE_TITLES_PER_ROW:
                return _rejected(
                    f"row has more than {MAX_HOMEPAGE_TITLES_PER_ROW} titles",
                    action_id,
                    user_id,
                    "home_page",
                    "row_over_maximum",
                )
            row_type = str((row or {}).get("row_type") or "").strip()
            if row_type and row_type not in HOMEPAGE_ROW_TYPES:
                return _rejected(
                    f"permitted row types are {sorted(HOMEPAGE_ROW_TYPES)}",
                    action_id,
                    user_id,
                    "home_page",
                    "invalid_row_type",
                )
            for tid in title_ids:
                tid = str(tid).strip()
                title_obj = self.title_for(tid)
                if title_obj is None:
                    return _rejected(
                        f"title not in catalogue: {tid!r}",
                        action_id,
                        user_id,
                        "home_page",
                        "invalid_title",
                    )
                if dialect and title_obj.dialect != dialect:
                    return _rejected(
                        f"title {tid!r} dialect {title_obj.dialect!r} != page dialect {dialect!r}",
                        action_id,
                        user_id,
                        "home_page",
                        "dialect_mismatch",
                    )
                if publish_when is not None and not title_obj.is_available_at(publish_when):
                    return _rejected(
                        f"title {tid!r} not available at publish time",
                        action_id,
                        user_id,
                        "home_page",
                        "unavailable_title",
                    )
                if title_obj.content_slug in seen_slugs:
                    return _rejected(
                        f"duplicate content_slug on page: {title_obj.content_slug!r}",
                        action_id,
                        user_id,
                        "home_page",
                        "duplicate_content_slug",
                    )
                seen_slugs.add(title_obj.content_slug)

        # Hero availability / dialect.
        if hero_title_id:
            hero = self.title_for(hero_title_id)
            if hero is None:
                return _rejected(
                    f"hero title not in catalogue: {hero_title_id!r}",
                    action_id,
                    user_id,
                    "home_page",
                    "invalid_title",
                )
            if dialect and hero.dialect != dialect:
                return _rejected(
                    f"hero dialect {hero.dialect!r} != page dialect {dialect!r}",
                    action_id,
                    user_id,
                    "home_page",
                    "dialect_mismatch",
                )
            if publish_when is not None and not hero.is_available_at(publish_when):
                return _rejected(
                    f"hero title {hero_title_id!r} not available at publish time",
                    action_id,
                    user_id,
                    "home_page",
                    "unavailable_title",
                )
        return _allowed(action_id, user_id, "home_page")

    def _validate_in_app(
        self, action: Mapping[str, Any], user_id: str, when: Optional[datetime]
    ) -> PolicyResult:
        action_id = str(action.get("action_id") or "").strip()
        # In-app messages require no consent; schema is minimal.
        return _allowed(action_id, user_id, "in_app_message")

    # -- caps ----------------------------------------------------------------

    def _check_frequency_cap(
        self, user_id: str, when: datetime, action_id: str, action_type: str
    ) -> PolicyResult:
        day = ist_date(when)
        key = (user_id, day)
        count = self._sent_today.get(key, 0)
        if count >= 1:
            return _rejected(
                f"already sent a push/whatsapp to {user_id} on {day.isoformat()}",
                action_id,
                user_id,
                action_type,
                "frequency_cap_exceeded",
            )
        self._sent_today[key] = count + 1
        return _allowed(action_id, user_id, action_type)

    def _check_in_app_cap(
        self, user_id: str, when: datetime, action_id: str, action_type: str
    ) -> PolicyResult:
        day = ist_date(when)
        key = (user_id, day)
        count = self._in_app_today.get(key, 0)
        if count >= 1:
            return _rejected(
                f"already sent an in-app message to {user_id} on {day.isoformat()}",
                action_id,
                user_id,
                action_type,
                "in_app_daily_cap_exceeded",
            )
        self._in_app_today[key] = count + 1
        return _allowed(action_id, user_id, action_type)


def _in_quiet_hours(when: datetime) -> bool:
    """True when ``when`` falls in 22:00-08:00 IST."""
    hour = when.astimezone(IST).hour
    return hour >= QUIET_HOURS_START_HOUR or hour < QUIET_HOURS_END_HOUR


__all__ = [
    "ACTION_TYPES",
    "PolicyEngine",
    "PolicyResult",
    "approved_prices",
    "channel_opt_outs",
    "is_opted_out",
    "ist_date",
    "load_interventions",
    "load_pricing",
    "profile_consent",
    "whatsapp_approved_templates",
]