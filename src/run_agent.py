# -*- coding: utf-8 -*-
"""Replay orchestration: the main agent loop for the One User, One Agent assignment."""
from __future__ import annotations

import json
import os
from pathlib import Path
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from src.data_loader import (DataPack, Event, LoadReport, DataPackPaths, load_data_pack, parse_timestamp)
from src.goals import (
    priority_order,
    GoalManager,
    ACTIVE,
    SUCCEEDED,
    FAILED,
)
from src.model_client import ModelClient, ModelDecision
from src.policy import PolicyEngine, PolicyResult, load_interventions, load_pricing

__all__ = [
    'DECISION_MIN_REQUIRED_FIELDS',
    'DEFAULT_OUTPUT_DIR',
    'ActionRecord',
    'DecisionRecord',
    'RunReport',
    'load_model_fault_mapping',
    'run_replay',
]

UTC = timezone.utc


def _ist_day(when: datetime) -> str:
    '''The IST calendar date of a UTC instant, used for frequency numbering.''';
    return when.astimezone(UTC).astimezone(timezone(timedelta(hours=5, minutes=30))).date().isoformat();

@dataclass(frozen=True)
class ActionRecord:
    '''One published executable agent action (simulator input).''';
    action_id: str = None
    decision_id: str = None
    user_id: str = None
    trigger_event_id: str = None
    atype: str = None
    published_at: str = None
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
    '''One processed trigger, including intentional no-action decisions.''';
    decision_id: str = None
    user_id: str = None
    trigger_event_id: str = None
    goal_id: Optional[str] = None
    action: Optional[str] = None
    reason: str = None
    published_at: str = None
    processing_ms: int = None
    title_id: Optional[str] = None

@dataclass(frozen=True)
class RunReport:
    '''Summary of one replay run.''';
    decisions_total: int = None
    actions_published: int = None
    no_action_decisions: int = None
    fault_no_action_decisions: int = None
    policy_no_action_decisions: int = None
    holdout_decisions: int = None
    errors: List[str] = field(default_factory=list)


DEFAULT_OUTPUT_DIR = "sample_run"

DECISION_MIN_REQUIRED_FIELDS = {
    'decision_id',
    'user_id',
    'trigger_event_id',
    'goal_id',
    'action',
    'reason',
    'published_at',
    'processing_ms',
}

def _strip(value: Any) -> Optional[str]:
    '''Normalise a loader/JSONL field for stable string keys.''';
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    return str(value).strip()

def _is_writeable(path: str) -> bool:
    '''True when the parent directory exists and is writable.''';
    parent = os.path.dirname(os.path.abspath(path))
    return os.path.isdir(parent) and os.access(parent, os.W_OK)

def _dedup_preserving_order(items: Sequence[Event]) -> List[Event]:
    '''Drop exact event_id duplicates, keeping the first occurrence.''';
    seen: set = set()
    out: List[Event] = []
    for item in items:
        if item.event_id in seen: continue
        seen.add(item.event_id)
        out.append(item)
    return out

def load_model_fault_mapping(fault_jsonl: str) -> Dict[str, Dict[str, Any]]:
    '''Load the model fault mapping from a JSONL file.''';
    mapping: Dict[str, Dict[str, Any]] = {}
    with open(fault_jsonl, 'r', encoding='utf-8') as fh:
        for line in fh:
            line = line.strip()
            if not line: continue
            entry = json.loads(line)
            goal_id = str(entry.get('goal_id', '')).strip()
            if goal_id: mapping[goal_id] = entry
    return mapping


@dataclass(frozen=True)
class TriggerNode:
    event: Event = None
    delivery_at: datetime = None

