"""The commit pipeline, contracts, concurrency and the document store, against
the demonstration assembly.

These are the behaviours the design document's section 16 asks each stage to
demonstrate, made re-runnable.
"""

from __future__ import annotations

import pytest

from interlock.db import documents
from interlock.db.database import Database
from interlock.model import concurrency, interference, merkle, tolerance
from interlock.model.commit import INTERNAL, Change, CommitPipeline, CommitRequest
from interlock.model.ingest import Ingestor
from interlock.query import traversal as q
from interlock.synthetic import assembly, bootstrap, cad


@pytest.fixture(scope="module")
def demo(tmp_path_factory):
    """Build the demo assembly once; the tests that mutate it use their own copy."""
    d = tmp_path_factory.mktemp("demo")
    step = assembly.write(str(d / "winch.step"))
    db = Database(d / "i.db", d / "blobs")
    report = bootstrap.load(db, step)
    bootstrap.subscribe_defaults(db)
    yield db, report, d
    db.close()


@pytest.fixture()
def fresh(tmp_path):
    """A private database, for tests that commit."""
    step = assembly.write(str(tmp_path / "winch.step"))
    db = Database(tmp_path / "i.db", tmp_path / "blobs")
    report = bootstrap.load(db, step)
    bootstrap.subscribe_defaults(db)
    yield db, report, tmp_path
    db.close()


# ------------------------------------------------------------------ bootstrap


def test_the_demo_assembly_loads_with_every_contract_satisfied(demo):
    db, report, _ = demo
    assert report.problems == [], report.problems
    assert report.contracts >= 20
    assert report.chains == 2
    assert db.scalar("SELECT COUNT(*) FROM part") >= 20
    assert db.scalar("SELECT COUNT(*) FROM team") == 4


def test_two_different_parts_share_one_shape_row(demo):
    """PLATE-A and PLATE-B are geometrically identical. Section 7: one shape row,
    two part rows -- dedup collapses the shape, never the part."""
    db, _, _ = demo
    a = db.scalar("""SELECT r.fingerprint FROM revision r JOIN part p USING(part_id)
                     WHERE p.part_number = 'PLATE-A'""")
    b = db.scalar("""SELECT r.fingerprint FROM revision r JOIN part p USING(part_id)
                     WHERE p.part_number = 'PLATE-B'""")
    assert a == b
    assert db.scalar("SELECT COUNT(*) FROM part WHERE part_number IN ('PLATE-A','PLATE-B')") == 2


def test_interfaces_are_measured_from_geometry_not_asserted(demo):
    db, _, _ = demo
    rev = q.resolve_revision(db, "BEARING-BLOCK")
    row = db.one("SELECT * FROM interface WHERE revision_id = ? AND name = 'foot'", (rev,))
    assert row["hole_count"] == 4
    assert row["hole_radius"] == pytest.approx(3.3, abs=1e-3)
    assert row["source"] == "declared"


def test_a_derived_socket_is_carried_into_the_parent_frame(demo):
    """The socket on WINCH-100 is a view of BASE-PLATE's interface, composed
    through the plate's placement."""
    db, report, _ = demo
    row = db.one(
        "SELECT * FROM socket WHERE revision_id = ? AND name = 'bearing_fwd'",
        (report.root_revision,))
    assert row["derivation"] == "derived"
    assert row["hole_count"] == 4


# ------------------------------------------------------------------ the tree


def test_reexport_with_a_different_child_order_keeps_the_root_hash(fresh, tmp_path):
    db, report, d = fresh
    other = assembly.write(str(d / "reexport.step"), shuffle_seed=11)
    ing = Ingestor(db)
    from interlock.model.ingest import begin_commit

    commit = begin_commit(db, "t", "reimport", "chassis")
    again = ing.ingest_file(str(other), "chassis", commit,
                            resolver=bootstrap.attrs_resolver(), status="released")
    assert again.shapes_new == 0
    assert again.revisions_created == 0
    assert again.root_hash == report.root_hash


