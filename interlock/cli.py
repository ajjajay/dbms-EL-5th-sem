"""Command line interface.

    python -m interlock.cli demo          build the demo database and run five stages
    python -m interlock.cli serve         start the review interface
    python -m interlock.cli ingest FILE   ingest a STEP file
    python -m interlock.cli commit PART FILE   propose a change, validated
    python -m interlock.cli export PART   write it back out as STEP (--open in FreeCAD)
    python -m interlock.cli bom           bill of materials
    python -m interlock.cli where-used P  who contains this part
    python -m interlock.cli impact P      who a contract change would break
    python -m interlock.cli chains        total every tolerance chain
    python -m interlock.cli tree          the hashed configuration tree
    python -m interlock.cli interference  advisory clash check
    python -m interlock.cli ask "..."     natural language question
    python -m interlock.cli log           the commit log
    python -m interlock.cli why COMMIT    a commit's full validation trace
    python -m interlock.cli drain         deliver the outbox to the document store
    python -m interlock.cli stats         row counts and storage
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .db.database import DEFAULT_DB, Database
from .db.documents import drain as drain_outbox
from .db.documents import open_document_store, pending_count
from .model import interference as interf
from .model import merkle, tolerance
from .query import traversal as q

DEMO_DIR = Path("data") / "demo"


def _db(args) -> Database:
    return Database(args.db)


def _head(db: Database, ref: str = "main"):
    row = db.get_ref(ref)
    if row is None:
        sys.exit("no configuration loaded; run `python -m interlock.cli demo` first")
    return row


# --------------------------------------------------------------------- demo


def cmd_demo(args) -> int:
    """Build the demonstration database and walk section 16's five stages."""
    from .synthetic import assembly, bootstrap
    from .synthetic import cad

    out = Path(args.out or DEMO_DIR)
    out.mkdir(parents=True, exist_ok=True)
    db_path = Path(args.db)
    if db_path.exists() and not args.keep:
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(db_path) + suffix)
            if not p.exists():
                continue
            try:
                p.unlink()
            except PermissionError:
                # Windows will not unlink a file another process has open, and
                # the most likely other process is a review server the user
                # started earlier. Say so rather than showing a traceback.
                sys.exit(
                    f"cannot rebuild {db_path}: it is open in another process.\n"
                    "A running `interlock serve` holds the database. Stop it, or "
                    "pass --keep to build on top of what is already there, or "
                    "use --db to write somewhere else."
                )

    db = Database(db_path)
    print("=" * 78)
    print("STAGE ONE  -  IDENTITY")
    print("=" * 78)
    baseline = assembly.write(str(out / "winch_100.step"))
    print(f"wrote {baseline} ({os.path.getsize(baseline) // 1024} KB)")

    report = bootstrap.load(db, baseline, author="demo")
    print(report.ingest_summary)
    print(f"contracts authored: {report.contracts}, chains: {report.chains}")
    print(f"root hash: {report.root_hash}")
    for p in report.problems:
        print(f"  problem: {p}")

    # The same assembly re-exported with its components in a different order.
    reexport = assembly.write(str(out / "winch_100_reexport.step"), shuffle_seed=17)
    from .model.ingest import Ingestor, begin_commit

    ing = Ingestor(db)
    commit = begin_commit(db, "demo", "re-import the same assembly", "chassis")
    again = ing.ingest_file(str(reexport), "chassis", commit,
                            resolver=bootstrap.attrs_resolver(), status="released")
    print("\nre-imported the same assembly, exported with a different child order:")
    print(f"  {again.summary()}")
    print(f"  root hash: {again.root_hash}  "
          f"({'identical' if again.root_hash == report.root_hash else 'DIFFERENT'})")
    print("  every shape was recognised as already known, so nothing new was stored.")

    print()
    print("=" * 78)
    print("STAGE TWO  -  STRUCTURE")
    print("=" * 78)
    root = report.root_revision
    bom = q.bill_of_materials(db, root)
    print(f"{len(bom)} distinct parts; the four most numerous:")
    for r in sorted(bom, key=lambda r: -r["total_qty"])[:4]:
        print(f"  {r['part_number']:<20} x{r['total_qty']:<4} {r['team_id']}")
    roll = q.rollup(db, root)
    print(f"mass {roll.mass_g:.0f} g, power {roll.power_w:.0f} W, thermal {roll.thermal_w:.0f} W")
    print(f"centre of gravity ({roll.cg[0]:.1f}, {roll.cg[1]:.1f}, {roll.cg[2]:.1f}) mm")
    used = q.where_used(db, "BOLT-M6X20")
    print(f"BOLT-M6X20 is used by {len(used)} assemblies: "
          f"{', '.join(u.part_number for u in used)}")

    print()
    print("=" * 78)
    print("STAGE THREE  -  CONTRACTS")
    print("=" * 78)
    from .model.commit import Change, CommitPipeline, CommitRequest

    pipeline = CommitPipeline(db, ingestor=ing)
    head = db.get_ref("main")
    cover = out / "gearbox_cover_thin.step"
    cad.write_step_part(assembly.gearbox_cover(8.4), "GEARBOX-COVER", str(cover))
    res = pipeline.submit(CommitRequest(
        author="ana", team_id="drivetrain", message="skim the cover 0.4 mm",
        base_root=head["root_hash"], changes=[Change("GEARBOX-COVER", step_path=str(cover))]))
    print(f"internal change: {res.verdict}  ({res.classifications})")
    print("  the body changed and the contract did not, so it lands silently.")

    bad_plate = out / "base_plate_bad_hole.step"
    cad.write_step_part(assembly.base_plate(hole_shift=(2.0, 0.0)), "BASE-PLATE", str(bad_plate))
    head = db.get_ref("main")
    res = pipeline.submit(CommitRequest(
        author="bo", team_id="chassis", message="move one mounting hole 2 mm",
        base_root=head["root_hash"], changes=[Change("BASE-PLATE", step_path=str(bad_plate))]))
    print(f"\none hole moved 2 mm: {res.verdict}")
    print(f"  {res.reason}")
    print("  caught by the cheap spacing comparison, before any geometry was fitted.")

    shifted = out / "base_plate_shifted_pattern.step"
    cad.write_step_part(assembly.base_plate(pattern_shift=(2.0, 0.0)), "BASE-PLATE", str(shifted))
    res = pipeline.submit(CommitRequest(
        author="bo", team_id="chassis", message="shift the whole bearing pattern 2 mm",
        base_root=head["root_hash"], changes=[Change("BASE-PLATE", step_path=str(shifted))]))
    print(f"\nwhole pattern shifted 2 mm: {res.verdict}")
    for s in res.steps:
        if s.stage == "socket":
            print(f"  {'ok  ' if s.passed else 'FAIL'} {s.detail}")
    print("  spacings, hole sizes and envelope are all unchanged, so steps one to four")
    print("  pass. Only measuring the holes against their partners under the placement")
    print("  the assembly actually applies catches it.")

    print()
    print("=" * 78)
    print("STAGE FOUR  -  TEAMS")
    print("=" * 78)
    bootstrap.subscribe_defaults(db)
    head = db.get_ref("main")
    heavier = out / "bearing_block_heavy.step"
    cad.write_step_part(assembly.bearing_block(), "BEARING-BLOCK", str(heavier))
    decl = assembly.declarations()["BEARING-BLOCK"]
    decl.mass_max = 300.0          # tightening a published limit is a contract change
    res = pipeline.submit(CommitRequest(
        author="cy", team_id="chassis", message="tighten the bearing block mass limit",
        base_root=head["root_hash"],
        changes=[Change("BEARING-BLOCK", step_path=str(heavier), declaration=decl)]))
    print(f"contract change: {res.verdict}")
    if res.reason:
        print(f"  {res.reason}")
    print("  a published limit the part itself cannot meet is refused, and the refusal")
    print("  names the constraint rather than saying 'invalid'.")

    print("\nconcurrency: two commits written against the same root")
    stale_root = db.get_ref("main")["root_hash"]
    pcb = out / "control_pcb_v2.step"
    # A real change, so the root genuinely moves and the second commit is
    # written against a root that no longer exists.
    cad.write_step_part(assembly.control_pcb(2.0), "CONTROL-PCB", str(pcb))
    first = pipeline.submit(CommitRequest(
        author="dee", team_id="controls", message="respin the PCB",
        base_root=stale_root, changes=[Change("CONTROL-PCB", step_path=str(pcb))]))
    print(f"  first commit (controls, touches CONTROL-PCB): {first.verdict}")

    print(f"    root after the first commit: {db.get_ref('main')['root_hash']} "
          f"(was {stale_root})")
    pinion = out / "output_pinion_v2.step"
    cad.write_step_part(assembly.output_pinion(), "OUTPUT-PINION", str(pinion))
    second = pipeline.submit(CommitRequest(
        author="eli", team_id="drivetrain", message="re-cut the pinion",
        base_root=stale_root, changes=[Change("OUTPUT-PINION", step_path=str(pinion))]))
    print(f"  second commit, written against the same (now stale) root: {second.verdict}")
    for s in second.steps:
        if s.stage == "concurrency":
            print(f"    {s.detail}")
    print("  the root moved, but the two commits touch unrelated parts and nothing")
    print("  couples them, so the second rebases instead of being rejected.")

    print()
    print("the same again, but both commits touch the same part")
    contested = db.get_ref("main")["root_hash"]
    pcb_a = out / "control_pcb_v3.step"
    cad.write_step_part(assembly.control_pcb(1.8), "CONTROL-PCB", str(pcb_a))
    a = pipeline.submit(CommitRequest(
        author="dee", team_id="controls", message="thin the PCB to 1.8",
        base_root=contested, changes=[Change("CONTROL-PCB", step_path=str(pcb_a))]))
    print(f"  dee commits first: {a.verdict}")
    pcb_b = out / "control_pcb_v4.step"
    cad.write_step_part(assembly.control_pcb(1.9), "CONTROL-PCB", str(pcb_b))
    b = pipeline.submit(CommitRequest(
        author="fay", team_id="controls", message="thin the PCB to 1.9",
        base_root=contested, changes=[Change("CONTROL-PCB", step_path=str(pcb_b))]))
    print(f"  fay commits against the same root: {b.verdict}")
    print(f"    {b.reason}")
    print("  a real conflict, and the rejection names the part and the other author")
    print("  rather than reporting that a hash did not match.")

    print()
    print("=" * 78)
    print("STAGE FIVE  -  TOLERANCE, INTERFERENCE, LANGUAGE")
    print("=" * 78)
    for r in tolerance.evaluate_all(db):
        print(f"  {'PASS' if r.passes() else 'FAIL'} {r.explain()}")
        if r.name == "bolt_grip_clearance":
            print(f"        worst case  {r.worst_low:+.4f} .. {r.worst_high:+.4f} mm")
            print(f"        statistical {r.rss_low:+.4f} .. {r.rss_high:+.4f} mm "
                  f"(sqrt of the sum of squares = {r.rss_half_band:.4f})")
            print(f"        Monte Carlo {r.mc_low:+.4f} .. {r.mc_high:+.4f} mm "
                  f"over {r.mc_samples} samples")
            print("        these are the design document's own worked numbers, recomputed.")

    head = db.get_ref("main")
    rep = interf.check(db, head["revision_id"], exact=not args.fast)
    print(f"\ninterference (advisory): {rep.summary()}")
    for c in rep.clashes[:5]:
        print(f"  {c.describe()}")

    from .nl.agent import Agent

    agent = Agent(db, pipeline=pipeline)
    print(f"\n{agent.status()}")
    for question in ("do any tolerance chains fail?", "who uses BOLT-M6X20?"):
        answer = agent.ask(question)
        print(f"  Q: {question}")
        print(f"     tools: {[c.tool for c in answer.calls]}")
        print("     " + answer.text.splitlines()[0][:100])

    store = open_document_store(Path(args.db).parent / "documents.json")
    drained = drain_outbox(db, store)
    print(f"\ndocument store ({store.name}): delivered {drained.delivered} document(s); "
          f"collections {store.collections()}")
    store.close()

    print()
    print("=" * 78)
    s = db.stats()
    print(f"{s['parts']} parts, {s['revisions']} revisions, {s['shapes']} distinct shapes, "
          f"{s['occurrences']} occurrences, {s['commits']} commits")
    print(f"database {s['db_bytes'] / 1e6:.2f} MB, blobs {s['blob_bytes'] / 1e6:.2f} MB")
    print(f"\nreview it:  {sys.executable} -m interlock.cli serve --db {args.db}")
    db.close()
    return 0


