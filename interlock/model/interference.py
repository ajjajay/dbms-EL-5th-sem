"""Interference checking (design doc section 14, "pairwise interference").

Labelled advisory, everywhere, on purpose. The design document lists interference
under things "enforced in the data layer"; that claim cannot be honoured and
pretending otherwise would be the dishonest part of this project. The check is
quadratic in the number of placed instances and each exact pair test costs a
boolean intersection in the kernel -- seconds per pair on real geometry. A write
transaction that ran it would hold its locks for minutes, and section 9 requires
commits to be atomic. So:

    interference is computed as a background job against a landed configuration,
    and its findings are recorded as advice, never as a veto.

Two stages, which is what makes it usable at all:

  1. **Bounding-box prefilter.** Each instance's stored axis-aligned box is
     carried into the assembly frame and the boxes are tested pairwise. This is
     arithmetic over columns -- no geometry is opened -- and it discards the
     overwhelming majority of pairs. Rotated parts get the box of the rotated
     box, which is conservative: it can propose a pair that does not really
     clash, never hide one that does.

  2. **Exact test, only for survivors.** The two solids are read back from the
     blob store, placed, and intersected. This is the only place outside ingest
     where the kernel runs, and it is why this module is not imported by the
     query layer.

Fasteners are excluded by default. A bolt is *meant* to occupy its hole, so every
bolt in the assembly would otherwise be reported as interfering with the part it
fastens, and a report that is ninety per cent false positives is not read.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from ..db.database import Database
from ..query import traversal as q

# Parts whose whole purpose is to sit inside another part's hole.
FASTENER_HINTS = ("BOLT", "SCREW", "WASHER", "NUT", "PIN", "DOWEL", "RIVET", "STUD")


@dataclass
class Clash:
    path_a: str
    path_b: str
    part_a: str
    part_b: str
    team_a: str
    team_b: str
    kind: str                 # clash | clearance
    volume: float = 0.0
    depth: float = 0.0
    overlap_box: tuple | None = None

    def describe(self) -> str:
        if self.kind == "clash":
            return (
                f"{self.path_a} and {self.path_b} overlap by {self.volume:.1f} mm^3 "
                f"({self.part_a}, team {self.team_a} / {self.part_b}, team {self.team_b})"
            )
        return (
            f"{self.path_a} and {self.path_b} are within the clearance limit "
            f"({self.depth:.2f} mm apart)"
        )


@dataclass
class Report:
    root_revision: str
    pairs_considered: int = 0
    pairs_after_prefilter: int = 0
    exact_tests: int = 0
    clashes: list[Clash] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    exact: bool = True

    def summary(self) -> str:
        return (
            f"{self.pairs_considered} pairs considered, "
            f"{self.pairs_after_prefilter} survived the bounding-box prefilter, "
            f"{self.exact_tests} exact tests, {len(self.clashes)} clash(es) found"
        )


def is_fastener(part_number: str) -> bool:
    upper = part_number.upper()
    return any(h in upper for h in FASTENER_HINTS)


def placed_box(bbox, world: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Axis-aligned bounds of a rotated box. Conservative by construction."""
    xmin, ymin, zmin, xmax, ymax, zmax = bbox
    corners = np.array(
        [[x, y, z, 1.0] for x in (xmin, xmax) for y in (ymin, ymax) for z in (zmin, zmax)]
    )
    placed = (world @ corners.T).T[:, :3]
    return placed.min(axis=0), placed.max(axis=0)


def boxes_overlap(a: tuple, b: tuple, margin: float = 0.0) -> tuple[bool, float]:
    """Do two placed boxes overlap, and by how much on the tightest axis?"""
    (a_lo, a_hi), (b_lo, b_hi) = a, b
    overlap = np.minimum(a_hi, b_hi) - np.maximum(a_lo, b_lo)
    return bool(np.all(overlap > -margin)), float(overlap.min())


