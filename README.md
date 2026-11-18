# One User, One Agent

A deterministic, replayable agent system that processes user events, selects measurable goals, evaluates actions, enforces policy constraints, and records decisions and outcomes. Each user's agent behavior is designed to be traceable, testable, and safe.

## Prerequisites

- Python 3.10 or later
- Git
- Windows, macOS, or Linux
- No external model API key required for deterministic replay

## Setup

Clone the repository and enter its root directory.

Create and activate a virtual environment.

**Windows PowerShell**

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

**macOS/Linux**

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Install dependencies:

```bash
python -m pip install -r requirements.txt
```

## Run the complete workflow

Run the following commands from the project root, in order.

### 1. Execute the agent replay

```bash
python -m src.run_agent
```

This loads the data pack, processes replay triggers, evaluates goals, applies policy checks, and writes:

- `sample_run/decisions.jsonl`
- `sample_run/actions.jsonl`

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

Run the complete test suite:

```bash
python -m pytest -q
```

Run focused replay and model-client tests:

```bash
python -m pytest tests/test_model_client.py tests/test_run_agent.py -q
```

## Output structure

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

## Measurement notes

- Intent-to-treat comparisons use all users assigned to each randomized arm, including users without observed outcome events.
- Goal outcomes are reconstructed from selected-goal decisions and simulated events. They are not goal-specific causal estimates.
- Simulator outcomes are synthetic and must not be interpreted as production performance.
- Confidence intervals are approximate and do not account for simulator-model uncertainty.
- Unmeasurable or incompletely observed goal windows must not be reported as successful or failed without supporting evidence.

## Design goals

- **Single-user ownership:** Keep each agent's decisions and state associated with the correct user.
- **Traceability:** Record decisions, reasons, evidence, and action outcomes.
- **Policy enforcement:** Prevent actions that violate consent, availability, frequency, quiet-hour, and other applicable constraints.
- **Deterministic replay:** Support repeatable execution against the supplied data pack.
- **Failure isolation:** Handle model and dependency failures safely without treating failures as successful actions.
- **Measurement integrity:** Separate operational metrics, reconstructed goal outcomes, and randomized-arm impact estimates.

## Project documentation

- `ARCHITECTURE.md` — component responsibilities, planning, memory, verification, latency, recovery, and trust boundaries.
- `AI_COLLABORATION.md` — AI-assisted development and verification practices.
- `KNOWN_LIMITATIONS.md` — current constraints, incomplete areas, and deliberate scope decisions.
