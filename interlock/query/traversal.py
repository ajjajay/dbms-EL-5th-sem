"""The query layer (design doc section 12).

Everything interesting here is a traversal of the occurrence table. None of it
opens geometry: the kernel ran at ingest and left ordinary columns behind, which
is why these queries return in milliseconds.

  explode        walk down from a root, multiplying quantities along the way
  where_used     walk up from a part to every assembly that contains it
  impact         who is affected if a part's contract changes
  substitution   what else would fit this socket, ranked by shape proximity
  configuration  flatten a revision into placed instances (used by rollups and
                 by every constraint that needs transforms composed)

The recursive queries carry an explicit depth guard. The cycle trigger should
make a loop impossible, but a recursive query without a guard turns any bug that
lets one through into an infinite loop, so the guard is not optional.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from ..db.database import Database, json_loads

DEPTH_GUARD = 64


# ------------------------------------------------------------------ helpers


def matrix_of(row) -> np.ndarray:
    m = np.eye(4)
    m[:3, :] = np.array(
        [row["m00"], row["m01"], row["m02"], row["m03"],
         row["m10"], row["m11"], row["m12"], row["m13"],
         row["m20"], row["m21"], row["m22"], row["m23"]], dtype=float,
    ).reshape(3, 4)
    return m


def resolve_revision(db: Database, ref: str) -> str:
    """Accept a revision id, a part number (its latest revision) or 'main'."""
    if db.one("SELECT 1 FROM revision WHERE revision_id = ?", (ref,)):
        return ref
    row = db.one(
        """SELECT r.revision_id FROM revision r JOIN part p ON p.part_id = r.part_id
           WHERE p.part_number = ? ORDER BY r.revision_index DESC LIMIT 1""",
        (ref,),
    )
    if row:
        return row["revision_id"]
    head = db.get_ref(ref)
    if head:
        return head["revision_id"]
    raise LookupError(f"no revision, part or ref named {ref!r}")


def resolve_part(db: Database, ref: str):
    row = db.one("SELECT * FROM part WHERE part_id = ? OR part_number = ?", (ref, ref))
    if row is None:
        rev = db.one(
            "SELECT p.* FROM revision r JOIN part p ON p.part_id = r.part_id WHERE r.revision_id = ?",
            (ref,),
        )
        row = rev
    if row is None:
        raise LookupError(f"no part {ref!r}")
    return row


# ------------------------------------------------------------------ explode


@dataclass
class ExplodedRow:
    depth: int
    path: str
    part_number: str
    revision_id: str
    revision_index: int
    team_id: str
    quantity: int            # total count in the product: product of quantities on the way down
    is_assembly: bool
    mass_g: float | None = None


# Mass uses `volume_exact` -- the kernel's own integration -- rather than
# `volume`, which comes from the mesh and is about half a percent light on a
# cylinder. A mass budget is an integrity constraint here, so it should not
# carry tessellation error. Identity keeps using the mesh volume, because the
# rest of the invariant vector is computed from the same mesh.
EXPLODE_SQL = """
WITH RECURSIVE tree(rev, qty, depth, path) AS (
    SELECT :root, 1, 0, (SELECT p.part_number FROM revision r JOIN part p ON p.part_id = r.part_id
                          WHERE r.revision_id = :root)
    UNION ALL
    SELECT o.child_rev,
           t.qty * o.quantity,
           t.depth + 1,
           t.path || '/' || CASE WHEN o.instance_name <> '' THEN o.instance_name ELSE p.part_number END
    FROM tree t
    JOIN occurrence o ON o.parent_rev = t.rev
    JOIN revision r   ON r.revision_id = o.child_rev
    JOIN part p       ON p.part_id = r.part_id
    WHERE t.depth < :guard
)
SELECT t.depth, t.path, t.qty, r.revision_id, r.revision_index, r.is_assembly,
       p.part_number, p.team_id,
       COALESCE(s.volume_exact, s.volume) * r.density AS mass_g
