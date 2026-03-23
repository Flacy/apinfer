"""
SQLite-backed persistence for schema snapshots.

Provides :class:`SnapshotStorage` — the single gateway for reading and writing
:class:`~apinfer.core.models.Snapshot` objects to a local SQLite database — and the
:func:`default_db_path` helper that returns the conventional storage location.

Database layout
---------------
A single table ``snapshots`` stores every snapshot ever persisted. The
:class:`~apinfer.core.models.EndpointKey` is flattened into three columns;
``inferred_schema`` is stored as canonical JSON text; ``timestamp`` as an ISO 8601
string.

A unique index on ``(method, path, status_code, content_hash)`` guarantees that the same
schema is never stored twice for the same endpoint: :meth:`SnapshotStorage.save` is
idempotent with respect to schema content.

Public surface
--------------
- :data:`DEFAULT_DB_FILENAME` — the conventional filename for the local database.
- :func:`default_db_path` — return the default :class:`~pathlib.Path` without
  instantiating storage (useful for CLI flags and future ``config.py``).
- :class:`SnapshotStorage` — open a database and read/write snapshots.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final, cast

from sqlite_utils import Database

from apinfer.core.models import EndpointKey, HttpMethod, Snapshot

if TYPE_CHECKING:
    from sqlite_utils.db import Table

__all__ = [
    "DEFAULT_DB_FILENAME",
    "SnapshotStorage",
    "default_db_path",
]

DEFAULT_DB_FILENAME: Final = "apinfer.db"
"""Conventional filename for the local snapshot database."""

_TABLE: Final = "snapshots"
# Column types passed to sqlite-utils Table.create()
_COLUMNS: Final[dict[str, type]] = {
    "id": int,
    "method": str,
    "path": str,
    "status_code": int,
    "content_hash": str,
    "inferred_schema": str,
    "timestamp": str,
    "sample_count": int,
}
_NOT_NULL: Final = frozenset(_COLUMNS.keys() - {"id"})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def default_db_path(base_dir: Path | None = None) -> Path:
    """Return the default database path.

    The database lives next to the project root (the current working directory)
    unless *base_dir* is provided.

    :param base_dir: Directory in which to place ``apinfer.db``.  Defaults to
        :func:`pathlib.Path.cwd`.
    :return: ``base_dir / DEFAULT_DB_FILENAME`` (or ``cwd / DEFAULT_DB_FILENAME``).
    :rtype: Path

    Examples::

        # Default: project root
        db_path = default_db_path()

        # Custom directory (from env var or CLI flag)
        db_path = default_db_path(Path("/var/lib/myapp"))
    """
    return (base_dir or Path.cwd()) / DEFAULT_DB_FILENAME


# ---------------------------------------------------------------------------
# Storage class
# ---------------------------------------------------------------------------


class SnapshotStorage:
    """SQLite-backed storage for schema snapshots.

    Opens (or creates) the database at *db_path* on construction and ensures
    the ``snapshots`` table and its indexes exist.

    Pass ``Path(":memory:")`` for an ephemeral in-memory database, which is
    useful in tests::

        store = SnapshotStorage(Path(":memory:"))

    :param db_path: Path to the SQLite database file, or ``Path(":memory:")``
        for an in-memory database.
    """

    def __init__(self, db_path: Path) -> None:
        if str(db_path) == ":memory:":
            self._db: Database = Database(memory=True)
        else:
            self._db = Database(db_path)
        self._ensure_table()

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def save(self, snapshot: Snapshot) -> Snapshot:
        """Persist *snapshot* and return a copy with :attr:`~Snapshot.id` set.

        Idempotent with respect to schema content: if a snapshot with the same
        ``(method, path, status_code, content_hash)`` already exists, the
        existing row is returned unchanged rather than inserting a duplicate.

        The returned :class:`~apinfer.core.models.Snapshot` is a new frozen
        instance produced by :meth:`~pydantic.BaseModel.model_copy`; the
        *snapshot* argument is never mutated.

        :param snapshot: A snapshot with ``id=None`` as produced by
            :func:`~apinfer.core.inferrer.build_snapshot`.
        :return: The same snapshot with its storage-assigned ``id``.
        :rtype: Snapshot
        """
        row = _to_row(snapshot)
        cast("Table", self._db[_TABLE]).insert(row, pk="id", ignore=True)
        # Retrieve the row to obtain its id regardless of whether the insert
        # succeeded or was silently ignored due to the unique constraint.
        existing = next(
            self._db[_TABLE].rows_where(
                "method = ? AND path = ? AND status_code = ? AND content_hash = ?",
                [
                    snapshot.key.method.value,
                    snapshot.key.path,
                    snapshot.key.status_code,
                    snapshot.content_hash,
                ],
            )
        )
        return snapshot.model_copy(update={"id": existing["id"]})

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get_latest(self, key: EndpointKey) -> Snapshot | None:
        """Return the most recent snapshot for *key*, or ``None``.

        "Most recent" is determined first by ``timestamp`` (newest wins), then
        by ``id`` as a tiebreaker for snapshots captured at the exact same
        instant.

        :param key: Endpoint identifier (method, path pattern, status code).
        :return: The most recent :class:`~apinfer.core.models.Snapshot`, or
            ``None`` if no snapshot exists for this endpoint.
        :rtype: Snapshot | None
        """
        rows = list(
            cast("Table", self._db[_TABLE]).rows_where(
                "method = ? AND path = ? AND status_code = ?",
                [key.method.value, key.path, key.status_code],
                order_by="timestamp DESC, id DESC",
                limit=1,
            )
        )
        return _from_row(rows[0]) if rows else None

    def get_previous(self, key: EndpointKey, before_id: int) -> Snapshot | None:
        """Return the snapshot immediately before *before_id* for *key*, or ``None``.

        Intended for the drift detector, which needs to compare the current
        snapshot against the one that preceded it for the same endpoint.

        :param key: Endpoint identifier.
        :param before_id: The storage id of the *current* snapshot.  The
            returned snapshot will have ``id < before_id``.
        :return: The immediately preceding :class:`~apinfer.core.models.Snapshot`,
            or ``None`` if *before_id* is the oldest snapshot for this endpoint.
        :rtype: Snapshot | None
        """
        rows = list(
            self._db[_TABLE].rows_where(
                "method = ? AND path = ? AND status_code = ? AND id < ?",
                [key.method.value, key.path, key.status_code, before_id],
                order_by="id DESC",
                limit=1,
            )
        )
        return _from_row(rows[0]) if rows else None

    def list_snapshots(self, key: EndpointKey) -> list[Snapshot]:
        """Return all snapshots for *key*, newest first.

        :param key: Endpoint identifier.
        :return: Snapshots ordered by ``timestamp DESC, id DESC``.  Empty list
            if no snapshots have been saved for this endpoint.
        :rtype: list[Snapshot]
        """
        rows = self._db[_TABLE].rows_where(
            "method = ? AND path = ? AND status_code = ?",
            [key.method.value, key.path, key.status_code],
            order_by="timestamp DESC, id DESC",
        )
        return [_from_row(row) for row in rows]

    def list_keys(self) -> list[EndpointKey]:
        """Return all distinct :class:`~apinfer.core.models.EndpointKey` values.

        Useful for the CLI ``apinfer report`` command, which needs to enumerate
        every endpoint that has been observed.

        :return: One :class:`~apinfer.core.models.EndpointKey` per distinct
            ``(method, path, status_code)`` tuple in the database, in
            unspecified order.
        :rtype: list[EndpointKey]
        """
        rows = self._db.execute(
            f"SELECT DISTINCT method, path, status_code FROM {_TABLE}"
        ).fetchall()
        return [
            EndpointKey(
                method=HttpMethod(row[0]),
                path=row[1],
                status_code=row[2],
            )
            for row in rows
        ]

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _ensure_table(self) -> None:
        """Create the ``snapshots`` table and its indexes if they don't exist."""
        self._db[_TABLE].create(  # type: ignore[union-attr]
            _COLUMNS,
            pk="id",
            not_null=_NOT_NULL,
            if_not_exists=True,
        )
        table = self._db[_TABLE]
        # Unique index: prevents duplicate snapshots for unchanged schemas.
        table.create_index(  # type: ignore[union-attr]
            ["method", "path", "status_code", "content_hash"],
            unique=True,
            if_not_exists=True,
        )
        # Lookup index: speeds up per-endpoint queries.
        table.create_index(  # type: ignore[union-attr]
            ["method", "path", "status_code"],
            if_not_exists=True,
        )


