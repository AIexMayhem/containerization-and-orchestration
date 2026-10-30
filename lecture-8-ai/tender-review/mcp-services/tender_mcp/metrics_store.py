"""Persistent, idempotent journal of collection starts and finishes."""

import math
import sqlite3
from contextlib import closing
from pathlib import Path


class EventConflict(ValueError):
    """An execution event conflicts with its previously recorded history."""


def _timestamp(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Timestamp must be a finite nonnegative number")
    try:
        value = float(value)
    except OverflowError as error:
        raise ValueError("Timestamp must be a finite nonnegative number") from error
    if not math.isfinite(value) or value < 0:
        raise ValueError("Timestamp must be a finite nonnegative number")
    return value


def _identity(workflow: str, execution_id: str) -> None:
    for name, value, limit in (("workflow", workflow, 64), ("execution_id", execution_id, 128)):
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            raise ValueError(f"{name} must be a nonempty string of at most {limit} characters")


class EventStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS collection_runs (
                    workflow TEXT NOT NULL,
                    execution_id TEXT NOT NULL,
                    started_at REAL NOT NULL,
                    finished_at REAL,
                    outcome TEXT CHECK (outcome IN ('complete', 'incomplete', 'error')),
                    PRIMARY KEY (workflow, execution_id),
                    CHECK ((finished_at IS NULL AND outcome IS NULL)
                        OR (finished_at IS NOT NULL AND outcome IS NOT NULL))
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=10, isolation_level=None)

    def record_start(self, workflow: str, execution_id: str, started_at: float) -> bool:
        _identity(workflow, execution_id)
        started_at = _timestamp(started_at)
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = connection.execute(
                "SELECT started_at FROM collection_runs WHERE workflow = ? AND execution_id = ?",
                (workflow, execution_id),
            ).fetchone()
            if previous is not None:
                if previous[0] != started_at:
                    raise EventConflict("Start event conflicts with the recorded timestamp")
                return False
            connection.execute(
                "INSERT INTO collection_runs (workflow, execution_id, started_at) VALUES (?, ?, ?)",
                (workflow, execution_id, started_at),
            )
            return True

    def record_finish(
        self, workflow: str, execution_id: str, finished_at: float, outcome: str
    ) -> bool:
        _identity(workflow, execution_id)
        finished_at = _timestamp(finished_at)
        if outcome not in ("complete", "incomplete", "error"):
            raise ValueError("Outcome must be complete, incomplete, or error")
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = connection.execute(
                """SELECT started_at, finished_at, outcome FROM collection_runs
                WHERE workflow = ? AND execution_id = ?""",
                (workflow, execution_id),
            ).fetchone()
            if previous is None:
                raise EventConflict("Finish event has no recorded start")
            if finished_at < previous[0]:
                raise EventConflict("Finish timestamp precedes the recorded start")
            if previous[1] is not None:
                if previous[1:] != (finished_at, outcome):
                    raise EventConflict("Finish event conflicts with the recorded result")
                return False
            connection.execute(
                """UPDATE collection_runs SET finished_at = ?, outcome = ?
                WHERE workflow = ? AND execution_id = ?""",
                (finished_at, outcome, workflow, execution_id),
            )
            return True

    def totals(self) -> dict[str, int]:
        counts = {"complete": 0, "incomplete": 0, "error": 0}
        with closing(self._connect()) as connection:
            counts.update(
                connection.execute(
                    """SELECT outcome, COUNT(*) FROM collection_runs
                    WHERE outcome IS NOT NULL GROUP BY outcome"""
                ).fetchall()
            )
        return counts

    def oldest_running_age(self, now: float) -> float:
        now = _timestamp(now)
        with closing(self._connect()) as connection:
            oldest = connection.execute(
                "SELECT MIN(started_at) FROM collection_runs WHERE finished_at IS NULL"
            ).fetchone()[0]
        return max(0.0, now - oldest) if oldest is not None else 0.0
