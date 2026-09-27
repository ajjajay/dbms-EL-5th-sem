"""Interface contracts (design doc section 8).

A part has a public face and a private body; other teams may depend only on the
public face. This module is the public face: declarations, how a declared
interface is *located in the geometry* rather than trusted, how a contract is
hashed so a change can be classified, and how a socket is matched to an
interface.

Two departures from the document, both deliberate:

  * An interface declaration is a *selector* ("the four 3.3 mm holes"), not a
    list of coordinates. The points are read off the geometry at ingest, so the
    declaration cannot drift from the part, and a carried-forward declaration is
    re-run against changed geometry -- which is how moving a hole becomes a
    contract change without anyone editing a coordinate.

  * The document calls step 5, placement, "one matrix multiplication" and says to
    treat it as an afterthought. It is the only step that catches a whole hole
    pattern shifted rigidly by two millimetres: spacings are unchanged, so steps
    1-4 all pass, and the bolts would not go in. Placement is therefore a
    verification (registration, then the pattern's holes are measured against the
    socket under the transform the assembly actually applies), not arithmetic.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from scipy.optimize import linear_sum_assignment

from ..db.database import Database, json_dumps, json_loads, new_id
from ..geometry.features import best_registration, canonical_direction
from ..query.traversal import FASTENER_DIAMETER, fastener_diameter, find_path

# --------------------------------------------------------------- fit classes

FIT_RULES = {
    # (min, max) hole diameter as a function of the fastener's nominal diameter d
    "clearance": lambda d: (d + 0.1, d * 1.20),
    "close": lambda d: (d + 0.05, d + 0.60),
    "press": lambda d: (d - 0.05, d + 0.05),
    "tapped": lambda d: (0.78 * d, 0.92 * d),
}


def hole_accepts(fastener: str | None, fit_class: str, hole_radius: float) -> tuple[bool, str]:
    d = fastener_diameter(fastener)
    if d is None:
        return True, f"no known fastener {fastener!r}; hole size not checked"
    lo, hi = FIT_RULES.get(fit_class, FIT_RULES["clearance"])(d)
    diameter = 2.0 * hole_radius
    ok = lo - 1e-9 <= diameter <= hi + 1e-9
    verdict = "accepts" if ok else "does not accept"
    return ok, (
        f"hole diameter {diameter:.3f} mm {verdict} {fastener} as a {fit_class} fit "
        f"(allowed {lo:.2f}-{hi:.2f} mm)"
    )


def alignment_allowance(iface_radius: float, sock_radius: float | None,
                        fastener: str | None, iface_fit: str, sock_fit: str) -> float:
    """How far a hole may sit from its partner and still pass the fastener.

    A clearance hole gives away (radius - fastener radius); a threaded or press
    hole gives away nothing. Both sides contribute, so two M6 clearance holes of
    3.3 mm radius tolerate 0.6 mm of misalignment, and two millimetres is a real
    failure rather than a rounding matter.
    """
    d = fastener_diameter(fastener)
    if d is None:
        return 0.25
    slack = 0.0
    if iface_fit in ("clearance", "close"):
        slack += max(0.0, iface_radius - d / 2.0)
    if sock_fit in ("clearance", "close") and sock_radius is not None:
        slack += max(0.0, sock_radius - d / 2.0)
    return max(0.05, slack)


# ---------------------------------------------------------------- declarations


@dataclass
class InterfaceDecl:
    name: str
    kind: str = "bolt_pattern"
    fastener: str | None = None
    fit_class: str = "clearance"
    select: dict = field(default_factory=dict)      # radius, radius_tol, count, seat, axis, region
    radius_tol: float = 0.1
    spacing_tol: float = 0.1
    # Explicit form, for kinds that are not derived from a hole pattern.
    points: list | None = None
    hole_radius: float | None = None
    origin: list | None = None
    normal: list | None = None


@dataclass
class SocketDecl:
    name: str
    kind: str = "bolt_pattern"
    fills: str | None = None
    interface_name: str | None = None
    fastener: str | None = None
    fit_class: str = "clearance"
    radius_tol: float = 0.2
    spacing_tol: float = 0.2
    hole_radius: float | None = None
    points: list | None = None
    origin: list | None = None
    normal: list | None = None
    allow: list | None = None                       # [dx, dy, dz]
    mass_budget: float | None = None
    power_budget: float | None = None
    thermal_budget: float | None = None
    derive_from: dict | None = None                 # {"instance": path, "interface": name}


@dataclass
class DimensionDecl:
    name: str
    nominal: float
    tol_plus: float
    tol_minus: float
    units: str = "mm"
    sigma_span: float = 3.0
    distribution: str = "normal"
    datum_a: str | None = None
    datum_b: str | None = None


@dataclass
class Declaration:
    envelope: list | None = None                    # [dx, dy, dz] in the part's own axes
    mass_max: float | None = None
    cg_window: dict | None = None                   # {"min": [..], "max": [..]}
    datums: dict | None = None
    attributes: dict = field(default_factory=dict)  # constraint-bearing: power_w, thermal_w, ...
    metadata: dict = field(default_factory=dict)    # free-form; no constraint reads it
    interfaces: list[InterfaceDecl] = field(default_factory=list)
    sockets: list[SocketDecl] = field(default_factory=list)
    dimensions: list[DimensionDecl] = field(default_factory=list)

    @staticmethod
    def from_dict(d: dict) -> "Declaration":
        return Declaration(
            envelope=d.get("envelope"),
            mass_max=d.get("mass_max"),
            cg_window=d.get("cg_window"),
            datums=d.get("datums"),
            attributes=dict(d.get("attributes", {})),
            metadata=dict(d.get("metadata", {})),
            interfaces=[InterfaceDecl(**i) for i in d.get("interfaces", [])],
            sockets=[SocketDecl(**s) for s in d.get("sockets", [])],
            dimensions=[DimensionDecl(**x) for x in d.get("dimensions", [])],
        )

    def to_dict(self) -> dict:
        from dataclasses import asdict

        return asdict(self)


# ------------------------------------------------------------ derived contract


@dataclass
class DerivedContract:
    """A declaration after it has been checked against real geometry."""

    declaration: Declaration
    interfaces: list[dict] = field(default_factory=list)
    sockets: list[dict] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    provenance: str = "declared"
    bbox: tuple | None = None                       # (xmin, ymin, zmin, xmax, ymax, zmax)

    @property
    def ok(self) -> bool:
        return not self.problems


def _round(x, digits=2):
    return None if x is None else round(float(x), digits)


def _round_points(points, digits=2):
    if points is None:
        return None
    return [[round(float(c), digits) + 0.0 for c in p] for p in points]


def contract_hash(c: DerivedContract) -> str:
    """Hash of a revision's public face, and nothing else.

    Classification (section 9) compares this. Free-form metadata is excluded on
    purpose: nothing checks it, so changing a supplier note must not notify
    another team.
    """
    d = c.declaration
    payload = {
        "envelope": None if d.envelope is None else [_round(v, 3) for v in d.envelope],
        "mass_max": _round(d.mass_max, 3),
        "cg_window": d.cg_window,
        "datums": d.datums,
        "attributes": d.attributes,
        "interfaces": sorted(
            (
                {
                    "name": i["name"], "kind": i["kind"], "count": i.get("hole_count"),
                    "radius": _round(i.get("hole_radius"), 3), "fastener": i.get("fastener"),
                    "fit": i.get("fit_class"), "points": _round_points(i.get("points")),
                    "normal": _round_points([i["normal"]], 3)[0] if i.get("normal") else None,
                    "rtol": i.get("radius_tol"), "stol": i.get("spacing_tol"),
                }
                for i in c.interfaces
            ),
            key=lambda x: x["name"],
        ),
        "sockets": sorted(
            (
                {
                    "name": s["name"], "kind": s["kind"], "fills": s.get("fills"),
                    "iface": s.get("interface_name"), "count": s.get("hole_count"),
                    "radius": _round(s.get("hole_radius"), 3), "fastener": s.get("fastener"),
                    "fit": s.get("fit_class"), "points": _round_points(s.get("points")),
                    "allow": s.get("allow"), "mass": s.get("mass_budget"),
                    "power": s.get("power_budget"), "thermal": s.get("thermal_budget"),
                }
                for s in c.sockets
            ),
            key=lambda x: x["name"],
        ),
        "dimensions": sorted(
            (
                [x.name, _round(x.nominal, 4), _round(x.tol_plus, 4), _round(x.tol_minus, 4),
                 x.sigma_span, x.distribution]
                for x in d.dimensions
            )
        ),
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.blake2b(blob.encode("utf-8"), digest_size=8).hexdigest()


# ----------------------------------------------------- locating an interface


@dataclass
class Located:
    points: np.ndarray
    hole_radius: float
    origin: np.ndarray
    normal: np.ndarray
    spacings: list[float]
    problems: list[str]


def _angle(a, b) -> float:
    a = np.asarray(a, float)
    b = np.asarray(b, float)
    c = float(np.clip(abs(np.dot(a, b)) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-15), 0.0, 1.0))
    return math.acos(c)


def locate_pattern(select: dict, bores, planes) -> Located | None:
    """Find a declared hole pattern in extracted bores, and put its points on the
    seating plane.

    Returns None with nothing to report when there are no candidate holes at all;
    otherwise returns the best match together with any disagreement between what
    was declared and what the geometry contains.
    """
    problems: list[str] = []
    radius = select.get("radius")
    rtol = float(select.get("radius_tol", 0.3))
    count = select.get("count")
    region = select.get("region")

    cands = [b for b in bores if b.complete]
    if radius is not None:
        cands = [b for b in cands if abs(b.radius - radius) <= rtol]
    if select.get("axis") is not None:
        ax = np.asarray(select["axis"], float)
        cands = [b for b in cands if _angle(b.axis, ax) <= math.radians(2.0)]
    if region is not None:
        centre = np.asarray(region["center"], float)
        within = float(region["within"])
        cands = [b for b in cands if np.linalg.norm(b.axis_point - centre) <= within]
    if not cands:
        what = f"radius {radius}+-{rtol} mm" if radius is not None else "any radius"
        return Located(np.zeros((0, 3)), float(radius or 0), np.zeros(3), np.array([0, 0, 1.0]),
                       [], [f"no holes matching {what} were found in the geometry"])

    groups: list[list] = []
    for b in cands:
        for g in groups:
            if _angle(b.axis, g[0].axis) <= math.radians(0.5):
                g.append(b)
                break
        else:
            groups.append([b])
    groups.sort(key=lambda g: -len(g))
    chosen = groups[0]
    if count is not None:
        exact = [g for g in groups if len(g) == count]
        if exact:
            chosen = exact[0]
        else:
            problems.append(
                f"declared {count} holes but the geometry has {len(chosen)} matching holes "
                f"on the best axis"
            )

    axis = canonical_direction(chosen[0].axis)
    seat = np.asarray(select.get("seat", axis), float)
    seat = seat / np.linalg.norm(seat)
    if abs(float(np.dot(seat, axis))) < math.cos(math.radians(1.0)):
        problems.append("declared seat direction is not parallel to the hole axes")
        seat = axis
    # The declared seat direction is kept as declared. Forcing it to agree with
    # the (arbitrarily signed) canonical hole axis would discard exactly the
    # information the declaration exists to carry: which face the part sits on.
    # Only the axis is re-signed, so the projection walks toward the seat.
    axis_toward_seat = axis if float(np.dot(seat, axis)) > 0 else -axis

    # Seating plane: the outermost planar face on the seat side that is parallel
    # to the holes' end faces and large enough to be a real seat.
    plane_offset = None
    parallel = [p for p in planes if _angle(p.normal, axis) <= math.radians(1.0)]
    if parallel:
        biggest = max(p.area for p in parallel)
        real = [p for p in parallel if p.area >= 0.05 * biggest]
        offsets = [float(np.dot(np.asarray(p.origin, float), seat)) for p in real]
        want = select.get("seat_offset")
        plane_offset = float(want) if want is not None else max(offsets)
    else:
        problems.append("no plane parallel to the pattern found; using hole axes at mid-height")

    pts = []
    for b in chosen:
        p0 = np.asarray(b.axis_point, float)
        if plane_offset is None:
            pts.append(p0)
        else:
            t = (plane_offset - float(np.dot(p0, seat))) / float(np.dot(axis_toward_seat, seat))
            pts.append(p0 + t * axis_toward_seat)
    pts = np.vstack(pts)
    r = float(np.mean([b.radius for b in chosen]))
    order = np.lexsort((pts[:, 2], pts[:, 1], pts[:, 0]))
    pts = pts[order]
    d = np.linalg.norm(pts[:, None, :] - pts[None, :, :], axis=-1)
    spacings = sorted(float(x) for x in d[np.triu_indices(len(pts), k=1)])
    return Located(pts, r, pts.mean(axis=0), seat, spacings, problems)


def derive_contract(
    decl: Declaration,
    identity=None,
    solid=None,
    provenance: str = "declared",
) -> DerivedContract:
    """Check a declaration against the geometry it describes.

    Every discrepancy is returned as a problem rather than silently repaired: a
    declaration that does not match its own part is exactly the stale interface
    document the system exists to replace.
    """
    out = DerivedContract(declaration=decl, provenance=provenance)
    if solid is not None:
        out.bbox = tuple(float(v) for v in solid.bbox_axis_aligned)
        if decl.envelope is None:
            pass

    for i in decl.interfaces:
        row: dict[str, Any] = {
            "name": i.name, "kind": i.kind, "fastener": i.fastener, "fit_class": i.fit_class,
            "radius_tol": i.radius_tol, "spacing_tol": i.spacing_tol,
            "selector": json_dumps(i.select) if i.select else None,
            "source": "auto_drafted" if provenance == "auto_drafted" else "declared",
        }
        if i.points is not None:
            pts = np.asarray(i.points, float)
            row.update(
                hole_count=len(pts), hole_radius=i.hole_radius, points=pts.tolist(),
                origin=(i.origin or pts.mean(axis=0).tolist()), normal=(i.normal or [0, 0, 1]),
            )
            d = np.linalg.norm(pts[:, None, :] - pts[None, :, :], axis=-1)
            row["spacings"] = sorted(float(x) for x in d[np.triu_indices(len(pts), k=1)])
        elif identity is not None:
            located = locate_pattern(i.select or {}, identity.bores, identity.planes)
            if located is None or len(located.points) == 0:
                out.problems.append(
                    f"interface {i.name!r}: " + "; ".join(located.problems if located else ["no geometry"])
                )
                continue
            for p in located.problems:
                out.problems.append(f"interface {i.name!r}: {p}")
            row.update(
                hole_count=len(located.points), hole_radius=located.hole_radius,
                points=located.points.tolist(), origin=located.origin.tolist(),
                normal=located.normal.tolist(), spacings=located.spacings,
            )
            if i.fastener:
                ok, msg = hole_accepts(i.fastener, i.fit_class, located.hole_radius)
                if not ok:
                    out.problems.append(f"interface {i.name!r}: {msg}")
        else:
            out.problems.append(f"interface {i.name!r} needs geometry or explicit points")
            continue
        out.interfaces.append(row)

    for s in decl.sockets:
        row = {
            "name": s.name, "kind": s.kind, "fills": s.fills or s.name,
            "interface_name": s.interface_name, "fastener": s.fastener, "fit_class": s.fit_class,
            "radius_tol": s.radius_tol, "spacing_tol": s.spacing_tol, "hole_radius": s.hole_radius,
            "allow": s.allow, "mass_budget": s.mass_budget, "power_budget": s.power_budget,
            "thermal_budget": s.thermal_budget, "derive_from": s.derive_from,
            "origin": s.origin, "normal": s.normal,
        }
        if s.points is not None:
            pts = np.asarray(s.points, float)
            row["points"] = pts.tolist()
            row["hole_count"] = len(pts)
            d = np.linalg.norm(pts[:, None, :] - pts[None, :, :], axis=-1)
            row["spacings"] = sorted(float(x) for x in d[np.triu_indices(len(pts), k=1)])
            row["derivation"] = "declared"
        out.sockets.append(row)

    if decl.envelope is not None and out.bbox is not None:
        ex = [out.bbox[3] - out.bbox[0], out.bbox[4] - out.bbox[1], out.bbox[5] - out.bbox[2]]
        for axis_name, actual, allowed in zip("xyz", ex, decl.envelope):
            if actual > allowed + 1e-6 * max(allowed, 1.0):
                out.problems.append(
                    f"envelope: actual {axis_name}-extent {actual:.3f} mm exceeds declared {allowed:.3f} mm"
                )
    return out


# ------------------------------------------------------------------- storage


def store_contract(
    db: Database, revision_id: str, c: DerivedContract, declared_by: str | None = None,
    reviewed: bool = True,
) -> str:
    """Write a derived contract for a revision. Returns the contract hash."""
    h = contract_hash(c)
    d = c.declaration
    env = d.envelope or [None, None, None]
    db.execute(
        """INSERT INTO contract
           (contract_id, revision_id, envelope_dx, envelope_dy, envelope_dz, mass_max,
            cg_window, datums, attributes, contract_hash, provenance, reviewed, declared_by)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            new_id("con"), revision_id, env[0], env[1], env[2], d.mass_max,
            json_dumps(d.cg_window) if d.cg_window else None,
            json_dumps(d.datums) if d.datums else None,
            json_dumps(d.attributes), h, c.provenance, int(reviewed), declared_by,
        ),
    )
    for i in c.interfaces:
        o = i.get("origin") or [None] * 3
        n = i.get("normal") or [None] * 3
        db.execute(
            """INSERT INTO interface
               (interface_id, revision_id, name, kind, hole_count, hole_radius, ox, oy, oz,
                nx, ny, nz, points, spacings, fastener, fit_class, radius_tol, spacing_tol,
                selector, source)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                new_id("if"), revision_id, i["name"], i["kind"], i.get("hole_count"),
                i.get("hole_radius"), *o, *n,
                json_dumps(i.get("points")), json_dumps(i.get("spacings")),
                i.get("fastener"), i.get("fit_class"), i.get("radius_tol", 0.1),
                i.get("spacing_tol", 0.1), i.get("selector"), i.get("source", "declared"),
            ),
        )
    for s in c.sockets:
        _insert_socket(db, revision_id, s)
    for x in d.dimensions:
        db.execute(
            """INSERT INTO dimension
               (dimension_id, revision_id, name, nominal, tol_plus, tol_minus, units,
                sigma_span, distribution, datum_a, datum_b)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (new_id("dim"), revision_id, x.name, x.nominal, x.tol_plus, x.tol_minus, x.units,
             x.sigma_span, x.distribution, x.datum_a, x.datum_b),
        )
    return h


