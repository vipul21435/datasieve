"""The run ledger: a SQLite file that remembers every content hash a pipeline has delivered.

Two tables. ``runs`` has one row per pipeline run (its id, the pipeline name,
a digest of the spec, the input path and a digest of the input's bytes, and
the counts it ended with). ``records`` has one row per ``(content digest,
record id)`` pair with the run that first saw it and the line it was on.
The :func:`~curator.stages.ledger.run_ledger` stage looks every record up
here and inserts the ones it has not seen; ``python -m curator ledger
stats|collisions`` reads the same file back.

A run's rows are committed together when the run finishes, so a run that
fails half-way leaves the ledger as it was. A run takes the ledger's write
lock when it begins, so two runs that share a ledger take turns: the second
waits up to :data:`DEFAULT_LOCK_TIMEOUT` seconds for the first to finish
and then fails with :class:`~curator.errors.LedgerError`, ledger untouched.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import Final
from typing import Self

from curator.errors import InputFileError
from curator.errors import LedgerError

DEFAULT_LEDGER_NAME: Final = "ledger.sqlite"
"""The ledger file a spec without ``path`` uses, inside ``CURATOR_WORK_DIR``."""

DEFAULT_LOCK_TIMEOUT: Final = 5.0
"""Seconds a run waits for another run to release the ledger before giving up."""

_SCHEMA: Final = """
CREATE TABLE IF NOT EXISTS runs (
    run_id        TEXT PRIMARY KEY,
    pipeline      TEXT NOT NULL,
    config_digest TEXT NOT NULL,
    source        TEXT NOT NULL,
    source_digest TEXT NOT NULL,
    started_at    TEXT NOT NULL,
    total         INTEGER NOT NULL DEFAULT 0,
    kept          INTEGER NOT NULL DEFAULT 0,
    skipped       INTEGER NOT NULL DEFAULT 0,
    rerun         INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS records (
    seq       INTEGER PRIMARY KEY,
    digest    TEXT NOT NULL,
    record_id TEXT NOT NULL,
    run_id    TEXT NOT NULL REFERENCES runs(run_id),
    line      INTEGER NOT NULL,
    UNIQUE (digest, record_id)
);
CREATE INDEX IF NOT EXISTS records_by_id ON records (record_id);
"""


@dataclass(frozen=True, slots=True)
class RunInfo:
    """Identity of one pipeline run, as the ledger records it."""

    run_id: str
    pipeline: str
    config_digest: str
    """Short SHA-256 of the spec (or stage config) as JSON, so a changed config is visible in the ledger."""
    source: str
    """The pipeline's input path, as the spec gives it."""
    source_digest: str
    """Short SHA-256 of the input file's bytes: two runs over the same bytes are re-runs of each other."""
    started_at: str
    """UTC timestamp, ISO 8601."""


def file_digest(path: Path) -> str:
    """Short (16 hex characters) SHA-256 of the bytes of ``path``."""
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
    except FileNotFoundError:
        raise InputFileError(f"input file not found: {path}", path=path) from None
    except OSError as exc:
        raise InputFileError(f"cannot read input file {path}: {exc.strerror or exc}", path=path) from exc
    return digest.hexdigest()[:16]


def config_digest(config: object) -> str:
    """Short SHA-256 of ``config`` serialised as sorted JSON.

    >>> config_digest({"b": 1, "a": [1, 2]}) == config_digest({"a": [1, 2], "b": 1})
    True
    """
    data = json.dumps(config, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(data).hexdigest()[:16]


def new_run(pipeline: str, source: Path, config: object) -> RunInfo:
    """Describe a run that starts now over ``source`` with ``config`` (any JSON-serialisable object).

    Raises :class:`~curator.errors.InputFileError` if ``source`` cannot be read.
    """
    started = datetime.now(UTC).replace(microsecond=0)
    run_id = f"{pipeline}-{started.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    return RunInfo(
        run_id=run_id,
        pipeline=pipeline,
        config_digest=config_digest(config),
        source=str(source),
        source_digest=file_digest(source),
        started_at=started.isoformat().replace("+00:00", "Z"),
    )


@dataclass(frozen=True, slots=True)
class Sighting:
    """The first time the ledger saw a content digest: which run, which record id, which input."""

    run_id: str
    record_id: str
    source: str
    source_digest: str
    line: int


@dataclass(frozen=True, slots=True)
class RunRow:
    """One row of the ``runs`` table."""

    run_id: str
    pipeline: str
    config_digest: str
    source: str
    source_digest: str
    started_at: str
    total: int
    kept: int
    skipped: int
    rerun: int

    def to_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "pipeline": self.pipeline,
            "config_digest": self.config_digest,
            "source": self.source,
            "source_digest": self.source_digest,
            "started_at": self.started_at,
            "total": self.total,
            "kept": self.kept,
            "skipped": self.skipped,
            "rerun": self.rerun,
        }


