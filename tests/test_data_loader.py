"""Tests for :mod:`src.data_loader`.

The tests build a tiny synthetic data pack in a temporary directory so that
every behaviour (duplicates, buffering, malformed rows, empty files) can be
asserted exactly.  A couple of tests additionally run against the real,
shipped data pack when it is present.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.data_loader import (  # noqa: E402
    DEFAULT_MAX_BUFFER_SECONDS,
    DataPack,
    DataPackPaths,
    LoadReport,
    load_arms,
    load_catalogue,
    load_data_pack,
    load_episodes,
    load_events,
    load_profiles,
    parse_timestamp,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REAL_PACK = os.path.join(REPO_ROOT, "data_pack", "one_user_one_agent_pack")

CATALOGUE_HEADER = [
    "title_id",
    "content_slug",
    "title",
    "dialect",
    "language",
    "content_type",
    "genres",
    "release_date",
    "available_from",
    "available_until",
    "is_premium",
    "description",
]


def write_csv(path, header, rows):
    """Write a small CSV file."""
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(",".join(header) + "\n")
        for row in rows:
            handle.write(",".join("" if cell is None else str(cell) for cell in row) + "\n")
    return path


def write_jsonl(path, records):
    """Write a small JSONL file from dicts and/or raw strings."""
    with open(path, "w", encoding="utf-8") as handle:
        for record in records:
            if isinstance(record, str):
                handle.write(record + "\n")
            else:
                handle.write(json.dumps(record) + "\n")
    return path


def event(
    event_id="e1",
    user_id="U-00001",
    name="app_open",
    occurred_at="2026-09-30T10:00:00.000Z",
    sent_at=None,
    received_at=None,
    **extra,
):
    """Build an event record with sensible defaults."""
    record = {
        "event_id": event_id,
        "user_id": user_id,
        "event": name,
        "occurred_at": occurred_at,
        "sent_at": sent_at if sent_at is not None else occurred_at,
        "received_at": received_at if received_at is not None else occurred_at,
        "session_id": "s1",
        "app_dialect": "d1",
        "app_language": "hin",
        "platform": "android",
        "app_version": "6.0.7",
        "properties": {},
    }
    record.update(extra)
    return record


@pytest.fixture()
def pack_dir(tmp_path):
    """A minimal but complete data pack on disk."""
    root = tmp_path / "pack"
    (root / "data").mkdir(parents=True)
    (root / "replay").mkdir(parents=True)

    write_csv(
        root / "data" / "catalogue.csv",
        CATALOGUE_HEADER,
        [
            [
                "T0001",
                "rava-baya-d1",
                "Rava Baya",
                "d1",
                "hin",
                "film",
                "Drama|Thriller",
                "2026-02-18",
                "2026-02-18T00:59:31.924Z",
                "",
                "true",
                "A drama film.",
            ],
            [
                "T0002",
                "rava-baya-d1",
                "Rava Baya (Part 2)",
                "d1",
                "hin",
                "film",
                "Drama",
                "2026-03-01",
                "2026-03-01T00:00:00.000Z",
                "2027-01-01T00:00:00.000Z",
                "false",
                "Sequel.",
            ],
        ],
    )

    write_csv(
        root / "data" / "episodes.csv",
        ["episode_id", "content_slug", "season", "episode", "minutes"],
        [
            ["rava-baya-d1-s1e2", "rava-baya-d1", "1", "2", "45"],
            ["rava-baya-d1-s1e1", "rava-baya-d1", "1", "1", "44"],
            ["rava-baya-d1-s2e1", "rava-baya-d1", "2", "1", "46"],
        ],
    )

    write_csv(
        root / "data" / "profiles_snapshot.csv",
        [
            "user_id",
            "signup_at",
            "subscription_status",
            "plan_started_at",
            "plan_ends_at",
            "preferred_dialect",
            "preferred_language",
            "notifications_opt_in",
            "whatsapp_opt_in",
            "platform",
            "updated_at",
            "snapshot_at",
        ],
        [
            [
                "U-00001",
                "2026-01-01T00:00:00.000Z",
                "trial_active",
                "2026-08-01T00:00:00.000Z",
                "2026-09-01T00:00:00.000Z",
                "d1",
                "hin",
                "true",
                "true",
                "android",
                "2026-08-02T00:00:00.000Z",
                "2026-09-30T00:00:00.000Z",
            ],
            [
                "U-00001",
                "2026-01-01T00:00:00.000Z",
                "lapsed",
                "2026-02-01T00:00:00.000Z",
                "2026-03-01T00:00:00.000Z",
                "d2",
                "hin",
                "false",
                "false",
                "ios",
                "2026-08-20T00:00:00.000Z",
                "2026-09-30T00:00:00.000Z",
            ],
            [
                "U-00002",
                "2026-02-01T00:00:00.000Z",
                "never",
                "",
                "",
                "d1",
                "hin",
                "true",
                "false",
                "android",
                "2026-08-05T00:00:00.000Z",
                "2026-09-30T00:00:00.000Z",
            ],
        ],
    )

    write_jsonl(
        root / "data" / "events.jsonl",
        [
            event("e1", received_at="2026-09-30T10:05:00.000Z"),
            event("e2", received_at="2026-09-30T10:01:00.000Z", name="home_viewed"),
        ],
    )

    write_jsonl(
        root / "replay" / "triggers.jsonl",
        [
            event(
                "t1",
                received_at="2026-09-30T18:32:59.458Z",
                deliver_at="2026-09-30T18:32:59.458Z",
            )
        ],
    )
    write_jsonl(root / "replay" / "live_events.jsonl", [])
    write_csv(
        root / "replay" / "arms.csv", ["user_id", "arm"], [["U-00001", "holdout"]]
    )
    return root


@pytest.fixture()
def paths(pack_dir):
    """Path configuration pointing at the synthetic pack."""
    return DataPackPaths.from_root(str(pack_dir))


# ---------------------------------------------------------------------------
# Normal loading
# ---------------------------------------------------------------------------


def test_load_data_pack_reads_every_section(paths):
    pack = load_data_pack(paths)

    assert isinstance(pack, DataPack)
    assert set(pack.titles) == {"T0001", "T0002"}
    assert set(pack.episodes) == {"rava-baya-d1"}
    assert set(pack.profiles) == {"U-00001", "U-00002"}
    assert pack.arms == {"U-00001": "holdout"}
    assert [event.event_id for event in pack.events] == ["e2", "e1"]
    assert [event.event_id for event in pack.triggers] == ["t1"]
    assert pack.live_events == []
    assert pack.report.rows_skipped == 0
    assert pack.report.missing_files == []


def test_catalogue_parses_every_field(paths):
    titles = load_catalogue(paths.resolve(paths.catalogue))

    first = titles["T0001"]
    assert first.content_slug == "rava-baya-d1"
    assert first.title == "Rava Baya"
    assert first.dialect == "d1"
    assert first.content_type == "film"
    assert first.genres == ("Drama", "Thriller")
    assert first.is_premium is True
    assert first.available_until is None
    assert first.available_from is not None

    second = titles["T0002"]
    assert second.is_premium is False
    assert second.available_until is not None


def test_episodes_are_grouped_and_ordered(paths):
    episodes = load_episodes(paths.resolve(paths.episodes))

    assert list(episodes) == ["rava-baya-d1"]
    assert [episode.episode_id for episode in episodes["rava-baya-d1"]] == [
        "rava-baya-d1-s1e1",
        "rava-baya-d1-s1e2",
        "rava-baya-d1-s2e1",
    ]
    assert episodes["rava-baya-d1"][0].minutes == 44


def test_arms_are_keyed_by_user(paths):
    arms = load_arms(paths.resolve(paths.arms))

    assert arms["U-00001"] == "holdout"


def test_lookups_resolve_by_id_and_slug(paths):
    pack = load_data_pack(paths)

    assert pack.title_by_id("T0001").title == "Rava Baya"
    assert pack.title_by_slug("rava-baya-d1").title_id == "T0001"
    assert pack.episodes_for_slug("rava-baya-d1")[0].episode_id == "rava-baya-d1-s1e1"
    assert pack.episode_by_id("rava-baya-d1-s2e1").season == 2
    assert pack.profile_for("U-00001").user_id == "U-00001"
    assert pack.arm_for("U-00001") == "holdout"
    assert pack.events_for_user("U-00001") == pack.events


def test_unknown_lookups_return_none(paths):
    pack = load_data_pack(paths)

    assert pack.title_by_id("nope") is None
    assert pack.title_by_slug("nope") is None
    assert pack.episodes_for_slug("nope") == []
    assert pack.episode_by_id("nope") is None
    assert pack.profile_for("nope") is None
    assert pack.arm_for("nope") is None
    assert pack.events_for_user("nope") == []


def test_pack_keeps_over_buffered_history_by_default(tmp_path):
    """The authoritative history survives the buffer rule untouched."""
    root = tmp_path / "pack"
    (root / "data").mkdir(parents=True)
    (root / "replay").mkdir(parents=True)

    write_jsonl(
        root / "data" / "events.jsonl",
        [
            event(
                "late",
                occurred_at="2026-09-30T09:00:00.000Z",
                sent_at="2026-09-30T09:20:00.000Z",  # 1200s later
                received_at="2026-09-30T09:20:00.000Z",
            )
        ],
    )
    write_jsonl(root / "replay" / "triggers.jsonl", [])
    write_jsonl(root / "replay" / "live_events.jsonl", [])

    pack = load_data_pack(DataPackPaths.from_root(str(root)))

    assert [item.event_id for item in pack.events] == ["late"]
    assert pack.report.buffered_dropped == 0


def test_pack_applies_the_buffer_rule_to_triggers_only(tmp_path):
    """The simulator's 600s rule scopes to replay-day triggers."""
    root = tmp_path / "pack"
    (root / "data").mkdir(parents=True)
    (root / "replay").mkdir(parents=True)

    write_jsonl(
        root / "data" / "events.jsonl",
        [
            event(
                "history",
                occurred_at="2026-09-30T09:00:00.000Z",
                sent_at="2026-09-30T09:20:00.000Z",  # 1200s later
                received_at="2026-09-30T09:20:00.000Z",
            )
        ],
    )
    write_jsonl(
        root / "replay" / "triggers.jsonl",
        [
            event(
                "trigger",
                occurred_at="2026-09-30T09:00:00.000Z",
                sent_at="2026-09-30T09:20:00.000Z",  # 1200s later
                received_at="2026-09-30T09:20:00.000Z",
            )
        ],
    )
    write_jsonl(root / "replay" / "live_events.jsonl", [])

    pack = load_data_pack(
        DataPackPaths.from_root(str(root)), trigger_max_buffer_seconds=600
    )

    assert [item.event_id for item in pack.events] == ["history"]
    assert pack.triggers == []
    assert pack.report.buffered_dropped == 1