FROM tree t
JOIN revision r ON r.revision_id = t.rev
JOIN part p     ON p.part_id = r.part_id
LEFT JOIN shape s ON s.fingerprint = r.fingerprint
ORDER BY t.path
"""


def explode(db: Database, root: str, max_depth: int = DEPTH_GUARD) -> list[ExplodedRow]:
    """Every appearance below a root, with the running quantity product."""
    rev = resolve_revision(db, root)
    rows = db.query(EXPLODE_SQL, {"root": rev, "guard": max_depth})
    return [
        ExplodedRow(
            depth=r["depth"], path=r["path"], part_number=r["part_number"],
            revision_id=r["revision_id"], revision_index=r["revision_index"],
            team_id=r["team_id"], quantity=int(r["qty"]), is_assembly=bool(r["is_assembly"]),
            mass_g=r["mass_g"],
        )
        for r in rows
    ]


BOM_SQL = EXPLODE_SQL.replace(
    """SELECT t.depth, t.path, t.qty, r.revision_id, r.revision_index, r.is_assembly,
       p.part_number, p.team_id,
       COALESCE(s.volume_exact, s.volume) * r.density AS mass_g
FROM tree t""",
    """SELECT p.part_number, p.team_id, r.revision_id, r.revision_index,
       SUM(t.qty) AS total_qty,
       COALESCE(s.volume_exact, s.volume) * r.density AS unit_mass_g,
       SUM(t.qty) * COALESCE(s.volume_exact, s.volume) * r.density AS total_mass_g
FROM tree t""",
).replace("ORDER BY t.path", "WHERE r.is_assembly = 0\nGROUP BY r.revision_id\nORDER BY p.part_number")


def bill_of_materials(db: Database, root: str, max_depth: int = DEPTH_GUARD) -> list[dict]:
    """Leaf parts aggregated over the whole tree. The mass rollup shares the
    traversal with the explosion and differs only in the aggregate, which is why
    storing volume at ingest pays off repeatedly."""
    rev = resolve_revision(db, root)
    return [dict(r) for r in db.query(BOM_SQL, {"root": rev, "guard": max_depth})]


# --------------------------------------------------------------- where-used

WHERE_USED_SQL = """
WITH RECURSIVE up(rev, depth, via) AS (
    SELECT r.revision_id, 0, NULL FROM revision r WHERE r.part_id = :part
    UNION
    SELECT o.parent_rev, u.depth + 1, u.rev
    FROM up u
    JOIN occurrence o ON o.child_rev = u.rev
    WHERE u.depth < :guard
)
SELECT u.rev AS revision_id, MIN(u.depth) AS depth, r.revision_index,
       p.part_id, p.part_number, p.team_id
FROM up u
JOIN revision r ON r.revision_id = u.rev
JOIN part p     ON p.part_id = r.part_id
WHERE u.depth > 0
GROUP BY u.rev
ORDER BY depth, p.part_number
"""


@dataclass
class WhereUsedRow:
    part_number: str
    part_id: str
    revision_id: str
    revision_index: int
    team_id: str
    depth: int
    current: bool = False


def configuration_revisions(db: Database, root_rev: str, max_depth: int = DEPTH_GUARD) -> set[str]:
    """The set of revisions reachable below a root: one whole configuration."""
    rows = db.query(
        """WITH RECURSIVE down(rev, depth) AS (
               SELECT :root, 0
               UNION
               SELECT o.child_rev, d.depth + 1 FROM down d
               JOIN occurrence o ON o.parent_rev = d.rev WHERE d.depth < :guard
           ) SELECT rev FROM down""",
        {"root": root_rev, "guard": max_depth},
    )
    return {r["rev"] for r in rows}


def where_used(
    db: Database, part: str, current_only: bool = True, ref: str = "main", max_depth: int = DEPTH_GUARD
) -> list[WhereUsedRow]:
    """Every assembly that contains a part, at any depth.

    Revisions are immutable, so old assembly revisions keep pointing at old child
    revisions forever. With ``current_only`` (the default) the answer is
    restricted to the live configuration, which is the question people mean by
    "who am I about to break".
    """
    row = resolve_part(db, part)
    live: set[str] | None = None
    if current_only:
        head = db.get_ref(ref)
        if head is not None:
            live = configuration_revisions(db, head["revision_id"], max_depth)
    out = []
    for r in db.query(WHERE_USED_SQL, {"part": row["part_id"], "guard": max_depth}):
        if live is not None and r["revision_id"] not in live:
            continue
        out.append(
            WhereUsedRow(
                part_number=r["part_number"], part_id=r["part_id"], revision_id=r["revision_id"],
                revision_index=r["revision_index"], team_id=r["team_id"], depth=r["depth"],
                current=live is None or r["revision_id"] in live,
            )
        )
    return out


def would_create_cycle(db: Database, parent_rev: str, child_rev: str, max_depth: int = DEPTH_GUARD) -> bool:
    """The trigger's check, available before attempting an insert."""
    return parent_rev == child_rev or parent_rev in configuration_revisions(db, child_rev, max_depth)


