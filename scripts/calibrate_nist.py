"""Cross-tool fingerprint calibration against the NIST MBE PMI test models.

The NIST archive ships every test case as several STEP files written by different
CAD systems (AP203 geometry-only, AP203 + graphical PMI, AP242 editions 1/2/3).
Same model, different exporters: exactly the situation the bounded match exists
for, and the only real evidence available about how far the invariant vector moves
between tools. Without this the cross-tool tolerance is a guess.

For every test case this script analyses each variant (kernel + fingerprint, no
database), then compares every pair of variants and records:

  * whether the strict hashes agree,
  * the relative size error and the largest dimensionless-component error,
  * the smallest tolerance that would have matched them.

It also lists what broke (invalid solids, shells, multi-solid bodies), which is
the other thing real data is for. Output: docs/nist_calibration.json and a table
on stdout. Run:  .venv/Scripts/python.exe scripts/calibrate_nist.py [case ...]
"""

from __future__ import annotations

import itertools
import json
import re
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from interlock.geometry import fingerprint as fp
from interlock.kernel import get_backend

DATA = ROOT / "data" / "external" / "nist" / "NIST-PMI-STEP-Files" / "NIST-PMI-STEP-Files"
OUT = ROOT / "docs" / "nist_calibration.json"


def variants() -> dict[str, list[Path]]:
    cases: dict[str, list[Path]] = {}
    for path in sorted(DATA.rglob("*.stp")):
        m = re.search(r"nist_((?:ctc|ftc|stc)_\d+)", path.name)
        if not m:
            continue
        if "-tg" in path.name:
            continue   # tessellated facets, not b-rep: outside what a solid kernel fingerprints
        cases.setdefault(m.group(1), []).append(path)
    return cases


def label(path: Path) -> str:
    parent = path.parent.name
    if parent == "AP203 geometry only":
        return "203-geo"
    if parent == "AP203 with PMI":
        return "203-pmi"
    m = re.search(r"ap242-(e\d)", path.name)
    return f"242-{m.group(1)}" if m else path.stem


def analyse(backend, path: Path):
    started = time.perf_counter()
    result = backend.read(str(path))
    read_s = time.perf_counter() - started
    entries = []
    for index, solid in enumerate(result.solids):
        entry = {
            "name": solid.source_name,
            "valid": solid.valid,
            "notes": solid.validity_notes,
            "volume": solid.mass.volume,
            "faces": solid.topology.faces,
        }
        if solid.valid:
            t0 = time.perf_counter()
            try:
                identity = fp.identify(solid, deflection=0.1)
                entry.update(
                    strict=identity.strict,
                    invariants=identity.invariants,
                    identify_s=time.perf_counter() - t0,
                    bores=len(identity.bores),
                    warnings=identity.warnings,
                )
            except Exception as exc:
                entry["error"] = f"{type(exc).__name__}: {exc}"
        entries.append(entry)
    return {"path": path, "read_s": read_s, "entries": entries, "warnings": result.warnings}


def main(argv: list[str]) -> int:
    backend = get_backend("occt")
    cases = variants()
    wanted = set(argv) if argv else set(cases)
    report: dict = {"cases": {}, "pairs": [], "problems": []}

    for case in sorted(cases):
        if case not in wanted:
            continue
        analysed = {}
        for path in cases[case]:
            name = label(path)
            try:
                analysed[name] = analyse(backend, path)
            except Exception as exc:
                report["problems"].append(f"{case} {name}: read failed: {type(exc).__name__}: {exc}")
                print(f"{case:8} {name:8} READ FAILED  {exc}")
                continue
            info = analysed[name]
            solids = info["entries"]
            good = [e for e in solids if e.get("strict")]
            print(
                f"{case:8} {name:8} solids={len(solids):3} identified={len(good):3} "
                f"read={info['read_s']:.1f}s"
                + (f"  BAD: {[e['notes'] or e.get('error') for e in solids if not e.get('strict')][:2]}"
                   if len(good) != len(solids) else "")
            )
            for e in solids:
                if not e.get("strict"):
                    report["problems"].append(
                        f"{case} {name} {e['name']!r}: {e.get('notes') or e.get('error')}"
                    )

        report["cases"][case] = {
            name: {
                "file": info["path"].name,
                "solids": len(info["entries"]),
                "identified": sum(1 for e in info["entries"] if e.get("strict")),
                "read_s": round(info["read_s"], 2),
            }
            for name, info in analysed.items()
        }

        # Pairwise: match solids across variants by best size agreement.
        for (na, a), (nb, b) in itertools.combinations(analysed.items(), 2):
            ea = [e for e in a["entries"] if e.get("strict")]
            eb = [e for e in b["entries"] if e.get("strict")]
            if not ea or not eb:
                continue
            for x in ea:
                best = None
                for y in eb:
                    r = fp.compare(x["invariants"], y["invariants"], 1.0, 1.0)
                    score = r.char_length_error + r.dimensionless_error
                    if best is None or score < best[0]:
                        best = (score, y, r)
                if best is None:
                    continue
                _, y, r = best
                row = {
                    "case": case,
                    "a": na,
                    "b": nb,
                    "same_strict": x["strict"] == y["strict"],
                    "char_length_error": r.char_length_error,
                    "dimensionless_error": r.dimensionless_error,
                    "chirality": (x["invariants"].chirality, y["invariants"].chirality),
                    "topology_agrees": r.topology_agrees,
                    "histogram_agrees": r.histogram_agrees,
                    "solids": (len(ea), len(eb)),
                }
                report["pairs"].append(row)
                print(
                    f"   {case} {na:8} vs {nb:8} strict={'=' if row['same_strict'] else '!'} "
                    f"size={r.char_length_error:.2e} shape={r.dimensionless_error:.2e} "
                    f"chir={row['chirality']} topo={'=' if r.topology_agrees else '!'}"
                )

    pairs = report["pairs"]
    if pairs:
        sizes = np.array([p["char_length_error"] for p in pairs])
        shapes = np.array([p["dimensionless_error"] for p in pairs])
        report["summary"] = {
            "pairs": len(pairs),
            "strict_agree": int(sum(p["same_strict"] for p in pairs)),
            "size_error_p50": float(np.percentile(sizes, 50)),
            "size_error_p95": float(np.percentile(sizes, 95)),
            "size_error_max": float(sizes.max()),
            "shape_error_p50": float(np.percentile(shapes, 50)),
            "shape_error_p95": float(np.percentile(shapes, 95)),
            "shape_error_max": float(shapes.max()),
        }
        print("\nSUMMARY", json.dumps(report["summary"], indent=2))
    if report["problems"]:
        print("\nPROBLEMS")
        for p in report["problems"]:
            print("  -", p)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"\nwrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