def test_pack_filters_triggers_by_default_and_keeps_history(tmp_path):
    """The default mirrors the simulator: triggers filtered, history kept."""
    root = tmp_path / "pack"
    (root / "data").mkdir(parents=True)
    (root / "replay").mkdir(parents=True)

    write_jsonl(
        root / "data" / "events.jsonl",
        [
            event(
                "history",
                occurred_at="2026-09-30T09:00:00.000Z",
                sent_at="2026-09-30T09:20:00.000Z",  # 1200s later
                received_at="2026-09-30T09:20:00.000Z",
            )
        ],
    )
    write_jsonl(
        root / "replay" / "triggers.jsonl",
        [
            event(
                "buffered-trigger",
                occurred_at="2026-09-30T09:00:00.000Z",
                sent_at="2026-09-30T09:20:00.000Z",  # 1200s later
                received_at="2026-09-30T09:20:00.000Z",
            ),
            event(
                "fresh-trigger",
                occurred_at="2026-09-30T09:00:00.000Z",
                sent_at="2026-09-30T09:01:00.000Z",  # 60s later
                received_at="2026-09-30T09:01:00.000Z",
            ),
        ],
    )
    write_jsonl(root / "replay" / "live_events.jsonl", [])

    pack = load_data_pack(DataPackPaths.from_root(str(root)))

    # History is authoritative and complete.
    assert [item.event_id for item in pack.events] == ["history"]
    # Only the offline-buffered trigger is dropped; the fresh one survives.
    assert [item.event_id for item in pack.triggers] == ["fresh-trigger"]
    assert pack.report.buffered_dropped == 1


