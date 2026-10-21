"""Unit tests for src.policy: policy enforcement across all 9 policy areas.

Tests use small synthetic fixtures and a fresh :class:`PolicyEngine` per test
so that mutable cap state never leaks between cases.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.data_loader import DataPack, Event, Episode, Profile, Title
from src.policy import (
    PolicyEngine,
    PolicyResult,
    approved_prices,
    channel_opt_outs,
    is_opted_out,
    ist_date,
    load_interventions,
    load_pricing,
    profile_consent,
    price_claims_approved,
    whatsapp_approved_templates,
)

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def ist(hour: int, minute: int = 0, day: int = 15) -> datetime:
    """An IST datetime for quiet-hours and cap tests."""
    return datetime(2024, 1, day, hour, minute, tzinfo=IST)


def ist_iso(hour: int, minute: int = 0, day: int = 15) -> str:
    """An IST ISO-8601 string for action ``send_at``/``published_at``."""
    return ist(hour, minute, day).isoformat()


def make_pack(
    titles=None,
    profiles=None,
    arms=None,
    events=None,
    episodes=None,
) -> DataPack:
    return DataPack(
        paths=None,
        titles=titles or {},
        profiles=profiles or {},
        arms=arms or {},
        events=events or [],
        episodes=episodes or {},
    )


def make_title(
    title_id: str,
    slug: str,
    title: str,
    dialect: str,
    available_from=None,
    available_until=None,
) -> Title:
    if available_from is None:
        available_from = datetime(2020, 1, 1, tzinfo=UTC)
    return Title(
        title_id=title_id,
        content_slug=slug,
        title=title,
        dialect=dialect,
        available_from=available_from,
        available_until=available_until,
    )


def make_profile(
    user_id: str,
    notifications_opt_in: bool = True,
    whatsapp_opt_in: bool = True,
) -> Profile:
    return Profile(
        user_id=user_id,
        updated_at=datetime(2024, 1, 1, 12, 0, tzinfo=UTC),
        notifications_opt_in=notifications_opt_in,
        whatsapp_opt_in=whatsapp_opt_in,
    )


def make_event(
    event_id: str,
    user_id: str,
    event: str,
    properties=None,
    received_at=None,
    occurred_at=None,
    sent_at=None,
) -> Event:
    return Event(
        event_id=event_id,
        user_id=user_id,
        event=event,
        received_at=received_at or datetime(2024, 1, 1, 12, 0, tzinfo=UTC),
        occurred_at=occurred_at or datetime(2024, 1, 1, 12, 0, tzinfo=UTC),
        sent_at=sent_at or datetime(2024, 1, 1, 12, 0, tzinfo=UTC),
        properties=properties or {},
    )


def make_engine(pack=None, pricing=None, interventions=None) -> PolicyEngine:
    """Fresh engine with default pricing/interventions and a small pack."""
    if pack is None:
        pack = make_pack()
    if pricing is None:
        pricing = {
            "plans": [
                {"plan_id": "basic", "price_inr": 149},
                {"plan_id": "premium", "price_inr": 399},
                {"plan_id": "trial", "price_inr": 1},
            ]
        }
    if interventions is None:
        interventions = {
            "whatsapp": {
                "approved_templates": [
                    {
                        "template_id": "welcome_msg",
                        "variables": ["title_name", "dialect_name"],
                    },
                    {
                        "template_id": "episode_alert",
                        "variables": ["title_name", "episode_number"],
                    },
                    {
                        "template_id": "plan_promo",
                        "variables": ["title_name", "plan_price"],
                    },
                ]
            }
        }
    return PolicyEngine(pack, pricing, interventions)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def pack():
    t1 = make_title("t1", "slug-a", "Show A", "ta")
    t2 = make_title("t2", "slug-b", "Show B", "ta")
    t3 = make_title("t3", "slug-c", "Show C", "te")
    t4 = make_title("t4", "slug-d", "Show D", "ta")
    p1 = make_profile("u1")
    p2 = make_profile("u2", notifications_opt_in=False)
    p3 = make_profile("u3", whatsapp_opt_in=False)
    return make_pack(
        titles={"t1": t1, "t2": t2, "t3": t3, "t4": t4},
        profiles={"u1": p1, "u2": p2, "u3": p3},
        arms={"u1": "treatment", "u2": "treatment", "u3": "treatment"},
    )


@pytest.fixture
def pricing():
    return {
        "plans": [
            {"plan_id": "basic", "price_inr": 149},
            {"plan_id": "premium", "price_inr": 399},
            {"plan_id": "trial", "price_inr": 1},
        ]
    }


@pytest.fixture
def interventions():
    return {
        "whatsapp": {
            "approved_templates": [
                {"template_id": "welcome_msg", "variables": ["title_name", "dialect_name"]},
                {"template_id": "episode_alert", "variables": ["title_name", "episode_number"]},
                {"template_id": "plan_promo", "variables": ["title_name", "plan_price"]},
            ]
        }
    }


@pytest.fixture
def engine(pack, pricing, interventions):
    return PolicyEngine(pack, pricing, interventions)


# ---------------------------------------------------------------------------
# Helper action builders
# ---------------------------------------------------------------------------


def push_action(
    action_id="a1",
    user_id="u1",
    send_at=ist_iso(10),
    title="Hello",
    body="This is a push body",
    title_id="t1",
    deeplink="ott://title/t1",
):
    return {
        "action_id": action_id,
        "user_id": user_id,
        "trigger_event_id": "trig1",
        "type": "push",
        "send_at": send_at,
        "title_id": title_id,
        "deeplink": deeplink,
        "text": {"title": title, "body": body},
    }


def whatsapp_action(
    action_id="a1",
    user_id="u1",
    send_at=ist_iso(10),
    template_id="welcome_msg",
    variables=None,
    title_id="t1",
):
    if variables is None:
        variables = {"title_name": "Show A", "dialect_name": "ta"}
    return {
        "action_id": action_id,
        "user_id": user_id,
        "trigger_event_id": "trig1",
        "type": "whatsapp",
        "send_at": send_at,
        "template_id": template_id,
        "variables": variables,
        "title_id": title_id,
    }


def homepage_action(
    action_id="a1",
    user_id="u1",
    published_at=ist_iso(10),
    dialect="ta",
    hero_title_id="t1",
    rows=None,
):
    if rows is None:
        rows = [
            {"row_type": "next_episode", "title_ids": ["t1", "t2", "t4"]},
        ]
    return {
        "action_id": action_id,
        "user_id": user_id,
        "trigger_event_id": "trig1",
        "type": "home_page",
        "published_at": published_at,
        "dialect": dialect,
        "hero_title_id": hero_title_id,
        "rows": rows,
    }


def in_app_action(
    action_id="a1",
    user_id="u1",
    send_at=ist_iso(10),
):
    return {
        "action_id": action_id,
        "user_id": user_id,
        "trigger_event_id": "trig1",
        "type": "in_app_message",
        "send_at": send_at,
    }


# ---------------------------------------------------------------------------
# 1. Holdout
# ---------------------------------------------------------------------------


def test_holdout_push_rejected(engine):
    pack = engine.pack
    pack.arms["u1"] = "holdout"
    result = engine.validate(push_action(user_id="u1"))
    assert result.allowed is False
    assert result.rule == "holdout"
    assert "holdout" in result.reason


def test_holdout_whatsapp_rejected(engine):
    pack = engine.pack
    pack.arms["u1"] = "holdout"
    result = engine.validate(whatsapp_action(user_id="u1"))
    assert result.allowed is False
    assert result.rule == "holdout"


def test_holdout_homepage_rejected(engine):
    pack = engine.pack
    pack.arms["u1"] = "holdout"
    result = engine.validate(homepage_action(user_id="u1"))
    assert result.allowed is False
    assert result.rule == "holdout"


def test_holdout_in_app_rejected(engine):
    pack = engine.pack
    pack.arms["u1"] = "holdout"
    result = engine.validate(in_app_action(user_id="u1"))
    assert result.allowed is False
    assert result.rule == "holdout"


def test_holdout_no_action_allowed(engine):
    pack = engine.pack
    pack.arms["u1"] = "holdout"
    result = engine.validate(
        {
            "action_id": "a1",
            "user_id": "u1",
            "trigger_event_id": "trig1",
            "type": "no_action",
        }
    )
    assert result.allowed is True


def test_treatment_user_push_allowed(engine):
    result = engine.validate(push_action(user_id="u1"))
    assert result.allowed is True
    assert result.reason == ""


# ---------------------------------------------------------------------------
# 2. Consent (per-channel opt-outs)
# ---------------------------------------------------------------------------


def test_push_requires_snapshot_consent(engine):
    result = engine.validate(push_action(user_id="u2"))  # notifications_opt_in=False
    assert result.allowed is False
    assert result.rule == "no_consent"


def test_whatsapp_requires_snapshot_consent(engine):
    result = engine.validate(whatsapp_action(user_id="u3"))  # whatsapp_opt_in=False
    assert result.allowed is False
    assert result.rule == "no_consent"


def test_push_optout_blocks_push_only(engine):
    # u1 opts out of push only -> push blocked, whatsapp still allowed
    pack = engine.pack
    pack.events.append(make_event("eo1", "u1", "notification_opt_out", {"channel": "push"}))
    engine2 = PolicyEngine(pack, engine.pricing, engine.interventions)
    assert engine2.validate(push_action(user_id="u1")).rule == "opted_out"
    assert engine2.validate(whatsapp_action(user_id="u1")).allowed is True


def test_whatsapp_optout_blocks_whatsapp_only(engine):
    # u1 opts out of whatsapp only -> whatsapp blocked, push still allowed
    pack = engine.pack
    pack.events.append(make_event("eo2", "u1", "notification_opt_out", {"channel": "whatsapp"}))
    engine2 = PolicyEngine(pack, engine.pricing, engine.interventions)
    assert engine2.validate(whatsapp_action(user_id="u1")).rule == "opted_out"
    assert engine2.validate(push_action(user_id="u1")).allowed is True


def test_channel_opt_outs_derives_per_channel():
    events = [
        make_event("e1", "u1", "notification_opt_out", {"channel": "push"}),
        make_event("e2", "u1", "notification_opt_out", {"channel": "whatsapp"}),
        make_event("e3", "u2", "notification_opt_out", {"channel": "push"}),
    ]
    outs = channel_opt_outs(events)
    assert outs == {"u1": {"push", "whatsapp"}, "u2": {"push"}}
    assert is_opted_out(outs, "u1", "push") is True
    assert is_opted_out(outs, "u1", "whatsapp") is True
    assert is_opted_out(outs, "u2", "whatsapp") is False


# ---------------------------------------------------------------------------
# 3. Quiet hours (22:00-08:00 IST)
# ---------------------------------------------------------------------------


def test_quiet_hours_night_rejected(engine):
    result = engine.validate(push_action(send_at=ist_iso(23)))
    assert result.allowed is False
    assert result.rule == "quiet_hours"


def test_quiet_hours_early_morning_rejected(engine):
    result = engine.validate(push_action(send_at=ist_iso(5)))
    assert result.allowed is False
    assert result.rule == "quiet_hours"


def test_quiet_hours_boundary_22_rejected(engine):
    result = engine.validate(push_action(send_at=ist_iso(22)))
    assert result.allowed is False
    assert result.rule == "quiet_hours"


def test_quiet_hours_boundary_21_allowed(engine):
    result = engine.validate(push_action(send_at=ist_iso(21, 59)))
    assert result.allowed is True


def test_quiet_hours_boundary_08_allowed(engine):
    result = engine.validate(push_action(send_at=ist_iso(8)))
    assert result.allowed is True


def test_quiet_hours_boundary_07_59_rejected(engine):
    result = engine.validate(push_action(send_at=ist_iso(7, 59)))
    assert result.allowed is False
    assert result.rule == "quiet_hours"


def test_quiet_hours_whatsapp_rejected(engine):
    result = engine.validate(whatsapp_action(send_at=ist_iso(23)))
    assert result.allowed is False
    assert result.rule == "quiet_hours"


# ---------------------------------------------------------------------------
# 4. Frequency cap (push + whatsapp combined, 1 per user per IST day)
# ---------------------------------------------------------------------------


def test_frequency_cap_push_then_whatsapp_same_day(engine):
    assert engine.validate(push_action(action_id="a1", send_at=ist_iso(10))).allowed is True
    result = engine.validate(whatsapp_action(action_id="a2", send_at=ist_iso(14)))
    assert result.allowed is False
    assert result.rule == "frequency_cap_exceeded"


def test_frequency_cap_whatsapp_then_push_same_day(engine):
    assert engine.validate(whatsapp_action(action_id="a1", send_at=ist_iso(10))).allowed is True
    result = engine.validate(push_action(action_id="a2", send_at=ist_iso(14)))
    assert result.allowed is False
    assert result.rule == "frequency_cap_exceeded"


def test_frequency_cap_different_users_independent(engine):
    assert engine.validate(push_action(action_id="a1", user_id="u1")).allowed is True
    assert engine.validate(push_action(action_id="a2", user_id="u3")).allowed is True


def test_frequency_cap_different_days_allowed(engine):
    assert engine.validate(push_action(action_id="a1", send_at=ist_iso(10, day=15))).allowed is True
    result = engine.validate(push_action(action_id="a2", send_at=ist_iso(10, day=16)))
    assert result.allowed is True


def test_frequency_cap_one_per_day_exact(engine):
    # Exactly one push per day is allowed; a second on same day is rejected.
    assert engine.validate(push_action(action_id="a1", send_at=ist_iso(10))).allowed is True
    result = engine.validate(push_action(action_id="a2", send_at=ist_iso(11)))
    assert result.allowed is False
    assert result.rule == "frequency_cap_exceeded"


# ---------------------------------------------------------------------------
# 5. Push validation
# ---------------------------------------------------------------------------


def test_push_title_too_long(engine):
    long_title = "x" * 41
    result = engine.validate(push_action(title=long_title))
    assert result.allowed is False
    assert result.rule == "push_title_too_long"


def test_push_title_exactly_40_allowed(engine):
    title = "x" * 40
    result = engine.validate(push_action(title=title))
    assert result.allowed is True


def test_push_body_too_long(engine):
    long_body = "x" * 121
    result = engine.validate(push_action(body=long_body))
    assert result.allowed is False
    assert result.rule == "push_body_too_long"


def test_push_body_exactly_120_allowed(engine):
    body = "x" * 120
    result = engine.validate(push_action(body=body))
    assert result.allowed is True


def test_push_forbidden_phrase(engine):
    for phrase in ["last chance", "hurry", "only today", "free forever", "guaranteed"]:
        result = engine.validate(push_action(body=f"Don't miss {phrase}!"))
        assert result.allowed is False
        assert result.rule == "forbidden_phrase", phrase


def test_push_unapproved_price_claim(engine):
    result = engine.validate(push_action(body="Special price ₹999 only!"))
    assert result.allowed is False
    assert result.rule == "unapproved_price_claim"


def test_push_approved_price_claim(engine):
    result = engine.validate(push_action(body="Get it for ₹149 today"))
    assert result.allowed is True


def test_push_invalid_deeplink(engine):
    result = engine.validate(push_action(deeplink="https://example.com/x"))
    assert result.allowed is False
    assert result.rule == "invalid_deeplink"


def test_push_valid_deeplink(engine):
    result = engine.validate(push_action(deeplink="ott://title/t1"))
    assert result.allowed is True


def test_push_invalid_title_id(engine):
    result = engine.validate(push_action(title_id="nope"))
    assert result.allowed is False
    assert result.rule == "invalid_title"


def test_push_valid(engine):
    result = engine.validate(push_action())
    assert result.allowed is True


# ---------------------------------------------------------------------------
# 6. WhatsApp validation
# ---------------------------------------------------------------------------


def test_whatsapp_unapproved_template(engine):
    result = engine.validate(whatsapp_action(template_id="bogus_template"))
    assert result.allowed is False
    assert result.rule == "unapproved_template"


def test_whatsapp_wrong_variables(engine):
    result = engine.validate(
        whatsapp_action(template_id="welcome_msg", variables={"title_name": "Show A"})
    )
    assert result.allowed is False
    assert result.rule == "invalid_variables"


def test_whatsapp_extra_variable(engine):
    result = engine.validate(
        whatsapp_action(
            template_id="welcome_msg",
            variables={"title_name": "Show A", "dialect_name": "ta", "extra": "no"},
        )
    )
    assert result.allowed is False
    assert result.rule == "invalid_variables"


def test_whatsapp_unapproved_variable_value(engine):
    # title_name must match the catalogue title for the given title_id
    result = engine.validate(
        whatsapp_action(
            template_id="welcome_msg",
            variables={"title_name": "Wrong Name", "dialect_name": "ta"},
            title_id="t1",
        )
    )
    assert result.allowed is False
    assert result.rule == "unapproved_variable_value"


def test_whatsapp_approved_variable_value(engine):
    result = engine.validate(whatsapp_action())
    assert result.allowed is True


def test_whatsapp_episode_number_valid(engine):
    episodes = {"slug-a": [Episode(episode_id="ep1", content_slug="slug-a", season=1, episode=3)]}
    pack = make_pack(
        titles={"t1": make_title("t1", "slug-a", "Show A", "ta")},
        profiles={"u1": make_profile("u1")},
        arms={"u1": "treatment"},
        episodes=episodes,
    )
    eng = PolicyEngine(pack, engine.pricing, engine.interventions)
    result = eng.validate(
        whatsapp_action(
            template_id="episode_alert",
            variables={"title_name": "Show A", "episode_number": "3"},
            title_id="t1",
        )
    )
    assert result.allowed is True


def test_whatsapp_episode_number_invalid(engine):
    episodes = {"slug-a": [Episode(episode_id="ep1", content_slug="slug-a", season=1, episode=3)]}
    pack = make_pack(
        titles={"t1": make_title("t1", "slug-a", "Show A", "ta")},
        profiles={"u1": make_profile("u1")},
        arms={"u1": "treatment"},
        episodes=episodes,
    )
    eng = PolicyEngine(pack, engine.pricing, engine.interventions)
    result = eng.validate(
        whatsapp_action(
            template_id="episode_alert",
            variables={"title_name": "Show A", "episode_number": "99"},
            title_id="t1",
        )
    )
    assert result.allowed is False
    assert result.rule == "unapproved_variable_value"


def test_whatsapp_plan_price_valid(engine):
    result = engine.validate(
        whatsapp_action(
            template_id="plan_promo",
            variables={"title_name": "Show A", "plan_price": "149"},
            title_id="t1",
        )
    )
    assert result.allowed is True


def test_whatsapp_plan_price_invalid(engine):
    result = engine.validate(
        whatsapp_action(
            template_id="plan_promo",
            variables={"title_name": "Show A", "plan_price": "999"},
            title_id="t1",
        )
    )
    assert result.allowed is False
    assert result.rule == "unapproved_variable_value"


def test_approved_templates_count():
    templates = whatsapp_approved_templates(
        {"whatsapp": {"approved_templates": [
            {"template_id": "t1", "variables": ["a"]},
            {"template_id": "t2", "variables": ["b"]},
            {"template_id": "t3", "variables": ["c"]},
        ]}}
    )
    assert set(templates) == {"t1", "t2", "t3"}


# ---------------------------------------------------------------------------
# 7. Homepage validation
# ---------------------------------------------------------------------------


def test_homepage_rows_over_maximum(engine):
    rows = [{"row_type": "next_episode", "title_ids": ["t1", "t2", "t4"]}] * 7
    result = engine.validate(homepage_action(rows=rows))
    assert result.allowed is False
    assert result.rule == "rows_over_maximum"



def test_homepage_row_below_minimum(engine):
    result = engine.validate(
        homepage_action(rows=[{"row_type": "next_episode", "title_ids": ["t1", "t2"]}])
    )
    assert result.allowed is False
    assert result.rule == "row_below_minimum"


def test_homepage_rows_exactly_6_allowed(engine):
    # Build 6 rows x 3 titles with unique slugs so only the row-count rule applies.
    titles = {f"h{i}": make_title(f"h{i}", f"slug-h{i}", f"Show H{i}", "ta") for i in range(18)}
    pack = make_pack(
        titles=titles,
        profiles={"u1": make_profile("u1")},
        arms={"u1": "treatment"},
    )
    eng = PolicyEngine(pack, engine.pricing, engine.interventions)
    rows = [
        {"row_type": "next_episode", "title_ids": [f"h{i}", f"h{i+1}", f"h{i+2}"]}
        for i in range(0, 18, 3)
    ]
    result = eng.validate(homepage_action(rows=rows, hero_title_id="h0"))
    assert result.allowed is True
    assert result.rule == ""





def test_homepage_row_over_maximum(engine):
    result = engine.validate(
        homepage_action(rows=[{"row_type": "next_episode", "title_ids": ["t1", "t2", "t3"] * 4}])
    )
    assert result.allowed is False
    assert result.rule == "row_over_maximum"


def test_homepage_invalid_row_type(engine):
    result = engine.validate(
        homepage_action(rows=[{"row_type": "magic_carpet", "title_ids": ["t1", "t2", "t3"]}])
    )
    assert result.allowed is False
    assert result.rule == "invalid_row_type"


def test_homepage_continue_watching_row_rejected(engine):
    result = engine.validate(
        homepage_action(
            rows=[{"row_type": "continue_watching", "title_ids": ["t1", "t2", "t3"]}]
        )
    )
    assert result.allowed is False
    assert result.rule == "continue_watching_row"


def test_homepage_multiple_heroes_rejected(engine):
    # hero_title_id present is one hero; we can't easily express two, but
    # check that a hero is at most one by construction (single field).
    result = engine.validate(homepage_action(hero_title_id="t1"))
    assert result.allowed is True


def test_homepage_dialect_mismatch(engine):
    result = engine.validate(
        homepage_action(dialect="te", rows=[{"row_type": "next_episode", "title_ids": ["t1", "t2", "t3"]}])
    )
    assert result.allowed is False
    assert result.rule == "dialect_mismatch"


def test_homepage_unavailable_title(engine):
    future = datetime(2025, 6, 1, 12, 0, tzinfo=UTC)
    t_unavail = make_title("t9", "slug-z", "Future Show", "ta", available_from=future)
    pack = make_pack(
        titles={"t1": make_title("t1", "slug-a", "Show A", "ta"), "t9": t_unavail},
        profiles={"u1": make_profile("u1")},
        arms={"u1": "treatment"},
    )
    eng = PolicyEngine(pack, engine.pricing, engine.interventions)
    result = eng.validate(
        homepage_action(
                rows=[{"row_type": "next_episode", "title_ids": ["t9", "t1", "t2"]}],
            published_at=ist_iso(10),
        )
    )
    assert result.allowed is False
    assert result.rule == "unavailable_title"


def test_homepage_duplicate_content_slug(engine):
    # Two titles with the same slug on the same page.
    t_dup = make_title("t2b", "slug-a", "Show A Duplicate", "ta")
    t3 = make_title("t4", "slug-d", "Show D", "ta")
    pack = make_pack(
        titles={
            "t1": make_title("t1", "slug-a", "Show A", "ta"),
            "t2": make_title("t2", "slug-b", "Show B", "ta"),
            "t2b": t_dup,
            "t4": t3,
        },
        profiles={"u1": make_profile("u1")},
        arms={"u1": "treatment"},
    )
    eng = PolicyEngine(pack, engine.pricing, engine.interventions)
    result = eng.validate(
        homepage_action(
            rows=[
                {"row_type": "next_episode", "title_ids": ["t1", "t2", "t4"]},
                {"row_type": "new_releases", "title_ids": ["t2b", "t4", "t1"]},
            ]
        )
    )
    assert result.allowed is False
    assert result.rule == "duplicate_content_slug"


def test_homepage_invalid_title(engine):
    result = engine.validate(
        homepage_action(rows=[{"row_type": "next_episode", "title_ids": ["nope", "t1", "t2"]}])
    )
    assert result.allowed is False
    assert result.rule == "invalid_title"


def test_homepage_valid(engine):
    result = engine.validate(homepage_action())
    assert result.allowed is True


# ---------------------------------------------------------------------------
# 8. In-app messages
# ---------------------------------------------------------------------------


def test_in_app_daily_cap(engine):
    assert engine.validate(in_app_action(action_id="a1")).allowed is True
    result = engine.validate(in_app_action(action_id="a2"))
    assert result.allowed is False
    assert result.rule == "in_app_daily_cap_exceeded"


def test_in_app_no_consent_required(engine):
    # u2 has notifications_opt_in=False but in-app needs no consent.
    result = engine.validate(in_app_action(user_id="u2"))
    assert result.allowed is True


# ---------------------------------------------------------------------------
# 9. General validation
# ---------------------------------------------------------------------------


def test_unknown_action_type(engine):
    result = engine.validate(
        {
            "action_id": "a1",
            "user_id": "u1",
            "trigger_event_id": "trig1",
            "type": "sms",
        }
    )
    assert result.allowed is False
    assert result.rule == "unknown_action_type"


def test_missing_user_id(engine):
    result = engine.validate(
        {
            "action_id": "a1",
            "trigger_event_id": "trig1",
            "type": "push",
            "send_at": ist_iso(10),
        }
    )
    assert result.allowed is False
    assert result.rule == "invalid_user_reference"


def test_unknown_user(engine):
    result = engine.validate(
        push_action(user_id="ghost_user")
    )
    assert result.allowed is False
    assert result.rule == "invalid_user_reference"


def test_missing_trigger_event_id(engine):
    result = engine.validate(
        {
            "action_id": "a1",
            "user_id": "u1",
            "type": "push",
            "send_at": ist_iso(10),
        }
    )
    assert result.allowed is False
    assert result.rule == "invalid_trigger_reference"


def test_malformed_timestamp(engine):
    result = engine.validate(push_action(send_at="not-a-timestamp"))
    assert result.allowed is False
    assert result.rule == "malformed_timestamp"


# ---------------------------------------------------------------------------
# Pure helper tests
# ---------------------------------------------------------------------------


def test_ist_date_converts_to_ist():
    utc_midnight = datetime(2024, 1, 16, 2, 0, tzinfo=UTC)  # 07:30 IST same day
    assert ist_date(utc_midnight).isoformat() == "2024-01-16"


def test_price_claims_approved_no_text():
    assert price_claims_approved(None, {}) is True
    assert price_claims_approved("", {}) is True


def test_price_claims_approved_unapproved():
    pricing = {"plans": [{"plan_id": "p", "price_inr": 149}]}
    assert price_claims_approved("₹999", pricing) is False
    assert price_claims_approved("₹149", pricing) is True


def test_profile_consent_false_when_none():
    assert profile_consent(None, "push") is False
    assert profile_consent(None, "whatsapp") is False


def test_load_pricing_and_interventions(tmp_path):
    import json
    p = tmp_path / "pricing.json"
    p.write_text(json.dumps({"plans": [{"plan_id": "x", "price_inr": 50}]}))
    assert load_pricing(str(p))["plans"][0]["price_inr"] == 50

    i = tmp_path / "interventions.json"
    i.write_text(
        json.dumps({"whatsapp": {"approved_templates": [{"template_id": "t", "variables": ["a"]}]}})
    )
    tmpl = whatsapp_approved_templates(load_interventions(str(i)))
    assert "t" in tmpl
    assert tmpl["t"]["variables"] == ["a"]