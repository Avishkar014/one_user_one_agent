
"""Generate operational, goal, latency, viewer-state, and ITT reports.

Run after the agent replay and outcome simulator:
    python -m src.reporting
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


IST = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc
# Simulator observes replay-day activity plus 14 subsequent calendar days.
# The observation period ends exclusively at midnight IST on 16 October.
SIMULATOR_OBSERVATION_END = datetime(
    2026, 10, 16, 0, 0, tzinfo=IST
).astimezone(UTC)
PRIMARY_OUTCOMES = (
    "play_started",
    "episode_completed",
    "purchase_completed",
)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    records = []
    if not path.exists():
        return records

    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
                if isinstance(value, dict):
                    records.append(value)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"Invalid JSON in {path}, line {line_number}: {exc}"
                ) from exc
    return records


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, ensure_ascii=False, default=str) + "\n",
        encoding="utf-8",
    )


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(
                json.dumps(record, ensure_ascii=False, default=str) + "\n"
            )


def parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except (TypeError, ValueError):
        return None


def iso(value: datetime | None) -> str | None:
    return value.isoformat().replace("+00:00", "Z") if value else None


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return round(ordered[lower], 3)
    result = ordered[lower] + (
        ordered[upper] - ordered[lower]
    ) * (position - lower)
    return round(result, 3)


def load_catalogue(path: Path) -> dict[str, dict[str, Any]]:
    result = {}
    if not path.exists():
        return result
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            title_id = row.get("title_id")
            if title_id:
                result[title_id] = row
    return result


def load_arms(path: Path) -> dict[str, str]:
    arms = {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            user_id = row.get("user_id")
            arm = row.get("arm")
            if user_id and arm:
                arms[user_id] = arm.strip().lower()
    return arms


def event_matches_goal(
    goal_id: str,
    event: dict[str, Any],
    started_at: datetime,
    catalogue: dict[str, dict[str, Any]],
) -> bool:
    """Apply the configured goal's measurable success condition."""
    event_name = event.get("event")
    properties = event.get("properties") or {}

    if goal_id == "G01":
        return event_name == "play_started"

    if goal_id == "G02":
        return event_name == "episode_completed"

    if goal_id == "G03":
        if event_name != "app_open":
            return False
        occurred = parse_time(event.get("occurred_at"))
        return bool(
            occurred
            and occurred.astimezone(IST).date()
            > started_at.astimezone(IST).date()
        )

    if goal_id in ("G04", "G05"):
        return (
            event_name == "purchase_completed"
            and properties.get("is_trial") is False
        )

    if goal_id == "G06":
        return event_name == "purchase_completed"

    if goal_id == "G07":
        if event_name != "play_started":
            return False
        title = catalogue.get(str(properties.get("title_id") or ""))
        return bool(
            title
            and title.get("content_slug")
            and properties.get("content_slug") == title.get("content_slug")
        )

    if goal_id == "G08":
        if event_name != "play_started":
            return False
        title = catalogue.get(str(properties.get("title_id") or ""))
        release_date = parse_time(
            f"{title.get('release_date')}T00:00:00Z"
        ) if title and title.get("release_date") else None
        return bool(
            release_date
            and started_at - timedelta(days=30)
            <= release_date
            <= started_at
        )

    if goal_id == "G09":
        return event_name == "play_started"

    if goal_id == "G11":
        return event_name == "tv_app_open"

    if goal_id == "G12":
        return event_name == "play_started"

    return False


def goal_success(
    goal_id: str,
    events: list[dict[str, Any]],
    started_at: datetime,
    ended_at: datetime,
    catalogue: dict[str, dict[str, Any]],
) -> bool:
    relevant = []
    for event in events:
        occurred = parse_time(event.get("occurred_at"))
        if (
            occurred
            and started_at <= occurred <= ended_at
            and event_matches_goal(goal_id, event, started_at, catalogue)
        ):
            relevant.append(event)

    if goal_id == "G09":
        distinct_days = {
            parse_time(event.get("occurred_at")).astimezone(IST).date()
            for event in relevant
            if parse_time(event.get("occurred_at"))
        }
        return len(distinct_days) >= 3

    return bool(relevant)


