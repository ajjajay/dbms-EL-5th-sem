"""The command line and the document store's fallback.

Both were unexercised by the suite. The CLI is what anyone actually runs, so a
regression there is the most visible kind; and the JSON-lines store is a claim
the documentation makes ("falls back to a stdlib implementation if TinyDB is
absent") which was never checked, and an untested claim is a possibly-false one.
"""

from __future__ import annotations


import pytest

from interlock import cli
from interlock.db import documents
from interlock.db.database import Database
from interlock.synthetic import assembly, bootstrap


@pytest.fixture(scope="module")
def loaded(tmp_path_factory):
    d = tmp_path_factory.mktemp("cli")
    step = assembly.write(str(d / "winch.step"))
    db = Database(d / "i.db", d / "blobs")
    report = bootstrap.load(db, step)
    bootstrap.subscribe_defaults(db)
    db.close()
    return d / "i.db", d, report


# ------------------------------------------------------------------- the CLI


@pytest.mark.parametrize("argv", [
    ["bom"],
    ["tree", "--depth", "3"],
    ["where-used", "BOLT-M6X20"],
    ["impact", "BASE-PLATE"],
    ["chains"],
    ["chains", "-v", "--method", "rss"],
    ["interference", "--fast"],
    ["log", "--limit", "5"],
    ["stats"],
    ["drain"],
    ["ask", "--status"],
    ["ask", "do", "any", "chains", "fail?"],
])
def test_cli_commands_run(loaded, argv, capsys):
    db_path, _, _ = loaded
    assert cli.main(["--db", str(db_path), *argv]) == 0
    assert capsys.readouterr().out.strip(), f"{argv[0]} printed nothing"


def test_cli_chains_strict_exits_non_zero_when_a_chain_fails(loaded):
    """So it can be used as a gate in a build script."""
    db_path, _, _ = loaded
    assert cli.main(["--db", str(db_path), "chains", "--strict"]) == 1


def test_cli_why_prints_the_full_trace(loaded, capsys):
    db_path, d, _ = loaded
    from interlock.model.commit import Change, CommitPipeline, CommitRequest
    from interlock.synthetic import cad

    db = Database(db_path)
    path = d / "bad.step"
    cad.write_step_part(assembly.base_plate(hole_shift=(2.0, 0.0)), "BASE-PLATE", str(path))
    res = CommitPipeline(db).submit(CommitRequest(
        author="t", team_id="chassis", message="break it",
        base_root=db.get_ref("main")["root_hash"],
        changes=[Change("BASE-PLATE", step_path=str(path))]))
    db.close()
    assert not res.landed

    assert cli.main(["--db", str(db_path), "why", res.commit_id[:10]]) == 0
    out = capsys.readouterr().out
    assert "rejected" in out
    assert "BEARING-BLOCK" in out          # names the other party
    assert "FAIL" in out


def test_cli_ingest_lands_a_part(loaded, capsys, tmp_path):
    db_path, _, _ = loaded
    from interlock.synthetic import cad

    path = tmp_path / "widget.step"
    cad.write_step_part(cad.box(12, 12, 12), "TEST-WIDGET", str(path))
    assert cli.main(["--db", str(db_path), "ingest", str(path), "--team", "chassis"]) == 0
    assert "1 solids read" in capsys.readouterr().out

    db = Database(db_path)
    assert db.scalar("SELECT COUNT(*) FROM part WHERE part_number = 'TEST-WIDGET'") == 1
    db.close()


def test_cli_rejects_an_unknown_part_cleanly(loaded):
    """A bad argument should say so, not raise a traceback at the user."""
    db_path, _, _ = loaded
    with pytest.raises(LookupError):
        cli.main(["--db", str(db_path), "where-used", "NO-SUCH-PART"])


def test_the_parser_exposes_every_documented_command():
    """The module docstring is the CLI's documentation; keep it honest."""
    parser = cli.build_parser()
    actions = [a for a in parser._actions if hasattr(a, "choices") and a.choices]
    commands = set(actions[0].choices)
    # "python -m interlock.cli bom   bill of materials" -> field 3 is the command
    documented = {line.split()[3] for line in cli.__doc__.splitlines()
                  if line.strip().startswith("python -m interlock.cli")}
    assert documented <= commands, f"documented but missing: {documented - commands}"
    assert commands <= documented | {"serve"}, f"undocumented: {commands - documented}"


# ---------------------------------------------------- the document store fallback


def test_the_jsonl_fallback_satisfies_the_same_interface(tmp_path):
    """The claim the documentation makes, checked."""
    tiny = documents.TinyDocumentStore(tmp_path / "tiny.json")
    jsonl = documents.JsonLinesDocumentStore(tmp_path / "jsonl.json")

    for store in (tiny, jsonl):
        store.put("traces", "c1", {"verdict": "landed", "author": "ana"})
        store.put("traces", "c2", {"verdict": "rejected", "author": "bo"})
        store.put("notes", "n1", {"team": "chassis"})

        assert store.get("traces", "c1")["verdict"] == "landed"
        assert store.get("traces", "missing") is None
        assert store.count("traces") == 2
        assert {d["author"] for d in store.all("traces")} == {"ana", "bo"}
        assert [d["author"] for d in store.find("traces", verdict="rejected")] == ["bo"]
        assert set(store.collections()) == {"traces", "notes"}

        # Keyed writes overwrite rather than duplicate: the outbox redelivers.
        store.put("traces", "c1", {"verdict": "landed", "author": "ana", "extra": 1})
        assert store.count("traces") == 2
        assert store.get("traces", "c1")["extra"] == 1
        store.close()