# ------------------------------------------------------------------ commits


def cmd_commit(args) -> int:
    """Submit a change for validation: the operation the whole system exists for.

    `ingest` puts geometry in the database. `commit` proposes a change to a part
    that already exists and runs it through the pipeline, so it lands whole,
    is rejected whole with the constraint and the other party named, or -- with
    --dry-run -- reports what would have happened and writes nothing.
    """
    from .model.commit import Change, CommitPipeline, CommitRequest

    db = _db(args)
    head = db.get_ref(args.ref)
    pipeline = CommitPipeline(db)

    # Commits are attributed to a real team, so default to whoever owns the part
    # rather than inventing one the commit log cannot reference.
    team = args.team or db.scalar(
        "SELECT team_id FROM part WHERE part_number = ?", (args.part,))
    if not team:
        sys.exit(
            f"{args.part} is not in the database and no --team was given.\n"
            "Use `ingest` to add a new part, or pass --team to create one here."
        )

    declaration = None
    if args.declaration:
        import json

        from .model.contracts import Declaration

        declaration = Declaration.from_dict(
            json.loads(Path(args.declaration).read_text(encoding="utf-8")))

    request = CommitRequest(
        author=args.author,
        team_id=team,
        message=args.message or f"update {args.part}",
        base_root=args.base_root or (head["root_hash"] if head else None),
        ref=args.ref,
        changes=[Change(
            part_number=args.part,
            step_path=args.file,
            declaration=declaration,
            team_id=team,
            material=args.material,
            density=args.density,
        )],
    )

    result = pipeline.dry_run(request) if args.dry_run else pipeline.submit(request)

    print(f"{result.verdict.upper()}   {result.commit_id}")
    for part, kind in sorted(result.classifications.items()):
        print(f"  {part}: {kind}")
    for step in result.steps:
        if not step.passed:
            print(f"  FAIL {step.summary()}")
        elif args.verbose:
            print(f"  ok   [{step.stage}] {step.detail}")
    for team, part, why in result.notified:
        print(f"  notify {team} about {part}: {why}")
    if result.landed:
        print(f"  root {result.parent_root} -> {result.new_root}"
              + ("  (rebased)" if result.rebased else ""))
    elif args.dry_run and result.verdict == "would_land":
        print("  nothing was written; this ran the real validator and rolled it back")
    return 0 if result.landed or result.verdict == "would_land" else 1


