"""Configurable, defensive loader for the One User, One Agent data pack.

The loader never mutates the data pack: it only reads files and returns
in-memory structures.  Every path is configurable through
:class:`DataPackPaths` so the same code can be pointed at the shipped pack, at
a test fixture, or at a future pack revision.

Rules that follow the data pack's own documentation:

* ``received_at`` is set by the server clock and is the only trustworthy
  timestamp.  Events are therefore ordered by ``received_at``.
* ``occurred_at`` and ``sent_at`` come from the phone clock and can be wrong.
  A phone that was offline buffers events and flushes them later; the
  simulator drops any *replay-day trigger* whose phone-side buffering delay
  (``sent_at - occurred_at``) exceeds 600 seconds.  The authoritative history
  in ``data/events.jsonl`` and the scripted replay-day live events are never
  dropped for buffering: goal evaluation needs the complete record.
* An event can be delivered more than once with the same ``event_id``; the
  loader de-duplicates globally by ``event_id``.  The id is claimed before
  the buffering rule runs, so a re-delivered copy of an over-buffered event
  counts as one dropped event rather than several.
* ``event_id``, ``user_id``, ``event`` and ``received_at`` are required for an
  event.  The phone-clock timestamps are optional: when they are missing the
  buffering delay simply cannot be measured, so the event is kept.
* Missing or malformed records are skipped and reported, never fatal.
"""

from __future__ import annotations

import csv
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence

__all__ = [
    "DataPackPaths",
    "DataPack",
    "Event",
    "Episode",
    "LoadReport",
    "MalformedRecord",
    "Profile",
    "Title",
    "DEFAULT_MAX_BUFFER_SECONDS",
    "EVENT_REQUIRED_COLUMNS",
    "load_arms",
    "load_catalogue",
    "load_data_pack",
    "load_episodes",
    "load_events",
    "load_profiles",
    "parse_timestamp",
]

DEFAULT_MAX_BUFFER_SECONDS = 600

EVENT_REQUIRED_COLUMNS: Sequence[str] = (
    "event_id",
    "user_id",
    "event",
    "received_at",
)

PROFILE_REQUIRED_COLUMNS: Sequence[str] = ("user_id", "updated_at")

CATALOGUE_REQUIRED_COLUMNS: Sequence[str] = ("title_id", "content_slug")

EPISODE_REQUIRED_COLUMNS: Sequence[str] = ("episode_id", "content_slug", "season", "episode")

ARM_REQUIRED_COLUMNS: Sequence[str] = ("user_id", "arm")


class MalformedRecord(ValueError):
    """Raised internally when a record cannot be turned into a valid object."""


