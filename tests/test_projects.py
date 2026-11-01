"""Projects: one database per product, real products built with auto-drafted contracts.

The real downloads cannot be fetched in a test, so the build path is exercised on
the generated assembly registered as if it were a downloaded project: same
ingest, same auto-draft, same refs. What a test must not do is touch the network.
"""

from __future__ import annotations

import pytest

from interlock import cli
from interlock import projects as proj
from interlock.db.database import Database
from interlock.model.interference import Clash
from interlock.query import readiness as rd
from interlock.synthetic import assembly


@pytest.fixture()
def fake_project(tmp_path, monkeypatch):
    """A project whose single source file is already on disk, so nothing is downloaded."""
    monkeypatch.setattr(proj, "PROJECTS_DIR", tmp_path / "projects")
    spec = proj.ProjectSpec(
        name="test-winch", title="test", licence="none", origin="generated",
        sources=[proj.Source("winch.step", "http://invalid.example/never-fetched", "main")],
        teams={"structure": "Structure", "fasteners": "Fasteners"},
        team_rules=[(r"^(BOLT|NUT|WASHER)", "fasteners")],
        default_team="structure",
        materials=[(r"^(BOLT|NUT|WASHER)", "", 0.0), (r".", "aluminium", 0.0027)],
    )
    monkeypatch.setitem(proj.PROJECTS, "test-winch", spec)
    src = proj.folder("test-winch") / "source"
    src.mkdir(parents=True)
    assembly.write(str(src / "winch.step"))
    return spec


def test_the_demo_keeps_its_original_location():
    assert proj.db_path(proj.DEMO) == proj.DEFAULT_DB
    assert proj.names()[0] == proj.DEMO


def test_an_unknown_project_is_refused():
    with pytest.raises(KeyError):
        proj.db_path("no-such-project")
    with pytest.raises(SystemExit):
        cli.main(["--project", "no-such-project", "bom"])


def test_demo_refuses_to_overwrite_another_project():
    with pytest.raises(SystemExit):
        cli.main(["--project", "so-arm100", "demo"])


def test_the_resolver_applies_the_first_matching_rule(fake_project):
    resolve = proj.resolver(fake_project)
    bolt, plate = resolve("BOLT-M6X20"), resolve("BASE-PLATE")
    assert (bolt.team_id, bolt.material, bolt.density) == ("fasteners", None, None)
    assert (plate.team_id, plate.material, plate.density) == ("structure", "aluminium", 0.0027)


def test_build_gives_every_part_an_unreviewed_draft(fake_project):
    report = proj.build("test-winch", log=lambda *_: None)
    assert report.drafted > 0
    db = Database(proj.db_path("test-winch"))
    try:
        assert db.get_ref("main") is not None
        rows = db.query("SELECT provenance, reviewed FROM contract")
        assert len(rows) == report.drafted
        assert {(r["provenance"], r["reviewed"]) for r in rows} == {("auto_drafted", 0)}
        # The status view must call every draft unfinished, not pass it off as done.
        r = rd.readiness(db, "main", with_sockets=False)
        drafted = [p for p in r.parts if "contract auto-drafted and not reviewed" in p.gaps]
        assert drafted and r.complete < 1.0
    finally:
        db.close()


def test_the_cli_reads_the_projects_own_database(fake_project, capsys):
    proj.build("test-winch", log=lambda *_: None)
    capsys.readouterr()
    assert cli.main(["--project", "test-winch", "bom"]) == 0
    out = capsys.readouterr().out
    assert "BOLT-M6X20" in out and "fasteners" in out


def test_a_box_overlap_is_not_described_as_a_gap():
    """A positive depth is how far the boxes interpenetrate, not a distance apart."""
    overlap = Clash("a", "b", "A", "B", "t1", "t2", "clearance", depth=3.5)
    gap = Clash("a", "b", "A", "B", "t1", "t2", "clearance", depth=-1.25)
    assert "bounding boxes overlap by 3.50 mm" in overlap.describe()
    assert "apart" not in overlap.describe()
    assert "1.25 mm apart" in gap.describe()


def test_status_prints_every_team_when_an_earlier_one_has_waiting_items(fake_project, capsys):
    """Regression: a loop variable once overwrote the column width, so the team
    after any team with waiting items crashed the command."""
    proj.build("test-winch", log=lambda *_: None)
    db = Database(proj.db_path("test-winch"))
    commit = db.scalar("SELECT commit_id FROM commit_log LIMIT 1")
    part = db.scalar("SELECT part_id FROM part WHERE team_id = 'structure' LIMIT 1")
    for team in ("structure", "fasteners"):
        db.execute("INSERT INTO notification (commit_id, team_id, part_id, reason) VALUES (?,?,?,?)",
                   (commit, team, part, "test"))
    db.close()
    capsys.readouterr()
    assert cli.main(["--project", "test-winch", "status", "--fast"]) == 0
    out = capsys.readouterr().out
    assert "structure" in out and "fasteners" in out
