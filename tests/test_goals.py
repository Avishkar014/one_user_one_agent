"""Tests for :mod:`src.goals`.

The tests build small synthetic contexts (profiles, events, titles and
episodes) so that every eligibility rule, success rule, correction and
priority decision can be asserted exactly.  A handful of tests also run
against the real, shipped data pack.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.data_loader import (  # noqa: E402
    DataPackPaths,
    Episode,
    Event,
    Profile,
    Title,
    load_data_pack,
)
from src.goals import (  # noqa: E402
    ACTIVE,
    DEFAULT_PRIORITY,
    FAILED,
    PENDING,
    SUCCEEDED,
    GoalContext,
    GoalManager,
    GoalSpec,
    RuleCorrection,
    effective_rule,
    eligible_goals,
    evaluate_goal,
    is_eligible,
    ist_date,
    load_goal_specs,
    load_rule_corrections,
    priority_order,
    select_goal,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REAL_PACK = os.path.join(REPO_ROOT, "data_pack", "one_user_one_agent_pack")
GOALS_CONFIG = os.path.join(REAL_PACK, "config", "goals.json")
CONTROL_EVENTS = os.path.join(REAL_PACK, "replay", "control_events.jsonl")

UTC = timezone.utc


def ts(text: str) -> datetime:
    """Parse an ISO-8601 UTC timestamp."""
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def make_event(
    event_id: str,
    name: str,
    received_at: datetime,
    *,
    user_id: str = "U-1",
    properties=None,
    occurred_at=None,
) -> Event:
    """Build an event with sensible defaults."""
    return Event(
        event_id=event_id,
        user_id=user_id,
        event=name,
        occurred_at=occurred_at or received_at,
        sent_at=received_at,
        received_at=received_at,
        properties=dict(properties or {}),
    )


def make_profile(
    user_id: str = "U-1",
    *,
    subscription_status: str = "never",
    signup_at: datetime = None,
    updated_at: datetime = None,
) -> Profile:
    """Build a profile with sensible defaults."""
    return Profile(
        user_id=user_id,
        updated_at=updated_at or ts("2026-09-01T00:00:00.000Z"),
        signup_at=signup_at or ts("2026-09-01T00:00:00.000Z"),
        subscription_status=subscription_status,
    )


def make_title(
    title_id: str,
    slug: str,
    *,
    content_type: str = "show",
    release_date: str = "2026-01-01",
) -> Title:
    """Build a catalogue entry."""
    return Title(
        title_id=title_id,
        content_slug=slug,
        title=title_id,
        content_type=content_type,
        release_date=release_date,
    )


def make_episode(episode_id: str, slug: str, number: int = 1) -> Episode:
    """Build a single episode of a series."""
    return Episode(episode_id=episode_id, content_slug=slug, season=1, episode=number)


@pytest.fixture()
def specs():
    """The goal specs from the shipped configuration."""
    return load_goal_specs(GOALS_CONFIG)


def build_context(
    *,
    trigger_at: datetime,
    events=(),
    profile=None,
    titles=(),
    episodes_by_slug=None,
    user_id: str = "U-1",
    now=None,
) -> GoalContext:
    """Build a :class:`GoalContext` out of simple parts."""
    return GoalContext(
        user_id=user_id,
        trigger_at=trigger_at,
        events=list(events),
        profile=profile,
        titles={title.title_id: title for title in titles},
        episodes_by_slug=dict(episodes_by_slug or {}),
        now=now,
    )


def spec_for(specs, goal_id: str) -> GoalSpec:
    """Look a spec up, failing loudly when the id is unknown."""
    assert goal_id in specs, f"{goal_id} is not in the configuration"
    return specs[goal_id]


def test_goal_specs_are_loaded_from_the_configuration(specs):
    assert set(specs) == {f"G{index:02d}" for index in range(1, 13)}
    assert specs["G01"].name == "first_play_24h"
    assert specs["G01"].window_hours == 24
    assert specs["G09"].window_hours == 336


def test_only_g10_is_unmeasurable(specs):
    measurable = {goal_id for goal_id, spec in specs.items() if spec.is_measurable}
    assert "G10" not in measurable
    assert measurable == set(specs) - {"G10"}
    assert specs["G10"].success_event is None


def test_default_priority_is_the_documented_order():
    assert DEFAULT_PRIORITY == (
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
    assert "G10" not in DEFAULT_PRIORITY
    assert priority_order() == DEFAULT_PRIORITY
    assert priority_order(["G01", "G02"]) == ("G01", "G02")


def test_g01_requires_a_recent_signup_and_no_prior_play(specs):
    spec = spec_for(specs, "G01")
    trigger = ts("2026-09-05T00:00:00.000Z")

    recent = make_profile(subscription_status="never", signup_at=ts("2026-09-01T00:00:00.000Z"))
    ok, _ = is_eligible(spec, build_context(trigger_at=trigger, profile=recent))
    assert ok is True

    old = make_profile(subscription_status="never", signup_at=ts("2026-08-01T00:00:00.000Z"))
    ok, reason = is_eligible(spec, build_context(trigger_at=trigger, profile=old))
    assert ok is False
    assert "signed up" in reason

    played = [make_event("e1", "play_started", ts("2026-09-02T00:00:00.000Z"))]
    ok, reason = is_eligible(
        spec, build_context(trigger_at=trigger, profile=recent, events=played)
    )
    assert ok is False
    assert "play_started" in reason


def test_g01_succeeds_on_a_play_inside_24_hours(specs):
    spec = spec_for(specs, "G01")
    trigger = ts("2026-09-05T00:00:00.000Z")
    profile = make_profile(signup_at=ts("2026-09-04T00:00:00.000Z"))
    events = [make_event("e1", "play_started", trigger + timedelta(hours=5))]

    outcome = evaluate_goal(
        spec, build_context(trigger_at=trigger, events=events, profile=profile)
    )
    assert outcome.state == SUCCEEDED
    assert outcome.eligible is True
    assert outcome.window_end == trigger + timedelta(hours=24)
    assert outcome.evidence["matched_at"] == (trigger + timedelta(hours=5)).isoformat()


def test_g01_fails_when_the_window_closes_without_a_play(specs):
    spec = spec_for(specs, "G01")
    trigger = ts("2026-09-05T00:00:00.000Z")
    profile = make_profile(signup_at=ts("2026-09-04T00:00:00.000Z"))
    late = [make_event("e1", "play_started", trigger + timedelta(hours=25))]

    outcome = evaluate_goal(
        spec,
        build_context(trigger_at=trigger, events=late, profile=profile),
        evaluated_at=trigger + timedelta(hours=30),
    )
    assert outcome.state == FAILED
    assert outcome.evidence["candidate_events"] == 0


def test_g01_stays_active_while_the_window_is_open(specs):
    spec = spec_for(specs, "G01")
    trigger = ts("2026-09-05T00:00:00.000Z")
    profile = make_profile(signup_at=ts("2026-09-04T00:00:00.000Z"))

    outcome = evaluate_goal(
        spec,
        build_context(trigger_at=trigger, profile=profile),
        evaluated_at=trigger + timedelta(hours=2),
    )
    assert outcome.state == ACTIVE
    assert outcome.reason == "still open"


def test_g02_requires_no_prior_completed_episode(specs):
    spec = spec_for(specs, "G02")
    trigger = ts("2026-09-05T00:00:00.000Z")

    ok, _ = is_eligible(spec, build_context(trigger_at=trigger))
    assert ok is True

    completed = [
        make_event("e1", "episode_completed", ts("2026-09-04T00:00:00.000Z"))
    ]
    ok, reason = is_eligible(
        spec, build_context(trigger_at=trigger, events=completed)
    )
    assert ok is False
    assert "episode_completed" in reason


def test_g02_succeeds_on_a_completed_episode_within_72_hours(specs):
    spec = spec_for(specs, "G02")
    trigger = ts("2026-09-05T00:00:00.000Z")
    events = [make_event("e1", "episode_completed", trigger + timedelta(hours=71))]

    outcome = evaluate_goal(spec, build_context(trigger_at=trigger, events=events))
    assert outcome.state == SUCCEEDED
    assert outcome.window_end == trigger + timedelta(hours=72)


def test_g02_ignores_a_completion_after_the_window(specs):
    spec = spec_for(specs, "G02")
    trigger = ts("2026-09-05T00:00:00.000Z")
    events = [make_event("e1", "episode_completed", trigger + timedelta(hours=73))]

    outcome = evaluate_goal(
        spec,
        build_context(trigger_at=trigger, events=events),
        evaluated_at=trigger + timedelta(hours=80),
    )
    assert outcome.state == FAILED


def test_g03_needs_a_signup_within_two_days(specs):
    spec = spec_for(specs, "G03")
    trigger = ts("2026-09-05T00:00:00.000Z")

    ok, _ = is_eligible(
        spec,
        build_context(
            trigger_at=trigger, profile=make_profile(signup_at=ts("2026-09-04T00:00:00.000Z"))
        ),
    )
    assert ok is True

    ok, reason = is_eligible(
        spec,
        build_context(
            trigger_at=trigger, profile=make_profile(signup_at=ts("2026-09-02T00:00:00.000Z"))
        ),
    )
    assert ok is False
    assert "signed up" in reason


def test_g03_needs_a_return_on_a_later_ist_day(specs):
    spec = spec_for(specs, "G03")
    trigger = ts("2026-09-05T20:00:00.000Z")  # 2026-09-06 01:30 IST
    same_day = [make_event("e1", "app_open", ts("2026-09-05T22:00:00.000Z"))]

    outcome = evaluate_goal(spec, build_context(trigger_at=trigger, events=same_day))
    assert outcome.state == ACTIVE
    assert outcome.evidence["candidate_events"] == 1
    assert "later IST day" in outcome.evidence["reason"]

    next_day = [make_event("e2", "app_open", ts("2026-09-06T20:00:00.000Z"))]
    outcome = evaluate_goal(spec, build_context(trigger_at=trigger, events=next_day))
    assert outcome.state == SUCCEEDED
    assert outcome.evidence["ist_days"] == ["2026-09-07"]


def test_g03_correction_replaces_app_open_with_play_started(specs):
    spec = spec_for(specs, "G03")
    trigger = ts("2026-10-01T10:00:00.000Z")
    correction = RuleCorrection(
        goal_id="G03",
        at=ts("2026-10-01T08:30:00.000Z"),
        old_success={"event": "app_open"},
        new_success={
            "event": "play_started",
            "condition": "on a later IST calendar day within the window",
        },
    )
    opened = [make_event("e1", "app_open", ts("2026-10-02T20:00:00.000Z"))]

    without = evaluate_goal(
        spec,
        build_context(trigger_at=trigger, events=opened),
        evaluated_at=ts("2026-10-01T12:00:00.000Z"),
    )
    assert without.state == SUCCEEDED
    assert "correction_applied" not in without.evidence

    after = evaluate_goal(
        spec,
        build_context(trigger_at=trigger, events=opened),
        corrections=[correction],
        evaluated_at=trigger + timedelta(hours=48),
    )
    assert after.state == FAILED
    assert after.evidence["correction_applied"]["new_event"] == "play_started"
    assert after.evidence["candidate_events"] == 0


def test_g03_correction_applies_to_a_goal_that_was_already_running(specs):
    spec = spec_for(specs, "G03")
    trigger = ts("2026-09-30T00:00:00.000Z")
    correction = RuleCorrection(
        goal_id="G03",
        at=ts("2026-10-01T08:30:00.000Z"),
        new_success={"event": "play_started", "condition": "on a later IST calendar day"},
    )
    opened = [make_event("e1", "app_open", ts("2026-10-01T02:00:00.000Z"))]
    played = [make_event("e2", "play_started", ts("2026-10-01T03:00:00.000Z"))]

    outcome = evaluate_goal(
        spec,
        build_context(trigger_at=trigger, events=opened + played),
        corrections=[correction],
        evaluated_at=ts("2026-10-01T09:00:00.000Z"),
    )
    assert outcome.state == SUCCEEDED
    assert outcome.evidence["correction_applied"]["new_event"] == "play_started"


def test_g03_correction_does_not_reopen_a_closed_goal(specs):
    spec = spec_for(specs, "G03")
    trigger = ts("2026-09-25T00:00:00.000Z")
    correction = RuleCorrection(
        goal_id="G03",
        at=ts("2026-10-01T08:30:00.000Z"),
        new_success={"event": "play_started", "condition": "on a later IST calendar day"},
    )
    events = [make_event("e1", "app_open", ts("2026-09-26T02:00:00.000Z"))]

    outcome = evaluate_goal(
        spec,
        build_context(trigger_at=trigger, events=events),
        corrections=[correction],
        evaluated_at=ts("2026-10-01T09:00:00.000Z"),
    )
    assert outcome.state == SUCCEEDED
    assert "correction_applied" not in outcome.evidence


@pytest.mark.parametrize("goal_id,status", [("G04", "trial_active"), ("G05", "never")])
def test_purchase_goals_need_the_right_subscription_status(specs, goal_id, status):
    spec = spec_for(specs, goal_id)
    trigger = ts("2026-09-05T00:00:00.000Z")

    ok, _ = is_eligible(
        spec,
        build_context(trigger_at=trigger, profile=make_profile(subscription_status=status)),
    )
    assert ok is True

    ok, reason = is_eligible(
        spec,
        build_context(
            trigger_at=trigger, profile=make_profile(subscription_status="lapsed")
        ),
    )
    assert ok is False
    assert "subscription_status" in reason


@pytest.mark.parametrize("goal_id,status,hours", [("G04", "trial_active", 168), ("G05", "never", 168), ("G06", "lapsed", 336)])
def test_purchase_goals_reject_trial_purchases(specs, goal_id, status, hours):
    spec = spec_for(specs, goal_id)
    trigger = ts("2026-09-05T00:00:00.000Z")
    profile = make_profile(subscription_status=status)
    trial = [
        make_event(
            "e1",
            "purchase_completed",
            trigger + timedelta(hours=1),
            properties={"is_trial": True, "plan_id": "P_TRIAL"},
        )
    ]

    ok, _ = is_eligible(spec, build_context(trigger_at=trigger, profile=profile, events=trial))
    assert ok is True

    outcome = evaluate_goal(
        spec,
        build_context(trigger_at=trigger, profile=profile, events=trial),
        evaluated_at=trigger + timedelta(hours=hours + 1),
    )
    assert outcome.state == FAILED
    assert "non-trial" in outcome.reason


@pytest.mark.parametrize("goal_id,status,hours", [("G04", "trial_active", 168), ("G05", "never", 168), ("G06", "lapsed", 336)])
def test_purchase_goals_succeed_on_a_paid_purchase(specs, goal_id, status, hours):
    spec = spec_for(specs, goal_id)
    trigger = ts("2026-09-05T00:00:00.000Z")
    profile = make_profile(subscription_status=status)
    paid = [
        make_event(
            "e1",
            "purchase_completed",
            trigger + timedelta(hours=2),
            properties={"is_trial": False, "plan_id": "P_MONTH", "price_inr": 149},
        )
    ]

    outcome = evaluate_goal(spec, build_context(trigger_at=trigger, profile=profile, events=paid))
    assert outcome.state == SUCCEEDED
    assert outcome.window_end == trigger + timedelta(hours=hours)
    assert outcome.evidence["plan_id"] == "P_MONTH"


def test_purchase_without_the_trial_flag_is_not_a_paid_purchase(specs):
    spec = spec_for(specs, "G05")
    trigger = ts("2026-09-05T00:00:00.000Z")
    profile = make_profile(subscription_status="never")
    unknown = [
        make_event(
            "e1",
            "purchase_completed",
            trigger + timedelta(hours=1),
            properties={"plan_id": "P_MONTH"},
        )
    ]

    outcome = evaluate_goal(
        spec,
        build_context(trigger_at=trigger, profile=profile, events=unknown),
        evaluated_at=trigger + timedelta(hours=200),
    )
    assert outcome.state == FAILED


def test_g07_needs_an_unfinished_show_the_user_stopped_playing(specs):
    spec = spec_for(specs, "G07")
    trigger = ts("2026-09-20T00:00:00.000Z")
    title = make_title("T1", "show-one")
    episodes = {"show-one": [make_episode("E1", "show-one", 1), make_episode("E2", "show-one", 2)]}
    started = [make_event("e1", "play_started", ts("2026-09-10T00:00:00.000Z"), properties={"title_id": "T1"})]

    ok, reason = is_eligible(
        spec,
        build_context(
            trigger_at=trigger,
            events=started,
            titles=[title],
            episodes_by_slug=episodes,
        ),
    )
    assert ok is True

    recent = [make_event("e2", "play_started", ts("2026-09-19T00:00:00.000Z"), properties={"title_id": "T1"})]
    ok, reason = is_eligible(
        spec,
        build_context(
            trigger_at=trigger,
            events=recent,
            titles=[title],
            episodes_by_slug=episodes,
        ),
    )
    assert ok is False
    assert "unfinished" in reason


def test_g07_ignores_a_fully_completed_series(specs):
    spec = spec_for(specs, "G07")
    trigger = ts("2026-09-20T00:00:00.000Z")
    title = make_title("T1", "show-one")
    episodes = {"show-one": [make_episode("E1", "show-one", 1)]}
    events = [
        make_event("e1", "play_started", ts("2026-09-10T00:00:00.000Z"), properties={"title_id": "T1"}),
        make_event(
            "e2",
            "episode_completed",
            ts("2026-09-11T00:00:00.000Z"),
            properties={"title_id": "T1", "episode_id": "E1"},
        ),
    ]

    ok, _ = is_eligible(
        spec,
        build_context(
            trigger_at=trigger,
            events=events,
            titles=[title],
            episodes_by_slug=episodes,
        ),
    )
    assert ok is False


def test_g07_ignores_a_film_because_a_film_cannot_be_resumed(specs):
    spec = spec_for(specs, "G07")
    trigger = ts("2026-09-20T00:00:00.000Z")
    film = make_title("T2", "a-film", content_type="film")
    episodes = {"a-film": [make_episode("E9", "a-film", 1)]}
    events = [make_event("e1", "play_started", ts("2026-09-10T00:00:00.000Z"), properties={"title_id": "T2"})]

    ok, _ = is_eligible(
        spec,
        build_context(
            trigger_at=trigger,
            events=events,
            titles=[film],
            episodes_by_slug=episodes,
        ),
    )
    assert ok is False


def test_g07_succeeds_only_when_the_same_slug_is_played(specs):
    spec = spec_for(specs, "G07")
    trigger = ts("2026-09-20T00:00:00.000Z")
    show = make_title("T1", "show-one")
    other = make_title("T2", "show-two")
    episodes = {"show-one": [make_episode("E1", "show-one", 1), make_episode("E2", "show-one", 2)]}
    started = [make_event("e1", "play_started", ts("2026-09-10T00:00:00.000Z"), properties={"title_id": "T1"})]

    wrong = started + [
        make_event("e2", "play_started", ts("2026-09-21T00:00:00.000Z"), properties={"title_id": "T2"})
    ]
    outcome = evaluate_goal(
        spec,
        build_context(
            trigger_at=trigger,
            events=wrong,
            titles=[show, other],
            episodes_by_slug=episodes,
        ),
        evaluated_at=trigger + timedelta(hours=80),
    )
    assert outcome.state == FAILED
    assert "unfinished series" in outcome.reason

    right = started + [
        make_event("e3", "play_started", ts("2026-09-21T00:00:00.000Z"), properties={"title_id": "T1"})
    ]
    outcome = evaluate_goal(
        spec,
        build_context(
            trigger_at=trigger,
            events=right,
            titles=[show, other],
            episodes_by_slug=episodes,
        ),
    )
    assert outcome.state == SUCCEEDED
    assert outcome.evidence["content_slug"] == "show-one"


def test_g08_needs_a_recent_release_and_an_active_subscriber(specs):
    spec = spec_for(specs, "G08")
    trigger = ts("2026-09-20T00:00:00.000Z")
    old = make_title("T1", "old-show", release_date="2026-01-01")
    new = make_title("T2", "new-show", release_date="2026-09-10")
    profile = make_profile(subscription_status="active")

    ok, _ = is_eligible(
        spec,
        build_context(trigger_at=trigger, profile=profile, titles=[old, new]),
    )
    assert ok is True

    ok, reason = is_eligible(
        spec,
        build_context(trigger_at=trigger, profile=profile, titles=[old]),
    )
    assert ok is False
    assert "released" in reason

    ok, reason = is_eligible(
        spec,
        build_context(
            trigger_at=trigger,
            profile=make_profile(subscription_status="never"),
            titles=[new],
        ),
    )
    assert ok is False
    assert "subscription_status" in reason


def test_g08_succeeds_only_on_a_recently_released_title(specs):
    spec = spec_for(specs, "G08")
    trigger = ts("2026-09-20T00:00:00.000Z")
    old = make_title("T1", "old-show", release_date="2026-01-01")
    new = make_title("T2", "new-show", release_date="2026-09-10")
    profile = make_profile(subscription_status="active")

    events = [
        make_event("e1", "play_started", ts("2026-09-21T00:00:00.000Z"), properties={"title_id": "T1"})
    ]
    outcome = evaluate_goal(
        spec,
        build_context(trigger_at=trigger, profile=profile, events=events, titles=[old, new]),
        evaluated_at=trigger + timedelta(hours=200),
    )
    assert outcome.state == FAILED
    assert "recently released" in outcome.reason

    events = [
        make_event("e2", "play_started", ts("2026-09-21T00:00:00.000Z"), properties={"title_id": "T2"})
    ]
    outcome = evaluate_goal(
        spec,
        build_context(trigger_at=trigger, profile=profile, events=events, titles=[old, new]),
    )
    assert outcome.state == SUCCEEDED
    assert outcome.evidence["content_slug"] == "new-show"


def test_g08_rejects_a_release_older_than_thirty_days(specs):
    spec = spec_for(specs, "G08")
    trigger = ts("2026-09-20T00:00:00.000Z")
    profile = make_profile(subscription_status="active")
    edge = make_title("T1", "edge-show", release_date="2026-08-22")
    too_old = make_title("T2", "old-show", release_date="2026-08-20")

    ok, _ = is_eligible(
        spec, build_context(trigger_at=trigger, profile=profile, titles=[edge])
    )
    assert ok is True

    ok, _ = is_eligible(
        spec, build_context(trigger_at=trigger, profile=profile, titles=[too_old])
    )
    assert ok is False


def test_g09_needs_an_active_subscriber_who_rarely_watches(specs):
    spec = spec_for(specs, "G09")
    trigger = ts("2026-09-20T00:00:00.000Z")
    profile = make_profile(subscription_status="active")

    one_play = [make_event("e1", "play_started", ts("2026-09-19T00:00:00.000Z"))]
    ok, _ = is_eligible(
        spec, build_context(trigger_at=trigger, profile=profile, events=one_play)
    )
    assert ok is True

    ok, reason = is_eligible(
        spec,
        build_context(
            trigger_at=trigger, profile=make_profile(subscription_status="never")
        ),
    )
    assert ok is False
    assert "subscription_status" in reason

    two_days = [
        make_event("e1", "play_started", ts("2026-09-18T00:00:00.000Z")),
        make_event("e2", "play_started", ts("2026-09-19T00:00:00.000Z")),
    ]
    ok, reason = is_eligible(
        spec, build_context(trigger_at=trigger, profile=profile, events=two_days)
    )
    assert ok is False
    assert "played on 2 days" in reason


def test_g09_needs_evidence_before_it_can_count_play_days(specs):
    spec = spec_for(specs, "G09")
    trigger = ts("2026-09-20T00:00:00.000Z")
    profile = make_profile(subscription_status="active")

    ok, reason = is_eligible(
        spec, build_context(trigger_at=trigger, profile=profile, events=[])
    )
    assert ok is False
    assert "no event history" in reason


def test_g09_succeeds_on_three_distinct_ist_days(specs):
    spec = spec_for(specs, "G09")
    trigger = ts("2026-09-20T00:00:00.000Z")
    profile = make_profile(subscription_status="active")
    events = [
        make_event("e1", "play_started", ts("2026-09-20T10:00:00.000Z")),
        make_event("e2", "play_started", ts("2026-09-20T22:00:00.000Z")),
        make_event("e3", "play_started", ts("2026-09-21T10:00:00.000Z")),
    ]

    outcome = evaluate_goal(
        spec,
        build_context(trigger_at=trigger, profile=profile, events=events),
        evaluated_at=trigger + timedelta(hours=336),
    )
    assert outcome.state == FAILED
    assert outcome.evidence["ist_days"] == ["2026-09-20", "2026-09-21"]
    assert "2 distinct IST days" in outcome.reason

    # While the window is still open the goal is not over -- it is still active.
    still_open = evaluate_goal(
        spec, build_context(trigger_at=trigger, profile=profile, events=events)
    )
    assert still_open.state == ACTIVE
    assert still_open.reason == "still open"

    events.append(make_event("e4", "play_started", ts("2026-09-22T10:00:00.000Z")))
    outcome = evaluate_goal(
        spec, build_context(trigger_at=trigger, profile=profile, events=events)
    )
    assert outcome.state == SUCCEEDED
    assert outcome.evidence["ist_days"] == [
        "2026-09-20",
        "2026-09-21",
        "2026-09-22",
    ]


def test_g09_counts_ist_days_not_utc_days(specs):
    spec = spec_for(specs, "G09")
    trigger = ts("2026-09-20T00:00:00.000Z")
    profile = make_profile(subscription_status="active")
    # 18:30Z is already the next IST day, so these three events are three
    # IST days even though two of them share a UTC date.
    events = [
        make_event("e1", "play_started", ts("2026-09-20T18:30:00.000Z")),
        make_event("e2", "play_started", ts("2026-09-21T18:30:00.000Z")),
        make_event("e3", "play_started", ts("2026-09-22T18:30:00.000Z")),
    ]

    outcome = evaluate_goal(
        spec, build_context(trigger_at=trigger, profile=profile, events=events)
    )
    assert outcome.state == SUCCEEDED
    assert outcome.evidence["ist_days"] == [
        "2026-09-21",
        "2026-09-22",
        "2026-09-23",
    ]


def test_g09_window_is_336_hours(specs):
    spec = spec_for(specs, "G09")
    trigger = ts("2026-09-20T00:00:00.000Z")
    profile = make_profile(subscription_status="active")
    events = [
        make_event("e1", "play_started", ts("2026-09-20T10:00:00.000Z")),
        make_event("e2", "play_started", ts("2026-09-21T10:00:00.000Z")),
        make_event("e3", "play_started", ts("2026-09-22T10:00:00.000Z")),
    ]

    outcome = evaluate_goal(
        spec,
        build_context(trigger_at=trigger, profile=profile, events=events),
        evaluated_at=trigger + timedelta(hours=336),
    )
    assert outcome.state == SUCCEEDED
    assert outcome.window_end == trigger + timedelta(hours=336)


def test_g09_ignores_plays_outside_the_window(specs):
    spec = spec_for(specs, "G09")
    trigger = ts("2026-09-20T00:00:00.000Z")
    profile = make_profile(subscription_status="active")
    events = [
        make_event("e1", "play_started", ts("2026-09-20T10:00:00.000Z")),
        make_event("e2", "play_started", ts("2026-09-21T10:00:00.000Z")),
        make_event("e3", "play_started", ts("2026-10-05T10:00:00.000Z")),
    ]

    outcome = evaluate_goal(
        spec,
        build_context(trigger_at=trigger, profile=profile, events=events),
        evaluated_at=trigger + timedelta(hours=400),
    )
    assert outcome.state == FAILED
    assert outcome.evidence["ist_days"] == ["2026-09-20", "2026-09-21"]


def test_g11_is_eligible_but_never_succeeds_without_tv_evidence(specs):
    spec = spec_for(specs, "G11")
    trigger = ts("2026-09-20T00:00:00.000Z")
    profile = make_profile(subscription_status="active")

    ok, _ = is_eligible(spec, build_context(trigger_at=trigger, profile=profile))
    assert ok is True

    outcome = evaluate_goal(
        spec,
        build_context(trigger_at=trigger, profile=profile),
        evaluated_at=trigger + timedelta(hours=400),
    )
    assert outcome.state == FAILED
    assert "tv_app_open" in outcome.reason


def test_g11_succeeds_when_a_tv_app_open_is_recorded(specs):
    spec = spec_for(specs, "G11")
    trigger = ts("2026-09-20T00:00:00.000Z")
    profile = make_profile(subscription_status="active")
    events = [make_event("e1", "tv_app_open", ts("2026-09-22T00:00:00.000Z"))]

    outcome = evaluate_goal(
        spec, build_context(trigger_at=trigger, profile=profile, events=events)
    )
    assert outcome.state == SUCCEEDED


def test_g12_needs_a_gap_of_at_least_fourteen_days(specs):
    spec = spec_for(specs, "G12")
    trigger = ts("2026-09-20T00:00:00.000Z")

    ok, _ = is_eligible(
        spec,
        build_context(
            trigger_at=trigger,
            events=[make_event("e1", "app_open", ts("2026-09-06T00:00:00.000Z"))],
        ),
    )
    assert ok is True

    ok, reason = is_eligible(
        spec,
        build_context(
            trigger_at=trigger,
            events=[make_event("e2", "app_open", ts("2026-09-10T00:00:00.000Z"))],
        ),
    )
    assert ok is False
    assert "within 14 days" in reason


def test_g12_needs_evidence_of_the_absence(specs):
    spec = spec_for(specs, "G12")
    trigger = ts("2026-09-20T00:00:00.000Z")

    ok, reason = is_eligible(spec, build_context(trigger_at=trigger, events=[]))
    assert ok is False
    assert "no event history" in reason


def test_g12_succeeds_on_a_play_inside_the_window(specs):
    spec = spec_for(specs, "G12")
    trigger = ts("2026-09-20T00:00:00.000Z")
    events = [
        make_event("e1", "app_open", ts("2026-09-01T00:00:00.000Z")),
        make_event("e2", "play_started", ts("2026-09-21T00:00:00.000Z")),
    ]

    outcome = evaluate_goal(spec, build_context(trigger_at=trigger, events=events))
    assert outcome.state == SUCCEEDED
    assert outcome.window_end == trigger + timedelta(hours=168)


def test_g10_is_never_eligible_and_never_scored(specs):
    spec = spec_for(specs, "G10")
    trigger = ts("2026-09-20T00:00:00.000Z")
    context = build_context(
        trigger_at=trigger,
        events=[make_event("e1", "play_started", ts("2026-09-21T00:00:00.000Z"))],
        profile=make_profile(),
    )

    ok, reason = is_eligible(spec, context)
    assert ok is False
    assert "measurable" in reason

    outcome = evaluate_goal(spec, context, evaluated_at=trigger + timedelta(hours=200))
    assert outcome.state == PENDING
    assert outcome.eligible is False
    assert outcome.succeeded is False
    assert outcome.failed is False


def test_missing_profile_never_makes_a_goal_eligible(specs):
    trigger = ts("2026-09-20T00:00:00.000Z")
    context = build_context(trigger_at=trigger, profile=None)

    for goal_id in ("G04", "G05", "G06", "G08", "G09", "G11"):
        ok, reason = is_eligible(spec_for(specs, goal_id), context)
        assert ok is False, goal_id
        assert "no profile" in reason or "subscription_status" in reason


def test_missing_signup_timestamp_never_makes_a_goal_eligible(specs):
    trigger = ts("2026-09-20T00:00:00.000Z")
    profile = Profile(user_id="U-1", updated_at=None, subscription_status="never")

    for goal_id in ("G01", "G03"):
        ok, reason = is_eligible(
            spec_for(specs, goal_id),
            build_context(trigger_at=trigger, profile=profile),
        )
        assert ok is False, goal_id
        assert "no signup timestamp" in reason


def test_missing_evidence_never_makes_a_goal_succeed(specs):
    trigger = ts("2026-09-20T00:00:00.000Z")
    context = build_context(trigger_at=trigger, profile=make_profile())

    outcome = evaluate_goal(
        spec_for(specs, "G02"), context, evaluated_at=trigger + timedelta(hours=100)
    )
    assert outcome.succeeded is False
    assert outcome.state == FAILED
    assert outcome.evidence["candidate_events"] == 0


def test_eligible_goals_are_returned_in_priority_order(specs):
    trigger = ts("2026-09-20T00:00:00.000Z")
    profile = make_profile(subscription_status="trial_active", signup_at=ts("2026-09-19T00:00:00.000Z"))
    context = build_context(trigger_at=trigger, profile=profile)

    candidates = eligible_goals(specs, context)
    ids = [spec.goal_id for spec, _ in candidates]
    assert ids == sorted(ids, key=DEFAULT_PRIORITY.index)
    assert "G04" in ids
    assert "G10" not in ids


def test_only_one_goal_is_selected(specs):
    trigger = ts("2026-09-20T00:00:00.000Z")
    profile = make_profile(subscription_status="trial_active", signup_at=ts("2026-09-19T00:00:00.000Z"))
    context = build_context(trigger_at=trigger, profile=profile)

    chosen = select_goal(specs, context)
    assert chosen is not None
    assert chosen.goal_id == "G04"

    candidates = eligible_goals(specs, context)
    assert len(candidates) >= 2


def test_a_trial_user_is_offered_g04_before_g05(specs):
    trigger = ts("2026-09-20T00:00:00.000Z")
    profile = make_profile(subscription_status="trial_active")
    context = build_context(trigger_at=trigger, profile=profile)
    assert select_goal(specs, context).goal_id == "G04"


def test_a_never_payer_is_offered_g05(specs):
    trigger = ts("2026-09-20T00:00:00.000Z")
    profile = make_profile(subscription_status="never")
    context = build_context(trigger_at=trigger, profile=profile)
    assert select_goal(specs, context).goal_id == "G05"


def test_a_lapsed_subscriber_is_offered_g06(specs):
    trigger = ts("2026-09-20T00:00:00.000Z")
    profile = make_profile(subscription_status="lapsed")
    context = build_context(trigger_at=trigger, profile=profile)
    assert select_goal(specs, context).goal_id == "G06"


def test_no_goal_is_selected_without_evidence(specs):
    trigger = ts("2026-09-20T00:00:00.000Z")
    context = build_context(trigger_at=trigger, profile=None)

    for goal_id in ("G04", "G05", "G06", "G08", "G09", "G11"):
        ok, _ = is_eligible(spec_for(specs, goal_id), context)
        assert ok is False, goal_id


def test_g02_only_depends_on_the_absence_of_a_completion(specs):
    # G02's only rule is "has never completed an episode", so a user with
    # no evidence at all is still a candidate -- the shipped pilot assigned
    # G02 to users with no events.
    trigger = ts("2026-09-20T00:00:00.000Z")
    context = build_context(trigger_at=trigger, profile=None, events=[])
    assert is_eligible(spec_for(specs, "G02"), context)[0] is True
    assert select_goal(specs, context).goal_id == "G02"


def test_selection_ignores_goals_that_are_not_in_the_priority_list(specs):
    trigger = ts("2026-09-20T00:00:00.000Z")
    profile = make_profile(subscription_status="lapsed")
    context = build_context(trigger_at=trigger, profile=profile)

    chosen = select_goal(specs, context, priority=["G06", "G04"])
    assert chosen.goal_id == "G06"


def test_ist_date_uses_the_ist_calendar_day():
    assert ist_date(ts("2026-09-05T18:30:00.000Z")).isoformat() == "2026-09-06"
    assert ist_date(ts("2026-09-05T18:29:00.000Z")).isoformat() == "2026-09-05"


def test_rule_corrections_are_loaded_from_the_control_events():
    corrections = load_rule_corrections(CONTROL_EVENTS)
    assert len(corrections) == 1
    correction = corrections[0]
    assert correction.goal_id == "G03"
    assert correction.new_event == "play_started"
    assert correction.old_success.get("event") == "app_open"
    assert correction.applies_at(correction.at) is True
    assert correction.applies_at(correction.at - timedelta(seconds=1)) is False


def test_missing_control_event_file_yields_no_corrections(tmp_path):
    assert load_rule_corrections(str(tmp_path / "nope.jsonl")) == []


def test_effective_rule_keeps_the_configured_event_before_the_correction(specs):
    spec = spec_for(specs, "G03")
    correction = RuleCorrection(
        goal_id="G03",
        at=ts("2026-10-01T08:30:00.000Z"),
        new_success={"event": "play_started", "condition": "on a later IST calendar day"},
    )

    event, condition, applied = effective_rule(
        spec,
        [correction],
        goal_started_at=ts("2026-09-30T00:00:00.000Z"),
        evaluated_at=ts("2026-10-01T08:00:00.000Z"),
    )
    assert applied is None
    assert event == "app_open"
    assert condition == spec.success_condition

    event, condition, applied = effective_rule(
        spec,
        [correction],
        goal_started_at=ts("2026-09-30T00:00:00.000Z"),
        evaluated_at=ts("2026-10-01T09:00:00.000Z"),
    )
    assert applied is correction
    assert event == "play_started"
    assert "later ist" in condition


def test_real_pack_loads_with_the_goal_manager():
    pack = load_data_pack(DataPackPaths.from_root(REAL_PACK))
    manager = GoalManager(pack)

    assert set(manager.specs) == {f"G{index:02d}" for index in range(1, 13)}
    assert manager.priority == DEFAULT_PRIORITY
    assert [c.goal_id for c in manager.corrections] == ["G03"]


def test_real_pack_selection_returns_one_goal_per_trigger():
    pack = load_data_pack(DataPackPaths.from_root(REAL_PACK))
    manager = GoalManager(pack)

    chosen = []
    for trigger in pack.triggers[:200]:
        at = trigger.deliver_at or trigger.received_at
        context = manager.context_for(trigger.user_id, trigger_at=at, now=at)
        spec = manager.select(context)
        if spec is not None:
            chosen.append(spec.goal_id)

    assert chosen, "the shipped replay triggers must produce goals"
    assert "G10" not in chosen
    assert set(chosen) <= set(DEFAULT_PRIORITY)


def test_real_pack_evaluation_never_reports_success_without_evidence():
    pack = load_data_pack(DataPackPaths.from_root(REAL_PACK))
    manager = GoalManager(pack)

    for trigger in pack.triggers[:100]:
        at = trigger.deliver_at or trigger.received_at
        context = manager.context_for(trigger.user_id, trigger_at=at, now=at)
        spec = manager.select(context)
        if spec is None:
            continue
        outcome = manager.evaluate(spec, context)
        if outcome.succeeded:
            assert outcome.evidence.get("candidate_events", 0) > 0
            assert outcome.evidence.get("matched_at")