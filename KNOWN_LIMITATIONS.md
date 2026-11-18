# Known Limitations

## 1. Purpose

This document records the remaining limitations and scope boundaries of the submitted One User, One Agent implementation.

It distinguishes verified implementation behavior from capabilities that would require additional infrastructure, integration, or production testing.

The project has been tested through automated tests, deterministic replay, simulator execution, and reporting. Successful assignment verification does not imply production readiness.

## 2. Replay and data configuration

**Status: Implemented and verified for the supplied data pack.**

The replay runner supports execution using the supplied assignment data pack.

The following commands were successfully executed:

```bash
python -m pytest -q
python -m src.run_agent
```

The simulator and reporting pipeline were also executed successfully:

```bash
python data_pack/one_user_one_agent_pack/simulator/simulate_outcomes.py --pack data_pack/one_user_one_agent_pack --actions sample_run/actions.jsonl --out sample_run/sim

python -m src.reporting
```

The completed replay processed:

- Triggers loaded: 2,255
- Historical events loaded: 140,730
- Decisions recorded: 2,255
- Actions published: 390
- Holdout decisions: 1,092
- Replay errors: 0

**Remaining limitation:** Successful execution has been verified against the supplied project environment and data pack. A completely independent clean-machine installation has not been established as part of this verification.

## 3. Model reliability

**Status: Validation and fault handling implemented.**

Model-generated decisions are treated as untrusted proposals rather than automatically approved actions.

The implementation includes:

- Expected response-structure validation.
- Unsupported-claim checks.
- Goal-mismatch safeguards.
- Handling for malformed model output.
- Handling for model timeouts.
- Configured replay fault scenarios.
- Safe fallback behavior when model output cannot be accepted.

**Remaining limitations:**

- Model quality depends on the provider, prompt, and available context.
- Replay fault handling does not establish reliability of every live model provider.
- Semantic correctness beyond the implemented validation rules cannot be guaranteed.
- External provider availability, rate limits, and service behavior remain outside the application's control.

## 4. Persistent memory

**Status: User-scoped goal context is implemented; general durable memory is not established.**

The implementation maintains user-associated goal context during replay and handles active-goal selection and transitions.

It also considers event history and available profile evidence when making decisions.

**Remaining limitations:**

- The implementation should not be considered a general-purpose persistent conversational memory service.
- Durable cross-run memory and distributed state recovery are not established.
- Production retention, deletion, and recovery guarantees have not been implemented or independently verified.

## 5. External service failures

**Status: Replay fault handling is implemented and tested.**

The model client and replay workflow account for configured failure scenarios, including malformed responses, timeouts, and unsupported claims.

The system uses validation and fallback behavior to prevent invalid model proposals from automatically becoming published actions.

**Remaining limitations:**

- The replay environment does not prove that every external service failure can be recovered from.
- Live API connectivity, credentials, provider outages, and rate limits require separate integration testing.
- Production-grade retry orchestration and distributed recovery are not established.

## 6. Action delivery and duplicate prevention

**Status: Local action publication is implemented; external delivery guarantees are outside the verified scope.**

Permitted actions are recorded in `sample_run/actions.jsonl`. The independent verification layer checks applicable policy requirements before publication.

The simulator evaluates the generated actions and reported zero policy problems.

**Remaining limitations:**

- Writing an action record does not prove delivery to a real push-notification or messaging provider.
- Exactly-once external delivery is not guaranteed.
- Provider acknowledgments, uncertain delivery outcomes, and distributed idempotency require additional integration.
- Production delivery monitoring and retry behavior have not been established.

## 7. Latency and scalability

**Status: Replay latency has been measured. Production-scale performance has not been established.**

The completed replay produced these measurements:

