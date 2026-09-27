"""The commit pipeline (design doc section 9).

A commit is a proposed change to one or more revisions. It is validated as a unit
and either lands whole or is rejected whole, because a half-applied change can
leave the assembly graph in a state no physical machine could occupy.

Validation runs cheapest first, so a commit that is going to be rejected for a
missing declaration never pays for a Kabsch fit:

    declaration -> classify -> envelope -> sockets -> budgets -> chains -> swap
    row lookup     hash cmp    one row     joins+fit   traversal  arithmetic  one row

How the proposed state is materialised is worth stating, because it is what makes
"whole or nothing" true rather than aspirational. The new revisions are *written*
inside the transaction and the constraints are then evaluated against the
database in its proposed state. A failure raises, the transaction rolls back, and
nothing survives. This is better than validating a simulated state, because the
thing validated is exactly the thing that would have landed -- there is no second
code path that could disagree with the first.

The geometry kernel runs before the transaction opens. Fingerprinting a solid
costs tens of milliseconds and a write transaction is not the place for it, so
`analyse` happens first and only rows enter the transaction.

Two consequences of revision immutability that the document does not draw out:

  * Changing a leaf forces a new revision of every assembly above it, because an
    occurrence row names a specific child revision and may not be edited. That is
    the Merkle rehash of section 6 expressed in rows, and it is done by
    `rebuild_ancestors`.

  * A chain member names a dimension, which belongs to a revision. Rebuilding an
    ancestor therefore has to rebind chain members onto the new revision, or the
    chain would quietly keep totalling superseded geometry and would never notice
    the change it exists to catch.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from ..db.database import Database, json_loads, new_id
from ..db.documents import TOPIC_NOTIFICATION, TOPIC_VALIDATION, enqueue
from ..query import traversal as q
from . import contracts as ct
from . import merkle, tolerance
from .ingest import Ingestor, add_occurrence, store_hashes

# Classification (section 9's table, with the misaligned row corrected: contract
# unchanged and body unchanged is a NO-OP, not an internal change).
NO_OP = "no_op"
INTERNAL = "internal"
BREAKING = "breaking"


class Rejected(Exception):
    """Raised inside the transaction to roll the whole commit back."""

    def __init__(self, step: "Step"):
        super().__init__(step.summary())
        self.step = step


@dataclass
class Step:
    """One validation verdict, kept so a rejection can be explained afterwards."""

    stage: str
    passed: bool
    detail: str
    constraint_name: str | None = None
    other_team: str | None = None
    other_part: str | None = None
    data: dict = field(default_factory=dict)

    def summary(self) -> str:
        text = f"[{self.stage}] {self.detail}"
        if self.constraint_name:
            text = f"[{self.stage}] {self.constraint_name}: {self.detail}"
        if self.other_part or self.other_team:
            who = " and ".join(x for x in (self.other_part, self.other_team) if x)
            text += f" (other party: {who})"
        return text


@dataclass
class Change:
    """One part's proposed new state."""

    part_number: str
    step_path: str | None = None                 # new geometry, or None to keep it
    declaration: ct.Declaration | None = None    # None carries the previous one forward
    material: str | None = None
    density: float | None = None
    team_id: str | None = None                   # required when the part is new
    description: str = ""
    status: str = "released"
    # Structure, for assemblies authored directly rather than imported.
    children: list[tuple[str, str, np.ndarray, int]] | None = None   # (instance, part_number, M, qty)


@dataclass
class CommitRequest:
    author: str
    team_id: str
    message: str
    changes: list[Change]
    base_root: str | None = None      # the root hash this commit was written against
    ref: str = "main"
    acknowledge: bool = False         # author states they have told the dependents


@dataclass
class CommitResult:
    commit_id: str
    verdict: str                      # landed | rejected
    reason: str | None = None
    new_root: str | None = None
    parent_root: str | None = None
    steps: list[Step] = field(default_factory=list)
    classifications: dict[str, str] = field(default_factory=dict)
    notified: list[tuple[str, str, str]] = field(default_factory=list)   # (team, part, reason)
    rebased: bool = False
    conflicts: list[str] = field(default_factory=list)

    @property
    def landed(self) -> bool:
        return self.verdict == "landed"

    @property
    def failure(self) -> Step | None:
        return next((s for s in self.steps if not s.passed), None)

    def report(self) -> str:
        head = f"commit {self.commit_id} {self.verdict}"
        if self.verdict == "landed":
            head += f"; root {self.parent_root} -> {self.new_root}"
            if self.rebased:
                head += " (rebased onto a moved root)"
        lines = [head]
        if self.reason:
            lines.append(f"  reason: {self.reason}")
        for part, kind in sorted(self.classifications.items()):
            lines.append(f"  {part}: {kind}")
        for team, part, why in self.notified:
            lines.append(f"  notify {team} about {part}: {why}")
        for s in self.steps:
            lines.append(f"  {'ok  ' if s.passed else 'FAIL'} {s.summary()}")
        return "\n".join(lines)