def _insert_socket(db: Database, revision_id: str, s: dict) -> None:
    o = s.get("origin") or [None] * 3
    n = s.get("normal") or [None] * 3
    allow = s.get("allow") or [None] * 3
    db.execute(
        """INSERT INTO socket
           (socket_id, revision_id, name, kind, hole_count, hole_radius, radius_tol, spacing_tol,
            ox, oy, oz, nx, ny, nz, points, spacings, fastener, fit_class,
            allow_dx, allow_dy, allow_dz, mass_budget, power_budget, thermal_budget,
            fills, interface_name, derivation, derived_from)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            new_id("sk"), revision_id, s["name"], s["kind"], s.get("hole_count"),
            s.get("hole_radius"), s.get("radius_tol", 0.2), s.get("spacing_tol", 0.2),
            *o, *n, json_dumps(s.get("points")), json_dumps(s.get("spacings")),
            s.get("fastener"), s.get("fit_class"), *allow, s.get("mass_budget"),
            s.get("power_budget"), s.get("thermal_budget"), s.get("fills") or s["name"],
            s.get("interface_name"), s.get("derivation", "declared"),
            json_dumps(s["derive_from"]) if s.get("derive_from") else None,
        ),
    )


def resolve_derived_sockets(db: Database, assembly_rev: str, sockets: list[dict]) -> list[str]:
    """Fill in socket points that are defined by reference to a sibling.

    A socket is written in its assembly's frame, but the thing it mates with is
    usually a sibling part with its own frame. Reading the sibling's interface
    and composing its placement carries the pattern into the assembly frame --
    the transform composition the document says matching does not need.
    """
    problems: list[str] = []
    for s in sockets:
        ref = s.get("derive_from")
        if not ref or s.get("points") is not None:
            continue
        inst = find_path(db, assembly_rev, ref["instance"])
        if inst is None:
            problems.append(f"socket {s['name']!r}: no instance {ref['instance']!r} under this assembly")
            continue
        row = db.one(
            "SELECT * FROM interface WHERE revision_id = ? AND name = ?",
            (inst.revision_id, ref["interface"]),
        )
        if row is None:
            problems.append(
                f"socket {s['name']!r}: {inst.part_number} publishes no interface {ref['interface']!r}"
            )
            continue
        local = np.asarray(json_loads(row["points"], []), float)
        world = (inst.world[:3, :3] @ local.T).T + inst.world[:3, 3]
        s["points"] = world.tolist()
        s["hole_count"] = len(world)
        s["hole_radius"] = s.get("hole_radius") or row["hole_radius"]
        d = np.linalg.norm(world[:, None, :] - world[None, :, :], axis=-1)
        s["spacings"] = sorted(float(x) for x in d[np.triu_indices(len(world), k=1)])
        n_local = np.array([row["nx"], row["ny"], row["nz"]], float)
        s["normal"] = (inst.world[:3, :3] @ n_local).tolist()
        s["origin"] = world.mean(axis=0).tolist()
        s["derivation"] = "derived"
        s["derived_from"] = ref
        s.setdefault("fastener", row["fastener"])
    return problems


def rederive_sockets(db: Database, revision_id: str) -> list[str]:
    """Recompute the sockets on a revision that are views of a child's interface.

    A derived socket is not data of its own: it is the parent's statement that
    "whatever fills this must match the pattern my other child publishes". When
    that child changes, the socket has to be recomputed, or the parent keeps
    validating against a pattern that no longer exists -- which is precisely the
    stale interface document the system exists to replace.

    This is what makes moving one hole on the base plate break the bearing mount
    rather than quietly passing.
    """
    rows = db.query(
        "SELECT * FROM socket WHERE revision_id = ? AND derived_from IS NOT NULL", (revision_id,)
    )
    if not rows:
        return []
    sockets = []
    for row in rows:
        s = row_to_socket(row)
        s["derive_from"] = json_loads(row["derived_from"])
        s["points"] = None
        sockets.append((row["socket_id"], s))

    problems = resolve_derived_sockets(db, revision_id, [s for _, s in sockets])
    for socket_id, s in sockets:
        if s.get("points") is None:
            continue
        o = s.get("origin") or [None] * 3
        n = s.get("normal") or [None] * 3
        db.execute(
            """UPDATE socket SET points = ?, spacings = ?, hole_count = ?, hole_radius = ?,
                   ox = ?, oy = ?, oz = ?, nx = ?, ny = ?, nz = ?, derivation = 'derived'
               WHERE socket_id = ?""",
            (json_dumps(s["points"]), json_dumps(s.get("spacings")), s.get("hole_count"),
             s.get("hole_radius"), *o, *n, socket_id),
        )
    return problems


def recompute_contract_hash(db: Database, revision_id: str) -> str | None:
    """Rehash a revision's public face from its stored rows.

    Needed after a derived socket moves: the socket is part of the contract, so
    the contract hash has to follow it, or classification would report that the
    assembly's public face is unchanged when it demonstrably is not.
    """
    row = db.one("SELECT * FROM contract WHERE revision_id = ?", (revision_id,))
    if row is None:
        return None
    decl = load_declaration(db, revision_id)
    derived = DerivedContract(
        declaration=decl,
        interfaces=[row_to_interface(r) for r in
                    db.query("SELECT * FROM interface WHERE revision_id = ?", (revision_id,))],
        sockets=[row_to_socket(r) for r in
                 db.query("SELECT * FROM socket WHERE revision_id = ?", (revision_id,))],
        provenance=row["provenance"],
    )
    h = contract_hash(derived)
    db.execute("UPDATE contract SET contract_hash = ? WHERE revision_id = ?", (h, revision_id))
    return h


def load_declaration(db: Database, revision_id: str) -> Declaration | None:
    """Reconstruct a revision's declaration from its stored rows, selectors
    included. This is what lets a contract be carried forward onto new geometry."""
    c = db.one("SELECT * FROM contract WHERE revision_id = ?", (revision_id,))
    if c is None:
        return None
    env = None if c["envelope_dx"] is None else [c["envelope_dx"], c["envelope_dy"], c["envelope_dz"]]
    decl = Declaration(
        envelope=env, mass_max=c["mass_max"], cg_window=json_loads(c["cg_window"]),
        datums=json_loads(c["datums"]), attributes=json_loads(c["attributes"], {}),
    )
    for i in db.query("SELECT * FROM interface WHERE revision_id = ? ORDER BY name", (revision_id,)):
        select = json_loads(i["selector"], {})
        decl.interfaces.append(
            InterfaceDecl(
                name=i["name"], kind=i["kind"], fastener=i["fastener"], fit_class=i["fit_class"],
                select=select, radius_tol=i["radius_tol"], spacing_tol=i["spacing_tol"],
                points=None if select else json_loads(i["points"]),
                hole_radius=i["hole_radius"],
                origin=None if select else [i["ox"], i["oy"], i["oz"]],
                normal=None if select else [i["nx"], i["ny"], i["nz"]],
            )
        )
    for s in db.query("SELECT * FROM socket WHERE revision_id = ? ORDER BY name", (revision_id,)):
        allow = None if s["allow_dx"] is None else [s["allow_dx"], s["allow_dy"], s["allow_dz"]]
        decl.sockets.append(
            SocketDecl(
                name=s["name"], kind=s["kind"], fills=s["fills"], interface_name=s["interface_name"],
                fastener=s["fastener"], fit_class=s["fit_class"], radius_tol=s["radius_tol"],
                spacing_tol=s["spacing_tol"], hole_radius=s["hole_radius"],
                points=json_loads(s["points"]),
                origin=[s["ox"], s["oy"], s["oz"]] if s["ox"] is not None else None,
                normal=[s["nx"], s["ny"], s["nz"]] if s["nx"] is not None else None,
                allow=allow, mass_budget=s["mass_budget"], power_budget=s["power_budget"],
                thermal_budget=s["thermal_budget"], derive_from=json_loads(s["derived_from"]),
            )
        )
    for d in db.query("SELECT * FROM dimension WHERE revision_id = ? ORDER BY name", (revision_id,)):
        decl.dimensions.append(
            DimensionDecl(
                name=d["name"], nominal=d["nominal"], tol_plus=d["tol_plus"], tol_minus=d["tol_minus"],
                units=d["units"], sigma_span=d["sigma_span"], distribution=d["distribution"],
                datum_a=d["datum_a"], datum_b=d["datum_b"],
            )
        )
    return decl


def copy_contract(db: Database, old_rev: str, new_rev: str) -> None:
    """Carry a contract onto a new revision unchanged (an ancestor rebuilt because
    a descendant changed). Socket points are copied as stored: they are written in
    the assembly's own frame, which did not move."""
    c = db.one("SELECT * FROM contract WHERE revision_id = ?", (old_rev,))
    if c is None:
        return
    db.execute(
        """INSERT INTO contract
           (contract_id, revision_id, envelope_dx, envelope_dy, envelope_dz, mass_max, cg_window,
            datums, attributes, contract_hash, provenance, reviewed, declared_by)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (new_id("con"), new_rev, c["envelope_dx"], c["envelope_dy"], c["envelope_dz"], c["mass_max"],
         c["cg_window"], c["datums"], c["attributes"], c["contract_hash"], c["provenance"],
         c["reviewed"], c["declared_by"]),
    )
    for i in db.query("SELECT * FROM interface WHERE revision_id = ?", (old_rev,)):
        db.execute(
            """INSERT INTO interface
               (interface_id, revision_id, name, kind, hole_count, hole_radius, ox, oy, oz, nx, ny, nz,
                points, spacings, fastener, fit_class, radius_tol, spacing_tol, selector, source)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (new_id("if"), new_rev, i["name"], i["kind"], i["hole_count"], i["hole_radius"],
             i["ox"], i["oy"], i["oz"], i["nx"], i["ny"], i["nz"], i["points"], i["spacings"],
             i["fastener"], i["fit_class"], i["radius_tol"], i["spacing_tol"], i["selector"], i["source"]),
        )
    for s in db.query("SELECT * FROM socket WHERE revision_id = ?", (old_rev,)):
        db.execute(
            """INSERT INTO socket
               (socket_id, revision_id, name, kind, hole_count, hole_radius, radius_tol, spacing_tol,
                ox, oy, oz, nx, ny, nz, points, spacings, fastener, fit_class,
                allow_dx, allow_dy, allow_dz, mass_budget, power_budget, thermal_budget,
                fills, interface_name, derivation, derived_from)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (new_id("sk"), new_rev, s["name"], s["kind"], s["hole_count"], s["hole_radius"],
             s["radius_tol"], s["spacing_tol"], s["ox"], s["oy"], s["oz"], s["nx"], s["ny"], s["nz"],
             s["points"], s["spacings"], s["fastener"], s["fit_class"], s["allow_dx"], s["allow_dy"],
             s["allow_dz"], s["mass_budget"], s["power_budget"], s["thermal_budget"], s["fills"],
             s["interface_name"], s["derivation"], s["derived_from"]),
        )
    for d in db.query("SELECT * FROM dimension WHERE revision_id = ?", (old_rev,)):
        db.execute(
            """INSERT INTO dimension
               (dimension_id, revision_id, name, nominal, tol_plus, tol_minus, units, sigma_span,
                distribution, datum_a, datum_b) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (new_id("dim"), new_rev, d["name"], d["nominal"], d["tol_plus"], d["tol_minus"],
             d["units"], d["sigma_span"], d["distribution"], d["datum_a"], d["datum_b"]),
        )


# --------------------------------------------------------------- auto-drafting


def guess_fastener(hole_radius: float) -> str | None:
    """Smallest standard fastener whose clearance range contains this hole."""
    for name, d in sorted(FASTENER_DIAMETER.items(), key=lambda kv: kv[1]):
        lo, hi = FIT_RULES["clearance"](d)
        if lo <= 2 * hole_radius <= hi:
            return name
    return None


def auto_draft(identity, solid) -> DerivedContract:
    """Draft a contract for a part that arrived with none.

    Downloaded models carry no declarations, and authoring several hundred by hand
    is the metadata tax the design document names as its main risk. Detected hole
    patterns become candidate interfaces and the bounding box becomes the
    envelope. Nothing here infers mates -- that stays a stated non-goal -- and the
    result is stored labelled 'auto_drafted' and unreviewed, so it can never pass
    for a human decision.
    """
    decl = Declaration(metadata={"note": "auto-drafted from geometry; unreviewed"})
    xmin, ymin, zmin, xmax, ymax, zmax = solid.bbox_axis_aligned
    decl.envelope = [round(xmax - xmin, 3), round(ymax - ymin, 3), round(zmax - zmin, 3)]
    for k, pattern in enumerate(identity.patterns[:6], start=1):
        decl.interfaces.append(
            InterfaceDecl(
                name=f"pattern_{k}",
                fastener=guess_fastener(pattern.radius),
                fit_class="clearance",
                select={"radius": pattern.radius, "radius_tol": 0.05, "count": pattern.count,
                        "axis": [float(x) for x in pattern.axis]},
            )
        )
    derived = derive_contract(decl, identity, solid, provenance="auto_drafted")
    # Problems in an auto-draft are informational, not a reason to drop the part.
    derived.problems = []
    return derived


# ------------------------------------------------------------ socket matching


@dataclass
class Step:
    name: str
    passed: bool
    detail: str
    data: dict = field(default_factory=dict)


@dataclass
class MatchOutcome:
    socket: str
    interface: str | None
    steps: list[Step] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(s.passed for s in self.steps)

    @property
    def first_failure(self) -> Step | None:
        return next((s for s in self.steps if not s.passed), None)


def check_pattern(sock: dict, iface: dict) -> Step:
    """Step 1: hole count matches and each pairwise spacing agrees within the
    looser of the two tolerances."""
    tol = max(float(sock.get("spacing_tol") or 0.2), float(iface.get("spacing_tol") or 0.1))
    sc, ic = sock.get("hole_count"), iface.get("hole_count")
    if sc != ic:
        return Step("pattern", False, f"socket needs {sc} holes, interface offers {ic}")
    a = sorted(json_loads(sock.get("spacings_json"), None) or sock.get("spacings") or [])
    b = sorted(json_loads(iface.get("spacings_json"), None) or iface.get("spacings") or [])
    if len(a) != len(b):
        return Step("pattern", False, "spacing lists differ in length")
    worst = max((abs(x - y) for x, y in zip(a, b)), default=0.0)
    ok = worst <= tol
    return Step(
        "pattern", ok,
        f"{ic} holes; worst pairwise spacing difference {worst:.3f} mm "
        f"({'within' if ok else 'exceeds'} the looser tolerance {tol:.2f} mm)",
        {"worst": worst, "tol": tol},
    )


def check_fit(sock: dict, iface: dict) -> Step:
    """Step 2: the offered hole accepts the fastener the socket specifies."""
    fastener = sock.get("fastener") or iface.get("fastener")
    r = iface.get("hole_radius")
    if r is None:
        return Step("fit", True, "interface declares no hole size")
    if sock.get("fastener") and iface.get("fastener") and sock["fastener"] != iface["fastener"]:
        return Step("fit", False,
                    f"socket takes {sock['fastener']} but interface is declared for {iface['fastener']}")
    fit = sock.get("fit_class") or iface.get("fit_class") or "clearance"
    ok, msg = hole_accepts(fastener, fit, float(r))
    if ok and sock.get("hole_radius") is not None and fastener is None:
        tol = float(sock.get("radius_tol") or 0.2)
        ok = abs(float(r) - float(sock["hole_radius"])) <= tol
        msg = f"hole radius {r:.3f} vs socket {sock['hole_radius']:.3f} (tol {tol:.2f})"
    return Step("fit", ok, msg)


def transformed_extents(bbox, world: np.ndarray) -> np.ndarray:
    xmin, ymin, zmin, xmax, ymax, zmax = bbox
    corners = np.array([[x, y, z, 1.0] for x in (xmin, xmax) for y in (ymin, ymax) for z in (zmin, zmax)])
    placed = (world @ corners.T).T[:, :3]
    return placed.max(axis=0) - placed.min(axis=0)


def check_envelope(sock: dict, bbox, world: np.ndarray) -> Step:
    """Step 3: the candidate's bounds, placed in the socket's frame, fit the allowance."""
    allow = sock.get("allow")
    if not allow or any(a is None for a in allow):
        return Step("envelope", True, "socket sets no allowance")
    if bbox is None:
        return Step("envelope", True, "no geometry bounds available")
    ext = transformed_extents(bbox, world)
    over = [(ax, float(e), float(a)) for ax, e, a in zip("xyz", ext, allow) if e > a + 1e-6]
    if over:
        ax, e, a = over[0]
        return Step("envelope", False,
                    f"placed {ax}-extent {e:.2f} mm exceeds the socket allowance {a:.2f} mm",
                    {"extents": ext.tolist(), "allow": list(allow)})
    return Step("envelope", True,
                f"placed extents {ext[0]:.1f} x {ext[1]:.1f} x {ext[2]:.1f} mm fit the allowance",
                {"extents": ext.tolist(), "allow": list(allow)})


def check_placement(sock: dict, iface: dict, world: np.ndarray) -> list[Step]:
    """Step 5, as verification.

    First the two point sets are registered (correspondence search, then Kabsch
    with the reflection branch suppressed). That fails for a mirrored pattern,
    which spacing comparison cannot see. Then the interface's holes, moved by the
    transform the assembly *actually applies*, are measured against the socket's,
    and the residual is compared with what the fastener's clearance permits.
    """
    steps: list[Step] = []
    ip = np.asarray(iface.get("points") or [], float)
    sp = np.asarray(sock.get("points") or [], float)
    if len(ip) == 0 or len(sp) == 0 or len(ip) != len(sp):
        return [Step("placement", False, "no point sets to register")]

    reg = best_registration(ip, sp)
    if reg is None:
        return [Step("registration", False, "could not find a correspondence between the patterns")]
    tol = max(float(sock.get("spacing_tol") or 0.2), float(iface.get("spacing_tol") or 0.1))
    ok = reg.rmsd <= tol and not reg.reflected
    detail = f"rigid registration residual {reg.rmsd:.3f} mm"
    if reg.reflected:
        detail = "the pattern only registers as a mirror image (opposite hand)"
    steps.append(Step("registration", ok, detail, {"rmsd": reg.rmsd}))

    placed = (world[:3, :3] @ ip.T).T + world[:3, 3]
    cost = np.linalg.norm(placed[:, None, :] - sp[None, :, :], axis=-1)
    rows, cols = linear_sum_assignment(cost)
    miss = cost[rows, cols]
    allowance = alignment_allowance(
        float(iface.get("hole_radius") or 0.0), sock.get("hole_radius"),
        sock.get("fastener") or iface.get("fastener"),
        iface.get("fit_class") or "clearance", sock.get("fit_class") or "clearance",
    )
    worst = float(miss.max())
    steps.append(
        Step(
            "alignment", worst <= allowance,
            f"under the assembly's placement the worst hole is {worst:.2f} mm from its partner; "
            f"the fastener clearance allows {allowance:.2f} mm",
            {"worst": worst, "allowance": allowance, "misses": [float(x) for x in miss]},
        )
    )

    n_i = iface.get("normal")
    n_s = sock.get("normal")
    if n_i is not None and n_s is not None and all(v is not None for v in list(n_i) + list(n_s)):
        ni = world[:3, :3] @ np.asarray(n_i, float)
        ns = np.asarray(n_s, float)
        cosang = float(np.dot(ni, ns) / (np.linalg.norm(ni) * np.linalg.norm(ns) + 1e-15))
        ok = cosang <= -math.cos(math.radians(5.0))
        steps.append(
            Step("orientation", ok,
                 "seating faces oppose each other" if ok
                 else f"seating faces do not oppose (cos {cosang:+.2f}): the part is flipped or misoriented")
        )
    return steps


def match_socket(
    sock: dict, iface: dict, bbox, world: np.ndarray,
) -> MatchOutcome:
    """Run steps 1, 2, 3 and 5 for one socket against one interface. Step 4
    (budgets) needs the whole configuration and is evaluated by the commit
    pipeline, which holds it."""
    out = MatchOutcome(socket=sock["name"], interface=iface.get("name"))
    for step in (check_pattern(sock, iface), check_fit(sock, iface), check_envelope(sock, bbox, world)):
        out.steps.append(step)
    out.steps.extend(check_placement(sock, iface, world))
    return out


def row_to_socket(row) -> dict:
    d = dict(row)
    d["points"] = json_loads(d.get("points"), [])
    d["spacings"] = json_loads(d.get("spacings"), [])
    d["allow"] = (
        [d["allow_dx"], d["allow_dy"], d["allow_dz"]] if d.get("allow_dx") is not None else None
    )
    if d.get("nx") is not None:
        d["normal"] = [d["nx"], d["ny"], d["nz"]]
    return d


def row_to_interface(row) -> dict:
    d = dict(row)
    d["points"] = json_loads(d.get("points"), [])
    d["spacings"] = json_loads(d.get("spacings"), [])
    if d.get("nx") is not None:
        d["normal"] = [d["nx"], d["ny"], d["nz"]]
    return d