def test_pack_can_keep_every_trigger(tmp_path):
    """``trigger_max_buffer_seconds=None`` disables the trigger rule."""
    root = tmp_path / "pack"
    (root / "data").mkdir(parents=True)
    (root / "replay").mkdir(parents=True)

    write_jsonl(root / "data" / "events.jsonl", [])
    write_jsonl(
        root / "replay" / "triggers.jsonl",
        [
            event(
                "buffered-trigger",
                occurred_at="2026-09-30T09:00:00.000Z",
                sent_at="2026-09-30T09:20:00.000Z",
                received_at="2026-09-30T09:20:00.000Z",
            )
        ],
    )
    write_jsonl(root / "replay" / "live_events.jsonl", [])

    pack = load_data_pack(
        DataPackPaths.from_root(str(root)), trigger_max_buffer_seconds=None
    )

    assert [item.event_id for item in pack.triggers] == ["buffered-trigger"]
    assert pack.report.buffered_dropped == 0


def test_pack_can_filter_the_history_explicitly(tmp_path):
    """The history filter stays available, but is off by default."""
    root = tmp_path / "pack"
    (root / "data").mkdir(parents=True)
    (root / "replay").mkdir(parents=True)

    write_jsonl(
        root / "data" / "events.jsonl",
        [
            event(
                "history",
                occurred_at="2026-09-30T09:00:00.000Z",
                sent_at="2026-09-30T09:20:00.000Z",
                received_at="2026-09-30T09:20:00.000Z",
            )
        ],
    )
    write_jsonl(root / "replay" / "triggers.jsonl", [])
    write_jsonl(root / "replay" / "live_events.jsonl", [])

    pack = load_data_pack(
        DataPackPaths.from_root(str(root)), max_buffer_seconds=600
    )

    assert pack.events == []
    assert pack.report.buffered_dropped == 1


