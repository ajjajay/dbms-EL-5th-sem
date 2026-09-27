"""Independent verification: open the exported STEP in FreeCAD and compare.

Every other check in this project reads its own exports through the same kernel
that wrote them. That is a real limitation, and it is stated as one in the
report. This script closes the gap as far as it can be closed locally: it opens
the generated assembly in **FreeCAD 1.1.3** -- a separate application, with its
own importer, its own OCCT build and its own document model, and the exact
version the design document names -- and compares what FreeCAD sees against what
Interlock stored.

Three claims are checked, and each is one the project makes elsewhere:

  placed solids      section 7's occurrence model. Interlock says the root
                     configuration contains N placed solids; FreeCAD's root
                     compound must hold N too.
  distinct shapes    section 5's deduplication. Interlock stores one shape row
                     per distinct geometry; FreeCAD must find exactly that many
                     distinct volumes among the leaves.
  validity           section 4's round trip. No body may come back open or
                     invalid, or the export is not something a second tool can
                     actually use.

A note on counting, because the first version of this script got it wrong and
reported 195 solids against Interlock's 73. FreeCAD's STEP importer materialises
an assembly as *both* a compound holding every descendant solid *and* a separate
feature per leaf. Summing solids over every object therefore counts each leaf
once per ancestor it has. The root compound's own solid count is the number to
compare against, and the leaf features are counted separately as a cross-check.

Run:

    .venv/Scripts/python.exe scripts/verify_with_freecad.py
    .venv/Scripts/python.exe scripts/verify_with_freecad.py --step data/demo/x.step
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STEP = ROOT / "data" / "demo" / "winch_100.step"
REPORT = ROOT / "docs" / "freecad_verification.json"

# winget's FreeCAD installer is per-user, so LOCALAPPDATA comes first.
CANDIDATES = [
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\FreeCAD 1.1\bin\freecadcmd.exe"),
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\FreeCAD\bin\freecadcmd.exe"),
    r"C:\Program Files\FreeCAD 1.1\bin\FreeCADCmd.exe",
    r"C:\Program Files\FreeCAD 1.0\bin\FreeCADCmd.exe",
    r"C:\Program Files\FreeCAD\bin\FreeCADCmd.exe",
]

# FreeCAD writes a percentage progress bar and a banner to stdout.
NOISE = ("%)", "Recompute", "(C) 2001", "free and open-source", "Libs:")


def find_freecad() -> str | None:
    for path in CANDIDATES:
        if Path(path).exists():
            return path
    from shutil import which

    return which("FreeCADCmd") or which("freecadcmd")


# --------------------------------------------------------------- inside FreeCAD


def verify(step: Path) -> int:
    """Runs under FreeCAD's own interpreter. Reports; does not judge."""
    import FreeCAD
    import Import

    doc = FreeCAD.newDocument("verify")
    Import.insert(str(step), doc.Name)
    doc.recompute()

    objects = []
    for obj in doc.Objects:
        shape = getattr(obj, "Shape", None)
        solids = list(getattr(shape, "Solids", []) or []) if shape is not None else []
        if not solids:
            continue
        objects.append({
            "label": obj.Label,
            "type": obj.TypeId,
            "n_solids": len(solids),
            "volume": float(shape.Volume),
            "placement": [round(float(v), 6) for v in obj.Placement.Base],
            "valid": bool(all(s.isValid() for s in solids)),
            "closed": bool(all(s.isClosed() for s in solids)),
            "solid_volumes": [round(float(s.Volume), 4) for s in solids],
        })

    root = max(objects, key=lambda o: o["n_solids"]) if objects else None
    leaves = [o for o in objects if o["n_solids"] == 1]

    report = {
        "freecad_version": ".".join(FreeCAD.Version()[:3]),
        "freecad_build": FreeCAD.Version()[3],
        "step_file": str(step),
        "root_label": root["label"] if root else None,
        "placed_solids": root["n_solids"] if root else 0,
        "leaf_features": len(leaves),
        "distinct_volumes": len({v for o in leaves for v in o["solid_volumes"]}),
        "total_volume_mm3": root["volume"] if root else 0.0,
        "invalid_or_open": sum(1 for o in objects if not o["valid"] or not o["closed"]),
        "leaf_labels": sorted({o["label"] for o in leaves}),
        "objects": objects,
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, indent=2), encoding="utf-8")
    FreeCAD.closeDocument(doc.Name)
    return 0


# --------------------------------------------------------- in the project venv


