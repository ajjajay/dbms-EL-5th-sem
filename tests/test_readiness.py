"""The status view: what is finished, what is outstanding, whose turn it is.

Every input already existed, so what needs checking is the bookkeeping: that a
gap is attributed to the right part and designer, that forty bolts count once,
and that something already checked and found wanting lands on the team that has
to act on it.
"""

from __future__ import annotations

import pytest

from interlock.db.database import Database
from interlock.query import readiness as rd
from interlock.synthetic import assembly, bootstrap


@pytest.fixture()
def loaded(tmp_path):
    step = assembly.write(str(tmp_path / "winch.step"))
    db = Database(tmp_path / "i.db", tmp_path / "blobs")
    report = bootstrap.load(db, step)
    bootstrap.subscribe_defaults(db)
    yield db, report
    db.close()


def _row(**over):
    row = {"contract_id": "c", "provenance": "declared", "reviewed": 1,
           "is_assembly": 0, "fingerprint": "f", "density": 0.0078, "status": "released"}
    row.update(over)
    return row


def test_a_fully_declared_part_has_no_gaps():
    assert rd.part_gaps(_row()) == []


@pytest.mark.parametrize("over, expected", [
    ({"contract_id": None}, "no contract declared"),
    ({"provenance": "auto_drafted", "reviewed": 0}, "contract auto-drafted and not reviewed"),
    ({"density": None}, "no material density, so it weighs nothing in the rollup"),
    ({"status": "draft"}, "still a draft, not released"),
])
def test_each_gap_is_named(over, expected):
    assert rd.part_gaps(_row(**over)) == [expected]


def test_an_assembly_needs_no_density_of_its_own():
    """Its mass is its children's; asking for one would be a false gap."""
    assert rd.part_gaps(_row(is_assembly=1, fingerprint=None, density=None)) == []


def test_identical_instances_count_once(loaded):
    db, _ = loaded
    r = rd.readiness(db, "main", with_sockets=False)
    numbers = [p.part_number for p in r.parts]
    assert len(numbers) == len(set(numbers))
    assert numbers.count("BOLT-M6X20") == 1
    assert len(numbers) == db.scalar(
        "SELECT COUNT(DISTINCT part_id) FROM revision")


def test_the_demo_assembly_is_fully_declared_but_not_ready(loaded):
    """Every part declares a contract and a density; the cross-team bolt-grip
    chain still fails worst case, so the product is waiting on a person."""
    db, _ = loaded
    r = rd.readiness(db, "main", with_sockets=False)
    assert r.complete == 1.0
    assert not r.ready
    chains = [w for w in r.waiting if w.kind == "chain"]
    assert any("bolt_grip" in w.detail for w in chains)


def test_an_unreviewed_contract_lands_on_its_designer(loaded):
    db, _ = loaded
    rev = db.scalar(
        """SELECT r.revision_id FROM v_current_revision r JOIN part p ON p.part_id = r.part_id
           WHERE p.part_number = 'BEARING-BLOCK'""")
    db.execute("UPDATE contract SET provenance = 'auto_drafted', reviewed = 0 WHERE revision_id = ?",
               (rev,))
    r = rd.readiness(db, "main", with_sockets=False)
    team = r.teams["chassis"]
    assert team.complete < 1.0
    queued = [p for parts in team.designers().values() for p in parts]
    assert [p.part_number for p in queued] == ["BEARING-BLOCK"]
    assert queued[0].designer == db.scalar(
        "SELECT cl.author FROM revision r JOIN commit_log cl ON cl.commit_id = r.commit_id "
        "WHERE r.revision_id = ?", (rev,))


def test_an_unacknowledged_notification_waits_on_its_team(loaded):
    db, _ = loaded
    commit = db.scalar("SELECT commit_id FROM commit_log LIMIT 1")
    part = db.scalar("SELECT part_id FROM part WHERE part_number = 'MOTOR'")
    db.execute("INSERT INTO notification (commit_id, team_id, part_id, reason) VALUES (?,?,?,?)",
               (commit, "controls", part, "test"))
    r = rd.readiness(db, "main", with_sockets=False)
    assert any(w.kind == "notification" for w in r.teams["controls"].waiting)
    db.execute("UPDATE notification SET acknowledged = 1")
    r = rd.readiness(db, "main", with_sockets=False)
    assert not any(w.kind == "notification" for w in r.waiting)


def test_the_socket_stage_finds_nothing_wrong_with_the_demo(loaded):
    db, _ = loaded
    r = rd.readiness(db, "main", with_sockets=True)
    assert not [w for w in r.waiting if w.kind == "socket"]
