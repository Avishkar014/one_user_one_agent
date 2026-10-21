"""Replay orchestration: the main agent loop for the One User, One Agent assignment.

The runner coordinates the existing data loader, goal engine, model client and
policy engine into one deterministic replay of the scripted replay day.

Design
------
1. Load the data pack with :func:src.data_loader.load_data_pack.
2. Load goal-rule corrections from the control-event stream.
3. Build a single merged event bus from triggers, live events and control events.
   Control events are treated as time-stamped bus items so their ordering is
   unambiguous.
4. Walk the bus in chronological order. For each trigger:
     * Trigger buffering (loader already dropped over-buffered triggers) and
       deduplication are handled by the loader's global `event_id` dedup.
     * Goal eligibility is re-evaluated against the user's full history.
     * Eligible triggers are sent to the model client. The model's proposed
       action is passed through the policy engine.
     * The policy result decides whether an executable action is published or
       the trigger is recorded as an explicit no-action.
5. Control events (title_withdrawn, goal_rule_corrected, dependency_*)
   update the pack/the engine while the replay runs so later triggers see the
   new state.

The runner is pure: it takes the pack paths, the model-fault mapping and the
scaffold of mocks so it can be pointed at the shipped pack or at tiny test fixtures.

The produced action records follow the simulator's JSONL schema (see the
assignment README, "Actions")::

    {"action_id": "A-0001", "decision_id": "D-0001", "user_id": "U-00042",
     "trigger_event_id": "uuid", "type": "home_page", "published_at": "...Z",
     "dialect": "d2", "hero_title_id": "T0042", "rows": [...]}

    {"action_id": "A-0002", "decision_id": "D-0001", "user_id": "U-00042",
     "trigger_event_id": "uuid", "type": "push", "published_at": "...Z",
     "send_at": "...Z", "title_id": "T0101",
     "text": {"title": "...", "body": "..."}, "deeplink": "ott://title/T0101"}

The produced decision records use the assignment's schema and always contain the
minimum fields `decision_id`, `user_id`, `trigger_event_id`, `goal_id`,
`action`, `reason`, `published_at` and `processing_ms`:

    {"decision_id": "D-0001", "user_id": "U-00042",
     "trigger_event_id": "uuid", "goal_id": "G01",
     "action": "push", "reason": "...", "published_at": "...Z", "processing_ms": 6}
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from src.data_loader import (
    DataPack,
    Event,
    LoadReport,
    DataPackPaths,
    load_data_pack,
    parse_timestamp,
)
from src.goals import RuleCorrection, load_rule_corrections, load_goal_specs, priority_order
from src.model_client import ModelClient, ModelDecision
from src.policy import PolicyEngine, PolicyResult, load_interventions, load_pricing


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


def _utcnow() -> datetime:
    """A stable, deterministic wall-clock for the replay runner."""
    return datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)


def _ist_day(when: datetime) -> str:
    """The IST calendar date of a UTC instant, used for frequency numbering."""
    return when.astimezone(UTC).astimezone(timezone(timedelta(hours=5, minutes=30))).date().isoformat()


@dataclass(frozen=True)
class ActionRecord:
    """One published executable agent action (simulator input)."""

    action_id: str
    decision_id: str
    user_id: str
    trigger_event_id: str
    atype: str
    published_at: str
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

    decision_id: str
    user_id: str
    trigger_event_id: str
    goal_id: Optional[str]
    action: Optional[str]
    reason: str
    published_at: str
    processing_ms: int


@dataclass(frozen=True)
class RunReport:
    """Summary of one replay run."""

    decisions_total: int
    actions_published: int
    no_action_decisions: int
    fault_no_action_decisions: int
    policy_no_action_decisions: int
    holdout_decisions: int
    errors: List[str] = field(default_factory=list)


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
    """Drop exact `event_id` duplicates, keeping the first occurrence."""
    seen: set[str] = set()
    out: List[Event] = []
    for item in items:
        if item.event_id in seen:
            continue
        seen.add(item.event_id)
        out.append(item)
    return out

def _preprocess(pack: DataPack) -> DataPack:
    """Apply the loader's public read-only rules to a freshly loaded pack."""
    report = pack.report
    combined = list(pack.triggers) + list(pack.live_events)
    combined = _dedup_preserving_order(combined)
    combined.sort(key=lambda item: (item.received_at, item.event_id))
    trigger_ids = {event.event_id for event in pack.triggers}
    pack.triggers = [event for event in combined if event.event_id in trigger_ids]
    pack.live_events = [event for event in combined if event.event_id not in trigger_ids]
    pack.events.sort(key=lambda item: (item.received_at, item.event_id))
    pack.rule_corrections = list(pack.rule_corrections or [])
    return pack


