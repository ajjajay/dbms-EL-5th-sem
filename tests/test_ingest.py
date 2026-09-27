"""First end-to-end runs of ingest and the Merkle tree on real STEP files."""

from __future__ import annotations

import numpy as np
import pytest

from interlock.model import merkle
from interlock.model.ingest import Ingestor, begin_commit, ensure_team
from interlock.synthetic import cad



def build_assembly(n_bolts=6, bolt_len=20.0, plate_t=6.0):
    """A plate with several identical bolts (one definition, many placements)
    plus a lid subassembly, mirroring section 6's gearbox example in miniature."""
    plate = cad.plate(80, 40, plate_t, holes=[(10 + 12 * i, 20) for i in range(n_bolts)], hole_r=3.3)
    bolt = cad.cylinder(3.0, bolt_len)
    lid = cad.box(30, 30, 3)
    cover = cad.Node("COVER").add("lid", cad.Node("LID", lid), cad.translation(0, 0, 0))
    cover.add("bolt_cov", cad.Node("BOLT", bolt), cad.translation(15, 15, 3))
    bolt_node = cad.Node("BOLT", bolt)
    root = cad.Node("GEARBOX")
    root.add("housing", cad.Node("HOUSING", plate), cad.translation(0, 0, 0))
    root.add("cover", cover, cad.translation(0, 0, plate_t))
    for i in range(n_bolts):
        root.add(f"bolt_{i}", bolt_node, cad.translation(10 + 12 * i, 20, plate_t))
    return root


@pytest.fixture()
def ingestor(db, backend):
    ensure_team(db, "T")
    return Ingestor(db, backend)


def _ingest(ingestor, path, author="t"):
    commit = begin_commit(ingestor.db, author, f"import {path}", "T")
    report = ingestor.ingest_file(str(path), "T", commit)
    ingestor.db.execute("UPDATE commit_log SET new_root = ? WHERE commit_id = ?", (report.root_hash, commit))
    return report


def test_step_round_trip_reads_hierarchy_names_and_shared_definitions(backend, tmp_path):
    path = cad.write_step(build_assembly(), str(tmp_path / "a.step"))
    result = backend.read(path)
    names = sorted(n.instance_name for n in result.root.walk() if n.instance_name)
    assert "bolt_0" in names and "housing" in names
    bolt_nodes = [n for n in result.root.walk() if n.name == "BOLT"]
    assert len({n.definition_key for n in bolt_nodes}) == 1      # shared definition


def test_forty_bolts_are_one_part_one_revision_forty_occurrences(ingestor, tmp_path):
    path = cad.write_step(build_assembly(n_bolts=40), str(tmp_path / "a.step"))
    report = _ingest(ingestor, path)
    db = ingestor.db

    assert db.scalar("SELECT COUNT(*) FROM part WHERE part_number = 'BOLT'") == 1
    bolt_rev = db.scalar(
        "SELECT r.revision_id FROM revision r JOIN part p USING(part_id) WHERE p.part_number = 'BOLT'"
    )
    assert bolt_rev is not None
    root_uses = db.scalar(
        "SELECT COUNT(*) FROM occurrence o JOIN revision r ON r.revision_id = o.parent_rev "
        "JOIN part p ON p.part_id = r.part_id WHERE p.part_number = 'GEARBOX' AND o.child_rev = ?",
        (bolt_rev,),
    )
    assert root_uses == 40
    assert report.shapes_new == 3          # housing, bolt, lid: the bolt shape is stored once
    assert report.root_hash


def test_importing_twice_creates_nothing_new(ingestor, tmp_path):
    a = cad.write_step(build_assembly(), str(tmp_path / "a.step"))
    first = _ingest(ingestor, a)
    stats_before = ingestor.db.stats()

    b = cad.write_step(build_assembly(), str(tmp_path / "b.step"))
    second = _ingest(ingestor, b)
    stats_after = ingestor.db.stats()

    assert second.shapes_new == 0 and second.shapes_exact_hit == first.solids_read
    assert second.revisions_created == 0 and second.parts_created == 0
    assert second.root_hash == first.root_hash
    assert stats_after["revisions"] == stats_before["revisions"]
    assert stats_after["shapes"] == stats_before["shapes"]