def test_the_fallback_survives_a_corrupt_line(tmp_path):
    """Append-only files get truncated by crashes; a bad line must not take the
    whole store down with it."""
    store = documents.JsonLinesDocumentStore(tmp_path / "s.json")
    store.put("traces", "good", {"v": 1})
    with open(store.path, "a", encoding="utf-8") as fh:
        fh.write('{"truncated": \n')
    store.put("traces", "after", {"v": 2})

    assert store.get("traces", "good")["v"] == 1
    assert store.get("traces", "after")["v"] == 2
    assert store.count("traces") == 2


def test_open_document_store_picks_tinydb_when_available(tmp_path):
    store = documents.open_document_store(tmp_path / "x.json")
    assert store.name == "tinydb"
    store.close()


def test_the_outbox_drains_into_either_store(tmp_path):
    """The outbox is the store's only writer, so it has to work against both."""
    for factory, suffix in ((documents.TinyDocumentStore, "a"),
                            (documents.JsonLinesDocumentStore, "b")):
        db = Database(tmp_path / f"{suffix}.db", tmp_path / f"blobs_{suffix}")
        with db.transaction():
            documents.enqueue(db, documents.TOPIC_VALIDATION, "c1", {"verdict": "landed"})
            documents.enqueue(db, documents.TOPIC_NL_CALL, "n1", {"question": "hi"})
        assert documents.pending_count(db) == 2

        store = factory(tmp_path / f"docs_{suffix}.json")
        report = documents.drain(db, store)
        assert report.delivered == 2 and report.failed == 0
        assert documents.pending_count(db) == 0
        assert store.get(documents.TOPIC_VALIDATION, "c1")["verdict"] == "landed"

        # Draining again delivers nothing and duplicates nothing.
        assert documents.drain(db, store).delivered == 0
        assert store.count(documents.TOPIC_VALIDATION) == 1
        store.close()
        db.close()


# ------------------------------------------------------------------- commit


def test_cli_commit_dry_run_reports_and_writes_nothing(loaded, capsys, tmp_path):
    """The question people actually want answered before they commit."""
    db_path, _, _ = loaded
    from interlock.synthetic import cad

    path = tmp_path / "shifted.step"
    cad.write_step_part(assembly.base_plate(pattern_shift=(2.0, 0.0)),
                        "BASE-PLATE", str(path))
    db = Database(db_path)
    before = db.stats()
    root_before = db.get_ref("main")["root_hash"]
    db.close()

    assert cli.main(["--db", str(db_path), "commit", "BASE-PLATE", str(path),
                     "--dry-run", "-m", "shift it"]) == 1
    out = capsys.readouterr().out
    assert "REJECTED" in out
    assert "BEARING-BLOCK" in out           # names the other party

    db = Database(db_path)
    assert db.stats()["revisions"] == before["revisions"]
    assert db.get_ref("main")["root_hash"] == root_before
    db.close()


def test_cli_commit_lands_a_good_change(loaded, capsys, tmp_path):
    db_path, _, _ = loaded
    from interlock.synthetic import cad

    path = tmp_path / "cover.step"
    cad.write_step_part(assembly.gearbox_cover(8.4), "GEARBOX-COVER", str(path))
    db = Database(db_path)
    root_before = db.get_ref("main")["root_hash"]
    db.close()

    assert cli.main(["--db", str(db_path), "commit", "GEARBOX-COVER", str(path),
                     "-m", "skim the cover"]) == 0
    out = capsys.readouterr().out
    assert "LANDED" in out and "internal" in out

    db = Database(db_path)
    assert db.get_ref("main")["root_hash"] != root_before
    db.close()


def test_cli_commit_defaults_the_team_to_the_parts_owner(loaded, capsys, tmp_path):
    """A commit references a real team; inventing one would break the log."""
    db_path, _, _ = loaded
    from interlock.synthetic import cad

    path = tmp_path / "pcb.step"
    cad.write_step_part(assembly.control_pcb(1.7), "CONTROL-PCB", str(path))
    # A real commit, not a dry run: a dry run rolls back, so there would be no
    # commit_log row left to read the attribution from.
    assert cli.main(["--db", str(db_path), "commit", "CONTROL-PCB", str(path),
                     "-m", "respin"]) == 0

    db = Database(db_path)
    row = db.one("""SELECT team_id FROM commit_log
                    WHERE message = 'respin' ORDER BY rowid DESC LIMIT 1""")
    assert row["team_id"] == "controls"     # CONTROL-PCB's owner, not a default
    db.close()


def test_cli_commit_refuses_an_unknown_part_with_advice(loaded, capsys):
    db_path, _, _ = loaded
    with pytest.raises(SystemExit) as exc:
        cli.main(["--db", str(db_path), "commit", "NO-SUCH-PART", "--dry-run"])
    assert "ingest" in str(exc.value)


def test_cli_export_writes_a_readable_step(loaded, capsys, tmp_path):
    from interlock.kernel import get_backend

    db_path, _, _ = loaded
    out = tmp_path / "exported.step"
    assert cli.main(["--db", str(db_path), "export", "BEARING-BLOCK", "-o", str(out)]) == 0
    assert out.exists() and out.stat().st_size > 0
    assert str(out) in capsys.readouterr().out

    result = get_backend("occt").read(str(out))
    assert len([s for s in result.solids if s.valid]) == 1


def test_cli_export_defaults_to_a_content_keyed_cache(loaded, capsys):
    db_path, _, _ = loaded
    assert cli.main(["--db", str(db_path), "export", "WINCH-100"]) == 0
    first = capsys.readouterr().out.strip()
    assert cli.main(["--db", str(db_path), "export", "WINCH-100"]) == 0
    assert capsys.readouterr().out.strip() == first      # same file, not re-exported