def _priority_number(goal_id: str) -> int:
    """Stable numeric priority so goal ordering is deterministic."""
    return int(str(goal_id).replace("G", "").lstrip("0") or "0")


def _priority_key(goal_id: str) -> Tuple[int, int]:
    """Negative so larger numbers sort earlier (higher priority)."""
    return (-_priority_number(goal_id), _strip(goal_id))


def _timestamp(value: Any) -> Optional[datetime]:
    """Parse a timestamp the way the loader does."""
    return parse_timestamp(value)


def _as_mapping(record: Mapping[str, Any]) -> Dict[str, Any]:
    return dict(record)


def _first(rec: Mapping[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        value = rec.get(name)
        if value not in (None, ""):
            return value
    return default


def _is_holdout(pack: DataPack, user_id: str) -> bool:
    return str(pack.arm_for(user_id)).strip() == "holdout"


def _select_goal(goal_manager: Any, user_id: str, trigger_at: datetime) -> Optional[str]:
    context = goal_manager.context_for(user_id, trigger_at=trigger_at, now=trigger_at)
    spec = goal_manager.select(context)
    return spec.goal_id if spec is not None else None


def _outcome(goal_manager: Any, spec: Any, context: Any, *, goal_started_at: datetime, evaluated_at: datetime) -> Any:
    return goal_manager.evaluate(spec, context, goal_started_at=goal_started_at, evaluated_at=evaluated_at)


def _is_quiet(u: datetime) -> bool:
    hour = u.astimezone(timezone(timedelta(hours=5, minutes=30))).hour
    return hour >= 22 or hour < 8


def _action_type_for(rule: str, default: str) -> str:
    return {"no_consent": "push", "holdout": "no_action", "quiet_hours": "push", "invalid_user_reference": "push"}.get(rule, default)


def _publish(action: ActionRecord) -> Dict[str, Any]:
    record = {
        "action_id": action.action_id,
        "decision_id": action.decision_id,
        "user_id": action.user_id,
        "trigger_event_id": action.trigger_event_id,
        "type": action.atype,
        "published_at": action.published_at,
    }
    if action.send_at is not None:
        record["send_at"] = action.send_at
    if action.dialect is not None:
        record["dialect"] = action.dialect
    if action.hero_title_id is not None:
        record["hero_title_id"] = action.hero_title_id
    if action.rows is not None:
        record["rows"] = action.rows
    if action.title_id is not None:
        record["title_id"] = action.title_id
    if action.text is not None:
        record["text"] = action.text
    if action.deeplink is not None:
        record["deeplink"] = action.deeplink
    if action.template_id is not None:
        record["template_id"] = action.template_id
    if action.variables is not None:
        record["variables"] = action.variables
    return record


def _to_decision(record: Mapping[str, Any],
                 published_at: str,
                 processing_ms: int,
                 decision_id: str) -> DecisionRecord:
    return DecisionRecord(
        decision_id=decision_id,
        user_id=_strip(record.get("user_id")),
        trigger_event_id=_strip(record.get("trigger_event_id")),
        goal_id=_strip(record.get("goal_id")),
        action=_strip(record.get("action")),
        reason=str(record.get("reason") or ""),
        published_at=published_at,
        processing_ms=int(record.get("processing_ms") or processing_ms),
    )


def _merge_buses(
    triggers: Sequence[Event],
    live: Sequence[Event],
    controls: Sequence[Event],
) -> List[Tuple[datetime, str, Event]]:
    """Merge three sorted streams into one ordered timeline of bus items."""
    out: List[Tuple[datetime, str, Event]] = []
    index = {"triggers": 0, "live": 0, "controls": 0}
    active = {"triggers": True, "live": True, "controls": True}
    streams = {"triggers": list(triggers), "live": list(live), "controls": list(controls)}

    while True:
        candidates: List[Tuple[datetime, str, int]] = []
        for name in ("triggers", "live", "controls"):
            if not active[name]:
                continue
            idx = index[name]
            if idx >= len(streams[name]):
                active[name] = False
                continue
            event = streams[name][idx]
            candidates.append((event.received_at, name, idx))

        if not candidates:
            break
        candidates.sort(key=lambda item: (item[0], item[1]))
        best_time, best_name, best_idx = candidates[0]

        if best_name == "triggers":
            event = streams["triggers"][best_idx]
            out.append((best_time, "trigger", event))
            index["triggers"] += 1
        elif best_name == "live":
            out.append((best_time, "live", streams["live"][best_idx]))
            index["live"] += 1
        else:
            out.append((best_time, "control", streams["controls"][best_idx]))
            index["controls"] += 1

    return out


def _control_event_at(event: Event) -> Optional[datetime]:
    if event.event in ("title_withdrawn", "goal_rule_corrected", "dependency_unavailable", "dependency_degraded"):
        return _timestamp(event.properties.get("at"))
    return None


def _first_value(rec: Mapping[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        value = rec.get(name)
        if value not in (None, ""):
            return value
    return default


def _holdout_assigned(pack: DataPack, user_id: str) -> bool:
    return str(pack.arm_for(user_id)).strip() == "holdout"


def _goal_start_for(pack: DataPack, user_id: str, goal_id: str) -> Optional[datetime]:
    """The moment a goal was started so it is never reset by a later trigger."""
    key = (user_id, goal_id, "goal")
    data = pack.goal_state.get(key)
    if data and data.get("goal") == goal_id:
        return _timestamp(data.get("start"))
    return None


def _goal_state(pack: DataPack, goal_id: str) -> Dict[str, Any]:
    return pack.goal_state.get("__global__", {}).get(goal_id, {"state": "unused", "count": 0})


def _goal_summary(pack: DataPack, goal_id: str) -> Dict[str, Any]:
    return {"state": _goal_state(pack, goal_id).get("state", "unused"), "count": _goal_state(pack, goal_id).get("count", 0)}



def run_replay(
    pack_paths: DataPackPaths,
    *,
    output_dir: str = DEFAULT_OUTPUT_DIR,
    model_faults: Optional[Mapping[str, Mapping[str, Any]]] = None,
    model_client_class: Any = None,
    policy_engine_class: Any = None,
    verbose: bool = False,
) -> Tuple[List[ActionRecord], List[DecisionRecord], RunReport]:
    """Run the full scripted replay and write the two output files."""
    if model_client_class is None:
        model_client_class = ModelClient
    if policy_engine_class is None:
        policy_engine_class = PolicyEngine

    pack = load_data_pack(pack_paths, load_history=True, load_replay=True)
    _preprocess(pack)

    pricing_path = pack_paths.resolve(pack_paths.pricing_json)
    interventions_path = pack_paths.resolve(pack_paths.interventions_json)
    report_errors: List[str] = []
    try:
        pricing = load_pricing(pricing_path)
    except Exception as exc:  # pragma: no cover - depends on pack files
        pricing = {}
        report_errors.append(f"cannot load pricing: {exc}")
    try:
        interventions = load_interventions(interventions_path)
    except Exception as exc:  # pragma: no cover - depends on pack files
        interventions = {}
        report_errors.append(f"cannot load interventions: {exc}")

    engine = policy_engine_class(pack, pricing, interventions, now=_utcnow())
    goal_manager = _GoalManager(pack)
    model_client = model_client_class(engine, goal_specs=goal_manager.specs)

    model_fault_lookup = {str(key).strip(): dict(value) for key, value in (model_faults or {}).items()}

    published_actions: List[ActionRecord] = []
    decisions: List[DecisionRecord] = []
    decision_counter = 0
    action_counter = 0
    frequency: Dict[Tuple[str, str], int] = {}
    in_app_frequency: Dict[Tuple[str, str], int] = {}

    def _next_decision_id() -> str:
        nonlocal decision_counter
        decision_counter += 1
        return f"D-{decision_counter:04d}"

    def _next_action_id() -> str:
        nonlocal action_counter
        action_counter += 1
        return f"A-{action_counter:04d}"

    def _published_at(event: Event) -> str:
        return _utcnow().isoformat()

    def _processing_ms(event: Event) -> int:
        return 1

    def _handle_control(event: Event) -> None:
        at = _control_event_at(event)
        if at is None:
            report_errors.append(f"control event missing timestamp: {event.event_id}")
            return
        if event.event == "title_withdrawn":
            engine.handle_title_withdrawn(at, str(_first_value(event.properties, "title_id", default="")))
        elif event.event == "goal_rule_corrected":
            goal_id = str(_first_value(event.properties, "goal_id", default=""))
            if goal_id:
                engine.handle_goal_rule_correction(at, goal_id)
        elif event.event == "dependency_unavailable":
            engine.handle_dependency_unavailable(at, str(_first_value(event.properties, "dependency", default="")))
        elif event.event == "dependency_degraded":
            engine.handle_dependency_degraded(at, str(_first_value(event.properties, "dependency", default="")))

    def _handle_live(event: Event) -> None:
        _handle_control(event)

    def _handle_trigger(event: Event) -> None:
        nonlocal decision_counter, action_counter
        trigger_event_id = str(event.event_id).strip()
        user_id = str(event.user_id).strip()
        at = event.deliver_at or event.received_at
        decision_counter += 1
        start = _utcnow()
        decision_id = _next_decision_id()
        goal_id: Optional[str] = None
        action: Optional[str] = None
        reason = ""

        try:
            if _holdout_assigned(pack, user_id):
                decision = _to_decision({}, _published_at(event), _processing_ms(event), decision_id)
                decision.goal_id = goal_id
                decision.action = "no_action"
                decision.reason = "user is in holdout arm; no agent action permitted"
                decisions.append(decision)
                report.holdout_decisions += 1
                return

            context = goal_manager.context_for(user_id, trigger_at=at, now=at)
            spec = goal_manager.select(context)
            goal_id = spec.goal_id if spec is not None else None

            decision = _to_decision(
                {}, _published_at(event), _processing_ms(event), decision_id
            )
            decision.goal_id = goal_id
            decision.processing_ms = _processing_ms(event)
            decision.user_id = user_id
            decision.trigger_event_id = trigger_event_id
            decision.published_at = _published_at(event)
            decisions.append(decision)

            if goal_id is None:
                decision.action = "no_action"
                decision.reason = "no eligible goal for user"
                decisions[-1] = decision
                report.no_action_decisions += 1
                return

            try:
                model_result = model_client.complete(
                    trigger_event_id,
                    user_id,
                    at,
                    action_type="push",
                )
            except Exception as exc:  # pragma: no cover - defensive
                decision.action = "no_action"
                decision.reason = f"model client error: {exc}"
                decisions[-1] = decision
                report_errors.append(str(exc))
                return

            if model_result.allowed:
                action_record = model_result.proposed_action
                action = model_result.action_type
                reason = ""
            else:
                action = _action_type_for(model_result.rule, "no_action")
                reason = model_result.reason
                if model_result.rule == "holdout":
                    action = "no_action"

                decision.action = "no_action"
                decision.reason = reason
                decisions[-1] = decision
                report.policy_no_action_decisions += 1
                return

            action_record["decision_id"] = decision_id
            action_record["published_at"] = _published_at(event)
            action_record["processing_ms"] = _processing_ms(event)
            published_actions.append(_publish(action_record))
            report.actions_published += 1

            if action in ("push", "whatsapp", "in_app_message"):
                day = _ist_day(at)
                key = (user_id, day)
                frequency[key] = frequency.get(key, 0) + 1
        finally:
            _goal_state(pack, goal_id or "").update({"goal_id": goal_id, "started": start.isoformat()})

        decision.action = action
        decision.reason = reason
        decisions[-1] = decision

    bus = _merge_buses(pack.triggers, pack.live_events, pack.control_events)
    for bus_type, item in bus:
        if bus_type == "trigger":
            _handle_trigger(item)
        elif bus_type == "live":
            _handle_live(item)
        else:
            _handle_control(item)

    os.makedirs(output_dir, exist_ok=True)
    decisions_path = os.path.join(output_dir, "decisions.jsonl")
    actions_path = os.path.join(output_dir, "actions.jsonl")

    with open(decisions_path, "w", encoding="utf-8") as handle:
        for decision in decisions:
            handle.write(json.dumps(_as_mapping(decision)) + "\n")

    with open(actions_path, "w", encoding="utf-8") as handle:
        for action in published_actions:
            handle.write(json.dumps(_as_mapping(action)) + "\n")

    report.decisions_total = len(decisions)
    report.no_action_decisions = sum(1 for d in decisions if d.action == "no_action")
    report.fault_no_action_decisions = sum(1 for d in decisions if d.reason.startswith(("model provider timed out", "no model output", "malformed model output", "unsupported claim", "goal not eligible")))
    report.policy_no_action_decisions = sum(1 for d in decisions if d.reason and not d.reason.startswith(("model provider timed out", "no model output", "malformed model output", "unsupported claim", "goal not eligible", "user is in holdout")))
    report.errors.extend(report_errors)
    return published_actions, decisions, report

class _GoalManager:
    """Small wrapper over the goal manager so the runner only uses public APIs."""

    def __init__(self, pack: DataPack) -> None:
        self.pack = pack
        self.specs = load_goal_specs(pack_paths.resolve(pack_paths.goals_json))
        self.corrections = list(pack.rule_corrections or [])
        self.priority = priority_order(priority)

    def context_for(self, user_id, trigger_at, now=None):
        return self.pack.context_for(user_id, trigger_at=trigger_at, now=now)

    def select(self, context):
        return self.pack.select(context)

    def evaluate(self, spec, context, *, goal_started_at=None, evaluated_at=None):
        return self.pack.evaluate(spec, context, goal_started_at=goal_started_at, evaluated_at=evaluated_at)


@dataclass(frozen=True)
class ModelFault:
    fault: str
    model_output: Optional[Dict[str, Any]] = None
    seconds: Optional[int] = None


@dataclass(frozen=True)
class PolicyGate:
    allowed: bool
    reason: str
    action_type: str
    rule: str
    proposed_action: Optional[Dict[str, Any]] = None


def load_model_fault_mapping(path: str) -> Dict[str, ModelFault]:
    """Load the model fault table from a JSONL file."""
    mapping: Dict[str, ModelFault] = {}
    if not os.path.exists(path):
        return mapping
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
            trigger = _strip(record.get("trigger_event_id"))
            if not trigger:
                continue
            mapping[trigger] = ModelFault(
                fault=str(record.get("fault") or "").strip(),
                model_output=dict(record.get("model_output") or {}),
                seconds=int(record.get("seconds") or 0),
            )
    return mapping