def test_paths_are_configurable(tmp_path):
    """Every file location can be overridden without touching the defaults."""
    root = tmp_path / "custom"
    (root / "a").mkdir(parents=True)
    (root / "b").mkdir(parents=True)

    write_csv(
        root / "a" / "titles.csv",
        CATALOGUE_HEADER,
        [["T9", "custom-slug", "Custom", "d1", "hin", "film", "Drama", "", "", "", "false", ""]],
    )
    write_csv(
        root / "b" / "eps.csv",
        ["episode_id", "content_slug", "season", "episode", "minutes"],
        [["ep-1", "custom-slug", "1", "1", "10"]],
    )
    write_jsonl(root / "b" / "ev.jsonl", [event("x1")])

    custom = DataPackPaths(
        root=str(root),
        catalogue=os.path.join("a", "titles.csv"),
        episodes=os.path.join("b", "eps.csv"),
        events=os.path.join("b", "ev.jsonl"),
    )
    pack = load_data_pack(custom, load_replay=False)

    assert pack.title_by_id("T9").content_slug == "custom-slug"
    assert pack.episode_by_id("ep-1") is not None
    assert [item.event_id for item in pack.events] == ["x1"]


# ---------------------------------------------------------------------------
# Duplicates
# ---------------------------------------------------------------------------


def test_duplicate_event_ids_are_collapsed(tmp_path):
    path = write_jsonl(
        tmp_path / "events.jsonl",
        [
            event("dup", received_at="2026-09-30T10:00:00.000Z"),
            event("dup", received_at="2026-09-30T10:00:00.000Z"),
            event("dup", received_at="2026-09-30T10:00:00.000Z"),
            event("other", received_at="2026-09-30T10:01:00.000Z"),
        ],
    )

    report = LoadReport()
    events = load_events(str(path), report)

    assert [item.event_id for item in events] == ["dup", "other"]
    assert report.duplicates_dropped == 2
    assert report.rows_loaded == 2


def test_duplicate_detection_is_shared_across_files(tmp_path):
    """The same event delivered in two files is only counted once."""
    first = write_jsonl(tmp_path / "a.jsonl", [event("same")])
    second = write_jsonl(tmp_path / "b.jsonl", [event("same"), event("other")])

    seen: set = set()
    a_events = load_events(str(first), seen_event_ids=seen)
    b_events = load_events(str(second), seen_event_ids=seen)

    assert [item.event_id for item in a_events] == ["same"]
    assert [item.event_id for item in b_events] == ["other"]
    assert seen == {"same", "other"}


def test_buffered_drop_consumes_the_event_id(tmp_path):
    """A buffered-out event is one dropped event, however often it is sent.

    The simulator keys triggers by ``event_id`` before it applies the
    buffering rule, so a re-delivered copy of an over-buffered event is a
    duplicate, not a second drop.
    """
    path = write_jsonl(
        tmp_path / "events.jsonl",
        [
            event(
                "same",
                occurred_at="2026-09-30T09:00:00.000Z",
                sent_at="2026-09-30T09:30:00.000Z",
                received_at="2026-09-30T09:30:00.000Z",
            ),
            event(
                "same",
                occurred_at="2026-09-30T09:00:00.000Z",
                sent_at="2026-09-30T09:00:10.000Z",
                received_at="2026-09-30T09:00:20.000Z",
            ),
        ],
    )

    report = LoadReport()
    events = load_events(str(path), report, max_buffer_seconds=600)

    assert [item.event_id for item in events] == []
    assert report.buffered_dropped == 1
    assert report.duplicates_dropped == 1


def test_duplicate_episode_ids_are_dropped(tmp_path):
    path = write_csv(
        tmp_path / "episodes.csv",
        ["episode_id", "content_slug", "season", "episode", "minutes"],
        [
            ["ep-1", "slug", "1", "1", "10"],
            ["ep-1", "slug", "1", "1", "10"],
            ["ep-2", "slug", "1", "2", "10"],
        ],
    )

    report = LoadReport()
    episodes = load_episodes(str(path), report)

    assert [episode.episode_id for episode in episodes["slug"]] == ["ep-1", "ep-2"]
    assert report.duplicates_dropped == 1


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------


