import sys
sys.path.insert(0, "d:/One_User_One_agent/stage-one-user-one-agent/src")
from src.data_loader import Event, DataPack, LoadReport
from src.run_agent import run_replay
from src.policy import PolicyEngine, PolicyResult
from unittest.mock import MagicMock
from pathlib import Path
import json

# From test_policy_rejection_no_action - use make_event style
e = Event(event_id='e1', user_id='U-00001', event='app_open', delivered_at='2026-09-30T10:00:00.000Z', occurred_at='2026-09-30T10:00:00.000Z', sent_at='2026-09-30T10:00:00.000Z', received_at='2026-09-30T10:00:00.000Z', session_id='s1', app_dialect='d1', app_language='hin', platform='android', app_version='6.0.7', properties={'goal_id': 'G01'})

pack = DataPack(paths=None, titles={}, profiles={}, arms={}, events=[e], episodes={})
pack.holdout_users = set()
pack.trigger_event_ids = set()
pack.triggers = []
pack.live_events = []
pack.control_event_path = '/tmp/control.jsonl'
pack.goal_definition_path = '/tmp/goals.json'

model = MagicMock()
model.propose_action.return_value = {'action_type': 'push', 'text': {'title': 'T', 'body': 'B'}, 'deeplink': None, 'template_id': None, 'variables': {}}

policy = MagicMock()
policy.evaluate.return_value = PolicyResult(allowed=True, reason='', action_id='a1', user_id='U-00001', action_type='no_action', rule='quiet_hours')

out_dir = Path('/tmp/test_out')
actions_path, decisions_path, report = run_replay(pack, model, policy, output_dir=str(out_dir))

print("decisions_total:", report.decisions_total)
print("no_action_decisions:", report.no_action_decisions)
print("policy_no_action_decisions:", report.policy_no_action_decisions)
print()
# Print decisions
decision_lines = [l for l in decisions_path.read_text(encoding='utf-8').splitlines() if l.strip()]
print("Decision lines:", decision_lines)
for line in decision_lines:
    d = json.loads(line)
    print("Decision:", d)