@dataclass(frozen=True, slots=True)
class Member:
    """One side of a collision: a record id or digest with the run and line that recorded it."""

    record_id: str
    digest: str
    run_id: str
    line: int

    def to_dict(self) -> dict[str, object]:
        return {"id": self.record_id, "digest": self.digest, "run_id": self.run_id, "line": self.line}


@dataclass(frozen=True, slots=True)
class Collision:
    """Records that disagree across batches: one id with several contents, or one content under several ids."""

    kind: str
    """``same_id`` (one id, different digests) or ``same_content`` (one digest, different ids)."""
    key: str
    """The shared id or digest."""
    members: tuple[Member, ...]

    def to_dict(self) -> dict[str, object]:
        return {"kind": self.kind, "key": self.key, "members": [member.to_dict() for member in self.members]}


def _wrap_sqlite(path: Path, exc: sqlite3.Error) -> LedgerError:
    if isinstance(exc, sqlite3.OperationalError) and "locked" in str(exc):
        message = f"ledger {path} is locked by another run; wait for it to finish or use another ledger path"
        return LedgerError(f"{message}: {exc}", path=path)
    return LedgerError(f"ledger {path} is unusable: {exc}", path=path)


class Ledger:
    """The ledger file, open for reading and writing. Use as a context manager; changes need :meth:`commit`.

    The first write (:meth:`begin_run`) opens an ``IMMEDIATE`` transaction,
    so the run holds the ledger's write lock until :meth:`commit` or
    :meth:`close`. Another run on the same file waits ``timeout`` seconds for
    it, then every method raises :class:`~curator.errors.LedgerError`.
    """

    def __init__(self, path: Path, *, timeout: float | None = None) -> None:
        self.path = path
        self.timeout = timeout if timeout is not None else DEFAULT_LOCK_TIMEOUT
        try:
            self._db = sqlite3.connect(path, timeout=self.timeout, isolation_level="IMMEDIATE")
            self._db.executescript(_SCHEMA)
        except sqlite3.Error as exc:
            raise _wrap_sqlite(path, exc) from exc

    @classmethod
    def open_existing(cls, path: Path) -> Ledger:
        """Open a ledger that must already exist (for reports), else raise :class:`~curator.errors.InputFileError`."""
        if not path.is_file():
            raise InputFileError(f"ledger not found: {path}", path=path)
        return cls(path)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> None:
        self.close()

    def close(self) -> None:
        """Roll back anything uncommitted and close the file."""
        self._db.close()

    def commit(self) -> None:
        """Make the run's rows visible to other runs and release the write lock."""
        try:
            self._db.commit()
        except sqlite3.Error as exc:
            raise _wrap_sqlite(self.path, exc) from exc

    def _execute(self, sql: str, params: tuple[object, ...]) -> sqlite3.Cursor:
        try:
            return self._db.execute(sql, params)
        except sqlite3.Error as exc:
            raise _wrap_sqlite(self.path, exc) from exc

    # -- writing ------------------------------------------------------------

    def begin_run(self, run: RunInfo) -> None:
        """Insert the run's row, taking the ledger's write lock until :meth:`commit`."""
        self._execute(
            "INSERT INTO runs (run_id, pipeline, config_digest, source, source_digest, started_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (run.run_id, run.pipeline, run.config_digest, run.source, run.source_digest, run.started_at),
        )

    def lookup(self, digest: str) -> Sighting | None:
        """The earliest sighting of ``digest``, or ``None`` when the content is new."""
        row = self._execute(
            "SELECT r.run_id, r.record_id, u.source, u.source_digest, r.line"
            " FROM records r JOIN runs u ON u.run_id = r.run_id WHERE r.digest = ? ORDER BY r.seq LIMIT 1",
            (digest,),
        ).fetchone()
        return Sighting(*row) if row is not None else None

    def record(self, digest: str, record_id: str, run_id: str, line: int) -> bool:
        """Remember ``(digest, record_id)`` as first seen by ``run_id``; ``False`` if the pair was already there."""
        cursor = self._execute(
            "INSERT OR IGNORE INTO records (digest, record_id, run_id, line) VALUES (?, ?, ?, ?)",
            (digest, record_id, run_id, line),
        )
        return cursor.rowcount == 1

    def finish_run(self, run_id: str, *, total: int, kept: int, skipped: int, rerun: int) -> None:
        self._execute(
            "UPDATE runs SET total = ?, kept = ?, skipped = ?, rerun = ? WHERE run_id = ?",
            (total, kept, skipped, rerun, run_id),
        )

    # -- reading ------------------------------------------------------------

    def runs(self) -> list[RunRow]:
        try:
            rows = self._db.execute(
                "SELECT run_id, pipeline, config_digest, source, source_digest, started_at,"
                " total, kept, skipped, rerun FROM runs ORDER BY rowid"
            ).fetchall()
        except sqlite3.Error as exc:
            raise _wrap_sqlite(self.path, exc) from exc
        return [RunRow(*row) for row in rows]

    def stats(self) -> dict[str, object]:
        """Totals plus one entry per run, JSON-safe."""
        try:
            records, digests, ids = self._db.execute(
                "SELECT COUNT(*), COUNT(DISTINCT digest), COUNT(DISTINCT record_id) FROM records"
            ).fetchone()
        except sqlite3.Error as exc:
            raise _wrap_sqlite(self.path, exc) from exc
        runs = self.runs()
        collisions = self.collisions()
        return {
            "ledger": str(self.path),
            "runs": len(runs),
            "records": records,
            "distinct_contents": digests,
            "distinct_ids": ids,
            "collisions": len(collisions),
            "run_list": [run.to_dict() for run in runs],
        }

    def collisions(self) -> list[Collision]:
        """Ids recorded with more than one content, and contents recorded under more than one id."""
        try:
            same_id = self._db.execute(
                "SELECT record_id FROM records GROUP BY record_id HAVING COUNT(DISTINCT digest) > 1 ORDER BY record_id"
            ).fetchall()
            same_content = self._db.execute(
                "SELECT digest FROM records GROUP BY digest HAVING COUNT(*) > 1 ORDER BY digest"
            ).fetchall()
            found = [self._collision("same_id", "record_id", key) for (key,) in same_id]
            found.extend(self._collision("same_content", "digest", key) for (key,) in same_content)
        except sqlite3.Error as exc:
            raise _wrap_sqlite(self.path, exc) from exc
        return found

    def _collision(self, kind: str, column: str, key: str) -> Collision:
        rows = self._db.execute(
            f"SELECT record_id, digest, run_id, line FROM records WHERE {column} = ? ORDER BY seq",  # noqa: S608
            (key,),
        ).fetchall()
        return Collision(kind, key, tuple(Member(*row) for row in rows))