def test_events_are_sorted_by_received_at(tmp_path):
    path = write_jsonl(
        tmp_path / "events.jsonl",
        [
            event("late", received_at="2026-09-30T12:00:00.000Z"),
            event("early", received_at="2026-09-30T08:00:00.000Z"),
            event("middle", received_at="2026-09-30T10:00:00.000Z"),
        ],
    )

    events = load_events(str(path))

    assert [item.event_id for item in events] == ["early", "middle", "late"]


def test_ordering_uses_received_at_not_phone_clock(tmp_path):
    """A phone with a wrong clock must not reorder the server timeline."""
    path = write_jsonl(
        tmp_path / "events.jsonl",
        [
            event(
                "skewed",
                occurred_at="2026-09-30T23:59:00.000Z",
                sent_at="2026-09-30T23:59:00.000Z",
                received_at="2026-09-30T09:00:00.000Z",
            ),
            event(
                "honest",
                occurred_at="2026-09-30T08:59:00.000Z",
                sent_at="2026-09-30T08:59:00.000Z",
                received_at="2026-09-30T09:00:01.000Z",
            ),
        ],
    )

    events = load_events(str(path))

    assert [item.event_id for item in events] == ["skewed", "honest"]


def test_events_with_identical_received_at_are_stable_by_id(tmp_path):
    path = write_jsonl(
        tmp_path / "events.jsonl",
        [
            event("b", received_at="2026-09-30T10:00:00.000Z"),
            event("a", received_at="2026-09-30T10:00:00.000Z"),
        ],
    )

    events = load_events(str(path))

    assert [item.event_id for item in events] == ["a", "b"]


# ---------------------------------------------------------------------------
# Buffering
# ---------------------------------------------------------------------------


def test_buffered_event_beyond_the_limit_is_dropped(tmp_path):
    """With a limit in force, events the phone buffered too long are dropped."""
    path = write_jsonl(
        tmp_path / "events.jsonl",
        [
            event(
                "offline",
                occurred_at="2026-09-30T09:00:00.000Z",
                sent_at="2026-09-30T09:20:00.000Z",  # 1200s later
                received_at="2026-09-30T09:20:00.000Z",
            ),
            event(
                "ok",
                occurred_at="2026-09-30T09:00:00.000Z",
                sent_at="2026-09-30T09:01:00.000Z",  # 60s later
                received_at="2026-09-30T09:01:00.000Z",
            ),
        ],
    )

    report = LoadReport()
    events = load_events(str(path), report, max_buffer_seconds=600)

    assert [item.event_id for item in events] == ["ok"]
    assert report.rows_skipped == 1
    assert any("buffered" in message for message in report.errors)


def test_history_events_are_not_dropped_for_buffering_by_default(tmp_path):
    """Authoritative history is kept even when the phone buffered it.

    Goal evaluation needs the complete record: a late-flushed event is still
    evidence, so the loader must not discard it.
    """
    path = write_jsonl(
        tmp_path / "events.jsonl",
        [
            event(
                "offline",
                occurred_at="2026-09-30T09:00:00.000Z",
                sent_at="2026-09-30T09:20:00.000Z",  # 1200s later
                received_at="2026-09-30T09:20:00.000Z",
            )
        ],
    )

    report = LoadReport()
    events = load_events(str(path), report)

    assert [item.event_id for item in events] == ["offline"]
    assert report.buffered_dropped == 0
    assert report.rows_skipped == 0
    assert events[0].is_over_buffered is True


def test_buffer_exactly_at_the_limit_is_kept(tmp_path):
    """The rule is ``> 600`` seconds, so exactly 600 is still delivered."""
    path = write_jsonl(
        tmp_path / "events.jsonl",
        [
            event(
                "edge",
                occurred_at="2026-09-30T09:00:00.000Z",
                sent_at="2026-09-30T09:10:00.000Z",
                received_at="2026-09-30T09:10:00.000Z",
            )
        ],
    )

    events = load_events(str(path), max_buffer_seconds=600)

    assert [item.event_id for item in events] == ["edge"]
    assert events[0].buffer_seconds == DEFAULT_MAX_BUFFER_SECONDS
    assert events[0].is_over_buffered is False


