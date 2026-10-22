"""Tests for src.run_agent: the deterministic replay orchestrator."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.data_loader import (DataPack, DataPackPaths, Event, LoadReport)
from src.goals import RuleCorrection, GoalSpec
from src.model_client import ModelClient, ModelDecision
from src.policy import PolicyEngine, PolicyResult
from src.run_agent import run_replay

UTC = timezone.utc

def make_event(
    event_id='e1',
    user_id='U-00001',
    name='app_open',
    delivered_at='2026-09-30T10:00:00.000Z',
    occurred_at=None,
    sent_at=None,
    received_at=None,
    goal_id=None,
    minutes=None,
    **extra,
):
    # Build a test event.
    if occurred_at is None: occurred_at = delivered_at
    if sent_at is None: sent_at = delivered_at
    if received_at is None: received_at = delivered_at
    rec = {
        'event_id': event_id,
        'user_id': user_id,
        'event': name,
        'deliver_at': delivered_at,
        'occurred_at': occurred_at,
        'sent_at': sent_at,
        'received_at': received_at,
        'session_id': 's1',
        'app_dialect': 'd1',
        'app_language': 'hin',
        'platform': 'android',
        'app_version': '6.0.7',
        'properties': {},
    }
    if goal_id is not None:
        rec['properties']['goal_id'] = goal_id
    if minutes is not None:
        rec['properties']['minutes'] = minutes
    rec.update(extra)
    rec['deliver_at'] = delivered_at
    return Event(**rec)

def make_goal_spec(
    goal_id='G01',
    rule=None,
    **extra,
):
    # Build a minimal goal spec.
    if rule is None: rule = {'event': 'app_open', 'condition': 'any event', 'window_hours': 24}
    s = {'goal_id': goal_id, 'rule': rule}
    s.update(extra)
    return GoalSpec(**s)

def make_correction(
    goal_id='G01',
    reason='test correction',
    new_rule=None,
):
    # Build a rule correction.
    if new_rule is None: new_rule = {'event': 'play_started', 'condition': 'any event', 'window_hours': 24}
    return RuleCorrection(goal_id=goal_id, reason=reason, new_rule=new_rule)

def load_synthetic_pack(tmp_path, titles=None, events=None, profiles=None, episodes=None):
    # Build a tiny data pack in a temp directory for replay tests.
    root = tmp_path / 'pack'
    (root / 'data').mkdir(parents=True)
    (root / 'replay').mkdir(parents=True)
    paths = DataPackPaths.from_root(root)
    pack = DataPack(paths=paths, report=LoadReport())
    pack.titles = titles or {}
    pack.profiles = profiles or {}
    pack.episodes = episodes or {}
    pack.events = events or []
    pack.arms = {}
    pack.holdout_users = set()
    pack.triggers = []
    pack.live_events = []
    pack.control_event_path = str(root / 'replay' / 'control_events.jsonl')
    pack.goal_definition_path = str(root / 'config' / 'goals.json')
    return pack

def test_end_to_end_trigger_processing(tmp_path):
    # A single trigger produces exactly one decision and optionally one action.
    pack = DataPack(paths=None, titles={}, profiles={}, arms={}, events=[], episodes={})
    pack.holdout_users = set()
    pack.trigger_event_ids = set()
    pack.triggers = []
    pack.live_events = []
    pack.control_event_path = str(tmp_path / 'control.jsonl')
    pack.goal_definition_path = str(tmp_path / 'goals.json')
    model = MagicMock()
    model.propose_action.return_value = {'action_type': 'push', 'text': {'title': 'T', 'body': 'B'}, 'deeplink': 'ott://title/T0001', 'template_id': None, 'variables': {}}
    policy = MagicMock()
    policy.evaluate.return_value = PolicyResult(allowed=True, reason='', action_id='a1', user_id='U-00001', action_type='push', rule='')
    out_dir = tmp_path / 'out'
    actions_path, decisions_path, report = run_replay(pack, model, policy, output_dir=str(out_dir))
    assert report.decisions_total == 1
    assert report.actions_published == 1
    assert report.no_action_decisions == 0
    assert report.fault_no_action_decisions == 0
    assert report.policy_no_action_decisions == 0
    assert report.holdout_decisions == 0
    assert report.errors == []
    decision_lines = [l for l in decisions_path.read_text(encoding='utf-8').splitlines() if l.strip()]
    assert len(decision_lines) == 1
    decision = json.loads(decision_lines[0])
    for field in ['decision_id', 'user_id', 'trigger_event_id', 'goal_id', 'action', 'reason', 'published_at', 'processing_ms']:
        assert field in decision
    assert decision['action'] is None
    action_lines = [l for l in actions_path.read_text(encoding='utf-8').splitlines() if l.strip()]
    assert len(action_lines) == 1
    action = json.loads(action_lines[0])
    assert action['action_id'].startswith('A-')
    assert action['decision_id'].startswith('D-')
    assert action['user_id'] == 'U-00001'
    assert action['atype'] == 'push'

def test_deliver_at_ordering(tmp_path):
    # Triggers with earlier deliver_at must be processed first.
    e1 = make_event(event_id='e2', delivered_at='2026-09-30T15:00:00.000Z')
    e2 = make_event(event_id='e1', delivered_at='2026-09-30T14:00:00.000Z')
    pack = DataPack(paths=None, titles={}, profiles={}, arms={}, events=[e2, e1], episodes={})
    pack.holdout_users = set()
    pack.trigger_event_ids = set()
    pack.triggers = []
    pack.live_events = []
    pack.control_event_path = str(tmp_path / 'control.jsonl')
    pack.goal_definition_path = str(tmp_path / 'goals.json')
    model = MagicMock()
    model.propose_action.return_value = {'action_type': 'push', 'text': {'title': 'T', 'body': 'B'}, 'deeplink': None, 'template_id': None, 'variables': {}}
    policy = MagicMock()
    policy.evaluate.return_value = PolicyResult(allowed=True, reason='', action_id='a1', user_id='U-00001', action_type='push', rule='')
    out_dir = tmp_path / 'out'
    actions_path, decisions_path, report = run_replay(pack, model, policy, output_dir=str(out_dir))
    assert report.decisions_total == 2
    decision_lines = [l for l in decisions_path.read_text(encoding='utf-8').splitlines() if l.strip()]
    assert len(decision_lines) == 2
    decisions = [json.loads(l) for l in decision_lines]
    ids = [d['trigger_event_id'] for d in decisions]
    assert ids == ['e1', 'e2']
    action_lines = [l for l in actions_path.read_text(encoding='utf-8').splitlines() if l.strip()]
    assert len(action_lines) == 2

def test_holdout_exclusion(tmp_path):
    # Holdout users never receive decisions or actions.
    pack = DataPack(paths=None, titles={}, profiles={}, arms={}, events=[], episodes={})
    pack.holdout_users = {'U-00001'}
    pack.trigger_event_ids = set()
    pack.triggers = []
    pack.live_events = []
    pack.control_event_path = str(tmp_path / 'control.jsonl')
    pack.goal_definition_path = str(tmp_path / 'goals.json')
    model = MagicMock()
    model.propose_action.return_value = {'action_type': 'push', 'text': {'title': 'T', 'body': 'B'}, 'deeplink': None, 'template_id': None, 'variables': {}}
    policy = MagicMock()
    policy.evaluate.return_value = PolicyResult(allowed=True, reason='', action_id='a1', user_id='U-00001', action_type='push', rule='')
    out_dir = tmp_path / 'out'
    actions_path, decisions_path, report = run_replay(pack, model, policy, output_dir=str(out_dir))
    assert report.holdout_decisions == 1
    assert report.actions_published == 0
    assert report.decisions_total == 1
    decision_lines = [l for l in decisions_path.read_text(encoding='utf-8').splitlines() if l.strip()]
    assert len(decision_lines) == 1
    decision = json.loads(decision_lines[0])
    assert decision['user_id'] == 'U-00001'
    assert decision['goal_id'] is None
    assert decision['action'] is None
    assert decision['reason'] == 'HOLDOUT'

def test_model_fault_recording(tmp_path):
    # A missing model proposal is recorded as a FAULT no-action decision.
    model = MagicMock()
    model.propose_action.return_value = None
    policy = MagicMock()
    policy.evaluate.return_value = PolicyResult(allowed=True, reason='', action_id='a1', user_id='U-00001', action_type='push', rule='')
    e = make_event(event_id='e1', delivered_at='2026-09-30T10:00:00.000Z', goal_id='G01')
    pack = DataPack(paths=None, titles={}, profiles={}, arms={}, events=[e], episodes={})
    pack.holdout_users = set()
    pack.trigger_event_ids = set()
    pack.triggers = []
    pack.live_events = []
    pack.control_event_path = str(tmp_path / 'control.jsonl')
    pack.goal_definition_path = str(tmp_path / 'goals.json')
    out_dir = tmp_path / 'out'
    actions_path, decisions_path, report = run_replay(pack, model, policy, output_dir=str(out_dir))
    assert report.fault_no_action_decisions == 1
    decision_lines = [l for l in decisions_path.read_text(encoding='utf-8').splitlines() if l.strip()]
    assert len(decision_lines) == 1
    decision = json.loads(decision_lines[0])
    assert decision['action'] is None
    assert decision['reason'] == 'FAULT'
    assert any('model fault for e1' in err for err in report.errors)
    model.propose_action.assert_called_once()

def test_jsonl_schemas_and_determinism(tmp_path):
    # Output files are valid JSONL and byte-stable across re-runs.
    e1 = make_event(event_id='e1', delivered_at='2026-09-30T10:00:00.000Z')
    e2 = make_event(event_id='e2', delivered_at='2026-09-30T10:01:00.000Z')
    pack = DataPack(paths=None, titles={}, profiles={}, arms={}, events=[e1, e2], episodes={})
    pack.holdout_users = set()
    pack.trigger_event_ids = set()
    pack.triggers = []
    pack.live_events = []
    pack.control_event_path = str(tmp_path / 'control.jsonl')
    pack.goal_definition_path = str(tmp_path / 'goals.json')
    model = MagicMock()
    model.propose_action.return_value = {'action_type': 'whatsapp', 'text': None, 'deeplink': None, 'template_id': 'tmpl_1', 'variables': {'name': 'Ananya'}}
    policy = MagicMock()
    policy.evaluate.return_value = PolicyResult(allowed=True, reason='', action_id='a1', user_id='U-00001', action_type='whatsapp', rule='template_approved')
    out_dir = tmp_path / 'run1'
    ap1, dp1, r1 = run_replay(pack, model, policy, output_dir=str(out_dir))
    ap2, dp2, r2 = run_replay(pack, model, policy, output_dir=str(out_dir))
    assert r1 == r2
    assert ap1 == ap2
    assert dp1 == dp2
    decision_lines = [l for l in dp1.read_text(encoding='utf-8').splitlines() if l.strip()]
    assert len(decision_lines) == 2
    action_lines = [l for l in ap1.read_text(encoding='utf-8').splitlines() if l.strip()]
    assert len(action_lines) == 2
    for path in (ap1, dp1):
        for line in path.read_text(encoding='utf-8').splitlines():
            obj = json.loads(line)
            assert isinstance(obj, dict)
            assert len(obj) > 0

def test_policy_rejection_no_action(tmp_path):
    # A policy result of no_action records a no-action decision.
    model = MagicMock()
    model.propose_action.return_value = {'action_type': 'push', 'text': {'title': 'T', 'body': 'B'}, 'deeplink': None, 'template_id': None, 'variables': {}}
    policy = MagicMock()
    policy.evaluate.return_value = PolicyResult(allowed=True, reason='', action_id='a1', user_id='U-00001', action_type='no_action', rule='quiet_hours')
    e = make_event(event_id='e1', delivered_at='2026-09-30T10:00:00.000Z', goal_id='G01')
    pack = DataPack(paths=None, titles={}, profiles={}, arms={}, events=[e], episodes={})
    pack.holdout_users = set()
    pack.trigger_event_ids = set()
    pack.triggers = []
    pack.live_events = []
    pack.control_event_path = str(tmp_path / 'control.jsonl')
    pack.goal_definition_path = str(tmp_path / 'goals.json')
    out_dir = tmp_path / 'out'
    actions_path, decisions_path, report = run_replay(pack, model, policy, output_dir=str(out_dir))
    assert report.policy_no_action_decisions == 1
    decision_lines = [l for l in decisions_path.read_text(encoding='utf-8').splitlines() if l.strip()]
    assert len(decision_lines) == 1
    decision = json.loads(decision_lines[0])
    assert decision['action'] is None
    assert decision['reason'] == 'POLICY'
    assert actions_path.stat().st_size == 0
    assert report.actions_published == 0

def test_quiet_hours_recheck(tmp_path):
    # A trigger inside quiet hours is delivered but may be suppressed by policy.
    e = make_event(event_id='e1', delivered_at='2026-09-30T10:00:00.000Z', goal_id='G01', minutes='1440')
    pack = DataPack(paths=None, titles={}, profiles={}, arms={}, events=[e], episodes={})
    pack.holdout_users = set()
    pack.trigger_event_ids = set()
    pack.triggers = []
    pack.live_events = []
    pack.control_event_path = str(tmp_path / 'control.jsonl')
    pack.goal_definition_path = str(tmp_path / 'goals.json')
    model = MagicMock()
    model.propose_action.return_value = {'action_type': 'push', 'text': {'title': 'T', 'body': 'B'}, 'deeplink': None, 'template_id': None, 'variables': {}}
    policy = MagicMock()
    policy.evaluate.return_value = PolicyResult(allowed=True, reason='', action_id='a1', user_id='U-00001', action_type='push', rule='')
    out_dir = tmp_path / 'out'
    actions_path, decisions_path, report = run_replay(pack, model, policy, output_dir=str(out_dir))
    assert report.decisions_total == 1
    decision_lines = [l for l in decisions_path.read_text(encoding='utf-8').splitlines() if l.strip()]
    assert len(decision_lines) == 1
    decision = json.loads(decision_lines[0])
    assert decision['action'] is None
    assert decision['reason'] == 'QUIET_HOURS'
    assert report.actions_published == 0