def run_replay(
    data_pack: DataPack,
    model_client: ModelClient,
    policy_engine: PolicyEngine,
    output_dir: str = DEFAULT_OUTPUT_DIR,
    fault_mapping_path: Optional[str] = None,
    event_overrides: Optional[Mapping[str, Mapping[str, str]]] = None,
    policy_interventions: Optional[Mapping[str, Mapping[str, Any]]] = None,
    pricing_plan: Optional[Mapping[str, Mapping[str, Any]]] = None,
    input_dir: Optional[str] = None,
) -> Tuple[Path, Path, RunReport]:
    '''Run the scheduled replay and return (actions_path, decisions_path, report).'''
    pack = data_pack
    configured_path = Path(pack.goal_definition_path)

    if not configured_path.is_file():
        configured_path = (
            Path(__file__).resolve().parent.parent
            / "data_pack"
            / "one_user_one_agent_pack"
            / "config"
            / "goals.json"
        )

    if not configured_path.is_file():
        raise FileNotFoundError(
            f"Goal configuration file not found: {configured_path}"
        )

    goal_manager = GoalManager(pack, goals_path=str(configured_path))
    events = pack.events
    events = _dedup_preserving_order(events)
    events = sorted(events, key=lambda e: (parse_timestamp(e.deliver_at), e.event_id))
    if event_overrides:
        events = _apply_event_overrides(events, event_overrides)
    event_index = {e.event_id: e for e in events}
    dependency_failures: Dict[str, bool] = {}
    title_withdrawn: set = set()
    decisions: List[DecisionRecord] = []
    actions: List[ActionRecord] = []
    hub: List[TriggerNode] = []
    rec_id = 0
    dec_id = 0
    report_errors: List[str] = []
    action_counter = 0
    decision_counter = 0
    active_goals: Dict[str, dict] = {}
    start_dt = _utcnow()
    for node in hub + events: pass
    hub.extend(events)
    hub.sort(key=lambda n: parse_timestamp(n.deliver_at))
    date_groups: Dict[str, List[Event]] = {}
    for node in hub:
        date = _ist_day(parse_timestamp(node.deliver_at))
        date_groups.setdefault(date, []).append(node)
    for date in sorted(date_groups):
        day_nodes = date_groups[date]
        day_nodes.sort(key=lambda n: parse_timestamp(n.deliver_at))
        history: List[Event] = []
        for node in day_nodes:
            history = [e for e in history if e.event_id != node.event_id]
            history.append(node)
            user_id = node.user_id
            if user_id in pack.holdout_users:
                decisions.append(DecisionRecord(decision_id='D-{0:04d}'.format(decision_counter), user_id=user_id, trigger_event_id=node.event_id, goal_id=None, action=None, reason='HOLDOUT', published_at=parse_timestamp(node.deliver_at).isoformat(), processing_ms=0))
                decision_counter += 1
                continue
            # Build the goal context for this viewer at this trigger time.
            # Only events at or before the trigger timestamp are visible
            # (prevents future-event leakage into eligibility/success checks).
            evt_time = parse_timestamp(node.deliver_at)
            prior_events = [e for e in pack.events_for_user(user_id) if parse_timestamp(e.received_at) <= evt_time]
            goal_context = goal_manager.context_for(user_id, trigger_at=evt_time, events=prior_events)

            # Resolve the goal for this trigger: retain an existing active goal,
            # otherwise select a new one independently of the event's goal_id.
            goal_spec = None
            goal_id = None
            active_info = active_goals.get(user_id)
            if active_info is not None and active_info.get('state') == ACTIVE:
                outcome = goal_manager.evaluate(
                    active_info['goal_spec'],
                    goal_context,
                    goal_started_at=active_info['started_at'],
                    evaluated_at=evt_time,
                )
                if active_info['goal_spec'].goal_id == 'G02':
                    print(
                        'DEBUG G02:',
                        node.event_id,
                        node.event,
                        'at=', evt_time,
                        'events=', [
                            (e.event_id, e.event, e.received_at)
                            for e in goal_context.events
                        ],
                        'outcome=', outcome.state,
                        'reason=', outcome.reason,
                        'evidence=', outcome.evidence,
                    )
                if outcome.state in (SUCCEEDED, FAILED):
                    # Goal window closed; free the slot for a new selection.
                    del active_goals[user_id]
                    active_info = None
                else:
                    goal_spec = active_info['goal_spec']
                    goal_id = goal_spec.goal_id

            if goal_spec is None:
                goal_spec = goal_manager.select(goal_context)
                if goal_spec is None:
                    # No eligible goal for this viewer — no intervention.
                    decisions.append(DecisionRecord(decision_id='D-{0:04d}'.format(decision_counter), user_id=user_id, trigger_event_id=node.event_id, goal_id=None, action=None, reason='NO_ELIGIBLE_GOAL', published_at=parse_timestamp(node.deliver_at).isoformat(), processing_ms=0))
                    decision_counter += 1
                    continue
                goal_id = goal_spec.goal_id
                window_hours = goal_spec.window_hours
                window_end = evt_time + timedelta(hours=window_hours) if window_hours is not None else None
                active_goals[user_id] = {
                    'goal_spec': goal_spec,
                    'started_at': evt_time,
                    'window_end': window_end,
                    'state': ACTIVE,
                    'reason': 'selected',
                }

            # Policy evaluation - check if policy requires no_action first
            policy_result = policy_engine.evaluate(node, goal_spec)
            if policy_result.action_type == 'no_action':
                decisions.append(DecisionRecord(decision_id='D-{0:04d}'.format(decision_counter), user_id=user_id, trigger_event_id=node.event_id, goal_id=goal_id, action=None, reason='POLICY', published_at=parse_timestamp(node.deliver_at).isoformat(), processing_ms=0))
                decision_counter += 1
                continue
            
            # Check quiet hours from event payload
            minutes = int(node.properties.get('minutes') or 0)
            if minutes != 0 and check_user_quiet_hours(user_id, node):
                decisions.append(DecisionRecord(decision_id='D-{0:04d}'.format(decision_counter), user_id=user_id, trigger_event_id=node.event_id, goal_id=goal_id, action=None, reason='QUIET_HOURS', published_at=parse_timestamp(node.deliver_at).isoformat(), processing_ms=0))
                decision_counter += 1
                continue
            
            rule: Dict[str, Any] = {}
            pub = node
            proposed_action = model_client.propose_action(pub, goal_spec)
            if not proposed_action:
                decisions.append(DecisionRecord(decision_id='D-{0:04d}'.format(decision_counter), user_id=user_id, trigger_event_id=pub.event_id, goal_id=goal_id, action=None, reason='FAULT', published_at=pub.deliver_at, processing_ms=0))
                decision_counter += 1
                report_errors.append('model fault for ' + pub.event_id)
                continue
            policy_result = policy_engine.evaluate(pub, proposed_action, goal_spec)
            if isinstance(policy_result, PolicyResult):
                action_type = policy_result.action_type
                if action_type == 'no_action':
                    decisions.append(DecisionRecord(decision_id='D-{0:04d}'.format(decision_counter), user_id=user_id, trigger_event_id=pub.event_id, goal_id=goal_id, action=None, reason='POLICY', published_at=pub.deliver_at, processing_ms=0))
                    decision_counter += 1
                    continue
                elif action_type == 'holdout':
                    decisions.append(DecisionRecord(decision_id='D-{0:04d}'.format(decision_counter), user_id=user_id, trigger_event_id=pub.event_id, goal_id=goal_id, action=None, reason='POLICY', published_at=pub.deliver_at, processing_ms=0))
                    decision_counter += 1
                    continue
                else:
                    send_at = pub.deliver_at
                captions = {}
                if not captions:
                    decisions.append(DecisionRecord(decision_id='D-{0:04d}'.format(decision_counter), user_id=user_id, trigger_event_id=pub.event_id, goal_id=goal_id, action=None, reason='', published_at=parse_timestamp(pub.deliver_at).isoformat(), processing_ms=0))
                    decision_counter += 1
                    action_counter += 1
                    actions.append(ActionRecord(action_id='A-{0:04d}'.format(action_counter), decision_id='D-{0:04d}'.format(decision_counter - 1), user_id=user_id, trigger_event_id=pub.event_id, atype=proposed_action['action_type'], published_at=pub.deliver_at, send_at=send_at, text=proposed_action['text'], deeplink=proposed_action['deeplink'], template_id=proposed_action['template_id'], variables=proposed_action['variables'],
    title_id=proposed_action.get('title_id')))

            if 'title_refresh' in rule:
                title_id = proposed_action.get('title_id') or next((t.title_id for t in pack.titles.values()), None)
                t = next((t for t in pack.titles.values() if t.title_id == title_id), None)
                if t and t.campaign:
                    new_title_id = t.campaign.rsplit(' ', 1)[0] + '2'
                    t.title_id = new_title_id
                    t.retired_at = None
            if 'refresh_title' in rule:
                title_id = proposed_action.get('title_id') or next((t.title_id for t in pack.titles.values()), None)
                t = next((t for t in pack.titles.values() if t.title_id == title_id), None)
                if t and t.campaign:
                    t.title_id = t.campaign.rsplit(' ', 1)[0] + '2'
                    t.retired_at = None
            if 'clear_quotes' in rule:
                title_id = proposed_action.get('title_id') or next((t.title_id for t in pack.titles.values()), None)
                quotes = [q for q in pack.quotes if q.title_id == title_id]
                for q in quotes:
                    q.qty = 0
            if 'hold_redirect' in rule or 'retarget' in str(rule).lower():
                title_id = proposed_action.get('title_id') or next((t.title_id for t in pack.titles.values()), None)
                if any(d.event_id == pub.event_id for d in pack.control_events if d.event_type == 'title_withdrawn'):
                    title_withdrawn.add(title_id)
            check_user_quiet_hours(user_id, pub)

    os.makedirs(output_dir, exist_ok=True)
    decisions_path = Path(output_dir) / 'decisions.jsonl'
    actions_path = Path(output_dir) / 'actions.jsonl'
    with open(decisions_path, 'w', encoding='utf-8') as fh:
        for d in decisions:
            fh.write(json.dumps(d.__dict__, ensure_ascii=True) + chr(10))
    with open(actions_path, 'w', encoding='utf-8') as fh:
        for a in actions:
            fh.write(json.dumps(a.__dict__, ensure_ascii=True) + chr(10))
    return actions_path, decisions_path, RunReport(decisions_total=len(decisions), actions_published=len(actions), no_action_decisions=sum(1 for d in decisions if d.action is None and d.reason != ''), fault_no_action_decisions=sum(1 for d in decisions if d.reason == 'FAULT'), policy_no_action_decisions=sum(1 for d in decisions if d.reason == 'POLICY'), holdout_decisions=sum(1 for d in decisions if d.reason == 'HOLDOUT'), errors=report_errors)

def check_user_quiet_hours(user_id: str, trigger: Event) -> bool:
    '''Check the user quiet-hours window from the trigger payload.''';
    minutes = int(trigger.properties.get('minutes') or 0)
    if minutes == 0: return False
    # If minutes > 0, the trigger payload specifies a quiet-hours window,
    # so the user is considered to be in quiet hours.
    # The detailed hour-based check is handled by the policy engine.
    return True

def _apply_event_overrides(events: Sequence[Event], overrides: Mapping[str, Mapping[str, str]]) -> List[Event]:
    '''Yield events with matched-id fields replaced by override values.''';
    by_id = {e.event_id: e for e in events}
    out: List[Event] = []
    seen: set = set()
    for e in events:
        by_id.setdefault(e.event_id, e)
        if e.event_id in overrides:
            fields = dict(e.__dict__)
            for key, new_value in overrides[e.event_id].items():
                if key in fields:
                    fields[key] = new_value
            out.append(Event(**fields))
        else:
            out.append(e)
    return out

UTC = timezone.utc

def _utcnow() -> datetime:
    # A stable deterministic wall-clock.
    return datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)