def test_the_mirrored_bracket_does_not_collide_with_the_right_hand_one(tmp_path):
    """Section 15 lists mirrored parts as OPEN. They must be distinguishable."""
    rh = assembly.write(str(tmp_path / "rh.step"), "baseline")
    lh = assembly.write(str(tmp_path / "lh.step"), "left_hand")
    db = Database(tmp_path / "i.db", tmp_path / "blobs")
    bootstrap.load(db, rh)
    ing = Ingestor(db)
    from interlock.model.ingest import begin_commit

    commit = begin_commit(db, "t", "left hand", "drivetrain")
    ing.ingest_file(str(lh), "drivetrain", commit, resolver=bootstrap.attrs_resolver())

    right = db.scalar("""SELECT r.fingerprint FROM revision r JOIN part p USING(part_id)
                         WHERE p.part_number = 'MOTOR-BRACKET-RH'""")
    left = db.scalar("""SELECT r.fingerprint FROM revision r JOIN part p USING(part_id)
                        WHERE p.part_number = 'MOTOR-BRACKET-LH'""")
    assert right and left and right != left
    chir = {f: db.scalar("SELECT chirality FROM shape WHERE fingerprint = ?", (f,))
            for f in (right, left)}
    assert chir[right] == -chir[left] != 0
    db.close()


# ------------------------------------------------------------------- queries


def test_explosion_multiplies_quantities_and_rollup_matches(demo):
    db, report, _ = demo
    bom = q.bill_of_materials(db, report.root_revision)
    by_part = {r["part_number"]: r for r in bom}
    assert by_part["BOLT-M6X20"]["total_qty"] > 20
    roll = q.rollup(db, report.root_revision)
    assert roll.mass_g == pytest.approx(sum(r["total_mass_g"] for r in bom), rel=1e-6)
    assert roll.massless_parts == []


def test_an_assemblys_declared_power_is_not_counted_on_top_of_its_children(demo):
    """DRIVE-ASSY declares 750 W and contains a 750 W motor. The rollup is 750,
    not 1500: the assembly's declaration summarises its children."""
    db, _, _ = demo
    rev = q.resolve_revision(db, "DRIVE-ASSY")
    assert q.rollup(db, rev).power_w == pytest.approx(750.0)


def test_where_used_walks_up_and_impact_names_other_teams(demo):
    db, _, _ = demo
    used = {w.part_number for w in q.where_used(db, "BOLT-M6X20")}
    assert {"WINCH-100", "GEARBOX", "DRIVE-ASSY"} <= used
    teams = q.impact(db, "BASE-PLATE")
    assert "drivetrain" in teams or "controls" in teams


def test_cycle_guard_rejects_an_assembly_containing_itself(demo):
    db, report, _ = demo
    child = db.scalar("SELECT child_rev FROM occurrence WHERE parent_rev = ? LIMIT 1",
                      (report.root_revision,))
    assert q.would_create_cycle(db, child, report.root_revision)


# ------------------------------------------------------------- the pipeline


def _commit(db, pipeline, part, shape, author="t", team="chassis", decl=None, tmp=None):
    path = tmp / f"{part.lower()}_{author}.step"
    cad.write_step_part(shape, part, str(path))
    head = db.get_ref("main")
    return pipeline.submit(CommitRequest(
        author=author, team_id=team, message=f"change {part}",
        base_root=head["root_hash"],
        changes=[Change(part, step_path=str(path), declaration=decl)]))


def test_an_internal_change_lands_silently(fresh):
    db, _, d = fresh
    p = CommitPipeline(db)
    res = _commit(db, p, "GEARBOX-COVER", assembly.gearbox_cover(8.4),
                  author="ana", team="drivetrain", tmp=d)
    assert res.landed, res.reason
    assert res.classifications["GEARBOX-COVER"] == INTERNAL
    assert res.notified == []


def test_one_hole_moved_is_caught_by_the_cheap_spacing_check(fresh):
    db, _, d = fresh
    p = CommitPipeline(db)
    res = _commit(db, p, "BASE-PLATE", assembly.base_plate(hole_shift=(2.0, 0.0)), tmp=d)
    assert not res.landed
    failure = res.failure
    assert failure.stage == "socket" and "pattern" in failure.constraint_name
    assert failure.other_part == "BEARING-BLOCK" and failure.other_team == "chassis"