# --------------------------------------------------------------- the pipeline


class CommitPipeline:
    def __init__(self, db: Database, ingestor: Ingestor | None = None, backend=None):
        self.db = db
        self.ingestor = ingestor or Ingestor(db, backend)

    # -- entry ---------------------------------------------------------------

    def submit(self, request: CommitRequest, dry_run: bool = False) -> CommitResult:
        """Validate and (unless dry_run) land a commit."""
        analyses = {}
        for change in request.changes:
            if change.step_path:
                analyses[change.part_number] = self.ingestor.analyse(change.step_path)

        head = self.db.get_ref(request.ref)
        parent_root = head["root_hash"] if head else None
        commit_id = new_id("c")
        result = CommitResult(commit_id=commit_id, verdict="rejected", parent_root=parent_root)

        try:
            with self.db.transaction():
                self.db.execute(
                    """INSERT INTO commit_log
                       (commit_id, author, team_id, message, parent_root, verdict)
                       VALUES (?,?,?,?,?,'pending')""",
                    (commit_id, request.author, request.team_id, request.message, parent_root),
                )
                self._run(request, analyses, commit_id, result, head)
                if dry_run:
                    raise _DryRun()
                result.verdict = "landed"
        except _DryRun:
            result.verdict = "would_land"
            result.reason = "dry run: nothing was written"
            return result
        except Rejected as exc:
            result.verdict = "rejected"
            result.reason = exc.step.summary()
            self._record_rejection(request, commit_id, result)
            return result

        self._record_landed(commit_id, result)
        return result

    def dry_run(self, request: CommitRequest) -> CommitResult:
        """"Would my commit conflict?" -- the read-only question the natural
        language layer is allowed to ask. Runs the entire pipeline, including the
        writes, and then rolls back."""
        return self.submit(request, dry_run=True)

    # -- the stages ----------------------------------------------------------

    def _run(self, request, analyses, commit_id, result, head) -> None:
        db = self.db
        steps = result.steps

        def check(step: Step) -> None:
            steps.append(step)
            if not step.passed:
                raise Rejected(step)

        # Stage 0: concurrency. Cheapest of all -- one row -- and it decides
        # whether the rest is even about the current product.
        from . import concurrency

        base = request.base_root
        if base is not None and head is not None and base != head["root_hash"]:
            verdict = concurrency.analyse(
                db, base_root=base, current_root=head["root_hash"],
                touched_parts={c.part_number for c in request.changes},
                current_rev=head["revision_id"],
            )
            result.conflicts = verdict.conflicts
            result.rebased = verdict.can_rebase
            check(Step(
                "concurrency", verdict.can_rebase, verdict.detail,
                constraint_name="compare-and-swap",
                other_team=verdict.other_team, other_part=verdict.other_part,
            ))
        elif base is not None and head is not None:
            steps.append(Step("concurrency", True, "base root matches the current root"))

        # Stage 1: every change must carry a declaration, or inherit one.
        # A row lookup, and the design principle the whole system rests on:
        # geometry with no declaration is refused, like a merge with no type
        # signature.
        prepared = []
        for change in request.changes:
            part = db.one("SELECT * FROM part WHERE part_number = ?", (change.part_number,))
            previous = None
            if part is not None:
                previous = db.one(
                    """SELECT * FROM revision WHERE part_id = ?
                       ORDER BY revision_index DESC LIMIT 1""",
                    (part["part_id"],),
                )
            decl = change.declaration
            if decl is None and previous is not None:
                decl = ct.load_declaration(db, previous["revision_id"])
            if decl is None:
                check(Step(
                    "declaration", False,
                    f"{change.part_number} arrived as bare geometry with no declared contract",
                    constraint_name="declaration required",
                ))
            if part is None and not change.team_id:
                check(Step(
                    "declaration", False,
                    f"{change.part_number} is a new part and names no owning team",
                    constraint_name="ownership required",
                ))
            prepared.append((change, part, previous, decl))
        steps.append(Step(
            "declaration", True,
            f"{len(prepared)} change(s) carry a contract declaration",
        ))

        # Stage 2: apply. Writes the proposed state so everything after this
        # validates the thing that would actually land.
        replaced: dict[str, str] = {}
        new_revisions: dict[str, str] = {}
        contract_rows: dict[str, ct.DerivedContract] = {}

        for change, part, previous, decl in prepared:
            team = change.team_id or (part["team_id"] if part else request.team_id)
            if part is None:
                part_id = new_id("part")
                db.execute(
                    "INSERT INTO part (part_id, part_number, description, team_id) VALUES (?,?,?,?)",
                    (part_id, change.part_number, change.description or change.part_number, team),
                )
            else:
                part_id = part["part_id"]

            identity = solid = None
            fingerprint = previous["fingerprint"] if previous else None
            analysis = analyses.get(change.part_number)
            if analysis is not None:
                index, identity = next(iter(analysis.identities.items()))
                solid = analysis.result.solids[index]
                outcome = self.ingestor.resolve_shape(identity, solid)
                fingerprint = outcome.fingerprint

            derived = ct.derive_contract(decl, identity, solid)
            if derived.problems:
                check(Step(
                    "contract", False,
                    f"{change.part_number}: " + "; ".join(derived.problems[:3]),
                    constraint_name="declaration does not match the geometry",
                ))
            contract_rows[change.part_number] = derived

            top = db.scalar(
                "SELECT COALESCE(MAX(revision_index), 0) FROM revision WHERE part_id = ?", (part_id,)
            )
            revision_id = new_id("rev")
            is_assembly = bool(change.children) or bool(previous and previous["is_assembly"])
            db.execute(
                """INSERT INTO revision
                   (revision_id, part_id, revision_index, fingerprint, commit_id, status,
                    material, density, is_assembly)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (revision_id, part_id, int(top) + 1, fingerprint, commit_id, change.status,
                 change.material or (previous["material"] if previous else None),
                 change.density if change.density is not None else (previous["density"] if previous else None),
                 int(is_assembly)),
            )
            new_revisions[change.part_number] = revision_id
            if previous is not None:
                replaced[previous["revision_id"]] = revision_id

        # Structure, once every new revision exists, so a child can be named.
        for change, part, previous, decl in prepared:
            revision_id = new_revisions[change.part_number]
            if change.children is not None:
                for instance, child_part, matrix, qty in change.children:
                    child_rev = new_revisions.get(child_part) or q.resolve_revision(db, child_part)
                    add_occurrence(db, revision_id, child_rev, instance, qty, matrix)
            elif previous is not None:
                for o in db.query(
                    "SELECT * FROM occurrence WHERE parent_rev = ? ORDER BY occurrence_id",
                    (previous["revision_id"],),
                ):
                    child = replaced.get(o["child_rev"], o["child_rev"])
                    add_occurrence(db, revision_id, child, o["instance_name"],
                                   int(o["quantity"]), q.matrix_of(o))

        # Sockets that read a sibling's interface need the structure in place.
        for change, part, previous, decl in prepared:
            revision_id = new_revisions[change.part_number]
            derived = contract_rows[change.part_number]
            problems = ct.resolve_derived_sockets(db, revision_id, derived.sockets)
            if problems:
                check(Step(
                    "contract", False, f"{change.part_number}: " + "; ".join(problems),
                    constraint_name="socket refers to something that is not there",
                ))
            ct.store_contract(db, revision_id, derived, declared_by=request.author)
            if previous is not None:
                tolerance.rebind_chain_members(db, previous["revision_id"], revision_id)

        # Stage 3: classify. One hash comparison per part, and it decides how
        # disruptive the commit is before any expensive check runs.
        for change, part, previous, decl in prepared:
            revision_id = new_revisions[change.part_number]
            new_hash = ct.contract_hash(contract_rows[change.part_number])
            old_hash = None
            if previous is not None:
                row = db.one("SELECT contract_hash FROM contract WHERE revision_id = ?",
                             (previous["revision_id"],))
                old_hash = row["contract_hash"] if row else None
            old_fp = previous["fingerprint"] if previous else None
            new_fp = db.scalar("SELECT fingerprint FROM revision WHERE revision_id = ?", (revision_id,))

            contract_changed = old_hash != new_hash
            body_changed = old_fp != new_fp
            if previous is None or contract_changed:
                kind = BREAKING
            elif body_changed:
                kind = INTERNAL
            else:
                kind = NO_OP
            result.classifications[change.part_number] = kind
            db.execute(
                """INSERT INTO change_classification
                   (commit_id, part_id, contract_changed, body_changed, classification)
                   VALUES (?,?,?,?,?)""",
                (commit_id, part["part_id"] if part else
                 db.scalar("SELECT part_id FROM part WHERE part_number = ?", (change.part_number,)),
                 int(contract_changed), int(body_changed), kind),
            )
        steps.append(Step(
            "classification", True,
            "; ".join(f"{p}={k}" for p, k in sorted(result.classifications.items())),
        ))

        # Rebuild every assembly above a changed part, then the new root.
        root_rev = head["revision_id"] if head else None
        if root_rev is not None:
            root_rev = self.rebuild_ancestors(root_rev, replaced, commit_id)
        else:
            root_rev = new_revisions[request.changes[-1].part_number]

        # Stage 4: envelope. One row per part against its own bounds.
        for change, part, previous, decl in prepared:
            derived = contract_rows[change.part_number]
            if decl.envelope is None or derived.bbox is None:
                continue
            ext = [derived.bbox[3] - derived.bbox[0], derived.bbox[4] - derived.bbox[1],
                   derived.bbox[5] - derived.bbox[2]]
            for axis, actual, allowed in zip("xyz", ext, decl.envelope):
                check(Step(
                    "envelope", actual <= allowed + 1e-6,
                    f"{change.part_number} {axis}-extent {actual:.2f} mm against its declared "
                    f"envelope {allowed:.2f} mm",
                    constraint_name=f"envelope.{axis}",
                ))
        steps.append(Step("envelope", True, "declared envelopes hold"))

        # Stage 5: sockets. The joins and the placement fit.
        for step in self.check_sockets(root_rev, touched=set(new_revisions.values())):
            check(step)

        # Stage 6: budgets. A traversal per socket that declares one.
        for step in self.check_budgets(root_rev):
            check(step)

        # Stage 7: tolerance chains. Arithmetic, but it needs every other part's
        # current dimensions, which is why it is last and why it is the check no
        # single team could run alone.
        touched_parts = {
            db.scalar("SELECT part_id FROM revision WHERE revision_id = ?", (r,))
            for r in new_revisions.values()
        }
        for step in self.check_chains(touched_parts):
            check(step)

        # Stage 8: breaking changes must name their dependents.
        for change, part, previous, decl in prepared:
            if result.classifications.get(change.part_number) != BREAKING or previous is None:
                continue
            for team, impacted in q.impact(db, change.part_number, ref=request.ref).items():
                why = impacted.reasons[0]
                result.notified.append((team, change.part_number, why))
                db.execute(
                    """INSERT INTO notification (commit_id, team_id, part_id, reason)
                       VALUES (?,?,?,?)""",
                    (commit_id, team,
                     db.scalar("SELECT part_id FROM part WHERE part_number = ?", (change.part_number,)),
                     why),
                )
                enqueue(db, TOPIC_NOTIFICATION, f"{commit_id}:{team}:{change.part_number}", {
                    "commit_id": commit_id, "team_id": team, "part": change.part_number,
                    "reasons": impacted.reasons, "parts": sorted(impacted.parts),
                })
        if result.notified:
            steps.append(Step(
                "notification", True,
                f"{len(result.notified)} dependent team(s) named: "
                + ", ".join(sorted({t for t, _, _ in result.notified})),
            ))

        # Stage 9: compare-and-swap. Touches one row.
        tree = merkle.build_from_occurrences(db, root_rev)
        store_hashes(db, tree)
        result.new_root = tree.node_hash
        db.set_ref(request.ref, root_rev, tree.node_hash, request.author)
        db.execute(
            "UPDATE commit_log SET new_root = ?, verdict = 'landed' WHERE commit_id = ?",
            (tree.node_hash, commit_id),
        )
        steps.append(Step("swap", True, f"ref {request.ref!r} now at {tree.node_hash}"))

        for s in steps:
            db.execute(
                """INSERT INTO validation
                   (commit_id, stage, passed, constraint_name, detail, other_team, other_part)
                   VALUES (?,?,?,?,?,?,?)""",
                (commit_id, s.stage, int(s.passed), s.constraint_name, s.detail,
                 s.other_team, s.other_part),
            )
        enqueue(db, TOPIC_VALIDATION, commit_id, {
            "commit_id": commit_id, "author": request.author, "team": request.team_id,
            "message": request.message, "verdict": "landed",
            "parent_root": result.parent_root, "new_root": result.new_root,
            "classifications": result.classifications,
            "steps": [
                {"stage": s.stage, "passed": s.passed, "constraint": s.constraint_name,
                 "detail": s.detail, "other_team": s.other_team, "other_part": s.other_part,
                 "data": s.data}
                for s in steps
            ],
        })
        for change, _, _, decl in prepared:
            if decl.metadata:
                enqueue(db, "contract_metadata", f"{change.part_number}:{commit_id}", {
                    "part": change.part_number, "commit_id": commit_id, "metadata": decl.metadata,
                })

    # -- constraint checks ---------------------------------------------------

    def check_sockets(self, root_rev: str, touched: set[str] | None = None) -> list[Step]:
        """Every socket in the configuration, matched against what fills it.

        Steps 1-4 of section 8 are joins over stored attributes. Step 5 is the
        registration and the placement measurement, and it is the one that
        catches a pattern moved rigidly, which no spacing comparison can see.
        """
        db = self.db
        steps: list[Step] = []
        instances = q.configuration(db, root_rev)
        by_rev: dict[str, list[q.Instance]] = {}
        for inst in instances:
            by_rev.setdefault(inst.revision_id, []).append(inst)

        for inst in instances:
            sockets = db.query("SELECT * FROM socket WHERE revision_id = ?", (inst.revision_id,))
            for row in sockets:
                sock = ct.row_to_socket(row)
                target = q.find_path(db, inst.revision_id, sock.get("fills") or sock["name"])
                if target is None:
                    steps.append(Step(
                        "socket", False,
                        f"socket {sock['name']!r} on {inst.part_number} is not filled: "
                        f"nothing at {sock.get('fills')!r}",
                        constraint_name=f"socket.{sock['name']}",
                        other_part=inst.part_number, other_team=inst.team_id,
                    ))
                    continue

                # A socket that allocates mass, power or thermal load but
                # declares no pattern is a budget boundary, not a mount. It is
                # satisfied by something being there; the allocation itself is
                # checked in the budget stage.
                if not sock.get("points") and sock.get("hole_count") is None:
                    steps.append(Step(
                        "socket", True,
                        f"socket {sock['name']!r} on {inst.part_number} is an allocation "
                        f"boundary, filled by {target.part_number}",
                        constraint_name=f"socket.{sock['name']}",
                        other_part=target.part_number, other_team=target.team_id,
                    ))
                    continue

                ifaces = db.query(
                    "SELECT * FROM interface WHERE revision_id = ?" +
                    (" AND name = ?" if sock.get("interface_name") else ""),
                    (target.revision_id,) + ((sock["interface_name"],) if sock.get("interface_name") else ()),
                )
                if not ifaces:
                    steps.append(Step(
                        "socket", False,
                        f"{target.part_number} publishes no interface "
                        f"{sock.get('interface_name') or '(any)'} for socket {sock['name']!r} "
                        f"on {inst.part_number}",
                        constraint_name=f"socket.{sock['name']}",
                        other_part=target.part_number, other_team=target.team_id,
                    ))
                    continue

                shape = db.one(
                    """SELECT bbox_xmin, bbox_ymin, bbox_zmin, bbox_xmax, bbox_ymax, bbox_zmax
                       FROM shape s JOIN revision r ON r.fingerprint = s.fingerprint
                       WHERE r.revision_id = ?""",
                    (target.revision_id,),
                )
                bbox = tuple(shape) if shape else None
                # find_path walks from the socket owner, so target.world is
                # already the filler's placement in the socket's own frame.
                # Composing inst.world again would mix two different origins.
                world = target.world

                best = None
                for irow in ifaces:
                    outcome = ct.match_socket(sock, ct.row_to_interface(irow), bbox, world)
                    if outcome.passed:
                        best = outcome
                        break
                    if best is None or sum(s.passed for s in outcome.steps) > sum(s.passed for s in best.steps):
                        best = outcome

                failure = best.first_failure
                steps.append(Step(
                    "socket", best.passed,
                    (f"socket {sock['name']!r} on {inst.part_number} is satisfied by "
                     f"{target.part_number}.{best.interface}: "
                     + "; ".join(s.detail for s in best.steps))
                    if best.passed else
                    (f"socket {sock['name']!r} on {inst.part_number} is not satisfied by "
                     f"{target.part_number}.{best.interface}: {failure.detail}"),
                    constraint_name=f"socket.{sock['name']}.{failure.name if failure else 'ok'}",
                    other_part=target.part_number, other_team=target.team_id,
                    data={"steps": [(s.name, s.passed, s.detail) for s in best.steps]},
                ))
        return steps

    def check_budgets(self, root_rev: str) -> list[Step]:
        """Mass, power, thermal and centre of gravity, rolled up per socket.

        Step 4 of section 8, but evaluated here rather than in the matcher,
        because it is the one step that needs the whole subtree rather than two
        rows.
        """
        db = self.db
        steps: list[Step] = []
        for inst in q.configuration(db, root_rev):
            contract = db.one("SELECT * FROM contract WHERE revision_id = ?", (inst.revision_id,))
            if contract is not None and contract["mass_max"] is not None:
                roll = q.rollup(db, inst.revision_id)
                ok = roll.mass_g <= float(contract["mass_max"]) + 1e-9
                steps.append(Step(
                    "budget", ok,
                    f"{inst.part_number} rolls up to {roll.mass_g:.1f} g against its declared "
                    f"limit of {float(contract['mass_max']):.1f} g"
                    + (f" (no density on {', '.join(roll.massless_parts[:3])})" if roll.massless_parts else ""),
                    constraint_name="mass_max", other_part=inst.part_number, other_team=inst.team_id,
                ))
            if contract is not None and contract["cg_window"]:
                window = json_loads(contract["cg_window"], {})
                roll = q.rollup(db, inst.revision_id)
                if roll.cg is not None and window.get("min") and window.get("max"):
                    lo, hi = np.asarray(window["min"], float), np.asarray(window["max"], float)
                    cg = np.asarray(roll.cg, float)
                    ok = bool(np.all(cg >= lo - 1e-9) and np.all(cg <= hi + 1e-9))
                    steps.append(Step(
                        "budget", ok,
                        f"{inst.part_number} centre of gravity at "
                        f"({cg[0]:.1f}, {cg[1]:.1f}, {cg[2]:.1f}) mm against its declared window",
                        constraint_name="cg_window", other_part=inst.part_number,
                        other_team=inst.team_id,
                    ))

            for row in db.query("SELECT * FROM socket WHERE revision_id = ?", (inst.revision_id,)):
                sock = ct.row_to_socket(row)
                if not any(sock.get(k) for k in ("mass_budget", "power_budget", "thermal_budget")):
                    continue
                target = q.find_path(db, inst.revision_id, sock.get("fills") or sock["name"])
                if target is None:
                    continue
                roll = q.rollup(db, target.revision_id)
                for key, actual, unit in (
                    ("mass_budget", roll.mass_g, "g"),
                    ("power_budget", roll.power_w, "W"),
                    ("thermal_budget", roll.thermal_w, "W"),
                ):
                    limit = sock.get(key)
                    if limit is None:
                        continue
                    steps.append(Step(
                        "budget", actual <= float(limit) + 1e-9,
                        f"{target.part_number} draws {actual:.1f} {unit} against the "
                        f"{key.split('_')[0]} allocation of {float(limit):.1f} {unit} that "
                        f"{inst.part_number} set for socket {sock['name']!r}",
                        constraint_name=f"socket.{sock['name']}.{key}",
                        other_part=inst.part_number, other_team=inst.team_id,
                    ))
        return steps

    def check_chains(self, part_ids: set[str]) -> list[Step]:
        """Re-total every chain a changed part participates in.

        This is the check that catches two commits that are each individually
        valid and jointly break an assembly, because only a validator holding the
        whole graph can see the path.
        """
        db = self.db
        steps: list[Step] = []
        for chain_id in tolerance.chains_touching_parts(db, {p for p in part_ids if p}):
            result = tolerance.evaluate(db, chain_id)
            worst = result.worst_contributor()
            steps.append(Step(
                "chain", result.passes(), result.explain(),
                constraint_name=f"chain.{result.name}",
                other_part=worst.part_number if worst else None,
                other_team=", ".join(t for t in result.teams) if result.cross_team else
                           (worst.team_id if worst else None),
                data={"teams": list(result.teams), "method": result.method},
            ))
        return steps

    # -- structure -----------------------------------------------------------

    def rebuild_ancestors(self, root_rev: str, replaced: dict[str, str], commit_id: str) -> str:
        """Create a new revision of every assembly above a replaced part.

        Revisions are immutable and an occurrence names a specific child
        revision, so a change to a leaf necessarily produces a new revision of
        each of its ancestors -- and of nothing else. That is section 6's "only
        the path to the root rehashes", expressed in rows rather than hashes.
        """
        db = self.db
        cache: dict[str, str] = {}

        def rebuild(rev: str, depth: int = 0) -> str:
            if depth > q.DEPTH_GUARD:
                raise RecursionError("assembly deeper than the guard while rebuilding")
            if rev in replaced:
                return replaced[rev]
            if rev in cache:
                return cache[rev]
            kids = db.query(
                "SELECT * FROM occurrence WHERE parent_rev = ? ORDER BY occurrence_id", (rev,)
            )
            mapped = [(o, rebuild(o["child_rev"], depth + 1)) for o in kids]
            if not any(new != o["child_rev"] for o, new in mapped):
                cache[rev] = rev
                return rev

            old = db.one("SELECT * FROM revision WHERE revision_id = ?", (rev,))
            top = db.scalar(
                "SELECT COALESCE(MAX(revision_index), 0) FROM revision WHERE part_id = ?",
                (old["part_id"],),
            )
            new_rev = new_id("rev")
            db.execute(
                """INSERT INTO revision
                   (revision_id, part_id, revision_index, fingerprint, commit_id, status,
                    material, density, is_assembly)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (new_rev, old["part_id"], int(top) + 1, old["fingerprint"], commit_id,
                 old["status"], old["material"], old["density"], old["is_assembly"]),
            )
            for o, child in mapped:
                add_occurrence(db, new_rev, child, o["instance_name"], int(o["quantity"]),
                               q.matrix_of(o))
            ct.copy_contract(db, rev, new_rev)
            # A socket that is a view of a child's interface must follow the
            # child. Without this the rebuilt assembly keeps the old pattern and
            # a moved hole passes validation unnoticed.
            ct.rederive_sockets(db, new_rev)
            ct.recompute_contract_hash(db, new_rev)
            tolerance.rebind_chain_members(db, rev, new_rev)
            replaced[rev] = new_rev
            cache[rev] = new_rev
            return new_rev

        return rebuild(root_rev)

    # -- records -------------------------------------------------------------

    def _record_rejection(self, request, commit_id: str, result: CommitResult) -> None:
        """A rejected commit still leaves a record, in its own transaction,
        because the rejection is the interesting event."""
        db = self.db
        with db.transaction():
            db.execute(
                """INSERT INTO commit_log
                   (commit_id, author, team_id, message, parent_root, verdict, reason)
                   VALUES (?,?,?,?,?,'rejected',?)""",
                (commit_id, request.author, request.team_id, request.message,
                 result.parent_root, result.reason),
            )
            for s in result.steps:
                db.execute(
                    """INSERT INTO validation
                       (commit_id, stage, passed, constraint_name, detail, other_team, other_part)
                       VALUES (?,?,?,?,?,?,?)""",
                    (commit_id, s.stage, int(s.passed), s.constraint_name, s.detail,
                     s.other_team, s.other_part),
                )
            enqueue(db, TOPIC_VALIDATION, commit_id, {
                "commit_id": commit_id, "author": request.author, "team": request.team_id,
                "message": request.message, "verdict": "rejected", "reason": result.reason,
                "parent_root": result.parent_root,
                "steps": [
                    {"stage": s.stage, "passed": s.passed, "constraint": s.constraint_name,
                     "detail": s.detail, "other_team": s.other_team, "other_part": s.other_part,
                     "data": s.data}
                    for s in result.steps
                ],
            })

    def _record_landed(self, commit_id: str, result: CommitResult) -> None:
        return None


class _DryRun(Exception):
    """Internal: unwinds the transaction after a successful dry run."""
