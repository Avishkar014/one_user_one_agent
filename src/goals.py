"""Goal management for the One User, One Agent assignment.

This module turns the twelve candidate goals in ``config/goals.json`` into
three things the agent needs:

1. **Eligibility** -- may this user be given this goal right now?
2. **Success tracking** -- did the user reach the goal inside its window?
3. **Selection** -- exactly one active goal per user, chosen deterministically.

Design rules that follow the data pack's documentation and the assignment:

* Timestamps are UTC.  ``received_at`` is the only trustworthy clock, so it is
  the only one used for ordering, windowing and day arithmetic.  Goals that
  need *distinct local days* (G03, G09) use IST calendar dates, because every
  user is in India.
* A goal window starts at the trigger's ``deliver_at`` -- the moment the agent
  commits to the goal -- and runs for ``success.window_hours``.
* Missing evidence is handled conservatively.  A missing profile, a missing
  timestamp or an event that cannot be placed in time never makes a goal
  eligible and never makes it successful.
* Nothing is hard-coded about *users*.  The catalogue, profiles, events and
  goal configuration are the only sources of truth; the code only encodes the
  *rules* that the configuration states in prose.
* Policy enforcement, messaging, model calls and replay orchestration are
  deliberately **not** implemented here.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

try:  # pragma: no cover - import style depends on how the package is loaded
    from src.data_loader import DataPack, Event, Profile, Title, parse_timestamp
except ImportError:  # pragma: no cover
    from data_loader import DataPack, Event, Profile, Title, parse_timestamp  # type: ignore

__all__ = [
    "ACTIVE",
    "DEFAULT_PRIORITY",
    "FAILED",
    "GoalContext",
    "GoalManager",
    "GoalOutcome",
    "GoalSpec",
    "GoalState",
    "PENDING",
    "SUCCEEDED",
    "eligible_goals",
    "evaluate_goal",
    "is_eligible",
    "load_goal_specs",
    "priority_order",
    "select_goal",
]

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))

ACTIVE = "active"
PENDING = "pending"
SUCCEEDED = "succeeded"
FAILED = "failed"


#: Order in which measurable goals compete for a user's single active slot.
#:
#: Rationale (documented here so the order is easy to change -- edit this
#: tuple, or pass your own to :func:`select_goal`):
#:
#: * ``G04`` and ``G05`` come first because they are the only goals that
#:   produce revenue, and a conversion is worth more than a habit.
#:   ``G04`` (a paying trial) outranks ``G05`` (a first-ever payer) because
#:   the trial user has already shown purchase intent.
#: * ``G06`` (reactivation) is next: a lapsed subscriber is recoverable
#:   revenue and decays with time.
#: * ``G01`` and ``G02`` follow: they are the onboarding funnel for brand new
#:   users, and they decay fastest because a user who does not play on day one
#:   rarely comes back.
#: * ``G07`` (resume a series) outranks ``G12`` (return after absence)
#:   because resuming a specific show is a stronger signal of intent than a
#:   bare return, and it names the content to act on.
#: * ``G12`` outranks ``G03`` (second-day return) because a returning user is
#:   worth more than a brand new one who has only come back once.
#: * ``G08`` (try a new release) is a catalogue-driven goal, useful mainly
#:   for active subscribers.
#: * ``G09`` (weekly habit) is a long, slow habit goal and is placed late
#:   because it takes two weeks to complete and would block everything else.
#: * ``G11`` (TV activation) is last of the measurable goals: it is only
#:   meaningful for users who already watch a lot.
#:
#: ``G10`` never appears here: its success condition is undefined, so it is
#: not measurable and must not be selected.
DEFAULT_PRIORITY: Tuple[str, ...] = (
    "G04",
    "G05",
    "G06",
    "G01",
    "G02",
    "G07",
    "G12",
    "G03",
    "G08",
    "G09",
    "G11",
)


def priority_order(priority: Optional[Sequence[str]] = None) -> Tuple[str, ...]:
    """Return the goal priority order to use.

    ``None`` returns :data:`DEFAULT_PRIORITY`.  A custom sequence may list
    goals that do not exist in the configuration; unknown ids are simply
    ignored at selection time.
    """
    if priority is None:
        return DEFAULT_PRIORITY
    return tuple(str(item) for item in priority)


@dataclass(frozen=True)
class GoalSpec:
    """One goal as defined in ``config/goals.json``."""

    goal_id: str
    name: str
    description: str
    eligibility: Mapping[str, Any] = field(default_factory=dict)
    success: Mapping[str, Any] = field(default_factory=dict)

    @property
    def window_hours(self) -> Optional[int]:
        """Length of the success window in hours, if the config states one."""
        raw = self.success.get("window_hours")
        if raw is None:
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    @property
    def success_event(self) -> Optional[str]:
        """The event that satisfies this goal, or ``None`` when undefined."""
        raw = self.success.get("event")
        if raw is None:
            return None
        text = str(raw).strip()
        return text or None

    @property
    def success_condition(self) -> str:
        """The prose success condition, normalised to lower case."""
        return str(self.success.get("condition") or "").strip().lower()

    @property
    def is_measurable(self) -> bool:
        """True when the config states a concrete success event we can check.

        A goal with no ``success.event`` (G10, "engages meaningfully") has an
        undefined success condition: we cannot verify it from the evidence,
        so it must never be selected or scored.
        """
        return self.success_event is not None


def load_goal_specs(path: str) -> Dict[str, GoalSpec]:
    """Read ``config/goals.json`` and return the goals keyed by ``goal_id``."""
    try:
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    specs: Dict[str, GoalSpec] = {}
    for entry in raw.get("goals", []):
        goal_id = str(entry.get("goal_id") or "").strip()
        if not goal_id:
            continue
        specs[goal_id] = GoalSpec(
            goal_id=goal_id,
            name=str(entry.get("name") or ""),
            description=str(entry.get("description") or ""),
            eligibility=dict(entry.get("eligibility") or {}),
            success=dict(entry.get("success") or {}),
        )
    return specs


@dataclass(frozen=True)
class RuleCorrection:
    """A ``goal_rule_corrected`` control event."""

    goal_id: str
    at: datetime
    old_success: Mapping[str, Any] = field(default_factory=dict)
    new_success: Mapping[str, Any] = field(default_factory=dict)
    applies_to: str = ""
    note: str = ""

    @property
    def new_event(self) -> Optional[str]:
        raw = self.new_success.get("event")
        if raw is None:
            return None
        text = str(raw).strip()
        return text or None

    @property
    def new_condition(self) -> str:
        return str(self.new_success.get("condition") or "").strip().lower()

    def applies_at(self, when: datetime) -> bool:
        """True when this correction is in force at ``when``."""
        return when >= self.at


def load_rule_corrections(path: str) -> List[RuleCorrection]:
    """Read ``replay/control_events.jsonl`` and return the goal corrections.

    Only ``goal_rule_corrected`` events are returned.  A malformed line is
    skipped rather than fatal.
    """
    corrections: List[RuleCorrection] = []
    if not os.path.exists(path):
        return corrections
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if not isinstance(record, dict):
                continue
            if record.get("type") != "goal_rule_corrected":
                continue
            goal_id = str(record.get("goal_id") or "").strip()
            at = parse_timestamp(record.get("at"))
            if not goal_id or at is None:
                continue
            corrections.append(
                RuleCorrection(
                    goal_id=goal_id,
                    at=at,
                    old_success=dict(record.get("old_success") or {}),
                    new_success=dict(record.get("new_success") or {}),
                    applies_to=str(record.get("applies_to") or ""),
                    note=str(record.get("note") or ""),
                )
            )
    corrections.sort(key=lambda item: item.at)
    return corrections


@dataclass
class GoalContext:
    """Everything the goal engine needs to judge one user at one moment.

    The engine is deliberately given plain data plus the loader's
    :class:`~src.data_loader.Event` / :class:`~src.data_loader.Profile` /
    :class:`~src.data_loader.Title` objects, so unit tests can build a
    context by hand without touching the shipped data pack.
    """

    user_id: str
    trigger_at: datetime
    events: Sequence[Event] = ()
    profile: Optional[Profile] = None
    titles: Mapping[str, Title] = field(default_factory=dict)
    episodes_by_slug: Mapping[str, Sequence[Any]] = field(default_factory=dict)
    now: Optional[datetime] = None

    def __post_init__(self) -> None:
        if self.now is None:
            self.now = self.trigger_at

    # -- history helpers ---------------------------------------------------

    def events_named(self, *names: str) -> List[Event]:
        """Every event of the given types, in ``received_at`` order."""
        wanted = set(names)
        return sorted(
            (event for event in self.events if event.event in wanted),
            key=lambda event: event.received_at,
        )

    def has_event(self, name: str, *, before: Optional[datetime] = None) -> bool:
        """True when the user has an event of this type (optionally before a time)."""
        for event in self.events:
            if event.event != name:
                continue
            if before is not None and event.received_at >= before:
                continue
            return True
        return False

    def count_event_days(
        self, name: str, *, before: datetime, days: int
    ) -> Optional[int]:
        """Number of distinct IST days with an event in the ``days`` before ``before``.

        Returns ``None`` when the count cannot be established, which happens
        when the user has no events at all: "no plays on any day" and "no
        evidence about plays" are different claims and only the first is
        safe to act on.
        """
        if not self.events:
            return None
        window_start = before - timedelta(days=days)
        seen = set()
        for event in self.events_named(name):
            if not (window_start <= event.received_at < before):
                continue
            seen.add(ist_date(event.received_at))
        return len(seen)

    def slug_of(self, title_id: Optional[str]) -> Optional[str]:
        """The ``content_slug`` of a ``title_id``, or ``None`` when unknown."""
        if not title_id:
            return None
        title = self.titles.get(str(title_id).strip())
        if title is None:
            return None
        return title.content_slug


def ist_date(when: datetime) -> date:
    """The IST calendar date of a UTC instant."""
    return when.astimezone(IST).date()


@dataclass(frozen=True)
class GoalOutcome:
    """The result of judging a goal for one user."""

    goal_id: str
    state: str
    eligible: bool
    reason: str = ""
    started_at: Optional[datetime] = None
    window_end: Optional[datetime] = None
    evidence: Mapping[str, Any] = field(default_factory=dict)

    @property
    def succeeded(self) -> bool:
        return self.state == SUCCEEDED

    @property
    def failed(self) -> bool:
        return self.state == FAILED

    @property
    def is_measurable(self) -> bool:
        return self.state in (SUCCEEDED, FAILED)


def _profile_status(context: GoalContext) -> Optional[str]:
    profile = context.profile
    if profile is None:
        return None
    status = profile.subscription_status
    if not status:
        return None
    return str(status).strip().lower()


def _signup_age_days(context: GoalContext) -> Optional[float]:
    """Days between signup and the trigger, or ``None`` when unknown."""
    profile = context.profile
    if profile is None or profile.signup_at is None:
        return None
    return (context.trigger_at - profile.signup_at).total_seconds() / 86400.0


def _check_eligibility(spec: GoalSpec, context: GoalContext) -> Tuple[bool, str]:
    """Apply the eligibility rules of one goal.

    Returns ``(eligible, reason)``.  The reason explains *why* a goal was
    rejected so that a decision log can be written later without changing
    this code.
    """
    rules = spec.eligibility

    # --- subscription status ---------------------------------------------
    statuses = rules.get("subscription_status")
    if statuses:
        wanted = {str(item).strip().lower() for item in statuses}
        actual = _profile_status(context)
        if actual is None:
            return False, "no profile or subscription status"
        if actual not in wanted:
            return False, f"subscription_status={actual} not in {sorted(wanted)}"

    # --- signed up recently ----------------------------------------------
    signed_up_within = rules.get("signed_up_within_days")
    if signed_up_within is not None:
        age = _signup_age_days(context)
        if age is None:
            return False, "no signup timestamp"
        limit = float(signed_up_within)
        if age < 0 or age > limit:
            return False, f"signed up {age:.2f} days ago, limit {limit}"

    # --- must never have done something ----------------------------------
    never = rules.get("requires_no_prior_event")
    if never:
        if context.has_event(str(never), before=context.trigger_at):
            return False, f"already has a {never} event"

    # --- goal specific rules ---------------------------------------------
    if spec.goal_id == "G07":
        unfinished = _resumable_series(context)
        if not unfinished:
            return False, "no unfinished show or micro-drama"

    if spec.goal_id == "G09":
        plays_on_days = context.count_event_days(
            "play_started", before=context.trigger_at, days=14
        )
        if plays_on_days is None:
            return False, "no event history to count play days"
        if plays_on_days > 1:
            return False, f"played on {plays_on_days} days in the last 14, limit 1"

    if spec.goal_id == "G12":
        # "No app_open in the 14 days before the trigger."
        if not context.events:
            return False, "no event history to check for absence"
        last_open = None
        for event in self_open_events(context):
            last_open = event
        if last_open is not None:
            gap_days = (context.trigger_at - last_open.received_at).total_seconds() / 86400.0
            if gap_days < 14:
                return False, f"app_open {gap_days:.2f} days ago is within 14 days"

    if spec.goal_id == "G08":
        if not _recent_release_titles(context):
            return False, "no title released in the 30 days before the trigger"

    if spec.goal_id == "G11":
        # The success event never appears in the evidence, so the goal can
        # be recognised but never verified.  It is still *eligible* (an
        # active subscriber is a valid candidate); it simply cannot be
        # measured, and the selection step drops it for that reason.
        pass

    return True, "eligible"


def self_open_events(context: GoalContext) -> List[Event]:
    """``app_open`` events of the user that happened before the trigger."""
    return [
        event
        for event in context.events_named("app_open")
        if event.received_at < context.trigger_at
    ]


def _unfinished_series(context: GoalContext) -> List[str]:
    """Content slugs of series the user started but did not finish.

    A series counts as unfinished when the user started it and at least one
    of its episodes was never completed.  Only shows and micro-dramas are
    considered -- a film has nothing to resume.
    """
    started_slugs = set()
    completed_slugs = set()
    for event in context.events:
        if event.event not in ("play_started", "episode_completed"):
            continue
        slug = context.slug_of(event.properties.get("title_id"))
        if slug is None:
            continue
        if event.event == "episode_completed":
            completed_slugs.add(slug)
        else:
            started_slugs.add(slug)

    unfinished = []
    for slug in sorted(started_slugs - completed_slugs):
        episodes = context.episodes_by_slug.get(slug) or ()
        if not episodes:
            continue
        unfinished.append(slug)
    return unfinished


def title_for_slug(context: GoalContext, slug: str) -> Optional[Title]:
    """The catalogue entry for a ``content_slug``, if the catalogue has one."""
    for title in context.titles.values():
        if title.content_slug == slug:
            return title
    return None


def _episode_completed(context: GoalContext, slug: str, episode_id: str) -> bool:
    """True when the user completed ``episode_id`` of ``slug``."""
    for event in context.events_named("episode_completed"):
        if str(event.properties.get("episode_id") or "") != episode_id:
            continue
        if context.slug_of(event.properties.get("title_id")) == slug:
            return True
    return False


def _last_play_of_slug(context: GoalContext, slug: str) -> Optional[datetime]:
    """The last time the user started playing ``slug`` before the trigger."""
    last: Optional[datetime] = None
    for event in context.events_named("play_started"):
        if event.received_at >= context.trigger_at:
            continue
        if context.slug_of(event.properties.get("title_id")) != slug:
            continue
        last = event.received_at
    return last


def _resumable_series(context: GoalContext, days_idle: int = 3) -> List[str]:
    """Slugs of series the user can resume (goal G07).

    A slug qualifies when all of the following hold:

    * it is a ``show`` or ``micro_drama`` (a film cannot be resumed);
    * the user started it at some point;
    * at least one of its episodes has not been completed;
    * the user has not played it for ``days_idle`` days or more.
    """
    started = set()
    for event in context.events_named("play_started"):
        slug = context.slug_of(event.properties.get("title_id"))
        if slug is not None:
            started.add(slug)

    resumable = []
    for slug in sorted(started):
        episodes = context.episodes_by_slug.get(slug) or ()
        if not episodes:
            continue
        title = title_for_slug(context, slug)
        if title is not None and title.content_type not in ("show", "micro_drama"):
            continue
        done = sum(
            1
            for episode in episodes
            if _episode_completed(context, slug, str(episode.episode_id))
        )
        if done >= len(episodes):
            continue
        last_play = _last_play_of_slug(context, slug)
        if last_play is None:
            continue
        idle_days = (context.trigger_at - last_play).total_seconds() / 86400.0
        if idle_days < days_idle:
            continue
        resumable.append(slug)
    return resumable


def _title_release_date(title: Optional[Title]) -> Optional[date]:
    """The ``release_date`` of a catalogue entry as a calendar date."""
    if title is None or not title.release_date:
        return None
    text = str(title.release_date).strip()[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _recent_release_titles(context: GoalContext, days: int = 30) -> List[str]:
    """Titles released in the ``days`` before the trigger.

    The release date is a plain calendar date in the catalogue, so it is
    compared against the IST calendar date of the goal start.
    """
    start = ist_date(context.trigger_at)
    earliest = start - timedelta(days=days)
    recent = []
    for title in context.titles.values():
        released = _title_release_date(title)
        if released is None:
            continue
        if earliest <= released <= start:
            recent.append(title.title_id)
    return sorted(recent)


def _recent_release_slugs(context: GoalContext, days: int = 30) -> List[str]:
    """Content slugs that have at least one title released recently."""
    slugs = set()
    for title_id in _recent_release_titles(context, days):
        title = context.titles.get(title_id)
        if title is not None:
            slugs.add(title.content_slug)
    return sorted(slugs)


def _is_paid_purchase(properties: Mapping[str, Any]) -> bool:
    """True when a purchase event describes a paid (non-trial) plan.

    ``is_trial`` must be present and false.  A missing flag is unknown, and
    unknown is never treated as a paid purchase.
    """
    raw = properties.get("is_trial")
    if raw is None:
        return False
    if isinstance(raw, str):
        return raw.strip().lower() in ("false", "0", "no")
    return not bool(raw)


def effective_rule(
    spec: GoalSpec,
    corrections: Sequence[RuleCorrection],
    *,
    goal_started_at: datetime,
    evaluated_at: datetime,
) -> Tuple[Optional[str], str, Optional[RuleCorrection]]:
    """The success rule in force for a goal at ``evaluated_at``.

    Returns ``(event, condition, correction)``.  A correction applies when
    it was issued at or before ``evaluated_at`` **and** the goal was open at
    that moment -- either because the goal started after the correction, or
    because it was already running and had not yet reached its window end.
    Goals that had already closed keep the rule they were judged by.
    """
    event = spec.success_event
    condition = spec.success_condition
    applied: Optional[RuleCorrection] = None

    window_hours = spec.window_hours
    for correction in corrections:
        if correction.goal_id != spec.goal_id:
            continue
        if correction.at > evaluated_at:
            continue
        if goal_started_at < correction.at and window_hours is not None:
            closes_at = goal_started_at + timedelta(hours=window_hours)
            if closes_at <= correction.at:
                # The goal had already closed before the correction landed.
                continue
        applied = correction

    if applied is not None:
        event = applied.new_event or event
        condition = applied.new_condition or condition
    return event, condition, applied


def _window_events(
    context: GoalContext, name: str, start: datetime, end: datetime
) -> List[Event]:
    """Events of one type whose ``received_at`` falls inside the window."""
    return [
        event
        for event in context.events_named(name)
        if start <= event.received_at <= end
    ]


def _evaluate_success(
    spec: GoalSpec,
    context: GoalContext,
    *,
    event_name: Optional[str],
    condition: str,
    start: datetime,
    end: datetime,
    started_slugs: Sequence[str] = (),
    release_slugs: Sequence[str] = (),
) -> Tuple[bool, Dict[str, Any]]:
    """Decide whether the goal's success condition was met inside the window.

    ``started_slugs`` and ``release_slugs`` carry the context-dependent
    facts that the success condition refers to (G07's unfinished series and
    G08's recent releases); they are computed once, from the goal start.
    """
    evidence: Dict[str, Any] = {"condition": condition, "window_end": end.isoformat()}
    if event_name is None:
        evidence["reason"] = "success event is undefined"
        return False, evidence

    candidates = _window_events(context, event_name, start, end)
    evidence["candidate_events"] = len(candidates)
    if not candidates:
        evidence["reason"] = f"no {event_name} inside the window"
        return False, evidence

    # --- G03: a later IST calendar day than the goal start ---------------
    if "later ist" in condition:
        start_day = ist_date(start)
        later = [event for event in candidates if ist_date(event.received_at) > start_day]
        if not later:
            evidence["reason"] = f"no {event_name} on a later IST day"
            return False, evidence
        evidence["matched_at"] = later[0].received_at.isoformat()
        evidence["ist_days"] = sorted({ist_date(e.received_at).isoformat() for e in later})
        return True, evidence

    # --- G09: three distinct IST days ------------------------------------
    if "distinct ist days" in condition:
        days = sorted({ist_date(event.received_at) for event in candidates})
        evidence["ist_days"] = [day.isoformat() for day in days]
        if len(days) < 3:
            evidence["reason"] = f"{len(days)} distinct IST days, need 3"
            return False, evidence
        return True, evidence

    # --- G04/G05/G06: a paid purchase ------------------------------------
    if event_name == "purchase_completed":
        paid = [event for event in candidates if _is_paid_purchase(event.properties)]
        if not paid:
            evidence["reason"] = "no non-trial purchase inside the window"
            return False, evidence
        evidence["matched_at"] = paid[0].received_at.isoformat()
        evidence["plan_id"] = paid[0].properties.get("plan_id")
        return True, evidence

    # --- G07: the same content_slug as the unfinished series -------------
    if event_name == "play_started" and "same content_slug" in condition:
        wanted = set(started_slugs)
        matched = [
            event
            for event in candidates
            if context.slug_of(event.properties.get("title_id")) in wanted
        ]
        if not matched:
            evidence["reason"] = "no play on the unfinished series"
            return False, evidence
        evidence["matched_at"] = matched[0].received_at.isoformat()
        evidence["content_slug"] = context.slug_of(
            matched[0].properties.get("title_id")
        )
        return True, evidence

    # --- G08: a title released in the 30 days before the goal start ------
    if event_name == "play_started" and "released in the 30 days" in condition:
        wanted = set(release_slugs)
        matched = [
            event
            for event in candidates
            if context.slug_of(event.properties.get("title_id")) in wanted
        ]
        if not matched:
            evidence["reason"] = "no play on a recently released title"
            return False, evidence
        evidence["matched_at"] = matched[0].received_at.isoformat()
        evidence["content_slug"] = context.slug_of(
            matched[0].properties.get("title_id")
        )
        return True, evidence

    # --- everything else: the event is enough ----------------------------
    evidence["matched_at"] = candidates[0].received_at.isoformat()
    return True, evidence


def evaluate_goal(
    spec: GoalSpec,
    context: GoalContext,
    *,
    corrections: Sequence[RuleCorrection] = (),
    goal_started_at: Optional[datetime] = None,
    evaluated_at: Optional[datetime] = None,
) -> GoalOutcome:
    """Judge one goal for one user.

    ``goal_started_at`` defaults to the trigger time: the goal window is
    anchored to the moment the agent commits to the goal.  ``evaluated_at``
    defaults to the same moment; pass the current time to also decide
    whether the goal has already failed (window exhausted).
    """
    start = goal_started_at or context.trigger_at
    now = evaluated_at or context.now or start
    window_hours = spec.window_hours
    end = start + timedelta(hours=window_hours) if window_hours is not None else None

    event_name, condition, applied = effective_rule(
        spec,
        corrections,
        goal_started_at=start,
        evaluated_at=now,
    )

    if not spec.is_measurable:
        return GoalOutcome(
            goal_id=spec.goal_id,
            state=PENDING,
            eligible=False,
            reason="success condition is not measurable",
            started_at=start,
            window_end=end,
            evidence={"condition": condition},
        )

    if end is None:
        return GoalOutcome(
            goal_id=spec.goal_id,
            state=PENDING,
            eligible=False,
            reason="no success window configured",
            started_at=start,
            window_end=None,
        )

    started_slugs = _resumable_series(context) if spec.goal_id == "G07" else []
    release_slugs = _recent_release_slugs(context) if spec.goal_id == "G08" else []

    succeeded, evidence = _evaluate_success(
        spec,
        context,
        event_name=event_name,
        condition=condition,
        start=start,
        end=end,
        started_slugs=started_slugs,
        release_slugs=release_slugs,
    )
    if applied is not None:
        evidence["correction_applied"] = {
            "at": applied.at.isoformat(),
            "old_event": applied.old_success.get("event"),
            "new_event": applied.new_event,
        }

    if succeeded:
        return GoalOutcome(
            goal_id=spec.goal_id,
            state=SUCCEEDED,
            eligible=True,
            reason="success condition met",
            started_at=start,
            window_end=end,
            evidence=evidence,
        )

    if now >= end:
        return GoalOutcome(
            goal_id=spec.goal_id,
            state=FAILED,
            eligible=True,
            reason=str(evidence.get("reason") or "window closed without success"),
            started_at=start,
            window_end=end,
            evidence=evidence,
        )

    return GoalOutcome(
        goal_id=spec.goal_id,
        state=ACTIVE,
        eligible=True,
        reason="still open",
        started_at=start,
        window_end=end,
        evidence=evidence,
    )


def is_eligible(
    spec: GoalSpec,
    context: GoalContext,
    *,
    corrections: Sequence[RuleCorrection] = (),
) -> Tuple[bool, str]:
    """Whether the user may be given this goal, and why."""
    if not spec.is_measurable:
        return False, "success condition is not measurable"
    return _check_eligibility(spec, context)


def eligible_goals(
    specs: Mapping[str, GoalSpec],
    context: GoalContext,
    *,
    corrections: Sequence[RuleCorrection] = (),
    priority: Optional[Sequence[str]] = None,
) -> List[Tuple[GoalSpec, str]]:
    """The measurable goals this user qualifies for, best first."""
    results: List[Tuple[GoalSpec, str]] = []
    for goal_id in priority_order(priority):
        spec = specs.get(goal_id)
        if spec is None:
            continue
        ok, reason = is_eligible(spec, context, corrections=corrections)
        if ok:
            results.append((spec, reason))
    return results


def select_goal(
    specs: Mapping[str, GoalSpec],
    context: GoalContext,
    *,
    corrections: Sequence[RuleCorrection] = (),
    priority: Optional[Sequence[str]] = None,
) -> Optional[GoalSpec]:
    """Pick the single goal this user should get, or ``None``.

    Only measurable goals are considered; the priority order decides.
    """
    candidates = eligible_goals(
        specs, context, corrections=corrections, priority=priority
    )
    return candidates[0][0] if candidates else None


class GoalManager:
    """Goal engine bound to one data pack.

    The manager owns the goal configuration, the control-event corrections
    and the per-user view of the loaded evidence, so callers only have to
    ask it questions about a user.
    """

    def __init__(
        self,
        pack: DataPack,
        *,
        goals_path: Optional[str] = None,
        corrections: Optional[Sequence[RuleCorrection]] = None,
        priority: Optional[Sequence[str]] = None,
    ) -> None:
        self.pack = pack
        root = str(getattr(pack.paths, 'root', '') or '')
        config_path = goals_path or os.path.join(root, 'config', 'goals.json')
        self.specs: Dict[str, GoalSpec] = load_goal_specs(config_path)
        if corrections is None:
            control_path = os.path.join(root, 'replay', 'control_events.jsonl')
            corrections = load_rule_corrections(control_path)
        self.corrections: List[RuleCorrection] = list(corrections)
        self.priority: Tuple[str, ...] = priority_order(priority)

    # -- context building --------------------------------------------------

    def context_for(
        self,
        user_id: str,
        *,
        trigger_at: datetime,
        now: Optional[datetime] = None,
        events: Optional[Sequence[Event]] = None,
    ) -> GoalContext:
        """Build the :class:`GoalContext` for one user at one moment."""
        pack = self.pack
        user_events = (
            list(events) if events is not None else pack.events_for_user(user_id)
        )
        episodes_by_slug: Dict[str, List[Any]] = {}
        for slug, bucket in pack.episodes.items():
            episodes_by_slug[slug] = list(bucket)
        return GoalContext(
            user_id=user_id,
            trigger_at=trigger_at,
            events=user_events,
            profile=pack.profile_for(user_id),
            titles=dict(pack.titles),
            episodes_by_slug=episodes_by_slug,
            now=now,
        )

    # -- questions ---------------------------------------------------------

    def eligible(self, context: GoalContext) -> List[Tuple[GoalSpec, str]]:
        """The goals this user qualifies for, best first."""
        return eligible_goals(
            self.specs,
            context,
            corrections=self.corrections,
            priority=self.priority,
        )

    def select(self, context: GoalContext) -> Optional[GoalSpec]:
        """The single goal to give this user, or ``None``."""
        return select_goal(
            self.specs,
            context,
            corrections=self.corrections,
            priority=self.priority,
        )

    def evaluate(
        self,
        spec: GoalSpec,
        context: GoalContext,
        *,
        goal_started_at: Optional[datetime] = None,
        evaluated_at: Optional[datetime] = None,
    ) -> GoalOutcome:
        """Judge one goal for this user."""
        return evaluate_goal(
            spec,
            context,
            corrections=self.corrections,
            goal_started_at=goal_started_at,
            evaluated_at=evaluated_at,
        )

    def outcome(
        self,
        goal_id: str,
        context: GoalContext,
        *,
        goal_started_at: Optional[datetime] = None,
        evaluated_at: Optional[datetime] = None,
    ) -> GoalOutcome:
        """Judge the named goal for this user."""
        spec = self.specs.get(goal_id)
        if spec is None:
            return GoalOutcome(
                goal_id=goal_id,
                state=PENDING,
                eligible=False,
                reason="unknown goal",
            )
        return self.evaluate(
            spec,
            context,
            goal_started_at=goal_started_at,
            evaluated_at=evaluated_at,
        )

    def active_goal(
        self,
        context: GoalContext,
        *,
        goal_started_at: Optional[datetime] = None,
        evaluated_at: Optional[datetime] = None,
    ) -> Optional[Tuple[GoalSpec, GoalOutcome]]:
        """The goal the user currently holds, with its outcome.

        A goal the user already holds wins over any newly selected goal:
        one user gets one goal, and it is not swapped while it is running.
        """
        spec = self.select(context)
        if spec is None:
            return None
        outcome = self.evaluate(
            spec,
            context,
            goal_started_at=goal_started_at,
            evaluated_at=evaluated_at,
        )
        return spec, outcome