def test_buffer_limit_is_configurable(tmp_path):
    path = write_jsonl(
        tmp_path / "events.jsonl",
        [
            event(
                "small",
                occurred_at="2026-09-30T09:00:00.000Z",
                sent_at="2026-09-30T09:00:30.000Z",
                received_at="2026-09-30T09:00:30.000Z",
            )
        ],
    )

    assert load_events(str(path), max_buffer_seconds=10) == []
    assert len(load_events(str(path), max_buffer_seconds=None)) == 1
    assert len(load_events(str(path), max_buffer_seconds=600)) == 1


def test_negative_buffer_is_not_treated_as_buffered(tmp_path):
    """A phone clock running fast produces a negative delay: keep the event."""
    path = write_jsonl(
        tmp_path / "events.jsonl",
        [
            event(
                "fast-clock",
                occurred_at="2026-09-30T09:00:30.000Z",
                sent_at="2026-09-30T09:00:00.000Z",
                received_at="2026-09-30T09:00:31.000Z",
            )
        ],
    )

    events = load_events(str(path))

    assert len(events) == 1
    assert events[0].buffer_seconds == -30.0
    assert events[0].is_over_buffered is False


def test_event_without_timestamps_is_not_buffered(tmp_path):
    """A missing phone timestamp means no measurable delay, so keep it."""
    record = event("no-phone-time")
    record["occurred_at"] = ""
    record["sent_at"] = ""
    path = write_jsonl(tmp_path / "events.jsonl", [record])

    report = LoadReport()
    events = load_events(str(path), report)

    assert len(events) == 1
    assert events[0].buffer_seconds is None
    assert events[0].is_over_buffered is False


def test_is_buffered_beyond_accepts_a_custom_limit(tmp_path):
    path = write_jsonl(
        tmp_path / "events.jsonl",
        [
            event(
                "late",
                occurred_at="2026-09-30T09:00:00.000Z",
                sent_at="2026-09-30T09:01:00.000Z",
                received_at="2026-09-30T09:01:00.000Z",
            )
        ],
    )

    (loaded,) = load_events(str(path))

    assert loaded.is_over_buffered is False
    assert loaded.is_buffered_beyond(30) is True
    assert loaded.is_buffered_beyond(60) is False
    assert loaded.is_buffered_beyond(None) is False


# ---------------------------------------------------------------------------
# Missing / malformed records
# ---------------------------------------------------------------------------


def test_event_missing_a_required_field_is_skipped(tmp_path):
    broken = event("broken")
    del broken["user_id"]
    path = write_jsonl(tmp_path / "events.jsonl", [event("good"), broken])

    report = LoadReport()
    events = load_events(str(path), report)

    assert [item.event_id for item in events] == ["good"]
    assert report.rows_skipped == 1
    assert any("user_id" in message for message in report.errors)


def test_event_with_blank_required_field_is_skipped(tmp_path):
    broken = event("broken")
    broken["received_at"] = ""
    path = write_jsonl(tmp_path / "events.jsonl", [broken])

    report = LoadReport()
    events = load_events(str(path), report)

    assert events == []
    assert report.rows_skipped == 1


def test_event_with_unparsable_timestamp_is_skipped(tmp_path):
    broken = event("broken", received_at="not-a-timestamp")
    path = write_jsonl(tmp_path / "events.jsonl", [event("good"), broken])

    report = LoadReport()
    events = load_events(str(path), report)

    assert [item.event_id for item in events] == ["good"]
    assert report.rows_skipped == 1


def test_invalid_json_line_is_skipped(tmp_path):
    path = write_jsonl(
        tmp_path / "events.jsonl",
        [event("good"), "{not json at all", json.dumps(event("good2"))],
    )

    report = LoadReport()
    events = load_events(str(path), report)

    assert [item.event_id for item in events] == ["good", "good2"]
    assert report.rows_skipped == 1
    assert any("invalid JSON" in message for message in report.errors)


def test_json_line_that_is_not_an_object_is_skipped(tmp_path):
    path = write_jsonl(tmp_path / "events.jsonl", ["[1, 2, 3]", json.dumps(event("good"))])

    report = LoadReport()
    events = load_events(str(path), report)

    assert [item.event_id for item in events] == ["good"]
    assert report.rows_skipped == 1


def test_missing_file_is_reported_not_raised(tmp_path):
    report = LoadReport()
    events = load_events(str(tmp_path / "nope.jsonl"), report)

    assert events == []
    assert len(report.missing_files) == 1
    assert report.errors


def test_catalogue_row_missing_required_column_is_skipped(tmp_path):
    path = write_csv(
        tmp_path / "catalogue.csv",
        CATALOGUE_HEADER,
        [
            ["T0001", "slug-1", "One", "d1", "hin", "film", "Drama", "", "", "", "false", ""],
            ["", "slug-2", "Two", "d1", "hin", "film", "Drama", "", "", "", "false", ""],
            ["T0003", "", "Three", "d1", "hin", "film", "Drama", "", "", "", "false", ""],
        ],
    )

    report = LoadReport()
    titles = load_catalogue(str(path), report)

    assert set(titles) == {"T0001"}
    assert report.rows_skipped == 2