def cmd_export(args) -> int:
    """Write a part or a whole configuration back out as STEP.

    Rebuilt from the content-addressed store and the occurrence table, not
    copied from whatever file it was ingested from -- so this is also a check
    that the store is faithful.
    """
    from .model import export as export_module

    db = _db(args)
    target = Path(args.out) if args.out else None
    if target is None:
        path = export_module.export_cached(db, args.part)
    else:
        path = Path(export_module.export_revision(db, args.part, target))
    print(f"{path}  ({path.stat().st_size // 1024} KB)")

    if args.open:
        started, message = export_module.open_in_cad(path)
        print(f"  {message}")
        return 0 if started else 1
    return 0


# ------------------------------------------------------------------ queries


def cmd_ingest(args) -> int:
    from .model.ingest import Ingestor, begin_commit, ensure_team

    db = _db(args)
    ensure_team(db, args.team, args.team)
    ing = Ingestor(db, cross_tool=args.cross_tool)
    commit = begin_commit(db, args.author, f"ingest {args.file}", args.team)
    report = ing.ingest_file(args.file, args.team, commit, status="released")
    db.execute("UPDATE commit_log SET new_root = ? WHERE commit_id = ?",
               (report.root_hash, commit))
    if report.root_revision and args.set_ref:
        db.set_ref(args.ref, report.root_revision, report.root_hash, args.author)
    print(report.summary())
    print(f"root hash: {report.root_hash}")
    for w in report.warnings:
        print(f"  warning: {w}")
    if args.verbose:
        for o in report.outcomes:
            print(f"  {o.status:<12} {o.source_name:<28} {o.detail}")
    return 0