def summarise_stats(stats: Mapping[str, object]) -> str:
    """The human-readable form of :meth:`Ledger.stats`."""
    lines = [
        f"ledger {stats['ledger']}: {stats['runs']} runs, {stats['records']} records, "
        f"{stats['distinct_contents']} distinct contents, {stats['distinct_ids']} distinct ids, "
        f"{stats['collisions']} collisions"
    ]
    run_list = stats.get("run_list")
    for run in run_list if isinstance(run_list, list) else []:
        lines.append(
            f"  {run['run_id']}  {run['started_at']}  {run['total']:>6} in -> {run['kept']:>6} kept "
            f"({run['skipped']} skipped, {run['rerun']} rerun)  {run['source']}"
        )
    return "\n".join(lines)


def summarise_collisions(collisions: list[Collision]) -> str:
    """The human-readable form of :meth:`Ledger.collisions`."""
    by_kind = {"same_id": "same id, different content", "same_content": "same content, different ids"}
    lines = []
    for kind, title in by_kind.items():
        found = [collision for collision in collisions if collision.kind == kind]
        lines.append(f"{title}: {len(found)}")
        for collision in found:
            members = ", ".join(
                f"{member.digest if kind == 'same_id' else member.record_id} (run {member.run_id}, line {member.line})"
                for member in collision.members
            )
            lines.append(f"  {collision.key}: {members}")
    return "\n".join(lines)


__all__ = [
    "DEFAULT_LEDGER_NAME",
    "DEFAULT_LOCK_TIMEOUT",
    "Collision",
    "Ledger",
    "Member",
    "RunInfo",
    "RunRow",
    "Sighting",
    "config_digest",
    "file_digest",
    "new_run",
    "summarise_collisions",
    "summarise_stats",
]