def expectations(step: Path) -> dict | None:
    """What Interlock makes of *this file*, ingested fresh.

    Deliberately not the project database. The first version of this script
    compared FreeCAD's reading of a baseline export against whatever the demo
    database currently held, and the demo lands commits -- so a cover that had
    since been thickened from 8.0 to 8.4 mm showed up as a 5% "disagreement"
    that was nothing of the kind. The file and the rows have to describe the
    same moment, so the file is ingested into a throwaway database here.
    """
    sys.path.insert(0, str(ROOT))
    try:
        import tempfile

        from interlock.db.database import Database
        from interlock.model.ingest import Ingestor, begin_commit, ensure_team
        from interlock.query import traversal as q
    except ImportError:
        return None

    scratch = Path(tempfile.mkdtemp(prefix="interlock_verify_"))
    db = Database(scratch / "scratch.db", scratch / "blobs")
    ensure_team(db, "verify", "verify")
    commit = begin_commit(db, "verify", f"verify {step.name}", "verify")
    report = Ingestor(db).ingest_file(str(step), "verify", commit, status="released")
    placed = [i for i in q.configuration(db, report.root_revision)
              if not i.is_assembly and i.fingerprint]
    out = {
        "placed_solids": len(placed),
        "distinct_shapes": len({i.fingerprint for i in placed}),
        "total_volume_mm3": sum(
            float(db.scalar(
                "SELECT COALESCE(volume_exact, volume) FROM shape WHERE fingerprint = ?",
                (i.fingerprint,)))
            for i in placed
        ),
        "leaf_part_numbers": sorted({i.part_number for i in placed}),
        "scratch_db": str(scratch),
    }
    db.close()
    return out


def compare(seen: dict, expected: dict | None) -> int:
    print(f"FreeCAD {seen['freecad_version']} ({seen['freecad_build']})")
    print(f"opened  {Path(seen['step_file']).name}")
    print(f"root    {seen['root_label']}  "
          f"({seen['placed_solids']} solids in the root compound, "
          f"{seen['leaf_features']} leaf features)")
    print()

    if expected is None:
        print("  (no Interlock database to compare against; reporting only)")
        for k in ("placed_solids", "leaf_features", "distinct_volumes", "invalid_or_open"):
            print(f"    {k:<18} {seen[k]}")
        return 0

    failures = 0
    rows = [
        ("placed solids", seen["placed_solids"], expected["placed_solids"],
         "one occurrence row per appearance (s7)"),
        ("distinct shapes", seen["distinct_volumes"], expected["distinct_shapes"],
         "one shape row per distinct geometry (s5)"),
    ]
    width = max(len(r[0]) for r in rows)
    print(f"  {'':<{width}}  {'FreeCAD':>8}  {'Interlock':>9}")
    for label, got, want, why in rows:
        ok = got == want
        failures += not ok
        print(f"  {label:<{width}}  {got:>8}  {want:>9}   "
              f"{'agree' if ok else 'DISAGREE'}   {why}")

    gap = abs(seen["total_volume_mm3"] - expected["total_volume_mm3"])
    rel = gap / max(expected["total_volume_mm3"], 1.0)
    ok = rel < 1e-6
    failures += not ok
    print(f"\n  total volume    FreeCAD  {seen['total_volume_mm3']:>14,.2f} mm^3")
    print(f"                  Interlock{expected['total_volume_mm3']:>14,.2f} mm^3")
    print(f"                  relative difference {rel:.2e}   "
          f"{'agree' if ok else 'DISAGREE'}")

    print(f"\n  invalid or open bodies: {seen['invalid_or_open']}", end="")
    if seen["invalid_or_open"]:
        failures += 1
        print("   <- the export is not clean")
    else:
        print("   (none)")

    missing = [n for n in expected["leaf_part_numbers"]
               if not any(n in lab for lab in seen["leaf_labels"])]
    recovered = len(expected["leaf_part_numbers"]) - len(missing)
    print(f"  part names recovered:   {recovered}/{len(expected['leaf_part_numbers'])}")
    if missing:
        failures += 1
        print(f"    missing: {', '.join(missing)}")

    print()
    if failures:
        print(f"  {failures} disagreement(s) between FreeCAD and Interlock.")
    else:
        print("  FreeCAD independently agrees with Interlock on every count.")
    print(f"\n  detail: {REPORT}")
    return 1 if failures else 0


def launch(step: Path) -> int:
    exe = find_freecad()
    if exe is None:
        print("FreeCAD not found. Looked in:")
        for c in CANDIDATES:
            print(f"  {c}")
        print("\nInstall it with:  winget install FreeCAD.FreeCAD")
        return 2

    expected = expectations(step)
    if REPORT.exists():
        REPORT.unlink()
    env = dict(os.environ, INTERLOCK_STEP=str(step))
    proc = subprocess.run([exe, str(Path(__file__).resolve())],
                          capture_output=True, text=True, timeout=1800, env=env)
    if not REPORT.exists():
        for line in proc.stdout.splitlines():
            if line.strip() and not any(n in line for n in NOISE):
                print(line)
        sys.stderr.write(proc.stderr[-3000:])
        print("\nFreeCAD produced no report.")
        return 1
    return compare(json.loads(REPORT.read_text(encoding="utf-8")), expected)


def main() -> int:
    args = sys.argv[1:]
    step = Path(os.environ.get("INTERLOCK_STEP") or DEFAULT_STEP)
    if "--step" in args:
        step = Path(args[args.index("--step") + 1])
    if not step.exists():
        print(f"no such file: {step}\nRun `python -m interlock.cli demo` first.")
        return 2

    try:
        import FreeCAD  # noqa: F401
    except ImportError:
        return launch(step)
    return verify(step)


# FreeCADCmd *imports* this file rather than running it as __main__, so the usual
# guard never fires and the script would silently do nothing.
if __name__ == "__main__":
    raise SystemExit(main())
else:
    main()
