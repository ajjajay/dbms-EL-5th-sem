"""Giving geometry back out.

The blob store's whole justification is that a solid put in can be taken out
again. These tests hold it to that: export a part, a subassembly and a whole
configuration, read each back through a cold kernel, and require the structure,
the names, the placements and the identities to survive.

That round trip is also what makes the review interface's "Open in FreeCAD"
button honest -- the file handed to a CAD application is rebuilt from what the
database stored, not copied from the source file it was ingested from.
"""

from __future__ import annotations

import numpy as np
import pytest

from interlock.db.database import Database
from interlock.geometry import fingerprint as fp
from interlock.kernel import get_backend
from interlock.model import export
from interlock.synthetic import assembly, bootstrap


@pytest.fixture(scope="module")
def loaded(tmp_path_factory):
    d = tmp_path_factory.mktemp("export")
    step = assembly.write(str(d / "winch.step"))
    db = Database(d / "i.db", d / "blobs")
    report = bootstrap.load(db, step)
    yield db, report, d
    db.close()


def test_a_single_part_exports_and_reads_back_as_itself(loaded):
    """The strongest statement the store can make: what comes out has the same
    fingerprint as what went in."""
    db, _, d = loaded
    original = db.scalar(
        """SELECT r.fingerprint FROM revision r JOIN part p USING(part_id)
           WHERE p.part_number = 'BEARING-BLOCK'""")

    path = export.export_revision(db, "BEARING-BLOCK", d / "block.step")
    result = get_backend("occt").read(str(path))

    assert len(result.solids) == 1
    solid = result.solids[0]
    assert solid.valid
    assert fp.identify(solid, deflection=0.2).strict == original


def test_an_assembly_exports_with_its_hierarchy_and_names(loaded):
    db, report, d = loaded
    path = export.export_revision(db, report.root_revision, d / "whole.step")
    result = get_backend("occt").read(str(path))

    nodes = list(result.root.walk())
    assert result.root.name == "WINCH-100"
    assert len(nodes) > 70

    instances = {n.instance_name for n in nodes if n.instance_name}
    assert {"bearing_fwd", "bearing_aft", "drive_assy", "control_box"} <= instances

    # One bolt definition, many appearances -- the occurrence model survives.
    bolts = [n for n in nodes if n.name == "BOLT-M6X20"]
    assert len(bolts) > 20
    assert len({n.definition_key for n in bolts}) == 1


def test_exported_placements_match_the_occurrence_table(loaded):
    """The arrangement comes from the rows, so the rows must be what lands in
    the file."""
    db, report, d = loaded
    path = export.export_revision(db, report.root_revision, d / "placed.step")
    result = get_backend("occt").read(str(path))
    by_instance = {n.instance_name: n for n in result.root.walk() if n.instance_name}

    for instance, origin in (
        ("bearing_fwd", assembly.FORWARD_ORIGIN),
        ("bearing_aft", assembly.AFT_ORIGIN),
        ("control_box", (60.0, 200.0, 8.0)),
    ):
        assert by_instance[instance].transform[:3, 3] == pytest.approx(
            np.array(origin), abs=1e-6)


def test_a_subassembly_exports_only_its_own_contents(loaded):
    db, _, d = loaded
    path = export.export_revision(db, "GEARBOX", d / "gearbox.step")
    result = get_backend("occt").read(str(path))
    names = {n.name for n in result.root.walk()}

    assert result.root.name == "GEARBOX"
    assert {"GEARBOX-HOUSING", "GEARBOX-COVER", "OUTPUT-PINION"} <= names
    assert "BASE-PLATE" not in names          # not under this node


def test_a_full_round_trip_keeps_every_identity_and_the_root_hash(loaded, tmp_path):
    """Export the whole configuration, ingest the export into a fresh database,
    and the shapes and the root hash must both come out the same."""
    db, report, d = loaded
    path = export.export_revision(db, report.root_revision, d / "roundtrip.step")

    fresh = Database(tmp_path / "fresh.db", tmp_path / "blobs")
    again = bootstrap.load(fresh, str(path))

    before = {r["fingerprint"] for r in db.query("SELECT fingerprint FROM shape")}
    after = {r["fingerprint"] for r in fresh.query("SELECT fingerprint FROM shape")}
    assert after == before
    assert again.root_hash == report.root_hash
    fresh.close()


def test_the_cache_is_keyed_on_content(loaded):
    """Clicking twice must not re-export, and a name must not collide across
    two different configurations."""
    db, _, d = loaded
    first = export.export_cached(db, "BEARING-BLOCK", cache_dir=d / "cache")
    stamp = first.stat().st_mtime_ns
    second = export.export_cached(db, "BEARING-BLOCK", cache_dir=d / "cache")

    assert first == second
    assert second.stat().st_mtime_ns == stamp, "re-exported when it should have cached"

    other = export.export_cached(db, "DRUM", cache_dir=d / "cache")
    assert other != first


def test_exporting_something_with_no_geometry_is_refused(loaded):
    db, _, d = loaded
    with pytest.raises(LookupError):
        export.export_revision(db, "NO-SUCH-PART", d / "nope.step")


def test_export_does_not_invent_geometry(loaded):
    """Section 2: the system never creates geometry. Every solid written has to
    be one the store already held."""
    db, report, d = loaded
    path = export.export_revision(db, report.root_revision, d / "audit.step")
    result = get_backend("occt").read(str(path))

    stored = {r["fingerprint"] for r in db.query("SELECT fingerprint FROM shape")}
    for solid in result.solids:
        if not solid.valid:
            continue
        assert fp.identify(solid, deflection=0.2).strict in stored


# ------------------------------------------------------------------ launching


def test_find_cad_prefers_an_explicit_override(monkeypatch, tmp_path):
    fake = tmp_path / "pretend-cad.exe"
    fake.write_text("", encoding="utf-8")
    monkeypatch.setenv("INTERLOCK_CAD", str(fake))
    assert export.find_cad() == str(fake)


def test_open_in_cad_reports_rather_than_raises(monkeypatch, tmp_path):
    """A review interface should say it could not open a viewer, not 500."""
    monkeypatch.setattr(export, "find_cad", lambda: None)
    started, message = export.open_in_cad(tmp_path / "anything.step")
    assert not started
    assert "not found" in message.lower()

    monkeypatch.setattr(export, "find_cad", lambda: "/definitely/not/here")
    started, message = export.open_in_cad(tmp_path / "missing.step")
    assert not started and "nothing to open" in message
