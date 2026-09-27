"""Ingestion: from a source file to rows.

Section 4. The kernel runs here and nowhere else. Each source file yields a tree
of occurrences, a set of distinct shapes, and the geometric features extracted
from them, and all of it is reduced to ordinary columns before anything is
stored.

The shape lookup is two-stage, which is the practical consequence of splitting
identity into a hash and a bounded match:

  1. Strict fingerprint lookup. One index probe. Catches the resave and the
     re-import of the same export, which is the overwhelmingly common case.
  2. Bounded match. A range query on characteristic length narrows the field to
     a handful of candidates, which are then compared component by component.
     Catches the same part arriving from a different tool, where the numbers
     have moved slightly and the topology counts may not agree at all.

A hit on stage two records an alias, so the next arrival of that same export is
a stage-one hit and costs one probe.

Landing the tree is memoised on the *definition*, not the appearance. A STEP
assembly that places one bolt forty times contains one bolt definition and forty
placements; it must become one part, one revision and forty occurrence rows
(section 7), not forty revisions. A re-import likewise reuses a revision when the
part's latest revision already has the same geometry (or, for an assembly, the
same children in the same places), so importing an assembly twice creates nothing
new -- the demonstration section 16 asks for.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable

import numpy as np

from ..db.database import Database, json_dumps, new_id
from ..geometry import fingerprint as fp
from ..geometry.quantize import transform_key
from ..kernel import get_backend


@dataclass
class PartAttrs:
    """What ingest needs to know about a part that a STEP file does not say."""

    team_id: str
    material: str | None = None
    density: float | None = None      # g/mm^3
    description: str = ""


AttrResolver = Callable[[str], PartAttrs]


@dataclass
class ShapeOutcome:
    source_name: str
    fingerprint: str
    status: str            # new, exact_hit, bounded_hit
    detail: str = ""
    char_length: float = 0.0
    chirality: int = 0
    warnings: list[str] = field(default_factory=list)


@dataclass
class Analysis:
    """The kernel's whole contribution to one source file.

    Produced without touching the database, which is what lets the commit
    pipeline run the (slow) kernel step outside its (fast) write transaction, and
    lets a dry run analyse a file and write nothing at all.
    """

    result: object                                   # kernel IngestResult
    identities: dict[int, object]                    # solid index -> ShapeIdentity
    warnings: list[str] = field(default_factory=list)


@dataclass
class IngestReport:
    source_path: str
    root_revision: str | None
    root_hash: str | None
    solids_read: int = 0
    shapes_new: int = 0
    shapes_exact_hit: int = 0
    shapes_bounded_hit: int = 0
    occurrences: int = 0
    parts_created: int = 0
    parts_known: int = 0
    revisions_created: int = 0
    revisions_reused: int = 0
    outcomes: list[ShapeOutcome] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def deduplicated(self) -> int:
        return self.shapes_exact_hit + self.shapes_bounded_hit

    def summary(self) -> str:
        return (
            f"{os.path.basename(self.source_path)}: {self.solids_read} solids read "
            f"({self.shapes_new} new shapes, {self.shapes_exact_hit} exact hits, "
            f"{self.shapes_bounded_hit} bounded hits); {self.parts_created} parts created, "
            f"{self.parts_known} already known; {self.revisions_created} revisions created, "
            f"{self.revisions_reused} reused; {self.occurrences} occurrences"
        )


class Ingestor:
    def __init__(
        self,
        db: Database,
        backend=None,
        deflection: float = 0.2,
        cross_tool: bool = False,
        char_tol: float | None = None,
        shape_tol: float | None = None,
    ):
        self.db = db
        self.backend = backend or get_backend("auto")
        self.deflection = deflection
        # Cross-tool tolerances are looser because surface reconstruction moves
        # the numbers further than a resave does. Made explicit rather than
        # hidden in a constant, because it is a genuine engineering decision.
        self.char_tol = char_tol if char_tol is not None else (
            fp.CROSS_TOOL_CHAR_LENGTH_TOLERANCE if cross_tool
            else fp.CHAR_LENGTH_RELATIVE_TOLERANCE
        )
        self.shape_tol = shape_tol if shape_tol is not None else (
            fp.CROSS_TOOL_DIMENSIONLESS_TOLERANCE if cross_tool
            else fp.DIMENSIONLESS_TOLERANCE
        )

    # --------------------------------------------------------------- analysis

    def analyse(self, path: str) -> Analysis:
        """Run the kernel over a file and identify every distinct solid in it."""
        result = self.backend.read(path)
        identities: dict[int, object] = {}
        warnings = list(result.warnings)
        for index, solid in enumerate(result.solids):
            if not solid.valid:
                warnings.append(
                    f"skipped {solid.source_name!r}: " + "; ".join(solid.validity_notes)
                )
                continue
            try:
                identities[index] = fp.identify(solid, deflection=self.deflection)
            except ValueError as exc:
                warnings.append(str(exc))

        # Real STEP files carry things that are not parts: PMI annotation
        # wireframes, construction curves, sheet bodies. The validity gate has
        # refused them; drop their nodes so they neither become empty parts nor
        # force a synthetic wrapper around what is really a single part.
        def usable(node) -> bool:
            if node.children:
                node.children = [c for c in node.children if usable(c)]
                return bool(node.children)
            return node.solid_index is not None and node.solid_index in identities

        if not usable(result.root):
            raise ValueError(f"{os.path.basename(path)}: no valid solid bodies to ingest")
        if not result.root.definition_key and len(result.root.children) == 1:
            result.root = result.root.children[0]      # unwrap the synthetic container
        return Analysis(result=result, identities=identities, warnings=warnings)

    # ----------------------------------------------------------- shape entry

    def resolve_shape(self, identity, solid, write: bool = True) -> ShapeOutcome:
        """Find or create the shape row for one identified solid.

        With ``write=False`` nothing is stored, and a shape that would have been
        new is reported under its strict fingerprint. That is the dry-run path.
        """
        strict = identity.strict
        inv = identity.invariants

        def outcome(fingerprint, status, detail):
            return ShapeOutcome(
                source_name=solid.source_name,
                fingerprint=fingerprint,
                status=status,
                detail=detail,
                char_length=inv.char_length,
                chirality=inv.chirality,
                warnings=identity.warnings,
            )

        if self.db.one("SELECT 1 FROM shape WHERE fingerprint = ?", (strict,)):
            return outcome(strict, "exact_hit", "strict fingerprint already present")

        alias = self.db.one(
            "SELECT fingerprint FROM shape_alias WHERE alias_fingerprint = ?", (strict,)
        )
        if alias:
            return outcome(alias["fingerprint"], "exact_hit", "known alias of an existing shape")

        match = self._bounded_match(inv)
        if match is not None:
            target, result = match
            if write:
                self.db.execute(
                    """INSERT OR IGNORE INTO shape_alias
                       (alias_fingerprint, fingerprint, char_length_error, shape_error,
                        corroborated, source_name)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        strict,
                        target,
                        result.char_length_error,
                        result.dimensionless_error,
                        int(result.corroborated),
                        solid.source_name,
                    ),
                )
            corroboration = (
                "counts agree" if result.corroborated
                else "counts differ, which is normal across exporters"
            )
            return outcome(
                target,
                "bounded_hit",
                f"matched {target} within tolerance "
                f"(size {result.char_length_error:.2e}, "
                f"shape {result.dimensionless_error:.2e}; {corroboration})",
            )

        if write:
            self._insert_shape(identity, solid)
        return outcome(strict, "new", "first time this geometry has been seen")

    def _bounded_match(self, inv):
        """Range query on characteristic length, then a full comparison.

        This is the cross-tool path. The index turns what would be a scan of
        every shape into a handful of candidates, and the comparison after it is
        the thing that actually decides -- not a hash collision.
        """
        window = inv.char_length * self.char_tol * 1.5
        candidates = self.db.query(
            """SELECT * FROM shape
               WHERE char_length BETWEEN ? AND ?
                 AND (chirality = 0 OR ? = 0 OR chirality = ?)
               ORDER BY ABS(char_length - ?)
               LIMIT 32""",
            (
                inv.char_length - window,
                inv.char_length + window,
                inv.chirality,
                inv.chirality,
                inv.char_length,
            ),
        )

        best = None
        for row in candidates:
            other = row_to_invariants(row)
            result = fp.compare(inv, other, self.char_tol, self.shape_tol)
            if not result.matched:
                continue
            score = (not result.corroborated, result.dimensionless_error)
            if best is None or score < best[0]:
                best = (score, row["fingerprint"], result)
        if best is None:
            return None
        return best[1], best[2]

    def _insert_shape(self, identity, solid) -> None:
        inv = identity.invariants
        blob = None
        if solid.handle is not None:
            blob = self.db.write_blob(identity.strict, solid.handle)
        if identity.mesh is not None:
            self.db.write_mesh(identity.strict, identity.mesh.vertices, identity.mesh.triangles)

        xmin, ymin, zmin, xmax, ymax, zmax = solid.bbox_axis_aligned
        self.db.execute(
            """INSERT INTO shape (
                   fingerprint, volume, volume_exact, area, char_length, sphericity, j1, j2, j3,
                   chirality, frame_stable, com_x, com_y, com_z,
                   bbox_dx, bbox_dy, bbox_dz,
                   bbox_xmin, bbox_ymin, bbox_zmin, bbox_xmax, bbox_ymax, bbox_zmax,
                   n_faces, n_edges, n_vertices, n_shells, n_solids,
                   face_histogram, blob_path, source_name, source_backend,
                   mesh_triangles, notes)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                identity.strict,
                inv.volume(),
                float(solid.mass.volume),
                solid.mass.area,
                inv.char_length,
                inv.sphericity,
                inv.j1,
                inv.j2,
                inv.j3,
                inv.chirality,
                int(inv.frame_stable),
                *solid.mass.centre_of_mass,
                xmax - xmin,
                ymax - ymin,
                zmax - zmin,
                xmin, ymin, zmin, xmax, ymax, zmax,
                solid.topology.faces,
                solid.topology.edges,
                solid.topology.vertices,
                solid.topology.shells,
                solid.topology.solids,
                json_dumps(inv.face_histogram),
                blob,
                solid.source_name,
                self.backend.name,
                identity.mesh_triangles,
                "; ".join(identity.frame_notes + identity.warnings) or None,
            ),
        )
        self._insert_features(identity)

    def _insert_features(self, identity) -> None:
        rows = []
        for bore in identity.bores:
            rows.append(
                (
                    identity.strict,
                    "bore",
                    *[float(x) for x in bore.axis],
                    *[float(x) for x in bore.axis_point],
                    bore.radius,
                    bore.depth,
                    None,
                    bore.sweep,
                    int(bore.through),
                    int(bore.complete),
                    int(bore.chamfered),
                    json_dumps(list(bore.counterbore_radii)),
                )
            )
        for plane in identity.planes[:64]:
            rows.append(
                (
                    identity.strict,
                    "plane",
                    *[float(x) for x in plane.normal],
                    *[float(x) for x in plane.origin],
                    None,
                    None,
                    plane.area,
                    None,
                    0,
                    1,
                    0,
                    "[]",
                )
            )
        if rows:
            self.db.executemany(
                """INSERT INTO feature
                   (fingerprint, kind, axis_x, axis_y, axis_z, px, py, pz,
                    radius, depth, area, sweep, through, complete, chamfered, counterbores)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                rows,
            )

    # ---------------------------------------------------------------- files

    def ingest_file(
        self,
        path: str,
        team_id: str,
        commit_id: str,
        part_prefix: str = "",
        status: str = "draft",
        density: float | None = None,
        material: str | None = None,
        resolver: AttrResolver | None = None,
        leaf_hook: Callable[[str, object, object, str], None] | None = None,
        analysis: Analysis | None = None,
    ) -> IngestReport:
        """Read one file and land its tree as parts, revisions and occurrences.

        ``resolver`` maps a part name to its owning team, material and density,
        because a STEP file records none of them. ``leaf_hook`` is called for each
        *new* leaf revision with (revision_id, identity, solid, part_number), which
        is where auto-drafted contracts attach.
        """
        analysis = analysis or self.analyse(path)
        result = analysis.result
        report = IngestReport(
            source_path=path,
            root_revision=None,
            root_hash=None,
            solids_read=len(result.solids),
            warnings=list(analysis.warnings),
        )

        fingerprints: dict[int, str] = {}
        for index, identity in analysis.identities.items():
            solid = result.solids[index]
            outcome = self.resolve_shape(identity, solid)
            fingerprints[index] = outcome.fingerprint
            report.outcomes.append(outcome)
            if outcome.status == "new":
                report.shapes_new += 1
            elif outcome.status == "exact_hit":
                report.shapes_exact_hit += 1
            else:
                report.shapes_bounded_hit += 1

        def attrs_for(name: str) -> PartAttrs:
            if resolver is not None:
                return resolver(name)
            return PartAttrs(team_id=team_id, material=material, density=density, description=name)

        landed: dict[str, str | None] = {}           # definition key -> revision id
        part_of_definition: dict[str, str] = {}      # part number -> definition key
        counters = {"occurrences": 0}

        def part_number_for(node, key: str) -> str:
            base = node.name or "unnamed"
            number = f"{part_prefix}{base}"
            owner = part_of_definition.get(number)
            if owner is not None and owner != key:
                # Two distinct definitions with the same name inside one file:
                # make the second one distinguishable rather than merging them.
                n = 2
                while f"{number}#{n}" in part_of_definition and part_of_definition[f"{number}#{n}"] != key:
                    n += 1
                number = f"{number}#{n}"
            part_of_definition[number] = key
            return number

        def land(node, depth: int = 0) -> str | None:
            key = node.definition_key or f"anon:{id(node)}"
            if key in landed:
                return landed[key]

            part_number = part_number_for(node, key)
            attrs = attrs_for(node.name or part_number)
            is_assembly = bool(node.children)

            fingerprint = None
            if node.solid_index is not None:
                fingerprint = fingerprints.get(node.solid_index)
                if fingerprint is None and not is_assembly:
                    landed[key] = None
                    return None

            part_id, created = self._ensure_part(part_number, attrs.team_id, attrs.description or node.name)
            if created:
                report.parts_created += 1
            else:
                report.parts_known += 1

            children: list[tuple[str, str, str, int, np.ndarray]] = []
            for child in node.children:
                child_rev = land(child, depth + 1)
                if child_rev is None:
                    continue
                children.append(
                    (child_rev, child.instance_name or child.name, transform_key(child.transform),
                     int(child.quantity), child.transform)
                )

            if is_assembly and not children:
                landed[key] = None
                report.warnings.append(f"assembly {part_number!r} has no usable components; skipped")
                return None

            reused = self._find_reusable(
                part_id, fingerprint, children, attrs.material, attrs.density, is_assembly
            )
            if reused is not None:
                report.revisions_reused += 1
                landed[key] = reused
                counters["occurrences"] += len(children)
                return reused

            revision_id = self._new_revision(
                part_id, fingerprint, commit_id, status, is_assembly,
                density=attrs.density, material=attrs.material,
            )
            report.revisions_created += 1
            for child_rev, name, _key, qty, matrix in children:
                self._add_occurrence(revision_id, child_rev, name, qty, matrix)
                counters["occurrences"] += 1

            if leaf_hook is not None and node.solid_index is not None:
                identity = analysis.identities.get(node.solid_index)
                if identity is not None:
                    leaf_hook(revision_id, identity, result.solids[node.solid_index], part_number)

            landed[key] = revision_id
            return revision_id

        root_revision = land(result.root)
        report.root_revision = root_revision
        report.occurrences = counters["occurrences"]

        if root_revision:
            from .merkle import build_from_occurrences

            tree = build_from_occurrences(self.db, root_revision)
            self.store_hashes(tree)
            report.root_hash = tree.node_hash

        return report

    # ------------------------------------------------------------- row help

    def _find_reusable(self, part_id, fingerprint, children, material, density, is_assembly):
        """The part's latest revision, if it is already what this file says."""
        row = self.db.one(
            """SELECT revision_id, fingerprint, material, density
               FROM revision WHERE part_id = ?
               ORDER BY revision_index DESC LIMIT 1""",
            (part_id,),
        )
        if row is None:
            return None
        if (row["fingerprint"] or None) != (fingerprint or None):
            return None
        if (row["material"] or None) != (material or None):
            return None
        if (row["density"] is None) != (density is None):
            return None
        if density is not None and abs(float(row["density"]) - float(density)) > 1e-12:
            return None

        existing = self.db.query(
            "SELECT child_rev, transform_key, quantity FROM occurrence WHERE parent_rev = ?",
            (row["revision_id"],),
        )
        have = sorted((o["child_rev"], o["transform_key"], int(o["quantity"])) for o in existing)
        want = sorted((c[0], c[2], c[3]) for c in children)
        return row["revision_id"] if have == want else None

    def _ensure_part(self, part_number: str, team_id: str, description: str) -> tuple[str, bool]:
        row = self.db.one("SELECT part_id FROM part WHERE part_number = ?", (part_number,))
        if row:
            return row["part_id"], False
        part_id = new_id("part")
        self.db.execute(
            "INSERT INTO part (part_id, part_number, description, team_id) VALUES (?,?,?,?)",
            (part_id, part_number, description, team_id),
        )
        return part_id, True

    def _new_revision(
        self,
        part_id: str,
        fingerprint: str | None,
        commit_id: str,
        status: str,
        is_assembly: bool,
        density: float | None = None,
        material: str | None = None,
    ) -> str:
        top = self.db.scalar(
            "SELECT COALESCE(MAX(revision_index), 0) FROM revision WHERE part_id = ?",
            (part_id,),
        )
        revision_id = new_id("rev")
        self.db.execute(
            """INSERT INTO revision
               (revision_id, part_id, revision_index, fingerprint, commit_id,
                status, material, density, is_assembly)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                revision_id,
                part_id,
                int(top) + 1,
                fingerprint,
                commit_id,
                status,
                material,
                density,
                int(is_assembly),
            ),
        )
        return revision_id

    def _add_occurrence(self, parent_rev, child_rev, name, quantity, transform) -> None:
        add_occurrence(self.db, parent_rev, child_rev, name, quantity, transform)

    def store_hashes(self, tree) -> None:
        store_hashes(self.db, tree)


# ---------------------------------------------------------------- shared rows


def add_occurrence(db: Database, parent_rev, child_rev, name, quantity, transform) -> None:
    m = np.asarray(transform, dtype=float)
    key = transform_key(m)
    db.execute(
        """INSERT INTO occurrence
           (parent_rev, child_rev, instance_name, quantity, transform_key, sort_key,
            m00,m01,m02,m03,m10,m11,m12,m13,m20,m21,m22,m23)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            parent_rev,
            child_rev,
            name or "",
            int(quantity),
            key,
            f"{child_rev}|{key}",
            *[float(v) for v in m[:3, :].reshape(12)],
        ),
    )