def test_a_rigidly_shifted_pattern_is_caught_only_by_the_placement_fit(fresh):
    """Spacings, hole sizes and envelope are all unchanged, so steps one to four
    pass. This is why placement is a verification, not an afterthought."""
    db, _, d = fresh
    p = CommitPipeline(db)
    res = _commit(db, p, "BASE-PLATE", assembly.base_plate(pattern_shift=(2.0, 0.0)), tmp=d)
    assert not res.landed
    socket_steps = [s for s in res.steps if s.stage == "socket"]
    failure = next(s for s in socket_steps if not s.passed)
    assert failure.constraint_name.endswith("alignment")
    assert "2.00 mm from its partner" in failure.detail


def test_a_commit_that_is_rejected_writes_nothing(fresh):
    db, _, d = fresh
    before = db.stats()
    p = CommitPipeline(db)
    res = _commit(db, p, "BASE-PLATE", assembly.base_plate(hole_shift=(2.0, 0.0)), tmp=d)
    assert not res.landed
    after = db.stats()
    assert after["revisions"] == before["revisions"]
    assert after["occurrences"] == before["occurrences"]
    # the rejection itself is recorded
    assert after["commits"] == before["commits"] + 1
    assert db.scalar("SELECT verdict FROM commit_log WHERE commit_id = ?",
                     (res.commit_id,)) == "rejected"


def test_a_declaration_the_part_cannot_meet_is_refused_naming_the_constraint(fresh):
    db, _, d = fresh
    p = CommitPipeline(db)
    decl = assembly.declarations()["BEARING-BLOCK"]
    decl.mass_max = 300.0
    res = _commit(db, p, "BEARING-BLOCK", assembly.bearing_block(), decl=decl, tmp=d)
    assert not res.landed
    assert res.failure.constraint_name == "mass_max"
    assert "300" in res.failure.detail


def test_bare_geometry_with_no_declaration_is_rejected(fresh):
    db, _, d = fresh
    p = CommitPipeline(db)
    path = d / "orphan.step"
    cad.write_step_part(cad.box(10, 10, 10), "BRAND-NEW-PART", str(path))
    res = p.submit(CommitRequest(
        author="t", team_id="chassis", message="bare geometry",
        changes=[Change("BRAND-NEW-PART", step_path=str(path), team_id="chassis")]))
    assert not res.landed
    assert res.failure.stage == "declaration"


def test_changing_a_leaf_rehashes_only_its_path_to_the_root(fresh):
    db, report, d = fresh
    before = merkle.build_from_occurrences(db, report.root_revision)
    p = CommitPipeline(db)
    res = _commit(db, p, "GEARBOX-COVER", assembly.gearbox_cover(8.4),
                  author="ana", team="drivetrain", tmp=d)
    assert res.landed
    after = merkle.build_from_occurrences(db, db.get_ref("main")["revision_id"])

    def hashes(tree):
        out = {}
        for n in tree.walk():
            out.setdefault(n.part_number, n.node_hash)
        return out

    a, b = hashes(before), hashes(after)
    changed = {k for k in a if a[k] != b.get(k)}
    assert changed == {"WINCH-100", "DRIVE-ASSY", "GEARBOX", "GEARBOX-COVER"}


def test_a_dry_run_reports_the_same_verdict_and_writes_nothing(fresh):
    db, _, d = fresh
    p = CommitPipeline(db)
    path = d / "dry.step"
    cad.write_step_part(assembly.base_plate(hole_shift=(2.0, 0.0)), "BASE-PLATE", str(path))
    before = db.stats()
    head = db.get_ref("main")
    res = p.dry_run(CommitRequest(
        author="t", team_id="chassis", message="would this work?",
        base_root=head["root_hash"], changes=[Change("BASE-PLATE", step_path=str(path))]))
    assert res.verdict == "rejected"          # a dry run still reports the failure
    assert db.stats()["revisions"] == before["revisions"]


