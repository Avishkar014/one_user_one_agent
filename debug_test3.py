import sys
sys.path.insert(0, "d:/One_User_One_agent/stage-one-user-one-agent/src")
from src.data_loader import Event

# From test_deliver_at_ordering
e1 = Event(event_id='e2', user_id='U-00001', event='app_open', deliver_at='2026-09-30T15:00:00.000Z', occurred_at='2026-09-30T15:00:00.000Z', sent_at='2026-09-30T15:00:00.000Z', received_at='2026-09-30T15:00:00.000Z', session_id='s1', app_dialect='d1', app_language='hin', platform='android', app_version='6.0.7', properties={})
e2 = Event(event_id='e1', user_id='U-00001', event='app_open', deliver_at='2026-09-30T14:00:00.000Z', occurred_at='2026-09-30T14:00:00.000Z', sent_at='2026-09-30T14:00:00.000Z', received_at='2026-09-30T14:00:00.000Z', session_id='s1', app_dialect='d1', app_language='hin', platform='android', app_version='6.0.7', properties={})

print('e1 (id=e2) goal_id:', e1.properties.get('goal_id'))
print('e2 (id=e1) goal_id:', e2.properties.get('goal_id'))
print()
print('Both have goal_id=None, so eligibility check will fail')