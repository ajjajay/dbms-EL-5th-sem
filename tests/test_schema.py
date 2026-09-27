"""The integrity rules that live in the database rather than in convention."""

from __future__ import annotations

import sqlite3

import pytest

from interlock.model.ingest import add_occurrence, begin_commit, ensure_team


def _rev(db, part_id, index=1, commit="c1"):
    rid = f"rev_{part_id}_{index}"
    db.execute(
        "INSERT INTO revision(revision_id, part_id, revision_index, commit_id) VALUES (?,?,?,?)",
        (rid, part_id, index, commit),
    )
    return rid


@pytest.fixture()
def seeded(db):
    ensure_team(db, "T")
    cid = begin_commit(db, "tester", "seed", "T")
    for p in ("A", "B", "C", "D"):
        db.execute("INSERT INTO part(part_id, part_number, team_id) VALUES (?,?,?)", (p, f"PN-{p}", "T"))
    revs = {p: _rev(db, p, commit=cid) for p in ("A", "B", "C", "D")}
    return db, revs, cid


def test_schema_applies_and_is_idempotent(db):
    db._ensure_schema()
    tables = {r[0] for r in db.query("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"part", "revision", "shape", "occurrence", "contract", "interface", "socket",
            "dimension", "chain", "commit_log", "outbox"} <= tables


def test_cycles_are_rejected_at_every_depth(seeded):
    db, r, _ = seeded
    import numpy as np

    eye = np.eye(4)
    add_occurrence(db, r["A"], r["B"], "b", 1, eye)
    add_occurrence(db, r["B"], r["C"], "c", 1, eye)
    # self, two-hop, three-hop
    with pytest.raises(sqlite3.IntegrityError):
        add_occurrence(db, r["A"], r["A"], "self", 1, eye)
    with pytest.raises(sqlite3.DatabaseError, match="cycle"):
        add_occurrence(db, r["C"], r["A"], "loop3", 1, eye)
    with pytest.raises(sqlite3.DatabaseError, match="cycle"):
        add_occurrence(db, r["B"], r["A"], "loop2", 1, eye)
    # A diamond is not a cycle.
    add_occurrence(db, r["A"], r["C"], "c_direct", 1, eye)


def test_revisions_are_immutable_but_hash_may_be_assigned(seeded):
    db, r, _ = seeded
    with pytest.raises(sqlite3.DatabaseError, match="immutable"):
        db.execute("UPDATE revision SET material = 'steel' WHERE revision_id = ?", (r["A"],))
    with pytest.raises(sqlite3.DatabaseError, match="immutable"):
        db.execute("UPDATE revision SET fingerprint = NULL, commit_id = 'x' WHERE revision_id = ?", (r["A"],))
    db.execute("UPDATE revision SET merkle_hash = 'abc' WHERE revision_id = ?", (r["A"],))
    db.execute("UPDATE revision SET status = 'released' WHERE revision_id = ?", (r["A"],))


def test_occurrences_are_immutable(seeded):
    db, r, _ = seeded
    import numpy as np
    add_occurrence(db, r["A"], r["B"], "b", 1, np.eye(4))
    with pytest.raises(sqlite3.DatabaseError, match="immutable"):
        db.execute("UPDATE occurrence SET quantity = 9 WHERE parent_rev = ?", (r["A"],))


def test_commit_log_is_append_only(seeded):
    db, _, cid = seeded
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        db.execute("DELETE FROM commit_log WHERE commit_id = ?", (cid,))


def test_a_transaction_lands_whole_or_not_at_all(seeded):
    db, r, cid = seeded
    before = db.scalar("SELECT COUNT(*) FROM part")
    with pytest.raises(RuntimeError):
        with db.transaction():
            db.execute("INSERT INTO part(part_id, part_number, team_id) VALUES ('Z','PN-Z','T')")
            raise RuntimeError("boom")
    assert db.scalar("SELECT COUNT(*) FROM part") == before
