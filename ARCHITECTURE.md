# Architecture — One User, One Agent

## 1. Overview

One User, One Agent is a replay-driven decision system that processes user events, maintains user-scoped goal context, evaluates eligibility, and determines whether an action can be published.

The architecture separates event processing, decision generation, independent policy verification, action publication, and outcome reporting. A model proposal is never treated as sufficient authorization to publish an action.

The implemented processing flow is:

```text
Input Data Pack
      |
      v
Data Loading and Validation
      |
      v
Historical Event Loading
      |
      v
Event Ordering and Trigger Processing
      |
      v
User-Scoped Goal Context
      |
      v
Eligibility and Trigger Checks
      |
      v
Agent / Model Decision
      |
      v
Independent Policy Verification
      |
      +------ Rejected / Held / Fault
      |                 |
      v                 v
Action Publication   Decision Record
      |
      v
Decision and Action Records
      |
      v
Simulator and Outcome Reporting
      |
      v
Impact, Latency and Final Reports
```

The replay runner is the primary execution entry point. The reporting module reconstructs goal outcomes and produces reports from the generated decisions, actions, and simulator outputs.

## 2. User and state ownership

The system follows a one-agent-per-user design.

- **User ownership:** Events, decisions, goals, and actions retain their user association.
- **Goal ownership:** Goal context is maintained for the corresponding user.
- **Decision ownership:** Decisions are associated with the user and triggering event.
- **Output ownership:** Structured records retain the identifiers needed for auditing and debugging.

The goal-management flow enforces one active goal per user. Goal selection and release are handled through the goal-management and replay orchestration logic.

User-specific state must not be shared across users. The replay implementation should not be interpreted as a guarantee of distributed or production-grade state isolation.

## 3. Event processing and ordering

The replay runner loads the supplied data pack and processes eligible events using their event-processing timestamps and ordering rules.

Important event-processing considerations include:

- Event identifiers are used for duplicate handling.
- Event receipt time is trusted for processing and freshness decisions where specified by the data contract.
- Unreliable event timestamps are not automatically treated as authoritative.
- Stale buffered app events must not be treated as fresh user activity.
- Events received after a relevant decision boundary must not incorrectly influence that earlier decision.
- Conflicting or corrected source information must be evaluated according to its provenance and freshness.

The event-processing layer is responsible for ensuring that historical events and incoming triggers are interpreted consistently during replay.

## 4. Goal management and decision-making

The agent evaluates whether a user has an eligible, measurable goal before selecting an action.

The goal-management process considers:

- Goal eligibility and configured success conditions.
- Goal priority and selection rules.
- Existing active goal context.
- New evidence and relevant event boundaries.
- Goal completion, release, and reselection.

Only one goal is active for a user at a time. Goals whose success conditions cannot be measured from the available data must not be treated as measurable goals.

When a trigger qualifies for processing, the agent determines whether an action is appropriate. Possible outcomes include no action or an action proposal such as a push notification.

A decision can be rejected or held even when the model proposes an action.

## 5. Model integration and validation

The model client is responsible for obtaining and validating model decisions.

The implementation supports controlled replay behavior so that the assignment can be executed without requiring a live model API key. Model-output handling includes validation of the expected structure and checks for unsupported claims.

The model integration also handles configured fault scenarios, including malformed responses, timeouts, and unsupported claims.

Important principles:

- Model output is untrusted input.
- A structurally valid response is not automatically a permitted action.
- Unsupported claims must not establish user eligibility or policy compliance.
- Model failures must not silently become successful actions.
- Safe fallback behavior must be used when a model response cannot be accepted.

Model calls and tool usage are subject to configured limits.

## 6. Independent policy verification

The policy-verification layer independently evaluates whether a proposed action is allowed.

It checks relevant conditions such as:

- User and goal eligibility.
- Holdout assignment.
- Notification consent and channel-specific opt-in.
- Quiet hours.
- Frequency limits.
- Content availability and dialect compatibility.
- Duplicate-action prevention.
- Policy restrictions and required action structure.

For example, push notifications require the appropriate notification consent. Quiet hours and daily frequency restrictions are checked before publication.

The verifier does not rely on a model's assertion that an action is permitted. The proposal must satisfy the actual policy checks.

This separation between proposal generation and authorization is a central architectural boundary.

## 7. Memory and user context

The implementation maintains user-scoped goal context and uses available event and profile evidence during decision-making.

Memory-related decisions must respect the following principles:

- Do not mix one user's state with another user's state.
- Do not treat stale profile information as automatically authoritative.
- Consider provenance and freshness when evidence conflicts.
- Keep goal state consistent with the relevant event history.
- Make decision outcomes traceable through structured records.

The implemented replay workflow maintains the state required for its processing and reporting. It should not be described as a general-purpose persistent conversational memory system or a production distributed-memory service.

## 8. Verification and testing

Verification occurs at multiple levels.

