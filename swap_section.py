#!/usr/bin/env python
# Script to swap the policy/title section in run_agent.py

with open('d:/One_User_One_agent/stage-one-user-one-agent/src/run_agent.py', 'r') as f:
    content = f.read()

# The old section to replace (exact match from file)
old = """            proposed_action = model_client.propose_action(pub, goal_spec)
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
                actions.append(ActionRecord(action_id='A-{0:04d}'.format(action_counter), decision_id='D-{0:04d}'.format(decision_counter - 1), user_id=user_id, trigger_event_id=pub.event_id, atype=proposed_action['action_type'], published_at=pub.deliver_at, send_at=send_at, text=proposed_action['text'], deeplink=proposed_action['deeplink'], template_id=proposed_action['template_id'], variables=proposed_action['variables']))"""

# The new section
new = """            proposed_action = model_client.propose_action(pub, goal_spec)
            if not proposed_action:
                decisions.append(DecisionRecord(decision_id='D-{0:04d}'.format(decision_counter), user_id=user_id, trigger_event_id=pub.event_id, goal_id=pub.properties.get('goal_id'), action=None, reason='FAULT', published_at=pub.deliver_at, processing_ms=0))
                decision_counter += 1
                report_errors.append('model fault for ' + pub.event_id)
                continue
            
            # Evaluate policy first - policy decision takes precedence
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
            
            # Title eligibility only affects action publication, not policy decision
            eligible_titles = any(t.title_id == pub.title_id and t.matched_count < t.max_uses for t in titles_for_user)
            if not eligible_titles:
                decisions.append(DecisionRecord(decision_id='D-{0:04d}'.format(decision_counter), user_id=user_id, trigger_event_id=pub.event_id, goal_id=pub.properties.get('goal_id'), action=None, reason='TITLE_QUOTA', published_at=pub.deliver_at, processing_ms=0))
                decision_counter += 1
            else:
                action_counter += 1
                actions.append(ActionRecord(action_id='A-{0:04d}'.format(action_counter), decision_id='D-{0:04d}'.format(decision_counter - 1), user_id=user_id, trigger_event_id=pub.event_id, atype=proposed_action['action_type'], published_at=pub.deliver_at, send_at=send_at, text=proposed_action['text'], deeplink=proposed_action['deeplink'], template_id=proposed_action['template_id'], variables=proposed_action['variables']))"""

if old in content:
    content = content.replace(old, new)
    with open('d:/One_User_One_agent/stage-one-user-one-agent/src/run_agent.py', 'w') as f:
        f.write(content)
    print("SUCCESS: File updated")
else:
    print("FAIL: old section not found")
    # Debug: print lines 217-228
    lines = content.split('\n')
    for i in range(216, 230):
        print(f"{i+1}: {lines[i][:80]}")