def build_goal_outcomes(
    decisions: list[dict[str, Any]],
    events_by_user: dict[str, list[dict[str, Any]]],
    triggers_by_id: dict[str, dict[str, Any]],
    goal_config: dict[str, Any],
    catalogue: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    specs = {
        item["goal_id"]: item
        for item in goal_config.get("goals", [])
        if item.get("goal_id")
    }

    by_user: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for decision in decisions:
        goal_id = decision.get("goal_id")
        user_id = decision.get("user_id")
        if goal_id and user_id:
            by_user[user_id].append(decision)

    results = []

    for user_id, user_decisions in by_user.items():
        user_decisions.sort(
            key=lambda item: (
                parse_time(item.get("published_at"))
                or parse_time(
                    triggers_by_id.get(
                        item.get("trigger_event_id"), {}
                    ).get("received_at")
                )
                or datetime.min.replace(tzinfo=UTC)
            )
        )

        # A change of goal marks the beginning of a new goal episode.
        episodes = []
        for decision in user_decisions:
            goal_id = decision["goal_id"]
            trigger = triggers_by_id.get(
                decision.get("trigger_event_id"), {}
            )
            start = (
                parse_time(trigger.get("received_at"))
                or parse_time(decision.get("published_at"))
            )
            if start is None:
                continue

            if (
                episodes
                and episodes[-1]["goal_id"] == goal_id
            ):
                episodes[-1]["last_decision"] = decision
                continue

            episodes.append({
                "goal_id": goal_id,
                "started_at": start,
                "first_decision": decision,
                "last_decision": decision,
            })
        user_events = events_by_user.get(user_id, [])
        for index, episode in enumerate(episodes):
            goal_id = episode["goal_id"]
            spec = specs.get(goal_id, {})
            success_spec = spec.get("success", {})
            window_hours = success_spec.get("window_hours")
            started_at = episode["started_at"]

            if not success_spec.get("event") or not window_hours:
                status = "unverifiable"
                reason = "Goal has no measurable success event or window."
                window_end = None
            else:
                window_end = started_at + timedelta(hours=int(window_hours))
                succeeded = goal_success(
                    goal_id,
                    user_events,
                    started_at,
                    window_end,
                    catalogue,
                )

                if succeeded:
                    status = "succeeded"
                    reason = "Configured success condition observed."
                elif window_end > SIMULATOR_OBSERVATION_END:
                    status = "unverifiable"
                    reason = (
                        "Available outcome events do not establish complete "
                        "coverage of the goal window."
                    )
                else:
                    status = "not_succeeded"
                    reason = (
                        "Success condition was not observed within the "
                        "configured window."
                    )

            results.append({
                "user_id": user_id,
                "goal_id": goal_id,
                "goal_name": spec.get("name"),
                "started_at": iso(started_at),
                "window_ends_at": iso(window_end),
                "status": status,
                "reason": reason,
                "first_decision_id": episode["first_decision"].get(
                    "decision_id"
                ),
                "last_decision_id": episode["last_decision"].get(
                    "decision_id"
                ),
                "measurement_note": (
                    "Reconstructed from selected-goal decisions and "
                    "simulated outcome events; not a randomized "
                    "goal-specific causal estimate."
                ),
            })

    results.sort(key=lambda item: (
        item["user_id"], item["started_at"], item["goal_id"]
    ))
    return results


def build_latency_report(
    decisions: list[dict[str, Any]],
    actions: list[dict[str, Any]],
    triggers_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    processing = [
        float(item["processing_ms"])
        for item in decisions
        if item.get("processing_ms") is not None
    ]

    action_by_trigger = {
        item.get("trigger_event_id"): item
        for item in actions
        if item.get("trigger_event_id")
    }

    publication_delays = []
    missing_timestamps = 0
    over_sla = 0

    for trigger_id, action in action_by_trigger.items():
        trigger = triggers_by_id.get(trigger_id, {})
        received = parse_time(trigger.get("received_at"))
        published = parse_time(action.get("published_at"))

        if received is None or published is None:
            missing_timestamps += 1
            continue

        delay = (published - received).total_seconds()
        publication_delays.append(delay)
        if delay > 60:
            over_sla += 1

    return {
        "decision_processing_ms": {
            "count": len(processing),
            "p50": percentile(processing, 0.50),
            "p95": percentile(processing, 0.95),
            "p99": percentile(processing, 0.99),
            "max": round(max(processing), 3) if processing else None,
        },
        "trigger_to_action_publication_seconds": {
            "count": len(publication_delays),
            "p50": percentile(publication_delays, 0.50),
            "p95": percentile(publication_delays, 0.95),
            "p99": percentile(publication_delays, 0.99),
            "max": (
                round(max(publication_delays), 3)
                if publication_delays else None
            ),
            "within_60_seconds": (
                len(publication_delays) - over_sla
            ),
            "over_60_seconds": over_sla,
            "missing_timestamps": missing_timestamps,
        },
        "note": (
            "Processing latency and trigger-to-publication latency are "
            "separate measurements. Missing timestamps are not treated "
            "as SLA successes."
        ),
    }


def build_impact_report(
    arms: dict[str, str],
    events_by_user: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    counts = Counter(arms.values())
    metrics = {}

    for outcome in PRIMARY_OUTCOMES:
        arm_results = {}

        for arm in ("agent", "holdout"):
            assigned = [
                user_id for user_id, assigned_arm in arms.items()
                if assigned_arm == arm
            ]
            successes = sum(
                any(
                    event.get("event") == outcome
                    for event in events_by_user.get(user_id, [])
                )
                for user_id in assigned
            )
            denominator = len(assigned)
            rate = successes / denominator if denominator else 0.0
            arm_results[arm] = {
                "assigned_users": denominator,
                "users_with_outcome": successes,
                "rate": round(rate, 6),
                "rate_percent": round(rate * 100, 3),
            }

        agent = arm_results["agent"]
        holdout = arm_results["holdout"]
        difference = agent["rate"] - holdout["rate"]

        n1 = agent["assigned_users"]
        n0 = holdout["assigned_users"]
        p1 = agent["rate"]
        p0 = holdout["rate"]

        standard_error = (
            math.sqrt(
                p1 * (1 - p1) / n1
                + p0 * (1 - p0) / n0
            )
            if n1 and n0 else None
        )

        metrics[outcome] = {
            "agent": agent,
            "holdout": holdout,
            "absolute_difference_percentage_points": round(
                difference * 100, 3
            ),
            "approximate_95_percent_ci_percentage_points": (
                [
                    round((difference - 1.96 * standard_error) * 100, 3),
                    round((difference + 1.96 * standard_error) * 100, 3),
                ]
                if standard_error is not None else None
            ),
        }

    return {
        "method": (
            "Intent-to-treat: all users assigned to each randomized arm "
            "are included, including users without observed events."
        ),
        "assigned_users": {
            "agent": counts.get("agent", 0),
            "holdout": counts.get("holdout", 0),
            "total": len(arms),
        },
        "outcomes": metrics,
        "interpretation": (
            "These are synthetic simulator outcomes, not observed "
            "production effects. Broad outcomes are not equivalent to "
            "goal-specific success. Approximate confidence intervals "
            "do not account for simulator-model uncertainty."
        ),
    }


def build_viewer_state(
    arms: dict[str, str],
    decisions: list[dict[str, Any]],
    actions: list[dict[str, Any]],
    events_by_user: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    latest_decision = {}
    latest_action = {}
    latest_event = {}

    def record_time(record: dict[str, Any]) -> datetime:
        return (
            parse_time(record.get("published_at"))
            or parse_time(record.get("received_at"))
            or parse_time(record.get("occurred_at"))
            or datetime.min.replace(tzinfo=UTC)
        )

    for decision in decisions:
        user_id = decision.get("user_id")
        if user_id and (
            user_id not in latest_decision
            or record_time(decision) >= record_time(latest_decision[user_id])
        ):
            latest_decision[user_id] = decision

    for action in actions:
        user_id = action.get("user_id")
        if user_id and (
            user_id not in latest_action
            or record_time(action) >= record_time(latest_action[user_id])
        ):
            latest_action[user_id] = action

    for user_id, events in events_by_user.items():
        if events:
            latest_event[user_id] = max(
                events,
                key=lambda item: record_time(item),
            )

    records = []
    for user_id in sorted(arms):
        decision = latest_decision.get(user_id, {})
        action = latest_action.get(user_id, {})
        event = latest_event.get(user_id, {})

        records.append({
            "user_id": user_id,
            "randomized_arm": arms[user_id],
            "latest_decision": decision or None,
            "latest_action": action or None,
            "latest_observed_event": event or None,
            "state_as_of": (
                event.get("received_at")
                or event.get("occurred_at")
                or action.get("published_at")
                or decision.get("published_at")
            ),
        })

    return records


def write_final_report(
    path: Path,
    arms: dict[str, str],
    decisions: list[dict[str, Any]],
    actions: list[dict[str, Any]],
    goals: list[dict[str, Any]],
    latency: dict[str, Any],
    impact: dict[str, Any],
) -> None:
    decision_counts = Counter(
        item.get("action") or "NO_ACTION" for item in decisions
    )
    goal_counts = Counter(item["status"] for item in goals)

    lines = [
        "# One User, One Agent - Replay Report",
        "Observation coverage: simulator follow-up runs through "
        "16 October 2026, 00:00 IST. All reconstructed measurable "
        "goal windows ended within this observation boundary.",
        "## Replay overview",
        "",
        f"- Randomized users: **{len(arms)}**",
        f"- Decisions recorded: **{len(decisions)}**",
        f"- Actions published: **{len(actions)}**",
        "",
        "### Decision actions",
        "",
    ]

    for action, count in sorted(decision_counts.items()):
        lines.append(f"- {action}: {count}")

    lines.extend([
        "",
        "## Goal outcome reconstruction",
        "",
        f"- Goal episodes reconstructed: **{len(goals)}**",
        f"- Succeeded: **{goal_counts['succeeded']}**",
        f"- Not succeeded: **{goal_counts['not_succeeded']}**",
        f"- Unverifiable: **{goal_counts['unverifiable']}**",
        "",
        "Goal outcomes are reconstructed from selected-goal decisions and "
        "simulated events. They are not goal-specific causal estimates.",
        "",
        "## Latency",
        "",
        f"- Decision processing p50: "
        f"{latency['decision_processing_ms']['p50']} ms",
        f"- Decision processing p95: "
        f"{latency['decision_processing_ms']['p95']} ms",
        f"- Trigger-to-publication p95: "
        f"{latency['trigger_to_action_publication_seconds']['p95']} seconds",
        f"- Actions over 60 seconds: "
        f"{latency['trigger_to_action_publication_seconds']['over_60_seconds']}",
        "",
        "## Randomized-arm intent-to-treat results",
        "",
        "| Outcome | Agent rate | Holdout rate | Difference (pp) |",
        "|---|---:|---:|---:|",
    ])

    for outcome, values in impact["outcomes"].items():
        lines.append(
            f"| {outcome} "
            f"| {values['agent']['rate_percent']:.3f}% "
            f"| {values['holdout']['rate_percent']:.3f}% "
            f"| {values['absolute_difference_percentage_points']:+.3f} |"
        )

    lines.extend([
        "",
        "## Interpretation and limitations",
        "- 'Not succeeded' means the configured success condition was "
        "not observed within the measured window; it does not establish "
        "that the user would never achieve the goal.",
        "- ITT denominators include every randomized user, including users "
        "with no generated outcome events.",
        "- Simulator results are synthetic and must not be represented as "
        "production performance.",
        "- Broad engagement and purchase outcomes are not interchangeable "
        "with each user's selected goal.",
        "- Goal episodes are reconstructed from decision history; a goal "
        "change is treated as the start of a new episode.",
        "- Goal-specific eligibility for holdout users is not reconstructed "
        "here, so no goal-specific causal uplift is claimed.",
        "- Approximate confidence intervals do not include simulator-model "
        "uncertainty.",
        "- Goal conditions that cannot be verified from the available "
        "fields are reported as unverifiable rather than assumed successful.",
        "",
    ])

    path.write_text("\n".join(lines), encoding="utf-8")


def generate_reports(project_root: Path) -> dict[str, Path]:
    pack_root = project_root / "data_pack" / "one_user_one_agent_pack"
    replay_root = pack_root / "replay"
    sample_root = project_root / "sample_run"
    sim_root = sample_root / "sim"

    required = [
        sample_root / "decisions.jsonl",
        sample_root / "actions.jsonl",
        sim_root / "outcome_events.jsonl",
        replay_root / "arms.csv",
        replay_root / "triggers.jsonl",
        pack_root / "config" / "goals.json",
    ]
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise FileNotFoundError(
            "Required replay inputs are missing:\n" + "\n".join(missing)
        )

    decisions = read_jsonl(sample_root / "decisions.jsonl")
    actions = read_jsonl(sample_root / "actions.jsonl")
    outcome_events = read_jsonl(sim_root / "outcome_events.jsonl")
    triggers = read_jsonl(replay_root / "triggers.jsonl")

    arms = load_arms(replay_root / "arms.csv")
    goal_config = json.loads(
        (pack_root / "config" / "goals.json").read_text(encoding="utf-8")
    )
    catalogue = load_catalogue(pack_root / "data" / "catalogue.csv")

    triggers_by_id = {
        item["event_id"]: item
        for item in triggers
        if item.get("event_id")
    }

    events_by_user: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in outcome_events:
        user_id = event.get("user_id")
        if user_id:
            events_by_user[user_id].append(event)

    for events in events_by_user.values():
        events.sort(
            key=lambda item: (
                parse_time(item.get("occurred_at"))
                or datetime.min.replace(tzinfo=UTC)
            )
        )

    goals = build_goal_outcomes(
        decisions,
        events_by_user,
        triggers_by_id,
        goal_config,
        catalogue,
    )
    latency = build_latency_report(
        decisions, actions, triggers_by_id
    )
    impact = build_impact_report(arms, events_by_user)
    viewer_state = build_viewer_state(
        arms, decisions, actions, events_by_user
    )

    paths = {
        "viewer_state": sample_root / "viewer_state.jsonl",
        "goal_outcomes": sample_root / "goal_outcomes.jsonl",
        "latency": sample_root / "latency_report.json",
        "impact": sample_root / "impact_report.json",
        "final_report": sample_root / "final_report.md",
    }

    write_jsonl(paths["viewer_state"], viewer_state)
    write_jsonl(paths["goal_outcomes"], goals)
    write_json(paths["latency"], latency)
    write_json(paths["impact"], impact)
    write_final_report(
        paths["final_report"],
        arms,
        decisions,
        actions,
        goals,
        latency,
        impact,
    )

    return paths


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate reports from the agent replay and simulator."
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    args = parser.parse_args()

    paths = generate_reports(args.project_root.resolve())

    print("Reporting completed")
    for name, path in paths.items():
        print(f"{name}: {path}")


if __name__ == "__main__":
    main()