| Measurement | Result |
|---|---:|
| Decisions processed | 2,255 |
| Decision processing p50 | 1 ms |
| Decision processing p95 | 6 ms |
| Decision processing p99 | 10 ms |
| Decision processing maximum | 157 ms |
| Trigger-to-publication p50 | 0.003 seconds |
| Trigger-to-publication p95 | 0.007 seconds |
| Trigger-to-publication maximum | 0.125 seconds |
| Publications within 60 seconds | 390 / 390 |

**Remaining limitations:**

- These measurements represent the supplied replay environment, not production infrastructure.
- Concurrent-user throughput and sustained traffic-burst behavior require further benchmarking.
- Live model-provider latency is not represented by local replay processing measurements.
- Memory consumption and production-scale resource utilization have not been comprehensively benchmarked.

## 8. Test coverage and verification

**Status: Automated test suite passed.**

The complete test suite was executed after the final implementation changes:

```bash
python -m pytest -q
```

Latest verified result:

- Tests passed: 213
- Tests failed: 0
- Outstanding test failures: None

Focused verification was also completed:

```bash
python -m pytest tests/test_model_client.py tests/test_run_agent.py -q
```

Result:

- Tests passed: 35
- Tests failed: 0

Additional replay and simulator verification:

- Replay errors: 0
- Simulator events generated: 31,514
- Simulator policy problems: 0
- Trigger-to-publication actions over 60 seconds: 0

**Remaining limitations:**

- Automated tests cover defined scenarios, not every possible production condition.
- Synthetic simulator outcomes cannot establish real-world user behavior.
- The impact report uses broad engagement and purchase outcomes; these are not equivalent to goal-specific success.
- Goal-specific eligibility for holdout users is not reconstructed, so goal-specific causal uplift is not claimed.
- Approximate confidence intervals do not account for simulator-model uncertainty.

## 9. Security and privacy

**Status: Basic validation and trust boundaries are implemented; comprehensive production security is outside the verified scope.**

The implementation validates model proposals and applies independent policy checks before action publication.

Credentials should be provided through environment variables or secure configuration rather than committed to source control.

**Remaining limitations:**

- Production authentication and authorization are not established.
- Comprehensive data-retention and deletion controls have not been verified.
- Generated replay files contain user-related identifiers and should be handled as potentially sensitive data.
- A complete production security audit has not been performed.

## 10. Deliberate scope decisions

The following capabilities are outside the verified assignment implementation:

1. **Production deployment:** The submitted solution focuses on local replay and evaluation using the supplied data pack.
2. **Live action delivery:** Actions are recorded locally; integration with real notification and messaging providers is not established.
3. **Distributed persistent memory:** User-scoped goal context is supported during replay, but a durable distributed memory service is not part of the verified implementation.
4. **Production-scale benchmarking:** The available latency measurements cover the replay workload rather than sustained production traffic.
5. **Real-world impact measurement:** Impact analysis uses synthetic simulator outcomes and must not be represented as observed production performance.

These boundaries do not prevent the implemented assignment workflow from being executed and evaluated.

## 11. Final completion status

The following submission checks were completed:

- [x] README and replay execution checked against the supplied project configuration.
- [x] Full automated test suite executed: 213 passed, 0 failed.
- [x] Focused model-client and replay tests executed: 35 passed.
- [x] Replay completed with 2,255 decisions and zero errors.
- [x] Simulator completed with 31,514 generated events and zero policy problems.
- [x] Decision, action, goal-outcome, viewer-state, latency, and impact outputs generated.
- [x] Trigger-to-publication latency verified against the 60-second requirement.
- [x] Policy verification and failure-handling behavior covered by automated tests.
- [x] Architecture and known limitations documented.
- [x] Changes committed and pushed to GitHub.
- [x] Local working tree verified clean and synchronized with `origin/main`.

**Final submission commit:** `64bf173`

**Repository:** https://github.com/Avishkar014/one_user_one_agent

The assignment implementation and its generated evidence are submitted. Remaining limitations primarily concern production deployment, external delivery, durable distributed state, and validation under real-world workloads.
