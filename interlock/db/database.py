"""Database handle, schema bootstrap, and the content-addressed blob store.

Two stores with different jobs (section 14). Relational rows hold structure,
attributes and the commit log. Geometry goes to a blob store keyed by
fingerprint, so a revision that changes only metadata stores no new geometry and
a thousand revisions cost roughly the storage of one plus what actually changed.

One consequence the design document does not draw out: because the key is
semantic (the fingerprint) and the payload is not (BREP bytes differ on every
save), the stored blob is whichever export arrived first. A file read back out is
geometrically the part but not byte-identical to what a later contributor
submitted. That is the right trade, and it is recorded here so nobody is
surprised by it later.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
DEFAULT_DB = Path("data") / "store" / "interlock.db"
DEFAULT_BLOBS = Path("data") / "store" / "blobs"


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class Database:
    """A thin wrapper over sqlite3 with the project's conventions applied.

    Deliberately thin. The interesting work in this project is the SQL itself --
    recursive traversals, range joins, constraint triggers -- and burying that
    behind an object-relational mapper would hide the part worth reading.
    """

    def __init__(self, path: str | os.PathLike = DEFAULT_DB, blob_dir: str | os.PathLike | None = None):
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.blob_dir = Path(blob_dir) if blob_dir else self.path.parent / "blobs"
        self.blob_dir.mkdir(parents=True, exist_ok=True)

        # The review interface runs its handlers on a threadpool, so the
        # connection has to outlive the thread that made it. SQLite allows that
        # only if the caller serialises access, which the re-entrant lock below
        # does: every statement takes it, and a transaction holds it for its
        # whole duration so two threads can never interleave inside one.
        self._lock = threading.RLock()
        self.connection = sqlite3.connect(
            str(self.path), isolation_level=None, check_same_thread=False
        )
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        if str(self.path) != ":memory:":
            self.connection.execute("PRAGMA journal_mode = WAL")
        self._ensure_schema()

    # ------------------------------------------------------------- lifecycle

    def _ensure_schema(self) -> None:
        self.connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ----------------------------------------------------------- statements

    def execute(self, sql: str, params=()) -> sqlite3.Cursor:
        with self._lock:
            return self.connection.execute(sql, params)

    def executemany(self, sql: str, seq) -> sqlite3.Cursor:
        with self._lock:
            return self.connection.executemany(sql, seq)

    def query(self, sql: str, params=()) -> list[sqlite3.Row]:
        with self._lock:
            return self.connection.execute(sql, params).fetchall()

    def one(self, sql: str, params=()) -> sqlite3.Row | None:
        with self._lock:
            return self.connection.execute(sql, params).fetchone()

    def scalar(self, sql: str, params=()):
        row = self.one(sql, params)
        return None if row is None else row[0]

    @contextmanager
    def transaction(self):
        """A real transaction, so a commit lands whole or not at all.

        Section 9 is explicit that partial acceptance is never allowed, because a
        half-applied change can leave the assembly graph in a state no physical
        machine could occupy. That guarantee is this context manager.
        """
        with self._lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                yield self
            except Exception:
                self.connection.execute("ROLLBACK")
                raise
            else:
                self.connection.execute("COMMIT")

    # ---------------------------------------------------------- blob store

    def blob_path_for(self, fingerprint: str) -> Path:
        # Two-character fan-out keeps directory listings usable once the store
        # holds thousands of shapes.
        return self.blob_dir / fingerprint[:2] / f"{fingerprint}.brep"

    def has_blob(self, fingerprint: str) -> bool:
        return self.blob_path_for(fingerprint).exists()

    def write_blob(self, fingerprint: str, shape) -> str:
        """Persist geometry under its fingerprint, once.

        A second arrival of the same shape writes nothing, which is the storage
        deduplication section 5 promises, falling out of the identity scheme
        rather than from a compression strategy.
        """
        target = self.blob_path_for(fingerprint)
        if target.exists():
            return str(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        from OCP.BRepTools import BRepTools

        BRepTools.Write_s(shape, str(target))
        return str(target)

    def read_blob(self, fingerprint: str):
        target = self.blob_path_for(fingerprint)
        if not target.exists():
            return None
        from OCP.BRep import BRep_Builder
        from OCP.BRepTools import BRepTools
        from OCP.TopoDS import TopoDS_Shape

        shape = TopoDS_Shape()
        builder = BRep_Builder()
        if not BRepTools.Read_s(shape, str(target), builder):
            return None
        return shape

    # --------------------------------------------------------- display mesh

    def mesh_path_for(self, fingerprint: str) -> Path:
        return self.blob_dir / fingerprint[:2] / f"{fingerprint}.mesh.npz"

    def write_mesh(self, fingerprint: str, vertices, triangles) -> None:
        """Store a display mesh beside the geometry blob.

        Written at ingest, where the kernel already ran, so the web viewer can
        show a part without ever opening a solid. Never used for identity.
        """
        import numpy as np

        target = self.mesh_path_for(fingerprint)
        if target.exists():
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(
            target,
            vertices=np.asarray(vertices, dtype=np.float32),
            triangles=np.asarray(triangles, dtype=np.int32),
        )

    def read_mesh(self, fingerprint: str):
        import numpy as np

        target = self.mesh_path_for(fingerprint)
        if not target.exists():
            return None
        with np.load(target) as data:
            return data["vertices"], data["triangles"]

    # ------------------------------------------------------------ utilities

    def stats(self) -> dict:
        def count(table: str) -> int:
            return int(self.scalar(f"SELECT COUNT(*) FROM {table}") or 0)

        blob_bytes = sum(p.stat().st_size for p in self.blob_dir.rglob("*.brep"))
        return {
            "teams": count("team"),
            "parts": count("part"),
            "revisions": count("revision"),
            "shapes": count("shape"),
            "shape_aliases": count("shape_alias"),
            "occurrences": count("occurrence"),
            "features": count("feature"),
            "interfaces": count("interface"),
            "sockets": count("socket"),
            "contracts": count("contract"),
            "dimensions": count("dimension"),
            "chains": count("chain"),
            "commits": count("commit_log"),
            "notifications": count("notification"),
            "blob_bytes": blob_bytes,
            "db_bytes": self.path.stat().st_size if self.path.exists() else 0,
        }

    def set_ref(self, name: str, revision_id: str, root_hash: str | None, author: str = "") -> None:
        self.execute(
            """INSERT INTO ref(ref_name, revision_id, root_hash, updated_at, updated_by)
               VALUES (?, ?, ?, datetime('now'), ?)
               ON CONFLICT(ref_name) DO UPDATE SET
                   revision_id = excluded.revision_id,
                   root_hash   = excluded.root_hash,
                   updated_at  = excluded.updated_at,
                   updated_by  = excluded.updated_by""",
            (name, revision_id, root_hash, author),
        )

    def get_ref(self, name: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM ref WHERE ref_name = ?", (name,))


def json_dumps(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def json_loads(text: str | None, default=None):
    if not text:
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default