# ---------------------------------------------------------------------------
# Serialization helpers (module-level, stateless)
# ---------------------------------------------------------------------------


def _to_row(snapshot: Snapshot) -> dict[str, object]:
    """Serialize *snapshot* to a plain dict for sqlite-utils.

    The ``id`` field is intentionally excluded so that SQLite assigns it
    automatically via ROWID autoincrement.

    :param snapshot: The snapshot to serialize.
    :return: A dict with string/int values ready for ``Table.insert``.
    """
    return {
        "method": snapshot.key.method.value,
        "path": snapshot.key.path,
        "status_code": snapshot.key.status_code,
        "content_hash": snapshot.content_hash,
        "inferred_schema": json.dumps(
            snapshot.inferred_schema, sort_keys=True, separators=(",", ":")
        ),
        "timestamp": snapshot.timestamp.isoformat(),
        "sample_count": snapshot.sample_count,
    }


def _from_row(row: dict[str, object]) -> Snapshot:
    """Deserialize a sqlite-utils row dict back to a
    :class:`~apinfer.core.models.Snapshot`.

    :param row: A row dict as returned by sqlite-utils ``rows_where``.
    :return: A fully populated :class:`~apinfer.core.models.Snapshot`.
    :rtype: Snapshot
    """
    key = EndpointKey(
        method=HttpMethod(str(row["method"])),
        path=str(row["path"]),
        status_code=int(str(row["status_code"])),
    )
    return Snapshot(
        id=int(str(row["id"])),
        key=key,
        inferred_schema=json.loads(str(row["inferred_schema"])),
        content_hash=str(row["content_hash"]),
        timestamp=datetime.fromisoformat(str(row["timestamp"])),
        sample_count=int(str(row["sample_count"])),
    )