def cmd_bom(args) -> int:
    db = _db(args)
    root = args.revision or _head(db, args.ref)["revision_id"]
    rows = q.bill_of_materials(db, root)
    total = 0.0
    print(f"{'PART':<24}{'TEAM':<14}{'QTY':>5}{'UNIT g':>11}{'TOTAL g':>11}")
    for r in rows:
        total += r["total_mass_g"] or 0
        print(f"{r['part_number']:<24}{r['team_id']:<14}{r['total_qty']:>5}"
              f"{(r['unit_mass_g'] or 0):>11.1f}{(r['total_mass_g'] or 0):>11.1f}")
    print(f"{'':<24}{'':<14}{'':>5}{'':>11}{total:>11.1f}")
    return 0


def cmd_tree(args) -> int:
    db = _db(args)
    root = args.revision or _head(db, args.ref)["revision_id"]
    print(merkle.build_from_occurrences(db, root).render(max_depth=args.depth))
    return 0


def cmd_where_used(args) -> int:
    db = _db(args)
    rows = q.where_used(db, args.part, current_only=not args.all)
    if not rows:
        print(f"{args.part} is not used in any assembly")
        return 0
    for r in rows:
        print(f"  {r.part_number:<24} team {r.team_id:<14} depth {r.depth}")
    return 0