def check(
    db: Database,
    root: str,
    exact: bool = True,
    include_fasteners: bool = False,
    clearance: float = 0.0,
    max_exact: int = 400,
    store: bool = True,
) -> Report:
    """Check a landed configuration for interference.

    `clearance` > 0 also reports pairs that come closer than that distance
    without touching, using the prefilter boxes only -- a genuine approximation,
    and reported as `clearance` rather than `clash` so the two are not confused.
    """
    root_rev = q.resolve_revision(db, root)
    report = Report(root_revision=root_rev, exact=exact)

    instances = []
    for inst in q.configuration(db, root_rev):
        if inst.is_assembly or inst.fingerprint is None:
            continue
        if not include_fasteners and is_fastener(inst.part_number):
            continue
        row = db.one(
            """SELECT bbox_xmin, bbox_ymin, bbox_zmin, bbox_xmax, bbox_ymax, bbox_zmax
               FROM shape WHERE fingerprint = ?""",
            (inst.fingerprint,),
        )
        if row is None:
            report.skipped.append(f"{inst.path}: no stored bounds")
            continue
        instances.append((inst, placed_box(tuple(row), inst.world)))

    candidates: list[tuple] = []
    for i in range(len(instances)):
        for j in range(i + 1, len(instances)):
            report.pairs_considered += 1
            (inst_a, box_a), (inst_b, box_b) = instances[i], instances[j]
            hit, tightest = boxes_overlap(box_a, box_b, margin=clearance)
            if not hit:
                continue
            report.pairs_after_prefilter += 1
            candidates.append((inst_a, inst_b, tightest))

    for inst_a, inst_b, tightest in candidates:
        if not exact or report.exact_tests >= max_exact:
            report.clashes.append(_clash(inst_a, inst_b, "clearance", depth=tightest))
            continue
        report.exact_tests += 1
        volume = _exact_overlap_volume(db, inst_a, inst_b)
        if volume is None:
            report.skipped.append(f"{inst_a.path} vs {inst_b.path}: geometry unavailable")
            report.clashes.append(_clash(inst_a, inst_b, "clearance", depth=tightest))
        elif volume > 1e-6:
            report.clashes.append(_clash(inst_a, inst_b, "clash", volume=volume, depth=tightest))
        elif clearance > 0 and tightest < clearance:
            report.clashes.append(_clash(inst_a, inst_b, "clearance", depth=tightest))

    if store:
        db.execute("DELETE FROM interference WHERE root_rev = ?", (root_rev,))
        for c in report.clashes:
            db.execute(
                """INSERT INTO interference
                   (root_rev, path_a, path_b, kind, label, depth, volume)
                   VALUES (?,?,?,?,'advisory',?,?)""",
                (root_rev, c.path_a, c.path_b, c.kind, c.depth, c.volume),
            )
    return report


def _clash(a, b, kind: str, volume: float = 0.0, depth: float = 0.0) -> Clash:
    return Clash(
        path_a=a.path, path_b=b.path, part_a=a.part_number, part_b=b.part_number,
        team_a=a.team_id, team_b=b.team_id, kind=kind, volume=volume, depth=depth,
    )


def _exact_overlap_volume(db: Database, inst_a, inst_b) -> float | None:
    """Volume of the intersection of two placed solids, via the kernel.

    The only place outside ingest where geometry is opened, and it reads from the
    content-addressed blob store rather than from any source file -- which is
    what that store is for.
    """
    shape_a = db.read_blob(inst_a.fingerprint)
    shape_b = db.read_blob(inst_b.fingerprint)
    if shape_a is None or shape_b is None:
        return None
    try:
        from OCP.BRepGProp import BRepGProp
        from OCP.GProp import GProp_GProps

        from ..synthetic.cad import common, place

        placed_a = place(shape_a, inst_a.world)
        placed_b = place(shape_b, inst_b.world)
        result = common(placed_a, placed_b)
        props = GProp_GProps()
        BRepGProp.VolumeProperties_s(result, props)
        return abs(float(props.Mass()))
    except Exception:
        return None


def stored(db: Database, root: str) -> list[dict]:
    root_rev = q.resolve_revision(db, root)
    return [dict(r) for r in db.query(
        "SELECT * FROM interference WHERE root_rev = ? ORDER BY kind, volume DESC", (root_rev,)
    )]
