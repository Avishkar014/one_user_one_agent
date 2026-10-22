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
from src.goals import (RuleCorrection, load_rule_corrections, load_goal_specs, priority_order)
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
    rule_corrections = load_rule_corrections(pack.control_event_path)
    goal_specs = load_goal_specs(pack.goal_definition_path)
    events = pack.events
    events = _dedup_preserving_order(events)
    events = sorted(events, key=lambda e: (parse_timestamp(e.deliver_at), e.event_id))
    if event_overrides:
        events = _apply_event_overrides(events, event_overrides)
    event_index = {e.event_id: e for e in events}
    dependency_failures: Dict[str, bool] = {}
    title_withdrawn: set = set()
    goal_rule_corrections: Dict[str, Dict[str, Any]] = {}
    for corr in rule_corrections:
        goal_rule_corrections[corr.goal_id] = {'reason': corr.reason, 'rule': corr.new_rule}
    decisions: List[DecisionRecord] = []
    actions: List[ActionRecord] = []
    hub: List[TriggerNode] = []
    rec_id = 0
    dec_id = 0
    report_errors: List[str] = []
    action_counter = 0
    decision_counter = 0
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
        day_nodes.sort(key=lambda n: n.event_id)
        history: List[Event] = []
        for node in day_nodes:
            history = [e for e in history if e.event_id != node.event_id]
            history.append(node)
            user_id = node.user_id
            if user_id in pack.holdout_users:
                decisions.append(DecisionRecord(decision_id='D-{0:04d}'.format(decision_counter), user_id=user_id, trigger_event_id=node.event.event_id, goal_id=None, action=None, reason='HOLDOUT', published_at=node.deliver_at.isoformat(), processing_ms=0))
                decision_counter += 1
                continue
            eligible = node.properties.get('goal_id') is not None and node.properties.get('goal_id') != 'G10'
            if node.properties.get('goal_id') == 'G11':
                evidence = any(e.trigger_type == 'viewed_profile' for e in history)
                eligible = evidence and evidence.target == node.properties.get('goal_id')
            if not eligible:
                decisions.append(DecisionRecord(decision_id='D-{0:04d}'.format(decision_counter), user_id=user_id, trigger_event_id=node.event_id, goal_id=node.properties.get('goal_id'), action=None, reason='ELIGIBILITY', published_at=parse_timestamp(node.deliver_at).isoformat(), processing_ms=0))
                decision_counter += 1
                continue
            goal_spec = goal_specs.get(node.properties.get('goal_id')) if node.properties.get('goal_id') else None
            if goal_spec is None: goal_spec = next(iter(goal_specs.values()), None)
            rule = goal_spec.rule if goal_spec else None
            if node.properties.get('goal_id') in goal_rule_corrections:
                rule = goal_rule_corrections[node.properties.get('goal_id')]['rule']
            titles = [t for t in pack.titles if t.title_id == node.event.title_id]
            pub = node
            evt_time = parse_timestamp(node.deliver_at)
            titles_for_user = []
            for t in pack.titles:
                t_match = t.title_id == pub.title_id
                if not t_match: continue
                if t.title in title_withdrawn: continue
                if 'retire_title' in rule and t.retired_at:
                    tunix = parse_timestamp(t.retired_at)
                    if tunix <= evt_time:
                        title_withdrawn.add(t.title_id)
                        continue
                if t.campaign and rule.get('retire_title', False):
                    retired = t.campaign.rsplit(' ', 1)[-1]
                    if event_index.get(pub.event_id, Event()).title_id == t.title_id:
                        title_withdrawn.add(t.title_id)
                        continue
                titles_for_user.append(t)
            proposed_action = model_client.propose_action(pub, goal_spec)
            if not proposed_action:
                decisions.append(DecisionRecord(decision_id='D-{0:04d}'.format(decision_counter), user_id=user_id, trigger_event_id=pub.event_id, goal_id=pub.properties.get('goal_id'), action=None, reason='FAULT', published_at=pub.deliver_at, processing_ms=0))
                decision_counter += 1
                report_errors.append('model fault for ' + pub.event_id)
                continue
            eligible_titles = any(t.title_id == pub.title_id and t.matched_count < t.max_uses for t in titles_for_user)
            if not eligible_titles:
                decisions.append(DecisionRecord(decision_id='D-{0:04d}'.format(decision_counter), user_id=user_id, trigger_event_id=pub.event_id, goal_id=pub.properties.get('goal_id'), action=None, reason='TITLE_QUOTA', published_at=pub.deliver_at, processing_ms=0))
                decision_counter += 1
                continue
            policy_result = policy_engine.evaluate(pub, proposed_action, goal_spec)
            if isinstance(policy_result, PolicyResult):
                action_type = policy_result.action_type
                if action_type == 'no_action':
                    decisions.append(DecisionRecord(decision_id='D-{0:04d}'.format(decision_counter), user_id=user_id, trigger_event_id=pub.event_id, goal_id=pub.properties.get('goal_id'), action=None, reason='POLICY', published_at=pub.deliver_at, processing_ms=0))
                    decision_counter += 1
                    continue
                elif action_type == 'holdout':
                    decisions.append(DecisionRecord(decision_id='D-{0:04d}'.format(decision_counter), user_id=user_id, trigger_event_id=pub.event_id, goal_id=pub.properties.get('goal_id'), action=None, reason='POLICY', published_at=pub.deliver_at, processing_ms=0))
                    decision_counter += 1
                    continue
                else:
                    send_at = pub.deliver_at
                captions = {}
                if not captions:
                    action_counter += 1
                    actions.append(ActionRecord(action_id='A-{0:04d}'.format(action_counter), decision_id='D-{0:04d}'.format(decision_counter - 1), user_id=user_id, trigger_event_id=pub.event_id, atype=proposed_action['action_type'], published_at=pub.deliver_at, send_at=send_at, text=proposed_action['text'], deeplink=proposed_action['deeplink'], template_id=proposed_action['template_id'], variables=proposed_action['variables']))

            if 'title_refresh' in rule:
                t = next((t for t in pack.titles if t.title_id == pub.title_id), None)
                if t and t.campaign:
                    new_title_id = t.campaign.rsplit(' ', 1)[0] + '2'
                    t.title_id = new_title_id
                    t.retired_at = None
            if 'refresh_title' in rule:
                t = next((t for t in pack.titles if t.title_id == pub.title_id), None)
                if t and t.campaign:
                    t.title_id = t.campaign.rsplit(' ', 1)[0] + '2'
                    t.retired_at = None
            if 'clear_quotes' in rule:
                quotes = [q for q in pack.quotes if q.title_id == pub.title_id]
                for q in quotes:
                    q.qty = 0
            if 'hold_redirect' in rule or 'retarget' in str(rule).lower():
                if any(d.event_id == pub.event_id for d in pack.control_events if d.event_type == 'title_withdrawn'):
                    title_withdrawn.add(pub.title_id)
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
    return actions_path, decisions_path, RunReport(decisions_total=len(decisions), actions_published=len(actions), no_action_decisions=sum(1 for d in decisions if d.action is None), fault_no_action_decisions=sum(1 for d in decisions if d.reason == 'FAULT'), policy_no_action_decisions=sum(1 for d in decisions if d.reason == 'POLICY'), holdout_decisions=sum(1 for d in decisions if d.reason == 'HOLDOUT'), errors=report_errors)

def check_user_quiet_hours(user_id: str, trigger: Event) -> bool:
    '''Check the user quiet-hours window from the trigger payload.''';
    minutes = int(trigger.properties.get('minutes') or 0)
    if minutes == 0: return True
    hour = datetime.now().hour
    if not (0 <= hour < 24): return True
    return not (hour < minutes // 60 or hour >= (minutes + 60) // 60)

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