def test_a_dry_run_of_a_good_change_would_land_and_still_writes_nothing(fresh):
    db, _, d = fresh
    p = CommitPipeline(db)
    path = d / "dry_ok.step"
    cad.write_step_part(assembly.gearbox_cover(8.4), "GEARBOX-COVER", str(path))
    before = db.stats()
    head = db.get_ref("main")
    res = p.dry_run(CommitRequest(
        author="t", team_id="drivetrain", message="would this work?",
        base_root=head["root_hash"], changes=[Change("GEARBOX-COVER", step_path=str(path))]))
    assert res.verdict == "would_land"
    assert db.stats()["revisions"] == before["revisions"]
    assert db.get_ref("main")["root_hash"] == head["root_hash"]


# ---------------------------------------------------------------- concurrency


def test_independent_commits_against_a_stale_root_both_land(fresh):
    db, _, d = fresh
    p = CommitPipeline(db)
    base = db.get_ref("main")["root_hash"]

    first = _commit(db, p, "CONTROL-PCB", assembly.control_pcb(2.0),
                    author="dee", team="controls", tmp=d)
    assert first.landed
    assert db.get_ref("main")["root_hash"] != base

    path = d / "pinion.step"
    cad.write_step_part(assembly.output_pinion(), "OUTPUT-PINION", str(path))
    second = p.submit(CommitRequest(
        author="eli", team_id="drivetrain", message="re-cut the pinion",
        base_root=base, changes=[Change("OUTPUT-PINION", step_path=str(path))]))
    assert second.landed, second.reason
    assert second.rebased


def test_two_commits_touching_the_same_part_conflict_and_name_the_author(fresh):
    db, _, d = fresh
    p = CommitPipeline(db)
    base = db.get_ref("main")["root_hash"]

    first = _commit(db, p, "CONTROL-PCB", assembly.control_pcb(1.8),
                    author="dee", team="controls", tmp=d)
    assert first.landed

    path = d / "pcb_b.step"
    cad.write_step_part(assembly.control_pcb(1.9), "CONTROL-PCB", str(path))
    second = p.submit(CommitRequest(
        author="fay", team_id="controls", message="different thickness",
        base_root=base, changes=[Change("CONTROL-PCB", step_path=str(path))]))
    assert not second.landed
    assert second.failure.stage == "concurrency"
    assert "dee" in second.failure.detail
    assert second.failure.other_part == "CONTROL-PCB"


def test_dependency_closure_widens_through_a_shared_chain(demo):
    db, _, _ = demo
    plate_a = db.scalar("SELECT part_id FROM part WHERE part_number = 'PLATE-A'")
    closure, why = concurrency.dependency_closure(db, {plate_a})
    numbers = {
        db.scalar("SELECT part_number FROM part WHERE part_id = ?", (p,)) for p in closure
    }
    assert {"SPACER-20", "BOLT-M6X32", "PLATE-B"} <= numbers


# ----------------------------------------------------------------- tolerance


def test_the_bolt_grip_chain_reproduces_the_design_documents_numbers(demo):
    """Section 11 prints: gap 0.20 mm nominal, worst case reaching -0.30 mm,
    statistical band sqrt(0.07) = 0.2646 reaching -0.06 mm."""
    db, _, _ = demo
    r = tolerance.evaluate(db, "bolt_grip_clearance")
    assert r.nominal == pytest.approx(0.20, abs=1e-9)
    assert r.worst_low == pytest.approx(-0.30, abs=1e-9)
    assert r.worst_high == pytest.approx(0.70, abs=1e-9)
    assert r.rss_half_band == pytest.approx(0.2646, abs=5e-4)
    assert r.rss_low == pytest.approx(-0.0646, abs=5e-4)
    assert not r.passes("worst_case")
    assert r.cross_team


def test_monte_carlo_agrees_with_rss_for_normal_contributors(demo):
    db, _, _ = demo
    r = tolerance.evaluate(db, "bolt_grip_clearance", samples=60000, seed=7)
    assert r.mc_low == pytest.approx(r.rss_low, abs=0.01)
    assert r.mc_high == pytest.approx(r.rss_high, abs=0.01)
    assert 0.0 < r.mc_failure_rate < 0.05