# -------------------------------------------------------- configuration walk


@dataclass
class Instance:
    path: str
    revision_id: str
    part_id: str
    part_number: str
    team_id: str
    world: np.ndarray            # 4x4 placement relative to the walk's root
    quantity: int                # product of quantities down to here
    parent_rev: str | None
    instance_name: str
    is_assembly: bool
    fingerprint: str | None


def configuration(db: Database, root_rev: str, max_depth: int = DEPTH_GUARD) -> list[Instance]:
    """Flatten a revision into placed instances with composed transforms.

    This is the "cheap transform composition" the design document says matching
    does not need and in fact does: a mate between two siblings, or between a part
    and a socket declared on a grandparent, is only well defined in a common frame.
    """
    revs: dict[str, object] = {}

    def meta(rev: str):
        if rev not in revs:
            revs[rev] = db.one(
                """SELECT r.revision_id, r.part_id, r.fingerprint, r.is_assembly,
                          p.part_number, p.team_id
                   FROM revision r JOIN part p ON p.part_id = r.part_id
                   WHERE r.revision_id = ?""",
                (rev,),
            )
        return revs[rev]

    kids: dict[str, list] = {}

    def children(rev: str):
        if rev not in kids:
            kids[rev] = db.query(
                "SELECT * FROM occurrence WHERE parent_rev = ? ORDER BY occurrence_id", (rev,)
            )
        return kids[rev]

    out: list[Instance] = []

    def walk(rev, path, world, qty, parent, name, depth):
        if depth > max_depth:
            raise RecursionError(f"assembly deeper than {max_depth} at {path}")
        m = meta(rev)
        out.append(
            Instance(
                path=path, revision_id=rev, part_id=m["part_id"], part_number=m["part_number"],
                team_id=m["team_id"], world=world, quantity=qty, parent_rev=parent,
                instance_name=name, is_assembly=bool(m["is_assembly"]), fingerprint=m["fingerprint"],
            )
        )
        for o in children(rev):
            label = o["instance_name"] or meta(o["child_rev"])["part_number"]
            walk(o["child_rev"], f"{path}/{label}", world @ matrix_of(o),
                 qty * int(o["quantity"]), rev, o["instance_name"], depth + 1)

    walk(root_rev, meta(root_rev)["part_number"], np.eye(4), 1, None, "", 0)
    return out


def find_path(db: Database, root_rev: str, path: str) -> Instance | None:
    """Resolve an instance path ('drive/bracket') below a revision, composing
    transforms along the way. ``path`` is relative to ``root_rev``."""
    world = np.eye(4)
    rev = root_rev
    parts = [p for p in path.split("/") if p]
    for step in parts:
        row = db.one(
            """SELECT o.*, p.part_number FROM occurrence o
               JOIN revision r ON r.revision_id = o.child_rev
               JOIN part p ON p.part_id = r.part_id
               WHERE o.parent_rev = ? AND (o.instance_name = ? OR (o.instance_name = '' AND p.part_number = ?))
               LIMIT 1""",
            (rev, step, step),
        )
        if row is None:
            return None
        world = world @ matrix_of(row)
        rev = row["child_rev"]
    m = db.one(
        """SELECT r.*, p.part_number, p.team_id FROM revision r
           JOIN part p ON p.part_id = r.part_id WHERE r.revision_id = ?""", (rev,),
    )
    return Instance(
        path=path, revision_id=rev, part_id=m["part_id"], part_number=m["part_number"],
        team_id=m["team_id"], world=world, quantity=1, parent_rev=None,
        instance_name=parts[-1] if parts else "", is_assembly=bool(m["is_assembly"]),
        fingerprint=m["fingerprint"],
    )


# ------------------------------------------------------------------- rollups


@dataclass
class Rollup:
    mass_g: float = 0.0
    power_w: float = 0.0
    thermal_w: float = 0.0
    cg: tuple[float, float, float] | None = None
    massless_parts: list[str] = field(default_factory=list)


