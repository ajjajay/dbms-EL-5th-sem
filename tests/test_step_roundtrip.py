"""What the exported STEP actually carries.

Section 4 claims a STEP assembly carries product structure natively: "The
hierarchy, the part names and the placement of each child relative to its parent
all survive the round trip." The design document marks that VERIFIED, on a
three-part assembly.

These tests make the claim re-runnable on the full twenty-part demonstration
assembly, reading each file back through a *cold* kernel session -- a reader that
has never seen the document that wrote it. That is the strongest statement this
project can make about its own export without a second vendor's CAD system
opening the file, and it is the check a third-party viewer would corroborate
rather than replace.
"""

from __future__ import annotations

import numpy as np
import pytest

from interlock.db.database import Database
from interlock.kernel import get_backend
from interlock.model.ingest import Ingestor, begin_commit
from interlock.synthetic import assembly, bootstrap


@pytest.fixture(scope="module")
def exported(tmp_path_factory):
    d = tmp_path_factory.mktemp("roundtrip")
    path = assembly.write(str(d / "winch_100.step"))
    return path, d


def test_the_file_is_a_conformant_step_assembly(exported):
    """Header, schema and assembly usage occurrences, read as text.

    Checked at the file level rather than through the kernel, because a reader
    can be lenient in ways a different vendor's will not be.
    """
    path, _ = exported
    text = open(path, encoding="utf-8", errors="replace").read()

    assert text.startswith("ISO-10303-21;")
    assert text.rstrip().endswith("END-ISO-10303-21;")
    for section in ("HEADER;", "FILE_DESCRIPTION", "FILE_SCHEMA", "DATA;", "ENDSEC;"):
        assert section in text, f"missing {section}"

    # Product structure, which is the thing section 4 depends on.
    assert "NEXT_ASSEMBLY_USAGE_OCCURRENCE" in text
    assert "ADVANCED_BREP_SHAPE_REPRESENTATION" in text
    # Instance names travel on the usage occurrences.
    for instance in ("bearing_fwd", "drive_assy", "control_box", "block_bolt_0"):
        assert instance in text, f"instance name {instance!r} did not survive"
    # Part definition names.
    for part in ("BASE-PLATE", "BEARING-BLOCK", "MOTOR-BRACKET-RH", "BOLT-M6X20"):
        assert part in text, f"part name {part!r} did not survive"


def test_a_cold_reader_recovers_the_whole_tree(exported):
    """A kernel session that never saw the writing document."""
    path, _ = exported
    result = get_backend("occt").read(path)

    nodes = list(result.root.walk())
    assert result.root.name == "WINCH-100"
    assert len(nodes) > 70, f"only {len(nodes)} nodes recovered"

    # Distinct part definitions, not appearances.
    definitions = {n.definition_key for n in nodes if n.definition_key}
    assert len(definitions) >= 25

    # One bolt definition, many appearances: the property that makes forty bolts
    # one revision and forty occurrences.
    bolts = [n for n in nodes if n.name == "BOLT-M6X20"]
    assert len(bolts) > 20
    assert len({n.definition_key for n in bolts}) == 1

    # Named instances survived onto the components.
    named = {n.instance_name for n in nodes if n.instance_name}
    assert {"bearing_fwd", "bearing_aft", "drive_assy", "control_box"} <= named


def test_placements_survive_to_the_declared_tolerance(exported):
    """Each child's transform relative to its parent, compared with what was
    written. The quantisation grid is 1 um / 10 urad, so the round trip has to be
    at least that faithful or the Merkle hash would be unstable across exports."""
    path, _ = exported
    result = get_backend("occt").read(path)
    by_instance = {n.instance_name: n for n in result.root.walk() if n.instance_name}

    expected = {
        "bearing_fwd": assembly.FORWARD_ORIGIN,
        "bearing_aft": assembly.AFT_ORIGIN,
        "drum_assy": (112.0, assembly.AXIS_Y, assembly.AXIS_Z),
        "control_box": (60.0, 200.0, 8.0),
        "clamp_stack": (330.0, 200.0, 8.0),
    }
    for instance, origin in expected.items():
        node = by_instance[instance]
        assert node.transform[:3, 3] == pytest.approx(np.array(origin), abs=1e-6)
        # And the rotation is still a proper rotation, not a drifted matrix.
        r = node.transform[:3, :3]
        assert r @ r.T == pytest.approx(np.eye(3), abs=1e-9)
        assert np.linalg.det(r) == pytest.approx(1.0, abs=1e-9)


def test_every_generated_file_reads_back_as_valid_solids(exported):
    """Including the deliberately broken variants: a rejected part must still be
    a well-formed file. It is refused for what it says, not for being corrupt."""
    _, d = exported
    backend = get_backend("occt")
    files = {
        "baseline": assembly.write(str(d / "a.step"), "baseline"),
        "hole_moved": assembly.write(str(d / "b.step"), "hole_moved"),
        "pattern_shifted": assembly.write(str(d / "c.step"), "pattern_shifted"),
        "left_hand": assembly.write(str(d / "e.step"), "left_hand"),
    }
    for name, path in files.items():
        result = backend.read(path)
        solids = [s for s in result.solids if s.valid]
        assert len(solids) >= 18, f"{name}: only {len(solids)} valid solids"
        assert all(s.mass.volume > 0 for s in solids), name
        assert not [s for s in result.solids if not s.valid], f"{name} produced an invalid body"


