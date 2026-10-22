# Known Limitations

## 1. Purpose

This document records areas that may remain incomplete or require additional verification. It distinguishes deliberate scope decisions from defects that must be resolved before the assignment can be considered complete.

Update this file to match the actual state of the submitted code.

## 2. Replay and data configuration

The replay runner may depend on particular input files, configuration values, and data structures. The supported command and required inputs must be verified against the current implementation.

**Reason for limitation:** A replay command should not be documented as working until it has been executed successfully from a clean setup.

**Follow-up:** Verify the documented setup and single-command replay using the actual project configuration.

## 3. Model reliability

Model-generated decisions can be incomplete, malformed, inconsistent, or incorrect. A valid response structure does not guarantee that the proposed content is correct.

**Reason for limitation:** Model quality depends on the model, prompt, input context, and any external service involved.

**Follow-up:** Validate model responses, test malformed outputs, and ensure model proposals cannot bypass policy checks.

## 4. Persistent memory

Do not assume that the system provides durable, cross-run user memory unless persistence is implemented and tested.

**Reason for limitation:** In-memory state and persistent storage have different lifecycle and reliability guarantees.

**Follow-up:** If persistent memory is required, define storage, isolation, retention, and recovery behavior and add tests for them.

## 5. External service failures

External model or API calls may fail because of timeouts, connectivity problems, rate limits, invalid credentials, or service outages.

**Reason for limitation:** The application cannot guarantee that external dependencies are always available.

**Follow-up:** Add bounded retries where safe, explicit failure records, timeouts, and tests for transient and permanent errors.

## 6. Action delivery and duplicate prevention

Writing an action to a local JSONL file is not equivalent to delivering that action to an external service. Likewise, a successful local replay does not prove exactly-once delivery.

**Reason for limitation:** External delivery requires additional integration, acknowledgment handling, and duplicate-prevention mechanisms.

**Follow-up:** If delivery is in scope, add idempotency keys, delivery status tracking, and recovery handling for uncertain outcomes.

## 7. Latency and scalability

Performance under large datasets or concurrent workloads may not have been measured.

**Reason for limitation:** Functional tests alone do not establish production-scale throughput or latency.

**Follow-up:** Benchmark representative inputs and record total runtime, per-event latency, model-call duration, and memory usage.

## 8. Test coverage

Automated tests only verify the scenarios they exercise. Untested edge cases may remain.

**Reason for limitation:** The current suite may not cover every malformed input, external dependency failure, or unusual event sequence.

**Follow-up:** Expand tests for invalid records, model failures, policy rejections, ordering, determinism, and output-schema validation.

The latest test result should be recorded after running:

```bash
python -m pytest tests/test_run_agent.py -q
```

Do not mark this suite as passing until all relevant failures are resolved or explicitly accounted for.

## 9. Security and privacy

The system should avoid exposing secrets and should limit sensitive information in logs. Full security hardening may be outside the scope of this assignment.

**Reason for limitation:** A replay prototype may not include production authentication, authorization, secret management, or comprehensive data-retention controls.

**Follow-up:** Keep credentials out of source control, validate untrusted input, and review output files for unnecessary personal information.

## 10. Deliberate scope decisions

Only list an item here as deliberately incomplete if that choice was actually made.

Potential examples, if applicable:

- No production deployment because the assignment focuses on local replay.
- No external action-delivery integration because local output records are sufficient for evaluation.
- No persistent memory because the current assignment scope does not require cross-run storage.
- No performance optimization until correctness and determinism are verified.

These are examples, not claims about the current implementation.

## 11. Completion criteria

Before submission:

- [ ] Confirm the README's single-command replay works.
- [ ] Run the relevant automated tests and record the actual results.
- [ ] Verify that output files contain the expected schemas.
- [ ] Check that policy-rejected actions are not published.
- [ ] Ensure architecture documentation distinguishes implemented behavior from intended design.
- [ ] Remove any limitation that no longer applies and document any remaining issue honestly.
