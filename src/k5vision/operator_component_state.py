"""Anchor mandatory component state in the durable account authority database.

An upgrade may initialize a component once. Losing its separate data file later
must never silently restore broad permissions, password-only access or defaults.
Seal only after the component's own file/schema has been durably admitted.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from pathlib import Path

from k5vision import identity_state

_COMPONENTS = frozenset(
    {"roles", "account_security", "audit", "storage", "integrations", "person_events"}
)
_TABLE = "operator_component_anchors"


class ComponentStateError(RuntimeError):
    """A mandatory durable component cannot be safely initialized or opened."""


class ComponentStateAnchor:
    def __init__(self, root: Path, site_id: str, component: str, *, present: bool):
        if component not in _COMPONENTS or type(present) is not bool:
            raise ComponentStateError("Component state admission failed")
        state = identity_state.load_identity_state(root)
        if state.site_id != site_id:
            raise ComponentStateError("Component state admission failed")
        self.root, self.site_id, self.component = state.directory, site_id, component
        self._root_identity = self.root.lstat()
        self.path = state.database_path
        info = self._admit()
        self._identity = info.st_dev, info.st_ino
        try:
            with closing(self._connect()) as connection:
                if self._validate(connection):
                    row = connection.execute(
                        "SELECT schema_version FROM operator_component_anchors "
                        "WHERE site_id=? AND component=?",
                        (site_id, component),
                    ).fetchone()
                    if row is not None and (tuple(row) != (1,) or not present):
                        raise ValueError
                self._admit()
        except Exception:
            raise ComponentStateError("Required component state is missing or invalid") from None

    def _validate(self, connection):
        entries = connection.execute(
            "SELECT type FROM sqlite_master WHERE name=?", (_TABLE,)
        ).fetchall()
        if not entries:
            return False
        if entries != [("table",)]:
            raise ValueError
        columns = connection.execute("PRAGMA table_info(operator_component_anchors)").fetchall()
        expected = [
            ("site_id", "TEXT", 1, 1),
            ("component", "TEXT", 1, 2),
            ("schema_version", "INTEGER", 1, 0),
        ]
        if [(row[1], row[2], row[3], row[5]) for row in columns] != expected:
            raise ValueError
        rows = connection.execute(
            "SELECT site_id,component,schema_version FROM operator_component_anchors LIMIT 7"
        ).fetchall()
        if (
            len(rows) > len(_COMPONENTS)
            or len({r[1] for r in rows}) != len(rows)
            or any(
                site != self.site_id or name not in _COMPONENTS or version != 1
                for site, name, version in rows
            )
        ):
            raise ValueError
        return True

    def _admit(self):
        identity_state._same_directory(self.root, self._root_identity)
        info = identity_state._regular_file(self.path)
        if os.name == "posix" and (info.st_uid != os.geteuid() or info.st_mode & 0o077):
            raise ComponentStateError("Component authority is unavailable")
        expected = getattr(self, "_identity", None)
        if expected is not None and (info.st_dev, info.st_ino) != expected:
            raise ComponentStateError("Component authority changed")
        for suffix in ("-journal", "-wal", "-shm"):
            sidecar = Path(str(self.path) + suffix)
            if sidecar.exists() or sidecar.is_symlink():
                try:
                    current = identity_state._regular_file(sidecar)
                except FileNotFoundError:
                    continue
                if os.name == "posix" and (
                    current.st_uid != os.geteuid() or current.st_mode & 0o077
                ):
                    raise ComponentStateError("Component authority journal is unsafe")
        return info

    def _connect(self):
        self._admit()
        return sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, timeout=3)

    def seal(self):
        """Idempotently record that a successfully admitted component is required."""
        connection = None
        try:
            connection = self._connect()
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS operator_component_anchors ("
                "site_id TEXT NOT NULL, component TEXT NOT NULL, schema_version INTEGER NOT NULL,"
                "PRIMARY KEY(site_id,component))"
            )
            self._validate(connection)
            connection.execute(
                "INSERT OR IGNORE INTO operator_component_anchors VALUES(?,?,1)",
                (self.site_id, self.component),
            )
            row = connection.execute(
                "SELECT schema_version FROM operator_component_anchors "
                "WHERE site_id=? AND component=?",
                (self.site_id, self.component),
            ).fetchone()
            if row != (1,):
                raise ValueError
            self._admit()
            connection.commit()
            self._admit()
        except Exception:
            if connection is not None:
                connection.rollback()
            raise ComponentStateError("Component authority could not be sealed") from None
        finally:
            if connection is not None:
                connection.close()
