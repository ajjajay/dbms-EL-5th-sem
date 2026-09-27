"""The typed tool surface (design doc section 13).

The tempting implementation is to let a language model write the query. That is
the wrong choice here for a specific reason: recursive traversal with quantity
rollup is exactly the shape of query that generated SQL gets subtly wrong, and a
subtly wrong bill of materials is worse than no bill of materials.

So the queries are written here, as ordinary parameterised functions, and each is
exposed with a typed signature. The model chooses which tool to call and with
what arguments, and never emits query text at all. Two consequences the document
draws out and one it does not:

  * the layer is optional, so the demonstration works with no network;
  * every answer is auditable, because the call and its arguments are logged
    beside the rows returned;
  * and -- not in the document -- **every tool here is read-only.** A language
    model has no role in deciding whether a commit is valid. The one tool that
    touches the commit pipeline is `would_my_commit_conflict`, which runs the
    real pipeline and rolls it back, so it can answer the question without ever
    being able to land anything.

The public/private contract boundary is respected here too: `contract_of` returns
a part's published face, and there is deliberately no tool that returns another
team's internal features.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from ..db.database import Database, json_loads
from ..model import concurrency, interference, tolerance
from ..query import traversal as q


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    run: Callable[..., Any]

    def schema(self) -> dict:
        """Anthropic tool-use schema."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": {
                "type": "object",
                "properties": self.parameters,
                "required": [
                    k for k, v in self.parameters.items() if v.pop("_required", False)
                ],
            },
        }


def _req(schema: dict) -> dict:
    schema["_required"] = True
    return schema


STR = {"type": "string"}
INT = {"type": "integer"}
NUM = {"type": "number"}
BOOL = {"type": "boolean"}