def rollup(db: Database, root_rev: str, max_depth: int = DEPTH_GUARD) -> Rollup:
    """Mass, power, thermal load and centre of gravity of a whole subtree, in the
    root's own frame. Mass is stored volume times density; the same traversal
    gives each aggregate, which is the point of extracting them at ingest."""
    total_m = 0.0
    weighted = np.zeros(3)
    power = 0.0
    thermal = 0.0
    massless: list[str] = []
    attr_cache: dict[str, dict] = {}

    def attrs(rev: str) -> dict:
        if rev not in attr_cache:
            row = db.one("SELECT attributes FROM contract WHERE revision_id = ?", (rev,))
            attr_cache[rev] = json_loads(row["attributes"], {}) if row else {}
        return attr_cache[rev]

    for inst in configuration(db, root_rev, max_depth):
        if inst.is_assembly or inst.fingerprint is None:
            # An assembly's declared power and thermal load are a summary of
            # what is inside it, not an additional draw. Adding both would count
            # every motor twice -- once on the motor and once on the subassembly
            # that declares it. Only leaves contribute; the assembly's own
            # declaration is what the rollup is then checked against.
            continue
        a = attrs(inst.revision_id)
        power += float(a.get("power_w", 0.0)) * inst.quantity
        thermal += float(a.get("thermal_w", 0.0)) * inst.quantity
        row = db.one(
            """SELECT COALESCE(s.volume_exact, s.volume) AS volume, s.com_x, s.com_y, s.com_z, r.density
               FROM shape s JOIN revision r ON r.fingerprint = s.fingerprint
               WHERE r.revision_id = ?""",
            (inst.revision_id,),
        )
        if row is None or row["density"] is None:
            massless.append(inst.part_number)
            continue
        mass = float(row["volume"]) * float(row["density"]) * inst.quantity
        com = inst.world @ np.array([row["com_x"], row["com_y"], row["com_z"], 1.0])
        total_m += mass
        weighted += mass * com[:3]
    cg = tuple(float(x) for x in weighted / total_m) if total_m > 0 else None
    return Rollup(mass_g=total_m, power_w=power, thermal_w=thermal, cg=cg,
                  massless_parts=sorted(set(massless)))


# -------------------------------------------------------------------- impact


@dataclass
class ImpactedTeam:
    team_id: str
    reasons: list[str] = field(default_factory=list)
    parts: set[str] = field(default_factory=set)


def impact(
    db: Database, part: str, interface_name: str | None = None, ref: str = "main"
) -> dict[str, ImpactedTeam]:
    """Who is affected if this part's contract changes.

    Four independent routes, unioned: teams that subscribed to the part (or the
    interface), teams owning assemblies that contain it, teams owning the parent
    of any socket the part fills, and teams whose parts share a tolerance chain
    with it. The design document describes only the first two; the others are
    where the surprises come from.
    """
    p = resolve_part(db, part)
    out: dict[str, ImpactedTeam] = {}

    def add(team, reason, part_number=None):
        if team is None or team == p["team_id"]:
            return
        entry = out.setdefault(team, ImpactedTeam(team_id=team))
        if reason not in entry.reasons:
            entry.reasons.append(reason)
        if part_number:
            entry.parts.add(part_number)

    for s in db.query(
        """SELECT team_id, interface_name FROM subscription
           WHERE part_id = ? AND (interface_name IS NULL OR ? IS NULL OR interface_name = ?)""",
        (p["part_id"], interface_name, interface_name),
    ):
        what = s["interface_name"] or "whole contract"
        add(s["team_id"], f"subscribes to {p['part_number']} ({what})")

    for w in where_used(db, p["part_id"], current_only=True, ref=ref):
        add(w.team_id, f"owns {w.part_number}, which contains {p['part_number']}", w.part_number)

    for chain in db.query(
        """SELECT DISTINCT c.name, p2.team_id, p2.part_number
           FROM dimension d
           JOIN revision r ON r.revision_id = d.revision_id AND r.part_id = ?
           JOIN chain_member cm ON cm.dimension_id = d.dimension_id
           JOIN chain c ON c.chain_id = cm.chain_id
           JOIN chain_member cm2 ON cm2.chain_id = c.chain_id
           JOIN dimension d2 ON d2.dimension_id = cm2.dimension_id
           JOIN revision r2 ON r2.revision_id = d2.revision_id
           JOIN part p2 ON p2.part_id = r2.part_id""",
        (p["part_id"],),
    ):
        add(chain["team_id"], f"shares tolerance chain {chain['name']!r} via {chain['part_number']}",
            chain["part_number"])

    for s in db.query(
        """SELECT s.name, pp.team_id, pp.part_number
           FROM socket s
           JOIN revision r ON r.revision_id = s.revision_id
           JOIN part pp ON pp.part_id = r.part_id
           WHERE s.fills IS NOT NULL AND (s.fills = ? OR s.fills LIKE ?)""",
        (p["part_number"], f"%/{p['part_number']}"),
    ):
        add(s["team_id"], f"owns socket {s['name']!r} on {s['part_number']}", s["part_number"])

    return out