def parse_timestamp(value: Any) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp into an aware UTC ``datetime``.

    Returns ``None`` for empty/``None`` input.  Naive timestamps are assumed
    to be UTC, matching the data pack's "everything is UTC" convention.
    Raises :class:`MalformedRecord` when the value is present but unparsable.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if not text:
            return None
        candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError as exc:
            raise MalformedRecord(f"unparsable timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class DataPackPaths:
    """Filesystem layout of a data pack.  Every path is overridable."""

    root: str
    catalogue: str = os.path.join("data", "catalogue.csv")
    episodes: str = os.path.join("data", "episodes.csv")
    profiles: str = os.path.join("data", "profiles_snapshot.csv")
    events: str = os.path.join("data", "events.jsonl")
    triggers: str = os.path.join("replay", "triggers.jsonl")
    live_events: str = os.path.join("replay", "live_events.jsonl")
    control_events: str = os.path.join("replay", "control_events.jsonl")
    arms: str = os.path.join("replay", "arms.csv")

    def resolve(self, relative: str) -> str:
        """Return the absolute path of ``relative`` inside this pack."""
        return os.path.join(self.root, relative)

    @classmethod
    def from_root(cls, root: str) -> "DataPackPaths":
        return cls(root=os.fspath(root))


@dataclass
class LoadReport:
    """Counters describing what a load actually did."""

    rows_seen: int = 0
    rows_loaded: int = 0
    rows_skipped: int = 0
    rows_invalid: int = 0
    buffered_dropped: int = 0
    duplicates_dropped: int = 0
    missing_files: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    def record_skip(self, message: str, *, invalid: bool = True) -> None:
        """Record a skipped row.  ``invalid`` marks genuinely broken records."""
        self.rows_skipped += 1
        if invalid:
            self.rows_invalid += 1
        if len(self.errors) < 100:
            self.errors.append(message)

    def merge(self, other: "LoadReport") -> None:
        self.rows_seen += other.rows_seen
        self.rows_loaded += other.rows_loaded
        self.rows_skipped += other.rows_skipped
        self.rows_invalid += other.rows_invalid
        self.buffered_dropped += other.buffered_dropped
        self.duplicates_dropped += other.duplicates_dropped
        self.missing_files.extend(other.missing_files)
        for message in other.errors:
            if len(self.errors) < 100:
                self.errors.append(message)


@dataclass(frozen=True)
class Event:
    """A single normalised event record."""

    event_id: str
    user_id: str
    event: str
    occurred_at: Optional[datetime]
    sent_at: Optional[datetime]
    received_at: datetime
    session_id: Optional[str] = None
    app_dialect: Optional[str] = None
    app_language: Optional[str] = None
    platform: Optional[str] = None
    app_version: Optional[str] = None
    properties: Mapping[str, Any] = field(default_factory=dict)
    deliver_at: Optional[datetime] = None
    source: str = ""

    @property
    def buffer_seconds(self) -> Optional[float]:
        """Phone-side buffering delay in seconds (``sent_at - occurred_at``).

        ``None`` when either timestamp is missing.  The simulator treats a
        delay greater than 600 seconds as an offline flush that must be
        ignored.
        """
        if self.occurred_at is None or self.sent_at is None:
            return None
        return (self.sent_at - self.occurred_at).total_seconds()

    @property
    def is_over_buffered(self) -> bool:
        """True when the phone buffered this event for longer than the limit.

        Uses the default 600-second simulator limit.  Pass an explicit
        ``limit`` to check against a different threshold.
        """
        return self.is_buffered_beyond(DEFAULT_MAX_BUFFER_SECONDS)

    def is_buffered_beyond(self, limit: Optional[float]) -> bool:
        """True when the buffering delay exceeds ``limit`` seconds.

        ``limit=None`` disables the check.  A missing phone timestamp means
        the delay cannot be measured, so the event is never considered
        over-buffered.
        """
        if limit is None:
            return False
        delay = self.buffer_seconds
        if delay is None:
            return False
        return delay > limit


@dataclass(frozen=True)
class Title:
    """A catalogue entry."""

    title_id: str
    content_slug: str
    title: Optional[str] = None
    dialect: Optional[str] = None
    language: Optional[str] = None
    content_type: Optional[str] = None
    genres: Sequence[str] = ()
    release_date: Optional[str] = None
    available_from: Optional[datetime] = None
    available_until: Optional[datetime] = None
    is_premium: bool = False
    description: Optional[str] = None

    def is_available_at(self, when: datetime) -> bool:
        """Mirror of the simulator's availability check."""
        if self.available_from is None or when < self.available_from:
            return False
        if self.available_until is not None and when >= self.available_until:
            return False
        return True


@dataclass(frozen=True)
class Episode:
    """A single episode of a series."""

    episode_id: str
    content_slug: str
    season: int
    episode: int
    minutes: Optional[int] = None

    @property
    def sort_key(self) -> tuple:
        return (self.season, self.episode, self.episode_id)


@dataclass(frozen=True)
class Profile:
    """The latest known profile row for a user."""

    user_id: str
    updated_at: Optional[datetime]
    signup_at: Optional[datetime] = None
    subscription_status: Optional[str] = None
    plan_started_at: Optional[datetime] = None
    plan_ends_at: Optional[datetime] = None
    preferred_dialect: Optional[str] = None
    preferred_language: Optional[str] = None
    notifications_opt_in: Optional[bool] = None
    whatsapp_opt_in: Optional[bool] = None
    platform: Optional[str] = None
    snapshot_at: Optional[datetime] = None
    raw: Mapping[str, Any] = field(default_factory=dict)


def _read_csv_rows(path: str, report: LoadReport) -> Iterator[Dict[str, str]]:
    """Yield rows of a CSV file, skipping unreadable lines."""
    try:
        handle = open(path, "r", encoding="utf-8-sig", newline="")
    except OSError as exc:
        report.missing_files.append(path)
        report.errors.append(f"cannot open {path}: {exc}")
        return
    with handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            report.errors.append(f"{path}: no header row")
            return
        for row in reader:
            yield {k: v for k, v in row.items() if k is not None}


def _read_jsonl_rows(path: str, report: LoadReport) -> Iterator[Dict[str, Any]]:
    """Yield JSON objects from a JSONL file, skipping malformed lines."""
    try:
        handle = open(path, "r", encoding="utf-8")
    except OSError as exc:
        report.missing_files.append(path)
        report.errors.append(f"cannot open {path}: {exc}")
        return
    with handle:
        for number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except (ValueError, TypeError):
                report.record_skip(f"{path}:{number}: invalid JSON")
                continue
            if not isinstance(payload, dict):
                report.record_skip(f"{path}:{number}: expected a JSON object")
                continue
            yield payload


def _check_columns(
    row: Mapping[str, Any], required: Sequence[str], label: str, report: LoadReport
) -> bool:
    """Return True when ``row`` has every required column with a value."""
    missing = [column for column in required if _is_blank(row.get(column))]
    if missing:
        report.record_skip(f"{label}: missing required field(s): {', '.join(missing)}")
        return False
    return True


def _is_blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    return False


def _to_bool(value: Any) -> Optional[bool]:
    """Interpret the pack's ``"true"``/``"false"`` style boolean strings."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("true", "1", "yes", "y"):
        return True
    if text in ("false", "0", "no", "n"):
        return False
    return None


def _to_int(value: Any) -> Optional[int]:
    if value is None or (isinstance(value, str) and value.strip() == ""):
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def load_catalogue(
    path: str, report: Optional[LoadReport] = None
) -> Dict[str, Title]:
    """Load the title catalogue keyed by ``title_id``.

    Rows without a ``title_id`` or ``content_slug`` are skipped and counted
    in ``report``.  A duplicate ``title_id`` keeps the last row seen, matching
    the simulator's dictionary-building behaviour.
    """
    report = report or LoadReport()
    titles: Dict[str, Title] = {}
    for index, row in enumerate(_read_csv_rows(path, report), start=2):
        report.rows_seen += 1
        label = f"{path}:row {index}"
        if not _check_columns(row, CATALOGUE_REQUIRED_COLUMNS, label, report):
            continue
        try:
            available_from = parse_timestamp(row.get("available_from"))
            available_until = parse_timestamp(row.get("available_until"))
        except MalformedRecord as exc:
            report.record_skip(f"{label}: {exc}")
            continue
        genres_raw = row.get("genres") or ""
        genres = tuple(g.strip() for g in genres_raw.split("|") if g.strip())
        title = Title(
            title_id=str(row["title_id"]).strip(),
            content_slug=str(row["content_slug"]).strip(),
            title=(row.get("title") or None),
            dialect=(row.get("dialect") or None),
            language=(row.get("language") or None),
            content_type=(row.get("content_type") or None),
            genres=genres,
            release_date=(row.get("release_date") or None),
            available_from=available_from,
            available_until=available_until,
            is_premium=bool(_to_bool(row.get("is_premium"))),
            description=(row.get("description") or None),
        )
        titles[title.title_id] = title
        report.rows_loaded += 1
    return titles


def load_episodes(
    path: str, report: Optional[LoadReport] = None
) -> Dict[str, List[Episode]]:
    """Load episodes grouped by ``content_slug``, sorted by season/episode."""
    report = report or LoadReport()
    episodes: Dict[str, List[Episode]] = {}
    seen_ids: set = set()
    for index, row in enumerate(_read_csv_rows(path, report), start=2):
        report.rows_seen += 1
        label = f"{path}:row {index}"
        if not _check_columns(row, EPISODE_REQUIRED_COLUMNS, label, report):
            continue
        season = _to_int(row.get("season"))
        episode_number = _to_int(row.get("episode"))
        if season is None or episode_number is None:
            report.record_skip(f"{label}: season/episode must be integers")
            continue
        episode_id = str(row["episode_id"]).strip()
        if episode_id in seen_ids:
            report.duplicates_dropped += 1
            continue
        seen_ids.add(episode_id)
        episode = Episode(
            episode_id=episode_id,
            content_slug=str(row["content_slug"]).strip(),
            season=season,
            episode=episode_number,
            minutes=_to_int(row.get("minutes")),
        )
        episodes.setdefault(episode.content_slug, []).append(episode)
        report.rows_loaded += 1
    for bucket in episodes.values():
        bucket.sort(key=lambda item: item.sort_key)
    return episodes


def load_profiles(
    path: str, report: Optional[LoadReport] = None
) -> Dict[str, Profile]:
    """Load the latest profile row per user.

    The snapshot file contains one row per user *change*, so several rows can
    share a ``user_id``.  The winning row is the one with the greatest
    ``updated_at`` (``snapshot_at`` is constant across the file and cannot
    break ties).  Rows with an unparsable ``updated_at`` are skipped.
    """
    report = report or LoadReport()
    profiles: Dict[str, Profile] = {}
    for index, row in enumerate(_read_csv_rows(path, report), start=2):
        report.rows_seen += 1
        label = f"{path}:row {index}"
        if not _check_columns(row, PROFILE_REQUIRED_COLUMNS, label, report):
            continue
        try:
            updated_at = parse_timestamp(row.get("updated_at"))
            signup_at = parse_timestamp(row.get("signup_at"))
            plan_started_at = parse_timestamp(row.get("plan_started_at"))
            plan_ends_at = parse_timestamp(row.get("plan_ends_at"))
            snapshot_at = parse_timestamp(row.get("snapshot_at"))
        except MalformedRecord as exc:
            report.record_skip(f"{label}: {exc}")
            continue
        user_id = str(row["user_id"]).strip()
        existing = profiles.get(user_id)
        if existing is not None:
            report.duplicates_dropped += 1
            current = existing.updated_at
            if current is not None and (updated_at is None or updated_at <= current):
                continue
        profiles[user_id] = Profile(
            user_id=user_id,
            updated_at=updated_at,
            signup_at=signup_at,
            subscription_status=(row.get("subscription_status") or None),
            plan_started_at=plan_started_at,
            plan_ends_at=plan_ends_at,
            preferred_dialect=(row.get("preferred_dialect") or None),
            preferred_language=(row.get("preferred_language") or None),
            notifications_opt_in=_to_bool(row.get("notifications_opt_in")),
            whatsapp_opt_in=_to_bool(row.get("whatsapp_opt_in")),
            platform=(row.get("platform") or None),
            snapshot_at=snapshot_at,
            raw=dict(row),
        )
        report.rows_loaded += 1
    return profiles


def load_arms(
    path: str, report: Optional[LoadReport] = None
) -> Dict[str, str]:
    """Load the replay arm assignment per user."""
    report = report or LoadReport()
    arms: Dict[str, str] = {}
    for index, row in enumerate(_read_csv_rows(path, report), start=2):
        report.rows_seen += 1
        label = f"{path}:row {index}"
        if not _check_columns(row, ARM_REQUIRED_COLUMNS, label, report):
            continue
        user_id = str(row["user_id"]).strip()
        arms[user_id] = str(row["arm"]).strip()
        report.rows_loaded += 1
    return arms


def _event_from_row(
    row: Mapping[str, Any], source: str, label: str, report: LoadReport
) -> Optional[Event]:
    """Build an :class:`Event` from a raw record, or ``None`` if unusable."""
    if not _check_columns(row, EVENT_REQUIRED_COLUMNS, label, report):
        return None
    try:
        occurred_at = parse_timestamp(row.get("occurred_at"))
        sent_at = parse_timestamp(row.get("sent_at"))
        received_at = parse_timestamp(row.get("received_at"))
        deliver_at = parse_timestamp(row.get("deliver_at"))
    except MalformedRecord as exc:
        report.record_skip(f"{label}: {exc}")
        return None
    if received_at is None:
        report.record_skip(f"{label}: received_at is required")
        return None
    properties = row.get("properties")
    if not isinstance(properties, dict):
        properties = {} if properties in (None, "") else {"value": properties}
    return Event(
        event_id=str(row["event_id"]).strip(),
        user_id=str(row["user_id"]).strip(),
        event=str(row["event"]).strip(),
        occurred_at=occurred_at,
        sent_at=sent_at,
        received_at=received_at,
        session_id=(str(row["session_id"]).strip() if row.get("session_id") else None),
        app_dialect=(str(row["app_dialect"]).strip() if row.get("app_dialect") else None),
        app_language=(str(row["app_language"]).strip() if row.get("app_language") else None),
        platform=(str(row["platform"]).strip() if row.get("platform") else None),
        app_version=(str(row["app_version"]).strip() if row.get("app_version") else None),
        properties=properties,
        deliver_at=deliver_at,
        source=source,
    )


def load_events(
    path: str,
    report: Optional[LoadReport] = None,
    *,
    source: str = "",
    max_buffer_seconds: Optional[int] = None,
    seen_event_ids: Optional[set] = None,
) -> List[Event]:
    """Load events from a JSONL file.

    The returned list is de-duplicated by ``event_id`` and sorted by
    ``received_at`` (the trustworthy server timestamp).

    ``max_buffer_seconds`` is ``None`` by default: every record is kept.
    Only the replay-day *triggers* are subject to the simulator's
    600-second buffering rule, and :func:`load_data_pack` opts into it
    explicitly for that file.  Authoritative history and scripted live
    events are never dropped for buffering, because a goal that needs the
    evidence must still see it.

    ``seen_event_ids`` lets a caller de-duplicate across several files; the
    set is updated in place.
    """
    report = report or LoadReport()
    if seen_event_ids is None:
        seen_event_ids = set()
    events: List[Event] = []
    for number, row in enumerate(_read_jsonl_rows(path, report), start=1):
        report.rows_seen += 1
        label = f"{path}:line {number}"
        event = _event_from_row(row, source, label, report)
        if event is None:
            continue
        if event.event_id in seen_event_ids:
            report.duplicates_dropped += 1
            continue
        # Claim the id before any further filtering: the simulator keys
        # triggers by ``event_id`` first and only then applies the buffering
        # rule, so a re-delivered copy of an over-buffered event counts as
        # one dropped event, not several.
        seen_event_ids.add(event.event_id)
        if max_buffer_seconds is not None:
            delay = event.buffer_seconds
            if event.is_buffered_beyond(max_buffer_seconds):
                report.buffered_dropped += 1
                report.record_skip(
                    f"{label}: buffered for {delay:.0f}s "
                    f"(> {max_buffer_seconds}s) and dropped",
                    invalid=False,
                )
                continue
        events.append(event)
        report.rows_loaded += 1
    events.sort(key=lambda item: (item.received_at, item.event_id))
    return events


@dataclass
class DataPack:
    """Everything the loader read from a data pack, plus handy lookups."""

    paths: DataPackPaths
    titles: Dict[str, Title] = field(default_factory=dict)
    episodes: Dict[str, List[Episode]] = field(default_factory=dict)
    profiles: Dict[str, Profile] = field(default_factory=dict)
    arms: Dict[str, str] = field(default_factory=dict)
    events: List[Event] = field(default_factory=list)
    triggers: List[Event] = field(default_factory=list)
    live_events: List[Event] = field(default_factory=list)
    report: LoadReport = field(default_factory=LoadReport)

    def _cached_index(self, name: str, source: Any, build) -> Dict[str, Any]:
        """Return a lazily built index, rebuilt when ``source`` changes.

        The cache is keyed on the identity *and* the size of the collection it
        was built from, so replacing or growing a collection invalidates it
        automatically.
        """
        token = (id(source), len(source))
        cached = getattr(self, "_index_cache", None)
        if cached is None:
            cached = {}
            self._index_cache = cached
        entry = cached.get(name)
        if entry is None or entry[0] != token:
            built = build()
            cached[name] = (token, built)
            return built
        return entry[1]

    def _slug_index(self) -> Dict[str, Title]:
        """Return (and cache) the ``content_slug`` -> :class:`Title` index."""

        def build() -> Dict[str, Title]:
            index: Dict[str, Title] = {}
            for title in self.titles.values():
                index.setdefault(title.content_slug, title)
            return index

        return self._cached_index("slug", self.titles, build)

    def _episode_index(self) -> Dict[str, Episode]:
        """Return (and cache) the ``episode_id`` -> :class:`Episode` index."""

        def build() -> Dict[str, Episode]:
            index: Dict[str, Episode] = {}
            for bucket in self.episodes.values():
                for episode in bucket:
                    index.setdefault(episode.episode_id, episode)
            return index

        return self._cached_index("episode", self.episodes, build)

    def _user_event_index(self) -> Dict[str, List[Event]]:
        """Return (and cache) the ``user_id`` -> events index."""

        def build() -> Dict[str, List[Event]]:
            index: Dict[str, List[Event]] = {}
            for event in self.events:
                index.setdefault(event.user_id, []).append(event)
            return index

        return self._cached_index("user_events", self.events, build)

    def title_by_id(self, title_id: str) -> Optional[Title]:
        """Look up a catalogue entry by its ``title_id``."""
        if title_id is None:
            return None
        return self.titles.get(str(title_id).strip())

    def title_by_slug(self, slug: str) -> Optional[Title]:
        """Look up a catalogue entry by its ``content_slug``."""
        if slug is None:
            return None
        return self._slug_index().get(str(slug).strip())

    def titles_by_slug(self) -> Dict[str, Title]:
        """Map every ``content_slug`` to its catalogue entry."""
        return dict(self._slug_index())

    def episodes_for_slug(self, slug: str) -> List[Episode]:
        """Return the episodes of a series, ordered by season and episode."""
        if slug is None:
            return []
        return list(self.episodes.get(str(slug).strip(), []))

    def episode_by_id(self, episode_id: str) -> Optional[Episode]:
        """Look up a single episode by its ``episode_id``."""
        if episode_id is None:
            return None
        return self._episode_index().get(str(episode_id).strip())

    def profile_for(self, user_id: str) -> Optional[Profile]:
        """Return the latest profile row for a user."""
        if user_id is None:
            return None
        return self.profiles.get(str(user_id).strip())

    def arm_for(self, user_id: str) -> Optional[str]:
        """Return the replay arm assigned to a user."""
        if user_id is None:
            return None
        return self.arms.get(str(user_id).strip())

    def events_for_user(self, user_id: str) -> List[Event]:
        """Return the loaded events of one user, in ``received_at`` order."""
        if user_id is None:
            return []
        key = str(user_id).strip()
        return list(self._user_event_index().get(key, ()))


def load_data_pack(
    paths: DataPackPaths,
    *,
    max_buffer_seconds: Optional[int] = None,
    trigger_max_buffer_seconds: Optional[int] = DEFAULT_MAX_BUFFER_SECONDS,
    load_history: bool = True,
    load_replay: bool = True,
) -> DataPack:
    """Load every part of a data pack.

    Each part is loaded independently and a failure in one part never stops
    the others: unreadable or malformed files are recorded in
    :attr:`DataPack.report` instead of raising.

    Parameters
    ----------
    paths:
        The pack layout to read.
    max_buffer_seconds:
        Buffering limit applied to the authoritative history
        (``data/events.jsonl``).  ``None`` (the default) keeps every
        historical event: goal evaluation must see the complete record,
        and a phone-side buffering delay never invalidates an event that
        the server already received.
    trigger_max_buffer_seconds:
        Buffering limit applied to the replay-day triggers only.  Defaults
        to :data:`DEFAULT_MAX_BUFFER_SECONDS` (600), mirroring the simulator:
        a trigger that was buffered offline for longer than that is not a
        live visit, so the agent must not act on it.  Pass ``None`` to keep
        every trigger.
    load_history:
        Read ``data/events.jsonl`` (the long behavioural history).
    load_replay:
        Read the replay day: triggers, live events and arm assignments.
    """
    report = LoadReport()
    pack = DataPack(paths=paths, report=report)

    pack.titles = load_catalogue(paths.resolve(paths.catalogue), report)
    pack.episodes = load_episodes(paths.resolve(paths.episodes), report)
    pack.profiles = load_profiles(paths.resolve(paths.profiles), report)

    if load_replay:
        pack.arms = load_arms(paths.resolve(paths.arms), report)

    seen: set = set()
    if load_history:
        pack.events = load_events(
            paths.resolve(paths.events),
            report,
            source="events",
            max_buffer_seconds=max_buffer_seconds,
            seen_event_ids=seen,
        )
    if load_replay:
        pack.triggers = load_events(
            paths.resolve(paths.triggers),
            report,
            source="triggers",
            max_buffer_seconds=trigger_max_buffer_seconds,
            seen_event_ids=seen,
        )
        pack.live_events = load_events(
            paths.resolve(paths.live_events),
            report,
            source="live_events",
            max_buffer_seconds=None,
            seen_event_ids=seen,
        )
    return pack