def cmd_impact(args) -> int:
    db = _db(args)
    teams = q.impact(db, args.part, interface_name=args.interface)
    if not teams:
        print(f"changing {args.part} affects no other team")
        return 0
    print(f"changing {args.part}'s contract affects {len(teams)} team(s):")
    for t in teams.values():
        print(f"  {t.team_id}")
        for reason in t.reasons:
            print(f"      {reason}")
    return 0


def cmd_chains(args) -> int:
    db = _db(args)
    results = tolerance.evaluate_all(db, method=args.method)
    if not results:
        print("no chains authored")
        return 0
    failed = 0
    for r in results:
        ok = r.passes()
        failed += not ok
        print(f"{'PASS' if ok else 'FAIL'}  {r.explain()}")
        if args.verbose:
            print(f"      worst case  {r.worst_low:+.4f} .. {r.worst_high:+.4f}")
            print(f"      statistical {r.rss_low:+.4f} .. {r.rss_high:+.4f}"
                  f"   ({', '.join(r.sigma_conventions)})")
            print(f"      Monte Carlo {r.mc_low:+.4f} .. {r.mc_high:+.4f}")
            for m in r.contributors:
                print(f"        {'+' if m.direction > 0 else '-'} {m.name:<22}"
                      f"{m.nominal:>9.3f} +{m.tol_plus}/-{m.tol_minus}  "
                      f"{m.part_number} ({m.team_id})")
        for w in r.warnings:
            print(f"      warning: {w}")
    return 1 if failed and args.strict else 0


def cmd_interference(args) -> int:
    db = _db(args)
    root = args.revision or _head(db, args.ref)["revision_id"]
    rep = interf.check(db, root, exact=not args.fast, include_fasteners=args.fasteners,
                       clearance=args.clearance)
    print(rep.summary() + "   [advisory, never a veto]")
    for c in rep.clashes:
        print(f"  {c.kind:<10} {c.describe()}")
    for s in rep.skipped[:5]:
        print(f"  skipped: {s}")
    return 0