def test_the_sigma_convention_is_recorded_and_changes_what_the_band_means(fresh):
    """The convention is not decoration.

    Reported at its own span the RSS band is the same number either way -- the
    span cancels -- which is exactly why quoting the band alone is misleading.
    What changes is what the band *means*: at three sigma a +-0.10 part is held
    to a third of the spread it is at one sigma, so the same chain goes from
    failing about one assembly in ninety to failing about one in five.
    """
    db, _, _ = fresh
    three = tolerance.evaluate(db, "bolt_grip_clearance", samples=40000, seed=3)
    assert three.sigma_conventions == ("normal/3sigma",)

    db.execute("""UPDATE dimension SET sigma_span = 1.0 WHERE dimension_id IN
                  (SELECT dimension_id FROM chain_member cm
                   JOIN chain c ON c.chain_id = cm.chain_id
                   WHERE c.name = 'bolt_grip_clearance')""")
    one = tolerance.evaluate(db, "bolt_grip_clearance", samples=40000, seed=3)
    assert one.sigma_conventions == ("normal/1sigma",)

    # Same reported band, because the span cancels out of span * sigma_total.
    assert one.rss_half_band == pytest.approx(three.rss_half_band, rel=1e-9)
    # Completely different risk.
    assert three.mc_failure_rate < 0.03
    assert one.mc_failure_rate > 0.15


def test_cross_team_chains_are_ranked_first(demo):
    db, _, _ = demo
    results = tolerance.evaluate_all(db)
    assert results[0].cross_team


# --------------------------------------------------------------- interference


def test_the_baseline_assembly_has_no_clashes_and_the_prefilter_does_work(demo):
    db, report, _ = demo
    rep = interference.check(db, report.root_revision, exact=True, store=False)
    assert rep.clashes == []
    # The prefilter must actually discard most pairs, and the exact test must
    # actually clear some -- otherwise neither stage is being exercised.
    assert rep.pairs_after_prefilter < rep.pairs_considered
    assert rep.exact_tests > 0


def test_interference_is_recorded_as_advisory(fresh):
    db, report, _ = fresh
    interference.check(db, report.root_revision, exact=False, clearance=5.0, store=True)
    rows = interference.stored(db, report.root_revision)
    assert all(r["label"] == "advisory" for r in rows)


# ------------------------------------------------------------- document store


def test_the_outbox_only_delivers_after_the_commit_lands(fresh, tmp_path):
    db, _, d = fresh
    store = documents.open_document_store(d / "documents.json")
    p = CommitPipeline(db)

    rejected = _commit(db, p, "BASE-PLATE", assembly.base_plate(hole_shift=(2.0, 0.0)), tmp=d)
    assert not rejected.landed
    landed = _commit(db, p, "GEARBOX-COVER", assembly.gearbox_cover(8.4),
                     author="ana", team="drivetrain", tmp=d)
    assert landed.landed

    documents.drain(db, store)
    traces = store.all(documents.TOPIC_VALIDATION)
    verdicts = {t["commit_id"]: t["verdict"] for t in traces}
    assert verdicts[landed.commit_id] == "landed"
    assert verdicts[rejected.commit_id] == "rejected"
    assert documents.pending_count(db) == 0
    store.close()


def test_draining_twice_does_not_duplicate_documents(fresh, tmp_path):
    db, _, d = fresh
    store = documents.open_document_store(d / "documents.json")
    p = CommitPipeline(db)
    _commit(db, p, "GEARBOX-COVER", assembly.gearbox_cover(8.4),
            author="ana", team="drivetrain", tmp=d)
    documents.drain(db, store)
    first = store.count(documents.TOPIC_VALIDATION)
    documents.drain(db, store)
    assert store.count(documents.TOPIC_VALIDATION) == first
    store.close()


def test_nothing_validation_reads_lives_in_the_document_store(fresh, tmp_path):
    """The rule that decides the split: anything read during commit validation
    stays in SQL, because two stores cannot share one transaction."""
    db, _, d = fresh
    store = documents.open_document_store(d / "documents.json")
    documents.drain(db, store)
    assert set(store.collections()) <= {
        documents.TOPIC_VALIDATION, documents.TOPIC_NOTIFICATION,
        documents.TOPIC_NL_CALL, documents.TOPIC_METADATA, "contract_metadata",
    }
    store.close()