def test_episode_row_with_non_integer_numbers_is_skipped(tmp_path):
    path = write_csv(
        tmp_path / "episodes.csv",
        ["episode_id", "content_slug", "season", "episode", "minutes"],
        [
            ["ep-1", "slug", "one", "1", "10"],
            ["ep-2", "slug", "1", "2", "10"],
        ],
    )

    report = LoadReport()
    episodes = load_episodes(str(path), report)

    assert [episode.episode_id for episode in episodes["slug"]] == ["ep-2"]
    assert report.rows_skipped == 1


def test_profile_row_with_unparsable_updated_at_is_skipped(tmp_path):
    header = [
        "user_id",
        "signup_at",
        "subscription_status",
        "plan_started_at",
        "plan_ends_at",
        "preferred_dialect",
        "preferred_language",
        "notifications_opt_in",
        "whatsapp_opt_in",
        "platform",
        "updated_at",
        "snapshot_at",
    ]
    path = write_csv(
        tmp_path / "profiles.csv",
        header,
        [
            ["U-1", "", "never", "", "", "d1", "hin", "true", "true", "android", "not-a-date", ""],
            ["U-2", "", "never", "", "", "d1", "hin", "true", "true", "android", "2026-01-01T00:00:00.000Z", ""],
        ],
    )

    report = LoadReport()
    profiles = load_profiles(str(path), report)

    assert set(profiles) == {"U-2"}
    assert report.rows_skipped == 1


# ---------------------------------------------------------------------------
# Empty inputs
# ---------------------------------------------------------------------------


def test_empty_events_file_yields_no_events(tmp_path):
    path = write_jsonl(tmp_path / "events.jsonl", [])

    report = LoadReport()
    events = load_events(str(path), report)

    assert events == []
    assert report.rows_seen == 0
    assert report.rows_skipped == 0
    assert report.missing_files == []


def test_empty_csv_files_yield_empty_collections(tmp_path):
    write_csv(tmp_path / "catalogue.csv", CATALOGUE_HEADER, [])
    write_csv(
        tmp_path / "episodes.csv",
        ["episode_id", "content_slug", "season", "episode", "minutes"],
        [],
    )
    write_csv(tmp_path / "profiles.csv", ["user_id", "updated_at"], [])
    write_csv(tmp_path / "arms.csv", ["user_id", "arm"], [])

    assert load_catalogue(str(tmp_path / "catalogue.csv")) == {}
    assert load_episodes(str(tmp_path / "episodes.csv")) == {}
    assert load_profiles(str(tmp_path / "profiles.csv")) == {}
    assert load_arms(str(tmp_path / "arms.csv")) == {}


def test_pack_with_nothing_in_it_still_loads(tmp_path):
    empty = DataPackPaths.from_root(str(tmp_path / "does-not-exist"))
    report = LoadReport()

    pack = load_data_pack(empty)

    assert pack.titles == {}
    assert pack.episodes == {}
    assert pack.profiles == {}
    assert pack.arms == {}
    assert pack.events == []
    assert pack.report.missing_files
    assert pack.report.errors


