# One User, One Agent

A deterministic, replayable agent system that processes user events, selects measurable goals, evaluates actions, enforces policy constraints, and records decisions and outcomes.

Each user's agent behavior is designed to be traceable, testable, and safe. Model-generated proposals are independently validated before an action can be published.

## Prerequisites

- Python 3.10 or later
- Git
- Windows, macOS, or Linux
- No external model API key required for deterministic replay

## Setup

### 1. Clone the repository

```bash
git clone https://github.com/Avishkar014/one_user_one_agent.git
cd one_user_one_agent
```

### 2. Create a virtual environment

**Windows PowerShell**

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

If PowerShell blocks script execution, use an appropriate execution policy for your environment or activate the environment through another supported method.

**macOS/Linux**

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 3. Install dependencies

```bash
python -m pip install -r requirements.txt
```

## Run the complete workflow

Execute the following commands from the project root, in order.

### 1. Execute the agent replay

```bash
python -m src.run_agent
```

This loads the data pack, processes replay triggers, evaluates goals, applies policy checks, and writes:

- `sample_run/decisions.jsonl`
- `sample_run/actions.jsonl`

The replay runner also reports the number of triggers, decisions, holdout decisions, published actions, and any execution errors.

### 2. Simulate outcomes

```bash
python data_pack/one_user_one_agent_pack/simulator/simulate_outcomes.py --pack data_pack/one_user_one_agent_pack --actions sample_run/actions.jsonl --out sample_run/sim
```

This generates simulated follow-up events and a simulator report under `sample_run/sim/`.

### 3. Generate reports

```bash
python -m src.reporting
```

This generates:

- `sample_run/viewer_state.jsonl`
- `sample_run/goal_outcomes.jsonl`
- `sample_run/latency_report.json`
- `sample_run/impact_report.json`
- `sample_run/final_report.md`

The final report summarizes replay activity, reconstructed goal outcomes, latency, and randomized-arm intent-to-treat results.

## Run the tests

### Complete test suite

```bash
python -m pytest -q
```

### Focused replay and model-client tests

```bash
python -m pytest tests/test_model_client.py tests/test_run_agent.py -q
```

**Latest verified test results:**

| Test suite | Passed | Failed |
|---|---:|---:|
| Complete suite | 213 | 0 |
| Focused model-client and replay tests | 35 | 0 |

These results reflect the final implementation verification before submission.

## Output structure

After executing the complete workflow:

```text
sample_run/
    actions.jsonl
    decisions.jsonl
    viewer_state.jsonl
    goal_outcomes.jsonl
    latency_report.json
    impact_report.json
    final_report.md
    sim/
        outcome_events.jsonl
        simulator_report.json
```

The JSONL files contain one JSON record per line. The JSON reports contain aggregated measurements and summaries.

## Verified replay results

The submitted replay produced the following results:

| Metric | Result |
|---|---:|
| Randomized users | 5,000 |
| Historical events loaded | 140,730 |
| Triggers loaded | 2,255 |
| Decisions recorded | 2,255 |
| Holdout decisions | 1,092 |
| Actions published | 390 |
| Replay errors | 0 |
| Simulator events generated | 31,514 |
| Simulator policy problems | 0 |

### Latency measurements

| Measurement | Result |
|---|---:|
| Decision processing p50 | 1 ms |
| Decision processing p95 | 6 ms |
| Decision processing p99 | 10 ms |
| Trigger-to-publication p95 | 0.007 seconds |
| Maximum trigger-to-publication latency | 0.125 seconds |
| Actions published within 60 seconds | 390 / 390 |

These measurements describe the supplied replay environment and are not production performance guarantees.

## Measurement notes

- Intent-to-treat comparisons use all users assigned to each randomized arm, including users without observed outcome events.
- Goal outcomes are reconstructed from selected-goal decisions and simulated events. They are not goal-specific causal estimates.
- Simulator outcomes are synthetic and must not be interpreted as production performance.
- Confidence intervals are approximate and do not account for simulator-model uncertainty.
- Unmeasurable or incompletely observed goal windows must not be reported as successful or failed without supporting evidence.
- A goal marked `not_succeeded` means its configured success condition was not observed within the measured window. It does not establish that the user would never achieve the goal.
- Broad engagement and purchase outcomes are not interchangeable with individual goal success.
- Goal-specific eligibility for holdout users is not reconstructed, so goal-specific causal uplift is not claimed.

## Design goals

- **Single-user ownership:** Keep each agent's decisions and state associated with the correct user.
- **Traceability:** Record decisions, reasons, evidence, and action outcomes.
- **Policy enforcement:** Prevent actions that violate consent, availability, frequency, quiet-hour, and other applicable constraints.
- **Deterministic replay:** Support repeatable execution against the supplied data pack.
- **Failure isolation:** Handle model and dependency failures safely without treating failures as successful actions.
- **Measurement integrity:** Separate operational metrics, reconstructed goal outcomes, and randomized-arm impact estimates.

## Project documentation

- `ARCHITECTURE.md` — component responsibilities, event processing, goal management, model validation, policy verification, latency, recovery, and trust boundaries.
- `AI_COLLABORATION.md` — AI-assisted development, human review, and verification practices.
- `KNOWN_LIMITATIONS.md` — implementation boundaries, remaining limitations, and final verification status.

## Repository

GitHub: https://github.com/Avishkar014/one_user_one_agent

Latest submitted commit:

```text
64bf173 Complete one user one agent implementation and replay
```

The repository's `main` branch contains the verified implementation, automated tests, documentation, and generated replay and simulator artifacts.
