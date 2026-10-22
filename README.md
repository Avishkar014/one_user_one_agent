# One User, One Agent

A deterministic replay system for processing user events, evaluating agent decisions, applying policy checks, and recording resulting actions. The goal is to make each user's agent behavior traceable, testable, and safe to operate.

## Prerequisites

- Python 3.10 or later
- Windows, macOS, or Linux
- A virtual environment for project dependencies

## Setup

1. Clone or download the project.
2. Open a terminal in the project root.
3. Create and activate a virtual environment.

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

4. Install the project dependencies:

```bash
python -m pip install -r requirements.txt
```

5. Configure any required environment variables or local data paths according to the project configuration.

## Run the full replay

The full replay should be launched through the project's replay entry point, which loads the required data, processes events, evaluates decisions, applies policy checks, and writes the run outputs.

**Single-command replay:**

```bash
python -m src.run_agent
```

> Confirm this command is supported by the current implementation. If `src/run_agent.py` requires arguments or the project uses a separate CLI entry point, replace it with the actual supported command before submission.

## Run the tests

Run the replay tests with:

```bash
python -m pytest tests/test_run_agent.py -q
```

Run the full test suite with:

```bash
python -m pytest -q
```

## Outputs

The replay implementation is expected to produce structured JSON Lines output files, including:

- `decisions.jsonl` — decision records, including relevant outcomes and reasons.
- `actions.jsonl` — action records for actions that pass the applicable processing and policy checks.

The actual output directory is determined by the runner configuration. Inspect the generated records to verify decision outcomes, action publication, and reproducibility.

## Design goals

- **Single-user ownership:** Keep each agent's decisions and state associated with the correct user.
- **Traceability:** Record why a decision was accepted, rejected, or held.
- **Policy enforcement:** Prevent actions that violate applicable policy rules.
- **Deterministic replay:** Make repeated runs over the same inputs reproducible where external dependencies and nondeterministic components are controlled.
- **Failure isolation:** Record and handle failures without silently treating them as successful actions.

## Project documentation

- `ARCHITECTURE.md` — component responsibilities, planning, memory, verification, latency, recovery, and trust boundaries.
- `AI_COLLABORATION.md` — AI-assisted development and verification practices.
- `KNOWN_LIMITATIONS.md` — incomplete areas, current constraints, and deliberate scope decisions.
