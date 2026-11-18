"""Tests for src.run_agent: the deterministic replay orchestrator.

Tests build small synthetic packs and run the real :func:`run_replay` against a
real :class:`src.model_client.ModelClient` and :class:`src.policy.PolicyEngine`,
verifying simulator-compatible serialization and replay integration.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.data_loader import (
    DataPack,
    DataPackPaths,
    Episode,
    Event,
    LoadReport,
    Profile,
    Title,
)
from src.model_client import ModelClient
from src.policy import PolicyEngine
from src.run_agent import run_replay

UTC = timezone.utc

PRICING = {
    "currency": "INR",
    "plans": [
        {"plan_id": "P_TRIAL", "price_inr": 1, "days": 3, "renews_to": "P_MONTH"},
        {"plan_id": "P_MONTH", "price_inr": 149, "days": 30},
        {"plan_id": "P_QUARTER", "price_inr": 399, "days": 90},
    ],
}

INTERVENTIONS = {
    "version": "2026-09-30",
    "home_page": {
        "ttl_minutes": 120,
        "rows": {
            "max_rows": 6,
            "min_titles_per_row": 3,
            "max_titles_per_row": 10,
            "row_types": ["next_episode", "because_you_watched", "popular_in_dialect"],
        },
    },
    "push": {
        "consent_field": "notifications_opt_in",
        "quiet_hours_ist": ["22:00", "08:00"],
        "copy": {
            "title_max_chars": 40,
            "body_max_chars": 120,
            "forbidden_phrases": ["last chance", "hurry", "only today"],
        },
        "deeplink_format": "ott://title/<title_id>",
    },
    "whatsapp": {
        "consent_field": "whatsapp_opt_in",
        "approved_templates": [],
    },
    "in_app_message": {"max_per_day": 1, "consent": "not required"},
    "no_action": {"description": "Doing nothing is always allowed."},
}


def make_event(
    event_id="e1",
    user_id="U-00001",
    name="app_open",
    delivered_at="2026-09-30T10:00:00.000Z",
    received_at=None,
    **extra,
):
    if received_at is None:
        received_at = delivered_at
    rec = {
        "event_id": event_id,
        "user_id": user_id,
        "event": name,
        "deliver_at": delivered_at,
        "occurred_at": delivered_at,
        "sent_at": delivered_at,
        "received_at": received_at,
        "session_id": "s1",
        "app_dialect": "d1",
        "app_language": "hin",
        "platform": "android",
        "app_version": "6.0.7",
        "properties": {},
    }
    rec.update(extra)
    rec["deliver_at"] = delivered_at
    return Event(**rec)


def make_profile(user_id="U-00001", signup_at=None, notifications_opt_in=True):
    if signup_at is None:
        signup_at = datetime(2026, 9, 29, 10, 0, 0, tzinfo=UTC)
    return Profile(
        user_id=user_id,
        updated_at=signup_at,
        signup_at=signup_at,
        subscription_status="never",
        notifications_opt_in=notifications_opt_in,
        whatsapp_opt_in=True,
    )


GOALS = {
    "goals": [
        {
            "goal_id": "G01",
            "name": "first_play_24h",
            "description": "A new user starts any episode within 24 hours.",
            "eligibility": {
                "signed_up_within_days": 7,
                "requires_no_prior_event": "play_started"
            },
            "success": {
                "event": "play_started",
                "condition": "any title",
                "window_hours": 24
            }
        },
        {
            "goal_id": "G03",
            "name": "second_day_return_2d",
            "description": "A new user comes back on a second day.",
            "eligibility": {
                "signed_up_within_days": 2
            },
            "success": {
                "event": "app_open",
                "condition": "on a later IST calendar day than the goal start",
                "window_hours": 48
            }
        }
    ]
}


def build_pack(tmp_path, titles=None, events=None, profiles=None, episodes=None,
               arms=None, triggers=None, live_events=None):
    """Build a synthetic pack with config files and a real paths.root."""
    root = tmp_path / "pack"
    (root / "data").mkdir(parents=True)
    (root / "replay").mkdir(parents=True)
    (root / "config").mkdir(parents=True)
    (root / "config" / "pricing.json").write_text(json.dumps(PRICING), encoding="utf-8")
    (root / "config" / "interventions.json").write_text(
        json.dumps(INTERVENTIONS), encoding="utf-8"
    )
    (root / "config" / "goals.json").write_text(json.dumps(GOALS), encoding="utf-8")
    paths = DataPackPaths.from_root(str(root))
    pack = DataPack(paths=paths, report=LoadReport())
    pack.titles = titles or {}
    pack.profiles = profiles or {}
    pack.episodes = episodes or {}
    pack.events = events or []
    pack.arms = arms or {}
    pack.triggers = triggers or []
    pack.live_events = live_events or []
    return pack, str(root)


def make_engine(pack):
    pricing = json.loads(
        (Path(pack.paths.root) / "config" / "pricing.json").read_text(encoding="utf-8")
    )
    interventions = json.loads(
        (Path(pack.paths.root) / "config" / "interventions.json").read_text(
            encoding="utf-8"
        )
    )
    return PolicyEngine(pack, pricing, interventions)


def push_output(goal_id="G01", title_id="T1", dialect="d1"):
    """A model output that builds an allowed push action."""
    return {
        "goal_id": goal_id,
        "eligible": True,
        "confidence": 0.9,
        "reason": "user watched similar content",
        "text": {"title": "New episode", "body": "Watch now"},
        "title_id": title_id,
        "dialect": dialect,
    }


def home_page_output(dialect="d1", hero_title_id="T1"):
    """A model output that builds an allowed home_page action."""
    return {
        "goal_id": "G01",
        "eligible": True,
        "confidence": 0.9,
        "reason": "home page refresh",
        "action_type": "home_page",
        "dialect": dialect,
        "rows": [{"row_type": "popular_in_dialect", "title_ids": ["T1", "T2", "T3"]}],
        "hero_title_id": hero_title_id,
    }


def read_jsonl(path):
    return [
        json.loads(line)
        for line in Path(path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# ---------------------------------------------------------------------------
# End-to-end trigger processing
# ---------------------------------------------------------------------------


def test_end_to_end_trigger_processing(tmp_path):
    titles = {"T1": Title(title_id="T1", content_slug="title1", dialect="d1")}
    profiles = {"U-00001": make_profile("U-00001")}
    arms = {"U-00001": "agent"}
    triggers = [
        make_event(
            event_id="trig-1",
            user_id="U-00001",
            delivered_at="2026-09-30T10:00:00.000Z",
        )
    ]
    pack, root = build_pack(
        tmp_path, titles=titles, profiles=profiles, arms=arms, triggers=triggers
    )

    model = ModelClient(make_engine(pack))
    model.register_output("trig-1", push_output(title_id="T1"))
    engine = make_engine(pack)

    actions_path, decisions_path, report = run_replay(
        pack, model, engine, output_dir=str(tmp_path / "out")
    )

    decisions = read_jsonl(decisions_path)
    actions = read_jsonl(actions_path)

    assert report.decisions_total == 1
    assert report.actions_published == 1
    assert report.holdout_decisions == 0
    assert len(decisions) == 1
    assert len(actions) == 1

    decision = decisions[0]
    for field in [
        "decision_id",
        "user_id",
        "trigger_event_id",
        "goal_id",
        "action",
        "reason",
        "published_at",
        "processing_ms",
    ]:
        assert field in decision
    assert decision["goal_id"] == "G01"
    assert decision["action"] == "push"

    # Simulator-compatible: action type under "type", never "atype".
    action = actions[0]
    assert "type" in action
    assert "atype" not in action
    assert action["type"] == "push"
    assert action["user_id"] == "U-00001"
    assert action["trigger_event_id"] == "trig-1"


# ---------------------------------------------------------------------------
# Delivery-time ordering
# ---------------------------------------------------------------------------


def test_deliver_at_ordering(tmp_path):
    profiles = {"U-00001": make_profile("U-00001")}
    arms = {"U-00001": "agent"}
    triggers = [
        make_event(event_id="late", user_id="U-00001", delivered_at="2026-09-30T10:05:00.000Z"),
        make_event(event_id="early", user_id="U-00001", delivered_at="2026-09-30T10:00:00.000Z"),
    ]
    pack, root = build_pack(
        tmp_path, profiles=profiles, arms=arms, triggers=triggers
    )

    model = ModelClient(make_engine(pack))
    model.register_output("early", push_output())
    model.register_output("late", push_output())
    engine = make_engine(pack)

    _, decisions_path, report = run_replay(
        pack, model, engine, output_dir=str(tmp_path / "out")
    )

    decisions = read_jsonl(decisions_path)
    assert report.decisions_total == 2
    assert decisions[0]["trigger_event_id"] == "early"
    assert decisions[1]["trigger_event_id"] == "late"


# ---------------------------------------------------------------------------
# Holdout users
# ---------------------------------------------------------------------------


def test_holdout_exclusion(tmp_path):
    profiles = {"U-00001": make_profile("U-00001")}
    arms = {"U-00001": "holdout"}
    triggers = [
        make_event(event_id="trig-1", user_id="U-00001")
    ]
    pack, root = build_pack(
        tmp_path, profiles=profiles, arms=arms, triggers=triggers
    )

    model = ModelClient(make_engine(pack))
    engine = make_engine(pack)

    _, decisions_path, report = run_replay(
        pack, model, engine, output_dir=str(tmp_path / "out")
    )

    decisions = read_jsonl(decisions_path)
    assert report.decisions_total == 1
    assert report.holdout_decisions == 1
    assert decisions[0]["reason"] == "HOLDOUT"
    assert decisions[0]["action"] is None
    # No action published for a holdout user.
    assert report.actions_published == 0


# ---------------------------------------------------------------------------
# Model faults
# ---------------------------------------------------------------------------


def test_model_fault_recording(tmp_path):
    profiles = {"U-00001": make_profile("U-00001")}
    arms = {"U-00001": "agent"}
    triggers = [
        make_event(event_id="fault-unsupported", user_id="U-00001"),
        make_event(event_id="fault-malformed", user_id="U-00001"),
        make_event(event_id="fault-timeout", user_id="U-00001"),
    ]
    pack, root = build_pack(
        tmp_path, profiles=profiles, arms=arms, triggers=triggers
    )

    # Write the faults file keyed by trigger_event_id.
    fault_path = Path(root) / "replay" / "model_faults.jsonl"
    fault_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "trigger_event_id": "fault-unsupported",
                        "user_id": "U-00001",
                        "fault": "unsupported_claim",
                        "model_output": {
                            "goal_id": "G04",
                            "eligible": True,
                            "confidence": 0.97,
                            "reason": "confirmed in billing",
                        },
                    }
                ),
                json.dumps(
                    {
                        "trigger_event_id": "fault-malformed",
                        "user_id": "U-00001",
                        "fault": "malformed_output",
                        "model_output": '{"goal_id": "G02", "eligible": tr',
                    }
                ),
                json.dumps(
                    {
                        "trigger_event_id": "fault-timeout",
                        "user_id": "U-00001",
                        "fault": "timeout",
                        "seconds": 45,
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )

    model = ModelClient(make_engine(pack))
    engine = make_engine(pack)

    _, decisions_path, report = run_replay(
        pack,
        model,
        engine,
        output_dir=str(tmp_path / "out"),
        fault_mapping_path=str(fault_path),
    )

    decisions = read_jsonl(decisions_path)
    assert report.decisions_total == 3
    assert report.fault_no_action_decisions == 3
    assert report.actions_published == 0
    for d in decisions:
        assert d["action"] is None
        assert d["rule"] in {
            "unsupported_claim",
            "malformed_output",
            "model_timeout",
        }


# ---------------------------------------------------------------------------
# Policy rejection -> no-action
# ---------------------------------------------------------------------------


def test_policy_rejection_no_action(tmp_path):
    titles = {"T1": Title(title_id="T1", content_slug="title1", dialect="d1")}
    profiles = {"U-00001": make_profile("U-00001", notifications_opt_in=False)}
    arms = {"U-00001": "agent"}
    triggers = [make_event(event_id="trig-1", user_id="U-00001")]
    pack, root = build_pack(
        tmp_path, titles=titles, profiles=profiles, arms=arms, triggers=triggers
    )

    model = ModelClient(make_engine(pack))
    model.register_output("trig-1", push_output(title_id="T1"))
    engine = make_engine(pack)

    _, decisions_path, report = run_replay(
        pack, model, engine, output_dir=str(tmp_path / "out")
    )

    decisions = read_jsonl(decisions_path)
    assert report.decisions_total == 1
    assert report.policy_no_action_decisions == 1
    assert report.actions_published == 0
    assert decisions[0]["action"] is None
    assert decisions[0]["reason"] == "POLICY"


# ---------------------------------------------------------------------------
# Simulator-compatible serialization
# ---------------------------------------------------------------------------


def test_home_page_serialization(tmp_path):
    titles = {
        "T1": Title(title_id="T1", content_slug="slug-1", dialect="d1", available_from=datetime(2026, 9, 1, tzinfo=UTC)),
        "T2": Title(title_id="T2", content_slug="slug-2", dialect="d1", available_from=datetime(2026, 9, 1, tzinfo=UTC)),
        "T3": Title(title_id="T3", content_slug="slug-3", dialect="d1", available_from=datetime(2026, 9, 1, tzinfo=UTC)),
    }
    profiles = {"U-00001": make_profile("U-00001")}
    arms = {"U-00001": "agent"}
    triggers = [make_event(event_id="trig-1", user_id="U-00001")]
    pack, root = build_pack(
        tmp_path, titles=titles, profiles=profiles, arms=arms, triggers=triggers
    )

    model = ModelClient(make_engine(pack))
    model.register_output("trig-1", home_page_output(dialect="d1", hero_title_id="T1"))
    engine = make_engine(pack)

    actions_path, _, _ = run_replay(
        pack, model, engine, output_dir=str(tmp_path / "out")
    )

    actions = read_jsonl(actions_path)
    assert len(actions) == 1
    action = actions[0]
    assert action["type"] == "home_page"
    assert "send_at" not in action
    assert action["dialect"] == "d1"
    assert action["hero_title_id"] == "T1"
    assert action["rows"] == [
        {"row_type": "popular_in_dialect", "title_ids": ["T1", "T2", "T3"]}
    ]


# ---------------------------------------------------------------------------
# No future-event leakage
# ---------------------------------------------------------------------------


def test_no_future_event_leakage(tmp_path):
    profiles = {"U-00001": make_profile("U-00001")}
    arms = {"U-00001": "agent"}
    # A live event received *after* the trigger must not influence eligibility.
    triggers = [
        make_event(
            event_id="trig-1",
            user_id="U-00001",
            delivered_at="2026-09-30T10:00:00.000Z",
            received_at="2026-09-30T10:00:00.000Z",
        )
    ]
    live_events = [
        make_event(
            event_id="live-late",
            user_id="U-00001",
            name="play_started",
            delivered_at="2026-09-30T11:00:00.000Z",
            received_at="2026-09-30T11:00:00.000Z",
        )
    ]
    pack, root = build_pack(
        tmp_path,
        profiles=profiles,
        arms=arms,
        triggers=triggers,
        live_events=live_events,
    )

    model = ModelClient(make_engine(pack))
    model.register_output("trig-1", push_output())
    engine = make_engine(pack)

    _, decisions_path, _ = run_replay(
        pack, model, engine, output_dir=str(tmp_path / "out")
    )

    decisions = read_jsonl(decisions_path)
    assert len(decisions) == 1
    # The late live event (received after the trigger) is not visible at
    # trigger time, so it cannot influence goal selection.
    assert decisions[0]["trigger_event_id"] == "trig-1"