def build_tools(db: Database, pipeline=None) -> dict[str, Tool]:
    """Every tool the model may call. Read-only by construction."""

    def explode(revision: str, depth: int = 64, aggregate: str = "rows") -> dict:
        rows = q.explode(db, revision, max_depth=depth)
        if aggregate == "bom":
            return {"bom": q.bill_of_materials(db, revision, max_depth=depth)}
        if aggregate == "mass":
            r = q.rollup(db, q.resolve_revision(db, revision), max_depth=depth)
            return {"mass_g": r.mass_g, "power_w": r.power_w, "thermal_w": r.thermal_w,
                    "cg_mm": r.cg, "parts_without_density": r.massless_parts}
        return {"instances": [
            {"depth": r.depth, "path": r.path, "part": r.part_number, "team": r.team_id,
             "quantity": r.quantity, "assembly": r.is_assembly, "mass_g": r.mass_g}
            for r in rows
        ]}

    def where_used(part: str, depth: int = 64, current_only: bool = True) -> dict:
        rows = q.where_used(db, part, current_only=current_only, max_depth=depth)
        return {"part": part, "used_in": [
            {"part": r.part_number, "team": r.team_id, "depth": r.depth,
             "revision": r.revision_id} for r in rows
        ]}

    def impact_of(part: str, interface: str | None = None) -> dict:
        teams = q.impact(db, part, interface_name=interface)
        return {"part": part, "interface": interface, "impacted_teams": [
            {"team": t.team_id, "reasons": t.reasons, "parts": sorted(t.parts)}
            for t in teams.values()
        ]}

    def find_compatible(
        hole_count: int | None = None, hole_radius: float | None = None,
        fastener: str | None = None, envelope: list | None = None,
        like_part: str | None = None, limit: int = 20,
    ) -> dict:
        like_fp = None
        if like_part:
            like_fp = db.scalar(
                """SELECT r.fingerprint FROM revision r JOIN part p ON p.part_id = r.part_id
                   WHERE p.part_number = ? ORDER BY r.revision_index DESC LIMIT 1""",
                (like_part,),
            )
        rows = q.find_compatible(
            db, hole_count=hole_count, hole_radius=hole_radius,
            envelope=tuple(envelope) if envelope else None,
            like_fingerprint=like_fp, limit=limit,
        )
        if fastener:
            rows = [r for r in rows if (r.get("fastener") or "").upper() == fastener.upper()]
        return {"candidates": rows}

    def evaluate_chain(chain: str | None = None, method: str | None = None) -> dict:
        if chain:
            r = tolerance.evaluate(db, chain, method=method)
            results = [r]
        else:
            results = tolerance.evaluate_all(db, method=method)
        return {"chains": [
            {
                "name": r.name, "method": r.method, "nominal": r.nominal,
                "worst_case": [r.worst_low, r.worst_high],
                "statistical": [r.rss_low, r.rss_high],
                "monte_carlo": [r.mc_low, r.mc_high],
                "failure_rate": r.mc_failure_rate,
                "sigma_conventions": list(r.sigma_conventions),
                "target": [r.target_low, r.target_high],
                "passes": r.passes(), "teams": list(r.teams),
                "cross_team": r.cross_team,
                "explanation": r.explain(),
                "warnings": r.warnings,
            }
            for r in results
        ]}

    def diff_configurations(root_a: str, root_b: str) -> dict:
        diffs = concurrency.diff_configurations(db, root_a, root_b)
        return {"differences": [
            {"path": d.path, "kind": d.kind, "part": d.part_number, "detail": d.detail}
            for d in diffs
        ]}

    def contract_of(part: str) -> dict:
        """A part's published face. Internal features are deliberately not here."""
        rev = q.resolve_revision(db, part)
        c = db.one("SELECT * FROM contract WHERE revision_id = ?", (rev,))
        if c is None:
            return {"part": part, "contract": None,
                    "note": "this revision publishes no contract"}
        ifaces = db.query(
            "SELECT name, kind, hole_count, hole_radius, fastener, fit_class, source "
            "FROM interface WHERE revision_id = ?", (rev,))
        sockets = db.query(
            "SELECT name, kind, hole_count, fastener, mass_budget, power_budget, "
            "thermal_budget, fills FROM socket WHERE revision_id = ?", (rev,))
        return {
            "part": part, "revision": rev,
            "envelope": [c["envelope_dx"], c["envelope_dy"], c["envelope_dz"]],
            "mass_max": c["mass_max"],
            "cg_window": json_loads(c["cg_window"]),
            "attributes": json_loads(c["attributes"], {}),
            "provenance": c["provenance"], "reviewed": bool(c["reviewed"]),
            "interfaces": [dict(r) for r in ifaces],
            "sockets": [dict(r) for r in sockets],
        }

    def part_summary(part: str) -> dict:
        rev = q.resolve_revision(db, part)
        row = db.one(
            """SELECT r.*, p.part_number, p.team_id, p.description,
                      s.volume, s.area, s.char_length, s.chirality, s.n_faces,
                      s.bbox_dx, s.bbox_dy, s.bbox_dz
               FROM revision r JOIN part p ON p.part_id = r.part_id
               LEFT JOIN shape s ON s.fingerprint = r.fingerprint
               WHERE r.revision_id = ?""", (rev,))
        if row is None:
            return {"part": part, "found": False}
        d = dict(row)
        d["mass_g"] = (d["volume"] or 0) * (d["density"] or 0) or None
        return d

    def history(part: str | None = None, limit: int = 20) -> dict:
        if part:
            rows = db.query(
                """SELECT cl.*, p.part_number FROM commit_log cl
                   JOIN revision r ON r.commit_id = cl.commit_id
                   JOIN part p ON p.part_id = r.part_id
                   WHERE p.part_number = ? ORDER BY cl.created_at DESC LIMIT ?""",
                (part, limit))
        else:
            rows = db.query(
                "SELECT * FROM commit_log ORDER BY created_at DESC LIMIT ?", (limit,))
        return {"commits": [
            {"commit_id": r["commit_id"], "author": r["author"], "team": r["team_id"],
             "message": r["message"], "verdict": r["verdict"], "reason": r["reason"],
             "parent_root": r["parent_root"], "new_root": r["new_root"],
             "at": r["created_at"]}
            for r in rows
        ]}

    def why_was_it_rejected(commit_id: str) -> dict:
        rows = db.query(
            "SELECT * FROM validation WHERE commit_id = ? ORDER BY validation_id", (commit_id,))
        return {"commit_id": commit_id, "steps": [
            {"stage": r["stage"], "passed": bool(r["passed"]),
             "constraint": r["constraint_name"], "detail": r["detail"],
             "other_team": r["other_team"], "other_part": r["other_part"]}
            for r in rows
        ]}

    def check_interference(revision: str = "main", exact: bool = False) -> dict:
        rep = interference.check(db, revision, exact=exact, store=False)
        return {
            "summary": rep.summary(), "advisory": True,
            "clashes": [
                {"a": c.path_a, "b": c.path_b, "kind": c.kind, "volume_mm3": c.volume,
                 "teams": [c.team_a, c.team_b], "description": c.describe()}
                for c in rep.clashes
            ],
        }

    def would_my_commit_conflict(
        part: str, step_file: str | None = None, base_root: str | None = None,
        author: str = "dry-run", team: str | None = None,
    ) -> dict:
        """Run the real pipeline and roll it back.

        Not in the design document. It is the question people actually want
        answered before they commit, and answering it by running the genuine
        validator rather than a summary of it is the only way the answer can be
        trusted.
        """
        if pipeline is None:
            return {"available": False,
                    "note": "no commit pipeline was supplied to this tool set"}
        from ..model.commit import Change, CommitRequest

        # The commit log references a real team, so default to whoever owns the
        # part rather than inventing one and tripping the foreign key.
        if not team:
            team = db.scalar(
                "SELECT team_id FROM part WHERE part_number = ?", (part,)
            ) or db.scalar("SELECT team_id FROM team LIMIT 1")
        head = db.get_ref("main")
        result = pipeline.dry_run(CommitRequest(
            author=author, team_id=team, message="dry run",
            base_root=base_root or (head["root_hash"] if head else None),
            changes=[Change(part_number=part, step_path=step_file)],
        ))
        return {
            "verdict": result.verdict,
            "would_land": result.verdict == "would_land",
            "classifications": result.classifications,
            "failures": [s.summary() for s in result.steps if not s.passed],
            "notifications": [
                {"team": t, "part": p, "reason": w} for t, p, w in result.notified
            ],
            "note": "nothing was written; this ran the real validator and rolled it back",
        }

    tools = [
        Tool("explode",
             "Walk downward from a revision or part number, multiplying quantities. "
             "aggregate='rows' lists every instance, 'bom' aggregates leaves into a bill "
             "of materials, 'mass' returns the mass, power, thermal and centre-of-gravity "
             "rollup.",
             {"revision": _req(dict(STR, description="revision id, part number, or 'main'")),
              "depth": dict(INT, description="maximum depth to walk"),
              "aggregate": dict(STR, enum=["rows", "bom", "mass"])},
             explode),
        Tool("where_used",
             "Walk upward from a part to every assembly that contains it, at any depth. "
             "This answers 'who am I about to break'.",
             {"part": _req(dict(STR, description="part number")),
              "depth": INT,
              "current_only": dict(BOOL, description="restrict to the live configuration")},
             where_used),
        Tool("impact_of",
             "Given a contract change on a part, list every team affected and why: "
             "subscribers, containing assemblies, shared tolerance chains and sockets.",
             {"part": _req(STR), "interface": dict(STR, description="optional interface name")},
             impact_of),
        Tool("find_compatible",
             "Find parts whose published interface satisfies a socket description. "
             "A range join over stored attributes; with like_part it is ranked by shape "
             "similarity, which answers 'find me something shaped roughly like this that "
             "also fits'.",
             {"hole_count": INT, "hole_radius": NUM, "fastener": STR,
              "envelope": {"type": "array", "items": NUM},
              "like_part": STR, "limit": INT},
             find_compatible),
        Tool("evaluate_chain",
             "Total a tolerance chain by worst case, statistical (RSS) and Monte Carlo. "
             "Omit the chain name to evaluate all chains, cross-team ones first.",
             {"chain": STR, "method": dict(STR, enum=["worst_case", "rss", "monte_carlo"])},
             evaluate_chain),
        Tool("diff_configurations",
             "Compare two configurations by their root hashes and report what changed, "
             "found by Merkle descent rather than by comparing every part.",
             {"root_a": _req(STR), "root_b": _req(STR)},
             diff_configurations),
        Tool("contract_of",
             "The published contract of a part: envelope, mass limit, interfaces, sockets "
             "and declared attributes. This is the public face only.",
             {"part": _req(STR)}, contract_of),
        Tool("part_summary",
             "Identity and mass properties of a part's current revision.",
             {"part": _req(STR)}, part_summary),
        Tool("history",
             "Recent commits, optionally only those touching one part.",
             {"part": STR, "limit": INT}, history),
        Tool("why_was_it_rejected",
             "The full validation trace for a commit, stage by stage.",
             {"commit_id": _req(STR)}, why_was_it_rejected),
        Tool("check_interference",
             "Bounding-box prefilter (and optionally exact solid intersection) over a "
             "configuration. Advisory only, never a veto.",
             {"revision": STR, "exact": BOOL}, check_interference),
        Tool("would_my_commit_conflict",
             "Run the real commit pipeline against a proposed change and roll it back, "
             "reporting what would have failed and who would have been notified. "
             "Writes nothing.",
             {"part": _req(STR), "step_file": STR, "base_root": STR,
              "author": STR, "team": STR},
             would_my_commit_conflict),
    ]
    return {t.name: t for t in tools}


def schemas(tools: dict[str, Tool]) -> list[dict]:
    return [t.schema() for t in tools.values()]


def call(tools: dict[str, Tool], name: str, arguments: dict) -> dict:
    tool = tools.get(name)
    if tool is None:
        return {"error": f"no tool named {name!r}"}
    try:
        return tool.run(**arguments)
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
