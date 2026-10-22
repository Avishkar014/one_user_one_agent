# AI Collaboration

## 1. Purpose

AI coding tools were used as development assistants during the implementation and debugging of the One User, One Agent assignment. Their role was to help investigate issues and suggest targeted changes, while the final implementation remained subject to source-code review and testing.

## 2. How AI tools helped

AI assistance was used for tasks such as:

- Investigating Python exceptions and failing unit tests.
- Reviewing function signatures and data-model definitions.
- Identifying mismatches between the replay runner and its test fixtures.
- Suggesting targeted fixes for event processing and policy handling.
- Improving project documentation and clarifying architecture.
- Suggesting test cases for normal processing, policy rejection, ordering, and failure handling.

## 3. How suggestions were checked

AI-generated suggestions were not treated as automatically correct.

The verification process included:

1. Comparing suggested changes with the existing source code.
2. Checking the actual dataclass fields and function signatures.
3. Reviewing whether test fixtures matched the data-loader contract.
4. Running the relevant pytest tests after changes.
5. Inspecting the remaining failures rather than assuming the first fix was sufficient.
6. Checking output records and failure reasons where the implementation supported those checks.

For example, the replay tests exposed a mismatch between a function expecting `DataPackPaths` and tests supplying a `DataPack`. They also exposed attempts to pass `goal_id` and `minutes` as direct `Event` fields even though the existing event model stores additional attributes in `properties`.

These mismatches needed to be resolved against the actual interfaces, rather than by blindly accepting a generated patch.

## 4. What was rejected or treated cautiously

The following types of suggestions were not to be accepted without verification:

- Changes that assumed fields existed when the actual data model did not define them.
- Changes that replaced the existing project structure without a clear need.
- Broad rewrites when a small compatibility fix was sufficient.
- Claims that tests passed without a fresh test run.
- Claims of deterministic behavior, reliable recovery, or production readiness without evidence.
- Changes to tests that merely hid a genuine implementation defect.

The goal was to preserve the existing data contracts and make focused changes rather than rewrite unrelated components.

**Before submission:** Keep only statements about suggestions actually rejected or changes actually made. If a suggested change was implemented after review, describe that accurately rather than claiming it was rejected.

## 5. Human review and responsibility

AI tools provided suggestions, explanations, and draft documentation. They did not replace review of the source code, execution of tests, or responsibility for the final submission.

The final implementation should be checked by the developer for correctness, consistency with the assignment requirements, and alignment between documentation and actual behavior.

## 6. Verification status

The replay tests should be run before the final submission:

```bash
python -m pytest tests/test_run_agent.py -q
```

Record the actual result here after running the command:

- Tests passed: [enter the verified count]
- Tests failed: [enter the verified count]
- Outstanding issues: [list any remaining failures or write "None" only if verified]

This status must reflect the latest actual test run, not an estimate or an AI-generated claim.
