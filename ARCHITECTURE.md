# Architecture — One User, One Agent

## 1. Overview

The system processes user events through a replay pipeline. It evaluates whether an event should trigger an agent decision, checks the proposed action against policy, and records the result for inspection.

The intended processing flow is:

```text
Input Data
    |
    v
Data Loading and Validation
    |
    v
Event Processing and Ordering
    |
    v
Trigger and Eligibility Checks
    |
    v
Agent / Model Decision
    |
    v
Policy Evaluation
    |
    v
Decision and Action Records
    |
    v
Replay Report
```

The exact behavior of each stage depends on the implementation and configuration in the source code.

## 2. Ownership

The system is designed around the principle of one agent per user.

- **User ownership:** Events, decisions, and resulting actions should retain their user association.
- **State ownership:** Any user-specific memory or state should be scoped to that user.
- **Decision ownership:** A decision should be traceable to the event and user that caused it.
- **Output ownership:** Output records should preserve user identifiers for auditing and debugging.

Cross-user state or data access should not occur unless explicitly authorized by the system's design.

## 3. Planning and decision-making

The replay pipeline evaluates incoming events and determines whether they are eligible for processing. When an event qualifies, the agent or model proposes a decision.

The proposal is not automatically treated as an approved action. It must pass the applicable validation and policy checks before it can be published.

The system should distinguish between:

1. An event that is not eligible for processing.
2. A decision that is held or rejected.
3. A model or processing failure.
4. A decision that is permitted to produce an action.

This separation makes it possible to understand why an action was or was not generated.

## 4. Memory

User-specific memory should be isolated by user identity and should only contain information needed for the agent's task.

Memory handling should follow these principles:

- Do not mix one user's state with another user's state.
- Validate input before adding it to persistent memory.
- Keep only information necessary for the intended behavior.
- Define how stale or invalid memory is handled.
- Make memory-dependent decisions traceable where practical.

The replay system's actual persistent-memory capabilities should be confirmed in the implementation. If memory persistence is not implemented, this document describes a design requirement rather than a completed feature.

## 5. Verification

Verification should occur at multiple levels:

- **Input verification:** Check required fields, data types, and event structure.
- **Processing verification:** Confirm that events are processed in the intended order.
- **Decision verification:** Check the shape and validity of model output.
- **Policy verification:** Ensure rejected or disallowed actions are not published.
- **Output verification:** Validate JSONL records and required fields.
- **Regression verification:** Run automated tests for normal behavior and failure cases.
- **Determinism verification:** Replay the same controlled inputs and compare the outputs.

The primary replay test command is:

```bash
python -m pytest tests/test_run_agent.py -q
```

A passing test suite is evidence for the cases covered by the tests; it does not prove that every production scenario is safe or correct.

## 6. Latency

The replay pipeline may include data loading, event processing, model inference, policy evaluation, and output writing.

Potential latency contributors include:

- Large input datasets.
- Model or API response times.
- Repeated policy evaluations.
- Disk I/O when writing output records.
- Retries following transient failures.

Latency should be measured using representative workloads before making performance claims. Useful measurements include total replay duration, per-event processing time, model latency, and failure or retry counts.

No latency target is claimed here unless it has been measured and documented separately.

## 7. Failure recovery

Failures should be handled explicitly rather than silently converted into successful decisions.

Relevant failure cases include:

- Invalid or missing input data.
- Model errors or malformed model responses.
- Policy rejection.
- Events that are not eligible for processing.
- Output-writing failures.
- Missing or invalid configuration.

Where supported, the system should record a structured reason for the outcome. Reasons such as `HOLDOUT`, `FAULT`, `POLICY`, and `QUIET_HOURS` can help distinguish different processing outcomes.

Retries should be limited to failures that are safe to retry. If action delivery is introduced, duplicate prevention and idempotency should be considered before retrying an operation whose result is uncertain.

The exact recovery behavior must match the implemented code.

## 8. Trust boundaries

The system should treat the following as separate trust boundaries:

- **Input data:** Event fields and user-provided properties may be malformed or untrusted.
- **Model output:** Model-generated text and structured proposals must be validated.
- **Policy engine:** Policy evaluation determines whether a proposed action is permitted.
- **External services:** APIs and other dependencies can fail or return unexpected results.
- **Output records:** Logs and replay outputs may contain user-related information and should be handled appropriately.

Model output should not bypass policy enforcement. Sensitive credentials should be supplied through configuration or environment variables rather than committed to source control.

## 9. Outputs and observability

The replay runner writes structured records intended to support debugging and verification.

- `decisions.jsonl` records decision outcomes.
- `actions.jsonl` records actions produced by the pipeline.
- A replay report summarizes the run where supported.

Each record should contain enough information to trace its outcome without unnecessarily exposing sensitive user data.

## 10. Design trade-offs

This architecture favors traceability and explicit decision outcomes over treating every event as a successful action. Deterministic replay and structured output make debugging easier, while policy checks provide a boundary between model proposals and permitted behavior.

Production-grade guarantees—such as persistent memory isolation, exactly-once delivery, strict latency limits, and automatic recovery—must only be claimed when they are implemented and verified.