# ------------------------------------------------------------- substitution

FASTENER_DIAMETER = {"M2": 2.0, "M2.5": 2.5, "M3": 3.0, "M4": 4.0, "M5": 5.0, "M6": 6.0,
                     "M8": 8.0, "M10": 10.0, "M12": 12.0, "M16": 16.0, "M20": 20.0}


def fastener_diameter(name: str | None) -> float | None:
    if not name:
        return None
    return FASTENER_DIAMETER.get(name.strip().upper())


def find_compatible(
    db: Database,
    kind: str = "bolt_pattern",
    hole_count: int | None = None,
    hole_radius: float | None = None,
    radius_tol: float = 0.2,
    spacings: list[float] | None = None,
    spacing_tol: float = 0.2,
    envelope: tuple[float, float, float] | None = None,
    like_fingerprint: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """Every part whose published interface satisfies a socket description.

    A range join over stored interface attributes; extended with fingerprint
    proximity it becomes "something shaped roughly like this that also fits".
    The spacing comparison here is the cheap filter; a real placement is only
    confirmed when the part is actually committed against a socket.
    """
    sql = """
        SELECT i.interface_id, i.revision_id, i.name AS interface_name, i.hole_count, i.hole_radius,
               i.spacings, i.fastener, i.fit_class, i.source,
               p.part_number, p.team_id, r.fingerprint, s.bbox_dx, s.bbox_dy, s.bbox_dz,
               c.envelope_dx, c.envelope_dy, c.envelope_dz
        FROM interface i
        JOIN revision r ON r.revision_id = i.revision_id
        JOIN part p ON p.part_id = r.part_id
        JOIN (SELECT part_id, MAX(revision_index) AS top FROM revision GROUP BY part_id) latest
             ON latest.part_id = r.part_id AND latest.top = r.revision_index
        LEFT JOIN shape s ON s.fingerprint = r.fingerprint
        LEFT JOIN contract c ON c.revision_id = r.revision_id
        WHERE i.kind = :kind
          AND (:count IS NULL OR i.hole_count = :count)
          AND (:radius IS NULL OR ABS(i.hole_radius - :radius) <= :rtol)
    """
    rows = db.query(sql, {"kind": kind, "count": hole_count, "radius": hole_radius, "rtol": radius_tol})

    ref_inv = None
    if like_fingerprint:
        from ..geometry import fingerprint as fp
        from ..model.ingest import row_to_invariants

        ref_row = db.one("SELECT * FROM shape WHERE fingerprint = ?", (like_fingerprint,))
        ref_inv = row_to_invariants(ref_row) if ref_row else None

    out = []
    for r in rows:
        their = json_loads(r["spacings"], [])
        if spacings is not None:
            if len(their) != len(spacings):
                continue
            if any(abs(a - b) > spacing_tol for a, b in zip(sorted(their), sorted(spacings))):
                continue
        dims = None
        if r["envelope_dx"]:
            dims = sorted((r["envelope_dx"], r["envelope_dy"], r["envelope_dz"]))
        elif r["bbox_dx"]:
            dims = sorted((r["bbox_dx"], r["bbox_dy"], r["bbox_dz"]))
        if envelope is not None and dims is not None:
            if any(d > a + 1e-9 for d, a in zip(dims, sorted(envelope))):
                continue
        score = 0.0
        if ref_inv is not None and r["fingerprint"]:
            from ..geometry import fingerprint as fp
            from ..model.ingest import row_to_invariants

            other = db.one("SELECT * FROM shape WHERE fingerprint = ?", (r["fingerprint"],))
            score = fp.similarity(ref_inv, row_to_invariants(other))
        out.append({
            "part_number": r["part_number"], "team_id": r["team_id"],
            "interface": r["interface_name"], "hole_count": r["hole_count"],
            "hole_radius": r["hole_radius"], "fastener": r["fastener"],
            "fit_class": r["fit_class"], "source": r["source"],
            "revision_id": r["revision_id"], "shape_distance": score,
        })
    out.sort(key=lambda x: (x["shape_distance"], x["part_number"]))
    return out[:limit]
