"""Tests for :mod:`src.model_client`: model output validation + PolicyEngine delegation + safe fallback."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from src.model_client import (
    ModelClient,
    ModelDecision,
    UNSUPPORTED_CLAIM_PATTERNS,
)

UTC = timezone.utc


class FakeResult:
    """Minimal stand-in for PolicyResult used by ModelClient tests."""

    def __init__(self, *, rejected, reason, action_type, rule, action_id="a1", user_id="u1"):
        self.rejected = rejected
        self.reason = reason
        self.action_type = action_type
        self.rule = rule
        self.action_id = action_id
        self.user_id = user_id


class FakeProfile:
    def __init__(self, preferred_dialect=None):
        self.preferred_dialect = preferred_dialect


class FakeTitle:
    def __init__(self, title_id, dialect=None):
        self.title_id = title_id
        self.dialect = dialect


class FakeEngine:
    """Minimal stand-in for PolicyEngine used by ModelClient tests."""

    def __init__(self, *, reject=False, reason="", rule="", action_type="push",
                 profile=None, titles=None, validated_action=None):
        self.reject = reject
        self.reason = reason
        self.rule = rule
        self.action_type = action_type
        self._profile = profile
        self.pack = MagicMock()
        self.pack.titles = titles or {}
        self.validated_action = validated_action
        self.validate_called_with = None

    def profile_for(self, user_id):
        return self._profile

    def validate(self, action):
        self.validate_called_with = action
        # When the engine rejects a proposal, the resulting action_type
        # is the no-op fallback (no_consent/holdout/quiet_hours/invalid_user),
        # not the original proposed type.
        if self.reject:
            action_type = "no_action"
        else:
            action_type = action.get("type", self.action_type)
        return FakeResult(
            rejected=self.reject,
            reason=self.reason,
            action_type=action_type,
            rule=self.rule,
            action_id=action.get("action_id", ""),
            user_id=action.get("user_id", ""),
        )


@pytest.fixture
def engine():
    return FakeEngine()


@pytest.fixture
def client(engine):
    return ModelClient(engine)


@pytest.fixture
def trigger_at():
    return datetime(2024, 1, 15, 10, 30, tzinfo=UTC)


# ---------------------------------------------------------------------------
# UNSUPPORTED_CLAIM_PATTERNS
# ---------------------------------------------------------------------------


def test_unsupported_claim_patterns_non_empty():
    assert len(UNSUPPORTED_CLAIM_PATTERNS) > 0


# ---------------------------------------------------------------------------
# register_output + complete: success path (push)
# ---------------------------------------------------------------------------


def test_complete_push_success(client, engine, trigger_at):
    client.register_output("trig-1", {
        "goal_id": "G01",
        "eligible": True,
        "confidence": 0.9,
        "reason": "user watched similar content",
        "text": {"title": "New episode", "body": "Watch now"},
    })
    decision = client.complete("trig-1", "u1", trigger_at, action_type="push")

    assert decision.allowed is True
    assert decision.reason == ""
    assert decision.action_type == "push"
    assert decision.rule == ""
    assert decision.proposed_action is not None
    assert decision.proposed_action["action_id"] == "A-trig-1"
    assert decision.proposed_action["user_id"] == "u1"
    assert decision.proposed_action["type"] == "push"
    assert decision.proposed_action["trigger_event_id"] == "trig-1"
    assert decision.proposed_action["send_at"] == trigger_at.isoformat()
    assert decision.proposed_action["text"]["title"] == "New episode"
    assert decision.proposed_action["text"]["body"] == "Watch now"


def test_complete_push_with_explicit_action_id(client, engine, trigger_at):
    client.register_output("trig-1", {
        "goal_id": "G01",
        "eligible": True,
        "reason": "user watched similar content",
        "text": {"title": "New episode", "body": "Watch now"},
    })
    decision = client.complete("trig-1", "u1", trigger_at, action_id="custom-id")

    assert decision.allowed is True
    assert decision.proposed_action["action_id"] == "custom-id"


# ---------------------------------------------------------------------------
# register_output + complete: success path (whatsapp)
# ---------------------------------------------------------------------------


def test_complete_whatsapp_success(client, engine, trigger_at):
    client.register_output("trig-2", {
        "goal_id": "G02",
        "eligible": True,
        "reason": "user opted into whatsapp",
        "template_id": "tmpl_1",
        "variables": {"name": "Alice"},
    })
    decision = client.complete("trig-2", "u1", trigger_at, action_type="whatsapp")

    assert decision.allowed is True
    assert decision.action_type == "whatsapp"
    assert decision.proposed_action["template_id"] == "tmpl_1"
    assert decision.proposed_action["variables"] == {"name": "Alice"}


# ---------------------------------------------------------------------------
# register_output + complete: success path (home_page)
# ---------------------------------------------------------------------------


def test_complete_home_page_success(client, engine, trigger_at):
    client.register_output("trig-3", {
        "goal_id": "G03",
        "eligible": True,
        "reason": "home page refresh",
        "dialect": "ta",
        "rows": [{"type": "popular_in_dialect", "titles": ["t1", "t2"]}],
        "hero_title_id": "t1",
    })
    decision = client.complete("trig-3", "u1", trigger_at, action_type="home_page")

    assert decision.allowed is True
    assert decision.action_type == "home_page"
    assert decision.proposed_action["dialect"] == "ta"
    assert decision.proposed_action["rows"] == [{"type": "popular_in_dialect", "titles": ["t1", "t2"]}]
    assert decision.proposed_action["hero_title_id"] == "t1"


# ---------------------------------------------------------------------------
# Fault: timeout
# ---------------------------------------------------------------------------


def test_complete_timeout_fault(client, engine, trigger_at):
    client.register_output("trig-1", {"goal_id": "G01"}, fault="timeout")
    decision = client.complete("trig-1", "u1", trigger_at)

    assert decision.allowed is False
    assert decision.action_type == "no_action"
    assert decision.rule == "model_timeout"
    assert "timed out" in decision.reason
    assert decision.model_output == {"goal_id": "G01"}


# ---------------------------------------------------------------------------
# Fault: missing model output
# ---------------------------------------------------------------------------


def test_complete_missing_model_output(client, engine, trigger_at):
    decision = client.complete("trig-missing", "u1", trigger_at)

    assert decision.allowed is False
    assert decision.action_type == "no_action"
    assert decision.rule == "missing_model_output"
    assert "no model output registered" in decision.reason


# ---------------------------------------------------------------------------
# Fault: malformed output (invalid JSON string)
# ---------------------------------------------------------------------------


def test_complete_malformed_json_string(client, engine, trigger_at):
    client.register_output("trig-1", "not valid json{{")
    decision = client.complete("trig-1", "u1", trigger_at)

    assert decision.allowed is False
    assert decision.action_type == "no_action"
    assert decision.rule == "malformed_output"
    assert "malformed model output" in decision.reason
    assert decision.model_output == "not valid json{{"


def test_complete_malformed_empty_string(client, engine, trigger_at):
    client.register_output("trig-1", "")
    decision = client.complete("trig-1", "u1", trigger_at)

    assert decision.allowed is False
    assert decision.rule == "malformed_output"


def test_complete_malformed_unsupported_type(client, engine, trigger_at):
    client.register_output("trig-1", 42)
    decision = client.complete("trig-1", "u1", trigger_at)

    assert decision.allowed is False
    assert decision.rule == "malformed_output"


# ---------------------------------------------------------------------------
# Fault: malformed output (valid JSON but bad structure)
# ---------------------------------------------------------------------------


def test_complete_malformed_missing_goal_id(client, engine, trigger_at):
    client.register_output("trig-1", {
        "eligible": True,
        "reason": "some reason",
    })
    decision = client.complete("trig-1", "u1", trigger_at)

    assert decision.allowed is False
    assert decision.rule == "malformed_output"
    assert "missing goal_id" in decision.reason


def test_complete_malformed_eligible_not_bool(client, engine, trigger_at):
    client.register_output("trig-1", {
        "goal_id": "G01",
        "eligible": "yes",
        "reason": "some reason",
    })
    decision = client.complete("trig-1", "u1", trigger_at)

    assert decision.allowed is False
    assert decision.rule == "malformed_output"
    assert "eligible must be a boolean" in decision.reason


def test_complete_malformed_missing_reason(client, engine, trigger_at):
    client.register_output("trig-1", {
        "goal_id": "G01",
        "eligible": True,
    })
    decision = client.complete("trig-1", "u1", trigger_at)

    assert decision.allowed is False
    assert decision.rule == "malformed_output"
    assert "missing reason" in decision.reason


# ---------------------------------------------------------------------------
# Fault: unsupported claim
# ---------------------------------------------------------------------------


def test_complete_unsupported_claim_billing(client, engine, trigger_at):
    client.register_output("trig-1", {
        "goal_id": "G01",
        "eligible": True,
        "reason": "confirmed in billing system",
        "text": {"title": "T", "body": "B"},
    })
    decision = client.complete("trig-1", "u1", trigger_at)

    assert decision.allowed is False
    assert decision.action_type == "no_action"
    assert decision.rule == "unsupported_claim"
    assert "unsupported claim" in decision.reason


def test_complete_unsupported_claim_external(client, engine, trigger_at):
    client.register_output("trig-1", {
        "goal_id": "G01",
        "eligible": True,
        "reason": "verified by external partner",
        "text": {"title": "T", "body": "B"},
    })
    decision = client.complete("trig-1", "u1", trigger_at)

    assert decision.allowed is False
    assert decision.rule == "unsupported_claim"


# ---------------------------------------------------------------------------
# Fault: ineligible goal
# ---------------------------------------------------------------------------


def test_complete_ineligible_goal(client, engine, trigger_at):
    client.register_output("trig-1", {
        "goal_id": "G01",
        "eligible": False,
        "reason": "user does not meet criteria",
        "text": {"title": "T", "body": "B"},
    })
    decision = client.complete("trig-1", "u1", trigger_at)

    assert decision.allowed is False
    assert decision.action_type == "no_action"
    assert decision.rule == "ineligible_goal"
    assert "not eligible" in decision.reason


# ---------------------------------------------------------------------------
# Policy passthrough: no_consent
# ---------------------------------------------------------------------------


def test_complete_policy_no_consent_push(client, engine, trigger_at):
    engine.reject = True
    engine.reason = "user has not consented to push notifications"
    engine.rule = "no_consent"
    engine.action_type = "push"

    client.register_output("trig-1", {
        "goal_id": "G01",
        "eligible": True,
        "reason": "user watched similar content",
        "text": {"title": "New episode", "body": "Watch now"},
    }, fault=None)
    decision = client.complete("trig-1", "u1", trigger_at, action_type="push")

    assert decision.allowed is False
    assert decision.action_type == "no_action"
    assert decision.rule == "no_consent"
    assert decision.reason == "user has not consented to push notifications"
    assert decision.proposed_action is not None


# ---------------------------------------------------------------------------
# Policy passthrough: holdout
# ---------------------------------------------------------------------------


def test_complete_policy_holdout(client, engine, trigger_at):
    engine.reject = True
    engine.reason = "user is in holdout group"
    engine.rule = "holdout"
    engine.action_type = "no_action"

    client.register_output("trig-1", {
        "goal_id": "G01",
        "eligible": True,
        "reason": "user watched similar content",
        "text": {"title": "New episode", "body": "Watch now"},
    })
    decision = client.complete("trig-1", "u1", trigger_at, action_type="push")

    assert decision.allowed is False
    assert decision.rule == "holdout"
    assert "holdout" in decision.reason


# ---------------------------------------------------------------------------
# Policy passthrough: quiet_hours
# ---------------------------------------------------------------------------


def test_complete_policy_quiet_hours(client, engine, trigger_at):
    engine.reject = True
    engine.reason = "send outside quiet hours"
    engine.rule = "quiet_hours"
    engine.action_type = "no_action"

    client.register_output("trig-1", {
        "goal_id": "G01",
        "eligible": True,
        "reason": "user watched similar content",
        "text": {"title": "New episode", "body": "Watch now"},
    })
    decision = client.complete("trig-1", "u1", trigger_at, action_type="push")

    assert decision.allowed is False
    assert decision.rule == "quiet_hours"
    assert "quiet hours" in decision.reason


# ---------------------------------------------------------------------------
# Policy passthrough: invalid_user
# ---------------------------------------------------------------------------


def test_complete_policy_invalid_user(client, engine, trigger_at):
    engine.reject = True
    engine.reason = "user not found in pack"
    engine.rule = "invalid_user_reference"
    engine.action_type = "no_action"

    client.register_output("trig-1", {
        "goal_id": "G01",
        "eligible": True,
        "reason": "user watched similar content",
        "text": {"title": "New episode", "body": "Watch now"},
    })
    decision = client.complete("trig-1", "u1", trigger_at, action_type="push")

    assert decision.allowed is False
    assert decision.rule == "invalid_user_reference"
    assert "not found" in decision.reason


# ---------------------------------------------------------------------------
# Safe fallback: all faults return no_action
# ---------------------------------------------------------------------------


def test_all_faults_return_no_action(client, trigger_at):
    # timeout
    client.register_output("trig-timeout", {"goal_id": "G01"}, fault="timeout")
    d = client.complete("trig-timeout", "u1", trigger_at)
    assert d.allowed is False and d.action_type == "no_action" and d.rule == "model_timeout"

    # missing
    d = client.complete("trig-missing", "u1", trigger_at)
    assert d.allowed is False and d.action_type == "no_action" and d.rule == "missing_model_output"

    # malformed
    client.register_output("trig-malformed", "bad json{{")
    d = client.complete("trig-malformed", "u1", trigger_at)
    assert d.allowed is False and d.action_type == "no_action" and d.rule == "malformed_output"

    # unsupported_claim
    client.register_output("trig-unsupported", {"goal_id": "G01", "eligible": True, "reason": "confirmed in billing"})
    d = client.complete("trig-unsupported", "u1", trigger_at)
    assert d.allowed is False and d.action_type == "no_action" and d.rule == "unsupported_claim"

    # ineligible_goal
    client.register_output("trig-ineligible", {"goal_id": "G01", "eligible": False, "reason": "nope"})
    d = client.complete("trig-ineligible", "u1", trigger_at)
    assert d.allowed is False and d.action_type == "no_action" and d.rule == "ineligible_goal"


# ---------------------------------------------------------------------------
# ModelClient with goal_specs (unknown goal)
# ---------------------------------------------------------------------------


def test_complete_unknown_goal_with_specs(trigger_at):
    engine = FakeEngine()
    client = ModelClient(engine, goal_specs={"G01": {}, "G02": {}})
    client.register_output("trig-1", {
        "goal_id": "G99",
        "eligible": True,
        "reason": "some reason",
        "text": {"title": "T", "body": "B"},
    })
    decision = client.complete("trig-1", "u1", trigger_at)

    assert decision.allowed is False
    assert decision.rule == "unsupported_claim"
    assert "not a known goal" in decision.reason


# ---------------------------------------------------------------------------
# ModelClient without goal_specs (any goal_id accepted)
# ---------------------------------------------------------------------------


def test_complete_without_goal_specs_accepts_any_goal(client, engine, trigger_at):
    client.register_output("trig-1", {
        "goal_id": "G99",
        "eligible": True,
        "reason": "some reason",
        "text": {"title": "T", "body": "B"},
    })
    decision = client.complete("trig-1", "u1", trigger_at)

    assert decision.allowed is True


# ---------------------------------------------------------------------------
# PolicyResult passthrough: allowed action preserves action_type and rule
# ---------------------------------------------------------------------------


def test_complete_allowed_preserves_policy_action_type(client, engine, trigger_at):
    engine.reject = False
    engine.action_type = "whatsapp"
    engine.rule = "template_approved"

    client.register_output("trig-1", {
        "goal_id": "G01",
        "eligible": True,
        "reason": "user watched similar content",
        "template_id": "tmpl_1",
    })
    decision = client.complete("trig-1", "u1", trigger_at, action_type="whatsapp")

    assert decision.allowed is True
    assert decision.action_type == "whatsapp"
    assert decision.rule == "template_approved"


# ---------------------------------------------------------------------------
# register_output stores output keyed by stripped trigger_event_id
# ---------------------------------------------------------------------------


def test_register_output_strips_trigger_id(client, engine, trigger_at):
    client.register_output("  trig-1  ", {
        "goal_id": "G01",
        "eligible": True,
        "reason": "some reason",
        "text": {"title": "T", "body": "B"},
    })
    decision = client.complete("trig-1", "u1", trigger_at)

    assert decision.allowed is True


# ---------------------------------------------------------------------------
# ModelDecision is frozen dataclass
# ---------------------------------------------------------------------------


def test_model_decision_is_frozen():
    d = ModelDecision(
        allowed=True, reason="", action_id="a1", user_id="u1", action_type="push"
    )
    with pytest.raises(AttributeError):
        d.allowed = False


# ---------------------------------------------------------------------------
# ModelClient delegates validate call to engine with proposed action
# ---------------------------------------------------------------------------


def test_complete_passes_proposed_action_to_engine(client, engine, trigger_at):
    client.register_output("trig-1", {
        "goal_id": "G01",
        "eligible": True,
        "reason": "user watched similar content",
        "text": {"title": "New episode", "body": "Watch now"},
    })
    client.complete("trig-1", "u1", trigger_at, action_type="push")

    assert engine.validate_called_with is not None
    assert engine.validate_called_with["action_id"] == "A-trig-1"
    assert engine.validate_called_with["user_id"] == "u1"
    assert engine.validate_called_with["type"] == "push"
    assert engine.validate_called_with["trigger_event_id"] == "trig-1"