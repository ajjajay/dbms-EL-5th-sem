"""The state of a configuration: what is finished, what is outstanding, and
whose turn it is.

Every other page answers a question about one thing -- this part's contract,
this commit's trace, this chain's total. This one puts a whole tree in front of
you at once. It adds no machinery: every input already exists, and the work is
one walk of the configuration plus a handful of lookups.

Two kinds of outstanding item, kept apart because they need different people:

  gaps     the part has not declared enough for anything to be checked -- no
           contract, an auto-drafted contract nobody reviewed, no density so it
           weighs nothing in the rollup, still a draft. Owned by the designer
           who last committed it.
  waiting  something already checked and found wanting, sitting with a team --
           an unacknowledged notification, a failing tolerance chain, a socket
           that is unfilled or unsatisfied, an advisory clash.

This is the metadata tax made visible. The pipeline refuses a change that breaks
a declared contract; what it cannot do on its own is say which parts have not
declared enough to be checked in the first place.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..db.database import Database
from ..model import interference, tolerance
from . import traversal as q

UNASSIGNED = "(unassigned)"


@dataclass
class PartState:
    part_number: str
    team_id: str
    designer: str               # author of the commit that made the revision
    revision_id: str
    status: str
    is_assembly: bool
    gaps: list[str] = field(default_factory=list)

    @property
    def done(self) -> bool:
        return not self.gaps


@dataclass
class Waiting:
    kind: str                   # notification | chain | socket | clash
    team_id: str
    detail: str
    link: str | None = None


@dataclass
class TeamState:
    team_id: str
    parts: list[PartState] = field(default_factory=list)
    waiting: list[Waiting] = field(default_factory=list)

    @property
    def complete(self) -> float:
        """Share of this team's parts with nothing left to declare."""
        return sum(p.done for p in self.parts) / len(self.parts) if self.parts else 1.0

    def designers(self) -> dict[str, list[PartState]]:
        out: dict[str, list[PartState]] = {}
        for p in self.parts:
            if not p.done:
                out.setdefault(p.designer, []).append(p)
        return out


@dataclass
class Readiness:
    root_rev: str
    root_part: str
    teams: dict[str, TeamState] = field(default_factory=dict)

    @property
    def parts(self) -> list[PartState]:
        return [p for t in self.teams.values() for p in t.parts]

    @property
    def waiting(self) -> list[Waiting]:
        return [w for t in self.teams.values() for w in t.waiting]

    @property
    def complete(self) -> float:
        parts = self.parts
        return sum(p.done for p in parts) / len(parts) if parts else 1.0

    @property
    def ready(self) -> bool:
        return self.complete == 1.0 and not self.waiting

    def team(self, team_id: str | None) -> TeamState:
        key = team_id or UNASSIGNED
        if key not in self.teams:
            self.teams[key] = TeamState(key)
        return self.teams[key]


def part_gaps(row) -> list[str]:
    """What a revision has not declared. ``row`` carries the revision, its
    contract (left-joined, so possibly all null) and its shape key."""
    gaps = []
    if row["contract_id"] is None:
        gaps.append("no contract declared")
    elif row["provenance"] == "auto_drafted" and not row["reviewed"]:
        gaps.append("contract auto-drafted and not reviewed")
    # An assembly's mass is its children's; only a leaf with geometry needs one.
    if not row["is_assembly"] and row["fingerprint"] is not None and row["density"] is None:
        gaps.append("no material density, so it weighs nothing in the rollup")
    if row["status"] == "draft":
        gaps.append("still a draft, not released")
    return gaps


def readiness(db: Database, root: str, with_sockets: bool = True) -> Readiness:
    """Gaps and waiting items for every part in the configuration under ``root``.

    ``with_sockets`` re-runs the socket stage of the commit pipeline against the
    current tree. It is the one expensive input, so a caller that only wants the
    declaration gaps can switch it off.
    """
    root_rev = q.resolve_revision(db, root)
    instances = q.configuration(db, root_rev)
    root_part = instances[0].part_number if instances else root
    out = Readiness(root_rev=root_rev, root_part=root_part)

    # One row per distinct revision: forty bolts are one thing to finish.
    seen: set[str] = set()
    for inst in instances:
        if inst.revision_id in seen:
            continue
        seen.add(inst.revision_id)
        row = db.one(
            """SELECT r.revision_id, r.status, r.is_assembly, r.fingerprint, r.density,
                      c.contract_id, c.provenance, c.reviewed, cl.author
               FROM revision r
               JOIN commit_log cl  ON cl.commit_id = r.commit_id
               LEFT JOIN contract c ON c.revision_id = r.revision_id
               WHERE r.revision_id = ?""",
            (inst.revision_id,),
        )
        out.team(inst.team_id).parts.append(PartState(
            part_number=inst.part_number, team_id=inst.team_id, designer=row["author"],
            revision_id=inst.revision_id, status=row["status"],
            is_assembly=bool(row["is_assembly"]), gaps=part_gaps(row),
        ))

    part_ids = {inst.part_id for inst in instances}
    for n in db.query(
        """SELECT n.notification_id, n.team_id, n.commit_id, n.reason, p.part_id, p.part_number
           FROM notification n JOIN part p ON p.part_id = n.part_id
           WHERE n.acknowledged = 0 ORDER BY n.notification_id"""
    ):
        if n["part_id"] in part_ids:
            out.team(n["team_id"]).waiting.append(Waiting(
                "notification", n["team_id"],
                f"{n['part_number']} changed: {n['reason']}", link=f"/commit/{n['commit_id']}",
            ))

    revision_ids = {inst.revision_id for inst in instances}
    touching = set(tolerance.chains_touching(db, revision_ids))
    for r in tolerance.evaluate_all(db):
        if r.chain_id not in touching or r.passes():
            continue
        owner = db.scalar("SELECT owning_team FROM chain WHERE chain_id = ?", (r.chain_id,))
        out.team(owner).waiting.append(Waiting("chain", owner or UNASSIGNED, r.explain(),
                                               link="/chains"))

    if with_sockets:
        from ..model.commit import CommitPipeline

        for step in CommitPipeline(db).check_sockets(root_rev):
            if not step.passed:
                out.team(step.other_team).waiting.append(Waiting(
                    "socket", step.other_team or UNASSIGNED, step.detail))

    by_path = {inst.path: inst.team_id for inst in instances}
    for c in interference.stored(db, root_rev):
        if c["kind"] != "clash":
            continue
        # Both owners have to agree on the fix, so it waits on both.
        for team_id in {by_path.get(c["path_a"]), by_path.get(c["path_b"])}:
            out.team(team_id).waiting.append(Waiting(
                "clash", team_id or UNASSIGNED,
                f"{c['path_a']} and {c['path_b']} overlap by {c['volume'] or 0:.1f} mm^3 "
                f"(advisory)", link="/interference",
            ))

    for t in out.teams.values():
        t.parts.sort(key=lambda p: (p.done, p.part_number))
    out.teams = dict(sorted(out.teams.items(), key=lambda kv: (kv[1].complete, -len(kv[1].waiting), kv[0])))
    return out
