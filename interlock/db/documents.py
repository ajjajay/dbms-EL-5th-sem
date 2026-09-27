"""The document store, and the outbox that feeds it.

Polyglot persistence, kept deliberately small. SQLite is the system of record;
this is a second store for the few things that are genuinely a bad fit for
relational rows. The rule that decides what goes here is short and is the whole
justification:

    Anything read during commit validation stays in SQL.

Two stores cannot share one transaction, and section 9 forbids a half-applied
commit. So anything a constraint reads -- interface points, budgets, CG windows,
chain members -- lives in SQL where it can be read inside the same transaction
that lands the commit. What is left over is write-once, schemaless, never joined
and never read by validation:

  * validation traces: the full step-by-step reasoning behind a verdict, which is
    variable in shape (a socket match has different fields from a budget sum) and
    is only ever read back whole, for display;
  * notification detail;
  * the natural-language tool-call log, which is an audit record of what the model
    was asked and which typed tool it chose;
  * free-form contract metadata -- finish, supplier, catalogue codes, notes --
    which genuinely varies per part type and which no constraint reads.

Writes go through an outbox table in SQL. The commit transaction appends rows to
`outbox`; a worker drains them afterwards. That ordering is the point: the
document store is only ever written *after* the SQL commit has landed, so a
rolled-back commit leaves no orphaned documents, and a crash between the two
leaves undelivered rows that the next drain picks up (at-least-once delivery,
made idempotent by writing each document under a deterministic key).

Engine: TinyDB. One JSON file, no server, MongoDB-style queries. Its limits are
real and are acceptable only because of how little is stored here: it rewrites
the whole file per write, has no indexes, no transactions, and is unsafe for
concurrent writers -- which is why the outbox worker is its only writer. If any
of that stopped being true, swapping MongoDB in behind `DocumentStore` would not
touch the rest of the system.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

DEFAULT_DOC_PATH = Path("data") / "store" / "documents.json"


class DocumentStore(Protocol):
    """The seam. Everything above this talks documents, not TinyDB."""

    def put(self, collection: str, key: str, document: dict) -> None: ...
    def get(self, collection: str, key: str) -> dict | None: ...
    def find(self, collection: str, **equals: Any) -> list[dict]: ...
    def all(self, collection: str) -> list[dict]: ...
    def count(self, collection: str) -> int: ...
    def close(self) -> None: ...


class TinyDocumentStore:
    """TinyDB-backed store. Documents are keyed, so redelivery overwrites."""

    name = "tinydb"

    def __init__(self, path: str | os.PathLike = DEFAULT_DOC_PATH):
        from tinydb import TinyDB

        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = TinyDB(str(self.path), indent=2, sort_keys=False)

    def put(self, collection: str, key: str, document: dict) -> None:
        from tinydb import Query

        table = self._db.table(collection)
        record = dict(document)
        record["_key"] = key
        table.upsert(record, Query()._key == key)

    def get(self, collection: str, key: str) -> dict | None:
        from tinydb import Query

        found = self._db.table(collection).get(Query()._key == key)
        return dict(found) if found else None

    def find(self, collection: str, **equals: Any) -> list[dict]:
        from tinydb import Query

        table = self._db.table(collection)
        if not equals:
            return [dict(d) for d in table.all()]
        q = None
        for field, value in equals.items():
            clause = Query()[field] == value
            q = clause if q is None else (q & clause)
        return [dict(d) for d in table.search(q)]

    def all(self, collection: str) -> list[dict]:
        return [dict(d) for d in self._db.table(collection).all()]

    def count(self, collection: str) -> int:
        return len(self._db.table(collection))

    def collections(self) -> list[str]:
        return sorted(self._db.tables())

    def close(self) -> None:
        self._db.close()


class JsonLinesDocumentStore:
    """Stdlib fallback with the same interface, in case TinyDB is unavailable.

    Append-only JSON lines with a last-write-wins index, which is the same
    delivery semantics the outbox needs and needs no dependency at all.
    """

    name = "jsonl"

    def __init__(self, path: str | os.PathLike = DEFAULT_DOC_PATH):
        self.path = Path(path).with_suffix(".jsonl")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)

    def _read(self) -> dict[tuple[str, str], dict]:
        index: dict[tuple[str, str], dict] = {}
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            index[(row["_collection"], row["_key"])] = row
        return index

    def put(self, collection: str, key: str, document: dict) -> None:
        record = dict(document)
        record["_collection"] = collection
        record["_key"] = key
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")

    def get(self, collection: str, key: str) -> dict | None:
        return self._read().get((collection, key))

    def find(self, collection: str, **equals: Any) -> list[dict]:
        return [
            d for (c, _), d in self._read().items()
            if c == collection and all(d.get(k) == v for k, v in equals.items())
        ]

    def all(self, collection: str) -> list[dict]:
        return [d for (c, _), d in self._read().items() if c == collection]

    def count(self, collection: str) -> int:
        return len(self.all(collection))

    def collections(self) -> list[str]:
        return sorted({c for (c, _) in self._read()})

    def close(self) -> None:
        return None


def open_document_store(path: str | os.PathLike = DEFAULT_DOC_PATH) -> DocumentStore:
    try:
        return TinyDocumentStore(path)
    except ImportError:
        return JsonLinesDocumentStore(path)


# ------------------------------------------------------------------- outbox

# Topics, each mapping to one document collection.
TOPIC_VALIDATION = "validation_trace"
TOPIC_NOTIFICATION = "notification_detail"
TOPIC_NL_CALL = "nl_tool_call"
TOPIC_METADATA = "contract_metadata"


def enqueue(db, topic: str, doc_key: str, payload: dict) -> None:
    """Append to the outbox. Must be called inside the commit's transaction.

    Nothing is written to the document store here, on purpose: this row and the
    commit it describes land together or not at all.
    """
    from .database import json_dumps

    db.execute(
        "INSERT INTO outbox(topic, doc_key, payload) VALUES (?,?,?)",
        (topic, doc_key, json_dumps(payload)),
    )


@dataclass
class DrainReport:
    delivered: int = 0
    failed: int = 0
    errors: list[str] = None

    def __post_init__(self):
        if self.errors is None:
            self.errors = []


def drain(db, store: DocumentStore, limit: int = 1000) -> DrainReport:
    """Deliver pending outbox rows to the document store.

    The single writer. Runs after the commit transaction has closed, so a
    document can never exist for a commit that did not land. Delivery is
    at-least-once and each document is written under a deterministic key, so a
    redelivery after a crash overwrites rather than duplicates.
    """
    from .database import json_loads

    report = DrainReport()
    pending = db.query(
        "SELECT * FROM outbox WHERE delivered_at IS NULL ORDER BY outbox_id LIMIT ?", (limit,)
    )
    for row in pending:
        try:
            payload = json_loads(row["payload"], {})
            key = row["doc_key"] or f"outbox_{row['outbox_id']}"
            store.put(row["topic"], key, payload)
        except Exception as exc:
            report.failed += 1
            report.errors.append(f"outbox {row['outbox_id']} ({row['topic']}): {exc}")
            continue
        db.execute(
            "UPDATE outbox SET delivered_at = datetime('now') WHERE outbox_id = ?",
            (row["outbox_id"],),
        )
        report.delivered += 1
    return report


def pending_count(db) -> int:
    return int(db.scalar("SELECT COUNT(*) FROM outbox WHERE delivered_at IS NULL") or 0)