def test_a_full_export_import_export_cycle_is_stable(tmp_path):
    """Write, ingest, and write again from what was stored: the root hash must
    not move. This is the property that makes the fingerprint a primary key
    rather than a checksum of one particular file."""
    first = assembly.write(str(tmp_path / "first.step"))
    db = Database(tmp_path / "i.db", tmp_path / "blobs")
    original = bootstrap.load(db, first)

    second = assembly.write(str(tmp_path / "second.step"), shuffle_seed=99)
    ing = Ingestor(db)
    commit = begin_commit(db, "t", "round trip", "chassis")
    again = ing.ingest_file(str(second), "chassis", commit,
                            resolver=bootstrap.attrs_resolver(), status="released")

    assert again.root_hash == original.root_hash
    assert again.shapes_new == 0
    assert again.revisions_created == 0
    db.close()


def test_blobs_read_back_as_the_same_geometry(tmp_path):
    """The content-addressed store is not write-only: a solid read back out has
    to be the same part, or the interference stage and contract re-derivation are
    both built on sand."""
    from interlock.geometry import fingerprint as fp

    path = assembly.write(str(tmp_path / "w.step"))
    db = Database(tmp_path / "i.db", tmp_path / "blobs")
    bootstrap.load(db, path)
    backend = get_backend("occt")

    checked = 0
    for row in db.query("SELECT fingerprint FROM shape LIMIT 6"):
        shape = db.read_blob(row["fingerprint"])
        assert shape is not None
        solid = backend.solid_record_from_shape(shape, "recovered")
        assert solid is not None and solid.valid
        assert fp.identify(solid, deflection=0.2).strict == row["fingerprint"]
        checked += 1
    assert checked == 6
    db.close()


def test_mass_uses_the_kernels_exact_volume_not_the_mesh(tmp_path):
    """Found by cross-checking against FreeCAD.

    `shape.volume` comes from the triangulated mesh, because the whole invariant
    vector is computed from that mesh and has to stay internally consistent.
    But a tessellated cylinder is about half a percent light, and a mass budget
    is an integrity constraint here -- so mass reads `volume_exact`, which is the
    kernel's own integration over the exact surfaces.
    """
    from interlock.kernel import get_backend
    from interlock.synthetic import assembly as A

    path = A.write(str(tmp_path / "w.step"))
    db = Database(tmp_path / "i.db", tmp_path / "blobs")
    bootstrap.load(db, path)
    backend = get_backend("occt")

    rows = db.query(
        """SELECT s.fingerprint, s.volume, s.volume_exact, p.part_number
           FROM shape s
           JOIN revision r ON r.fingerprint = s.fingerprint
           JOIN part p ON p.part_id = r.part_id
           WHERE p.part_number IN ('DRUM', 'BASE-PLATE', 'WASHER-M6')""")
    assert rows

    for row in rows:
        # The exact volume must match what the kernel says about the stored blob.
        shape = db.read_blob(row["fingerprint"])
        solid = backend.solid_record_from_shape(shape, row["part_number"])
        assert row["volume_exact"] == pytest.approx(solid.mass.volume, rel=1e-12)

        if row["part_number"] == "DRUM":
            # A cylinder: the mesh is measurably light, which is the whole point.
            assert row["volume"] < row["volume_exact"]
            error = (row["volume_exact"] - row["volume"]) / row["volume_exact"]
            assert 1e-3 < error < 2e-2, f"unexpected tessellation error {error:.2e}"
        if row["part_number"] == "BASE-PLATE":
            # Flat faces tessellate exactly, so the two agree closely.
            assert row["volume"] == pytest.approx(row["volume_exact"], rel=1e-4)
    db.close()


def test_rollups_use_the_exact_volume(tmp_path):
    """The mass a budget is checked against must be the accurate one."""
    from interlock.query import traversal as q

    path = assembly.write(str(tmp_path / "w.step"))
    db = Database(tmp_path / "i.db", tmp_path / "blobs")
    report = bootstrap.load(db, path)

    exact = 0.0
    for inst in q.configuration(db, report.root_revision):
        if inst.is_assembly or not inst.fingerprint:
            continue
        row = db.one(
            """SELECT s.volume_exact, r.density FROM shape s
               JOIN revision r ON r.revision_id = ?
               WHERE s.fingerprint = ?""", (inst.revision_id, inst.fingerprint))
        exact += float(row["volume_exact"]) * float(row["density"])

    assert q.rollup(db, report.root_revision).mass_g == pytest.approx(exact, rel=1e-9)
    bom_total = sum(r["total_mass_g"] for r in q.bill_of_materials(db, report.root_revision))
    assert bom_total == pytest.approx(exact, rel=1e-9)
    db.close()