def test_only_empty_lines_in_jsonl_is_not_an_error(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text("\n\n   \n", encoding="utf-8")

    report = LoadReport()
    events = load_events(str(path), report)

    assert events == []
    assert report.rows_skipped == 0


# ---------------------------------------------------------------------------
# Latest-profile selection
# ---------------------------------------------------------------------------


def test_latest_profile_row_wins_by_updated_at(paths):
    """Several rows exist per user; the newest ``updated_at`` is the truth."""
    profiles = load_profiles(paths.resolve(paths.profiles))

    profile = profiles["U-00001"]
    assert profile.updated_at.isoformat() == "2026-08-20T00:00:00+00:00"
    assert profile.subscription_status == "lapsed"
    assert profile.preferred_dialect == "d2"
    assert profile.platform == "ios"
    assert profile.whatsapp_opt_in is False


def test_latest_profile_selection_ignores_row_order(tmp_path):
    header = ["user_id", "subscription_status", "updated_at"]
    rows = [
        ["U-1", "old", "2026-01-01T00:00:00.000Z"],
        ["U-1", "new", "2026-05-01T00:00:00.000Z"],
        ["U-1", "middle", "2026-03-01T00:00:00.000Z"],
    ]
    forward = write_csv(tmp_path / "fwd.csv", header, rows)
    backward = write_csv(tmp_path / "bwd.csv", header, list(reversed(rows)))

    assert load_profiles(forward)["U-1"].subscription_status == "new"
    assert load_profiles(backward)["U-1"].subscription_status == "new"


def test_duplicate_profiles_are_counted(paths):
    report = LoadReport()
    load_profiles(paths.resolve(paths.profiles), report)

    # U-00001 appears twice in the fixture.
    assert report.duplicates_dropped == 1


def test_parse_timestamp_handles_formats():
    assert parse_timestamp(None) is None
    assert parse_timestamp("") is None
    assert parse_timestamp("   ") is None
    assert parse_timestamp("2026-09-30T10:00:00.000Z").isoformat() == (
        "2026-09-30T10:00:00+00:00"
    )
    assert parse_timestamp("2026-09-30T10:00:00+05:30").isoformat() == (
        "2026-09-30T04:30:00+00:00"
    )
    naive = parse_timestamp("2026-09-30T10:00:00")
    assert naive.tzinfo is not None
    assert naive.isoformat() == "2026-09-30T10:00:00+00:00"


def test_parse_timestamp_rejects_garbage():
    from src.data_loader import MalformedRecord

    with pytest.raises(MalformedRecord):
        parse_timestamp("yesterday")


# ---------------------------------------------------------------------------
# The shipped data pack (skipped when it is not available)
# ---------------------------------------------------------------------------


requires_real_pack = pytest.mark.skipif(
    not os.path.isdir(REAL_PACK), reason="shipped data pack is not present"
)


@requires_real_pack
def test_real_pack_loads_without_errors():
    pack = load_data_pack(DataPackPaths.from_root(REAL_PACK))

    assert pack.titles
    assert pack.episodes
    assert pack.profiles
    assert pack.arms
    assert pack.events
    assert pack.report.missing_files == []
    # No broken records: every skip must be a deliberate simulator rule.
    assert pack.report.rows_invalid == 0
    # The authoritative history is complete: only replay-day triggers are
    # dropped for buffering, and only under the simulator's 600s rule.
    assert pack.report.buffered_dropped == 424
    assert pack.report.rows_skipped == 424
    assert all("triggers.jsonl" in message for message in pack.report.errors)
    # The history keeps every over-buffered event: only the triggers are
    # filtered, so goal evaluation still sees the complete record.
    assert [event for event in pack.events if event.is_over_buffered]


@requires_real_pack
def test_real_pack_events_are_deduplicated_and_ordered():
    pack = load_data_pack(
        DataPackPaths.from_root(REAL_PACK), load_replay=False
    )

    ids = [event.event_id for event in pack.events]
    assert len(ids) == len(set(ids))
    timestamps = [event.received_at for event in pack.events]
    assert timestamps == sorted(timestamps)


@requires_real_pack
def test_real_pack_lookups_work():
    pack = load_data_pack(DataPackPaths.from_root(REAL_PACK))

    title_id = next(iter(pack.titles))
    title = pack.title_by_id(title_id)
    assert title is not None
    assert pack.title_by_slug(title.content_slug) is not None

    user_id = next(iter(pack.profiles))
    assert pack.profile_for(user_id).user_id == user_id


@requires_real_pack
def test_real_pack_profile_rows_are_latest_by_updated_at():
    """The snapshot holds several rows per user; the latest one must win."""
    import csv as _csv

    latest = {}
    with open(
        os.path.join(REAL_PACK, "data", "profiles_snapshot.csv"), encoding="utf-8"
    ) as handle:
        for row in _csv.DictReader(handle):
            current = latest.get(row["user_id"])
            if current is None or row["updated_at"] > current:
                latest[row["user_id"]] = row["updated_at"]

    pack = load_data_pack(DataPackPaths.from_root(REAL_PACK), load_replay=False)
    for user_id, updated_at in list(latest.items())[:200]:
        profile = pack.profile_for(user_id)
        assert profile is not None
        assert profile.updated_at.isoformat().startswith(
            updated_at[:19]
        ), user_id


@requires_real_pack
def test_real_pack_is_not_modified(tmp_path):
    """Loading must not write anything into the data pack."""
    before = {}
    for folder, _dirs, names in os.walk(REAL_PACK):
        for name in names:
            full = os.path.join(folder, name)
            before[full] = os.path.getmtime(full)

    load_data_pack(DataPackPaths.from_root(REAL_PACK), load_replay=False)

    after = {}
    for folder, _dirs, names in os.walk(REAL_PACK):
        for name in names:
            full = os.path.join(folder, name)
            after[full] = os.path.getmtime(full)

    assert before == after