def test_reexport_with_shuffled_children_has_the_same_root_hash(ingestor, tmp_path):
    tree = build_assembly()
    a = cad.write_step(tree, str(tmp_path / "a.step"))
    b = cad.write_step(tree, str(tmp_path / "b.step"), shuffle_seed=7)
    ra = _ingest(ingestor, a)
    rb = _ingest(ingestor, b)
    assert ra.root_hash == rb.root_hash
    assert rb.revisions_created == 0


def test_thickening_one_leaf_rehashes_only_its_path(ingestor, tmp_path):
    a = _ingest(ingestor, cad.write_step(build_assembly(), str(tmp_path / "a.step")))
    thick = build_assembly()
    # thicken the lid inside the COVER subassembly
    cover = next(n for _, n, _ in thick.children if n.name == "COVER")
    lid_node = next(n for _, n, _ in cover.children if n.name == "LID")
    lid_node.shape = cad.box(30, 30, 4)
    b = _ingest(ingestor, cad.write_step(thick, str(tmp_path / "b.step")))

    ta = merkle.build_from_occurrences(ingestor.db, a.root_revision)
    tb = merkle.build_from_occurrences(ingestor.db, b.root_revision)

    def hashes(tree):
        return {n.part_number: n.node_hash for n in tree.walk()}

    ha, hb = hashes(ta), hashes(tb)
    changed = {name for name in ha if ha[name] != hb[name]}
    assert changed == {"GEARBOX", "COVER", "LID"}
    assert ha["HOUSING"] == hb["HOUSING"] and ha["BOLT"] == hb["BOLT"]


def test_diff_finds_the_change_and_ignores_reordering(ingestor, tmp_path):
    a = _ingest(ingestor, cad.write_step(build_assembly(), str(tmp_path / "a.step")))
    thick = build_assembly()
    cover = next(n for _, n, _ in thick.children if n.name == "COVER")
    next(n for _, n, _ in cover.children if n.name == "LID").shape = cad.box(30, 30, 4)
    b = _ingest(ingestor, cad.write_step(thick, str(tmp_path / "b.step"), shuffle_seed=3))

    ta = merkle.build_from_occurrences(ingestor.db, a.root_revision)
    tb = merkle.build_from_occurrences(ingestor.db, b.root_revision)
    diffs = merkle.diff(ta, tb)
    assert [d.part_number for d in diffs] == ["LID"]
    assert diffs[0].kind == "changed"


def test_diff_sees_a_change_among_forty_identically_named_bolts(ingestor, tmp_path):
    """Known problem 1: a name-keyed dict lets forty same-named bolts overwrite
    each other. One moved bolt among forty must still be found."""
    tree = build_assembly(n_bolts=40)
    a = _ingest(ingestor, cad.write_step(tree, str(tmp_path / "a.step")))

    moved_tree = build_assembly(n_bolts=40)
    inst, node, matrix = moved_tree.children[-1]          # the last bolt
    moved_tree.children[-1] = (inst, node, cad.translation(500, 500, 500))
    b = _ingest(ingestor, cad.write_step(moved_tree, str(tmp_path / "b.step")))

    ta = merkle.build_from_occurrences(ingestor.db, a.root_revision)
    tb = merkle.build_from_occurrences(ingestor.db, b.root_revision)
    diffs = merkle.diff(ta, tb)
    assert len(diffs) == 1 and diffs[0].kind == "moved"
    assert diffs[0].part_number == "BOLT"


def test_quantised_placement_absorbs_reexport_jitter(ingestor, tmp_path):
    tree = build_assembly()
    a = _ingest(ingestor, cad.write_step(tree, str(tmp_path / "a.step")))

    jittered = build_assembly()
    rng = np.random.default_rng(1)
    jittered.children = [
        (n, c, m @ cad.translation(*rng.normal(0, 1e-8, 3))) for n, c, m in jittered.children
    ]
    b = _ingest(ingestor, cad.write_step(jittered, str(tmp_path / "b.step")))
    assert a.root_hash == b.root_hash