def cmd_ask(args) -> int:
    from .model.commit import CommitPipeline
    from .nl.agent import Agent

    db = _db(args)
    agent = Agent(db, pipeline=CommitPipeline(db))
    if args.status:
        print(agent.status())
        return 0
    answer = agent.ask(" ".join(args.question))
    if args.show_calls:
        for c in answer.calls:
            print(f"  -> {c.summary()}")
    print(answer.text)
    if answer.error:
        print(f"[{answer.error}]")
    return 0


def cmd_log(args) -> int:
    db = _db(args)
    rows = db.query("SELECT * FROM commit_log ORDER BY created_at DESC, rowid DESC LIMIT ?",
                    (args.limit,))
    for r in rows:
        mark = {"landed": "+", "rejected": "x"}.get(r["verdict"], "?")
        print(f"{mark} {r['commit_id'][:10]}  {r['author']:<8} {r['message'][:54]}")
        if r["reason"]:
            print(f"    {r['reason'][:150]}")
    return 0


def cmd_why(args) -> int:
    db = _db(args)
    c = db.one("SELECT * FROM commit_log WHERE commit_id LIKE ?", (args.commit + "%",))
    if c is None:
        sys.exit(f"no commit matching {args.commit!r}")
    print(f"{c['commit_id']}  {c['verdict']}  by {c['author']} ({c['team_id']})")
    print(f"  {c['message']}")
    if c["reason"]:
        print(f"  reason: {c['reason']}")
    print(f"  {c['parent_root'] or 'none'} -> {c['new_root'] or 'not applied'}")
    for s in db.query("SELECT * FROM validation WHERE commit_id = ? ORDER BY validation_id",
                      (c["commit_id"],)):
        mark = "ok  " if s["passed"] else "FAIL"
        who = f"  [{s['other_part'] or ''} {s['other_team'] or ''}]".rstrip()
        print(f"  {mark} {s['stage']:<15}{s['detail']}{who if who.strip('[] ') else ''}")
    return 0


def cmd_drain(args) -> int:
    db = _db(args)
    store = open_document_store(Path(args.db).parent / "documents.json")
    pending = pending_count(db)
    report = drain_outbox(db, store)
    print(f"{pending} pending, {report.delivered} delivered, {report.failed} failed")
    print(f"store: {store.name} at {getattr(store, 'path', '?')}")
    for name in store.collections():
        print(f"  {name}: {store.count(name)} document(s)")
    for e in report.errors:
        print(f"  error: {e}")
    store.close()
    return 0


def cmd_stats(args) -> int:
    db = _db(args)
    for k, v in db.stats().items():
        print(f"  {k:<16} {v}")
    row = db.get_ref(args.ref)
    if row:
        print(f"  {'ref ' + args.ref:<16} {row['root_hash']}")
    print(f"  {'outbox pending':<16} {pending_count(db)}")
    return 0