def store_hashes(db: Database, tree) -> None:
    seen: set[str] = set()
    for node in tree.walk():
        if node.revision_id and node.revision_id not in seen:
            seen.add(node.revision_id)
            db.execute(
                "UPDATE revision SET merkle_hash = ? WHERE revision_id = ?",
                (node.node_hash, node.revision_id),
            )


def row_to_invariants(row):
    from ..db.database import json_loads
    from ..geometry.invariants import InvariantVector

    return InvariantVector(
        char_length=row["char_length"],
        sphericity=row["sphericity"],
        j1=row["j1"],
        j2=row["j2"],
        j3=row["j3"],
        chirality=int(row["chirality"]),
        face_histogram=json_loads(row["face_histogram"], {}),
        topology=(
            row["n_faces"], row["n_edges"], row["n_vertices"],
            row["n_shells"], row["n_solids"],
        ),
        frame_stable=bool(row["frame_stable"]),
    )


def begin_commit(
    db: Database,
    author: str,
    message: str,
    team_id: str | None = None,
    verdict: str = "landed",
    parent_root: str | None = None,
    new_root: str | None = None,
) -> str:
    """Create a commit_log row. Revisions reference one, so ingest needs it first."""
    commit_id = new_id("c")
    db.execute(
        """INSERT INTO commit_log
           (commit_id, author, team_id, message, parent_root, new_root, verdict)
           VALUES (?,?,?,?,?,?,?)""",
        (commit_id, author, team_id, message, parent_root, new_root, verdict),
    )
    return commit_id


def ensure_team(db: Database, team_id: str, name: str | None = None, contact: str | None = None) -> None:
    db.execute(
        "INSERT OR IGNORE INTO team(team_id, name, contact) VALUES (?,?,?)",
        (team_id, name or team_id, contact),
    )