- **Input verification:** Validate required data and expected input structures.
- **Event verification:** Check event ordering, duplicate handling, and processing boundaries.
- **Goal verification:** Check eligibility, measurable conditions, and active-goal transitions.
- **Model verification:** Validate model response structure and unsupported claims.
- **Policy verification:** Reject actions that violate eligibility, consent, quiet-hour, frequency, or content rules.
- **Output verification:** Inspect structured decision and action records.
- **Regression verification:** Run automated tests covering normal processing and failure scenarios.
- **Replay verification:** Execute the complete pipeline and inspect generated outputs.

The complete automated test suite was executed successfully:

```bash
python -m pytest -q
```

**Latest verified result:**
- Tests passed: 213
- Tests failed: 0

The test suite provides evidence for the covered scenarios; it does not establish that every possible production scenario is safe or correct.

## 9. Latency and performance

Latency is measured separately for decision processing and trigger-to-action publication.

The completed replay produced the following measurements:

| Measurement | Result |
|---|---:|
| Decisions processed | 2,255 |
| Decision processing p50 | 1 ms |
| Decision processing p95 | 6 ms |
| Decision processing p99 | 10 ms |
| Decision processing maximum | 157 ms |
| Trigger-to-publication p50 | 0.003 s |
| Trigger-to-publication p95 | 0.007 s |
| Trigger-to-publication maximum | 0.125 s |
| Actions published within 60 seconds | 390 / 390 |

These are measurements from the supplied replay environment and its configured execution path. They are not production infrastructure or live API latency guarantees.

## 10. Failure handling and recovery

The system distinguishes between ineligible events, policy rejections, model failures, and permitted decisions.

Relevant failure scenarios include:

- Invalid or missing input data.
- Malformed model responses.
- Model timeouts.
- Unsupported model claims.
- Policy rejection.
- Events that are not eligible for processing.
- Dependency or service failures.
- Duplicate or stale events.

Where supported, failures and decision outcomes are recorded with structured reasons.

The replay implementation includes safe fallback behavior for model and processing failures. Fault scenarios can be exercised through the configured replay fault data.

Retries and action delivery must be treated carefully. Exactly-once delivery and distributed idempotency guarantees should not be inferred from replay behavior alone.

## 11. Trust boundaries

The system treats the following as separate trust boundaries:

- **Input data:** Event fields, profile snapshots, and user-provided properties may be malformed, stale, or conflicting.
- **Model output:** Model-generated proposals must be structurally and semantically validated.
- **Policy verifier:** The verifier independently decides whether a proposal is permitted.
- **External dependencies:** Model providers and other services may fail or return unexpected results.
- **Output records:** Replay files may contain user-related information and must be handled appropriately.

Model output must never bypass policy enforcement. Credentials and API keys should be supplied through environment variables or secure configuration rather than committed to source control.

## 12. Outputs and observability

The replay runner and reporting module generate structured outputs for inspection and evaluation.

| Output | Purpose |
|---|---|
| `decisions.jsonl` | Records decision outcomes and associated context |
| `actions.jsonl` | Records actions published by the pipeline |
| `viewer_state.jsonl` | Reconstructed user-facing state |
| `goal_outcomes.jsonl` | Reconstructed goal episodes and outcomes |
| `latency_report.json` | Processing and publication latency measurements |
| `impact_report.json` | Randomized-arm intent-to-treat outcome comparison |
| `final_report.md` | Consolidated replay and evaluation summary |
| `sim/outcome_events.jsonl` | Simulated outcome events |
| `sim/simulator_report.json` | Simulator execution and policy-problem summary |

These outputs support debugging, auditability, and evaluation of the replay.

## 13. Experiment evaluation

The assignment includes randomized agent and holdout groups.

Impact reporting uses an intent-to-treat approach: all users assigned to each randomized arm are included in the denominators, including users without observed outcome events.

The simulator-generated results are synthetic. They are not production measurements.

The reporting implementation distinguishes broad engagement and purchase outcomes from goal-specific success. It does not claim goal-specific causal uplift where the required holdout goal-eligibility reconstruction is unavailable.

The confidence intervals are approximate and do not account for simulator-model uncertainty.

## 14. Design trade-offs and limitations

The architecture prioritizes traceability, explicit policy enforcement, and reproducible replay over treating every incoming event as an opportunity to publish an action.

Key trade-offs include:

- Controlled replay makes debugging reproducible but does not replace live production evaluation.
- Model assistance provides flexibility, while independent verification limits what model proposals can authorize.
- Structured records improve auditability but increase output volume.
- Goal reconstruction enables evaluation but depends on the quality and coverage of the available event data.
- Synthetic simulation enables controlled testing but cannot establish real-world business impact.

Production-grade guarantees such as distributed state isolation, exactly-once delivery, strict infrastructure-level latency guarantees, and automatic recovery must only be claimed after implementation and independent verification.