def cmd_serve(args) -> int:
    import uvicorn

    from .web.app import create_app

    app = create_app(args.db, ref=args.ref)
    print(f"http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


# --------------------------------------------------------------------- main


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="interlock", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", default=str(DEFAULT_DB), help="database path")
    p.add_argument("--ref", default="main", help="which ref to read")
    sub = p.add_subparsers(dest="command", required=True)

    d = sub.add_parser("demo", help="build the demo database and run the five stages")
    d.add_argument("--out", help="where to write the generated STEP files")
    d.add_argument("--keep", action="store_true", help="keep an existing database")
    d.add_argument("--fast", action="store_true", help="skip exact interference tests")
    d.set_defaults(func=cmd_demo)

    i = sub.add_parser("ingest", help="ingest a STEP or BREP file")
    i.add_argument("file")
    i.add_argument("--team", default="unassigned")
    i.add_argument("--author", default=os.environ.get("USERNAME", "cli"))
    i.add_argument("--cross-tool", action="store_true",
                   help="use the looser cross-tool matching tolerance")
    i.add_argument("--set-ref", action="store_true", help="point the ref at this import")
    i.add_argument("-v", "--verbose", action="store_true")
    i.set_defaults(func=cmd_ingest)

    c = sub.add_parser("commit", help="submit a change to an existing part for validation")
    c.add_argument("part", help="part number, which must already exist")
    c.add_argument("file", nargs="?", help="new geometry (STEP); omit to change only metadata")
    c.add_argument("--author", default=os.environ.get("USERNAME", "cli"))
    c.add_argument("--team", default=None, help="defaults to the part's owning team")
    c.add_argument("-m", "--message", help="what this change is")
    c.add_argument("--declaration", help="JSON declaration; omit to carry the previous one forward")
    c.add_argument("--material")
    c.add_argument("--density", type=float, help="g/mm^3")
    c.add_argument("--base-root", help="the root hash this was written against")
    c.add_argument("--dry-run", action="store_true",
                   help="run the real validator and roll it back")
    c.add_argument("-v", "--verbose", action="store_true", help="show every stage, not just failures")
    c.set_defaults(func=cmd_commit)

    e = sub.add_parser("export", help="write a part or assembly back out as STEP")
    e.add_argument("part", help="part number, revision id, or a ref such as 'main'")
    e.add_argument("-o", "--out", help="where to write it (default: a content-keyed cache)")
    e.add_argument("--open", action="store_true", help="open it in FreeCAD afterwards")
    e.set_defaults(func=cmd_export)

    b = sub.add_parser("bom", help="bill of materials")
    b.add_argument("revision", nargs="?")
    b.set_defaults(func=cmd_bom)

    t = sub.add_parser("tree", help="hashed configuration tree")
    t.add_argument("revision", nargs="?")
    t.add_argument("--depth", type=int, default=12)
    t.set_defaults(func=cmd_tree)

    w = sub.add_parser("where-used", help="which assemblies contain a part")
    w.add_argument("part")
    w.add_argument("--all", action="store_true", help="include superseded configurations")
    w.set_defaults(func=cmd_where_used)

    im = sub.add_parser("impact", help="who a contract change would break")
    im.add_argument("part")
    im.add_argument("--interface")
    im.set_defaults(func=cmd_impact)

    c = sub.add_parser("chains", help="total every tolerance chain")
    c.add_argument("--method", choices=["worst_case", "rss", "monte_carlo"])
    c.add_argument("--strict", action="store_true", help="exit non-zero if a chain fails")
    c.add_argument("-v", "--verbose", action="store_true")
    c.set_defaults(func=cmd_chains)

    f = sub.add_parser("interference", help="advisory clash check")
    f.add_argument("revision", nargs="?")
    f.add_argument("--fast", action="store_true", help="bounding boxes only")
    f.add_argument("--fasteners", action="store_true", help="include bolts and washers")
    f.add_argument("--clearance", type=float, default=0.0)
    f.set_defaults(func=cmd_interference)

    a = sub.add_parser("ask", help="natural language question")
    a.add_argument("question", nargs="*")
    a.add_argument("--status", action="store_true", help="report whether the model is reachable")
    a.add_argument("--show-calls", action="store_true", default=True)
    a.set_defaults(func=cmd_ask)

    lg = sub.add_parser("log", help="the commit log")
    lg.add_argument("--limit", type=int, default=25)
    lg.set_defaults(func=cmd_log)

    y = sub.add_parser("why", help="a commit's full validation trace")
    y.add_argument("commit")
    y.set_defaults(func=cmd_why)

    dr = sub.add_parser("drain", help="deliver the outbox to the document store")
    dr.set_defaults(func=cmd_drain)

    st = sub.add_parser("stats", help="row counts and storage")
    st.set_defaults(func=cmd_stats)

    s = sub.add_parser("serve", help="the review interface")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.set_defaults(func=cmd_serve)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
