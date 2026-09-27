"""Load the demonstration assembly into a database.

Ingest first, contracts second. That ordering is not incidental: a declaration is
checked *against the geometry it describes*, so the geometry has to be there to
check it against, and a declaration naming four holes that the part does not have
is rejected here rather than believed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..db.database import Database
from ..model import contracts as ct
from ..model import merkle, tolerance
from ..model.ingest import Ingestor, PartAttrs, begin_commit, ensure_team, store_hashes
from . import assembly


@dataclass
class BootstrapReport:
    commit_id: str
    root_revision: str
    root_hash: str
    parts: int = 0
    contracts: int = 0
    chains: int = 0
    problems: list[str] = field(default_factory=list)
    ingest_summary: str = ""


def attrs_resolver():
    def resolve(name: str) -> PartAttrs:
        material = assembly.MATERIAL.get(name)
        return PartAttrs(
            team_id=assembly.OWNER.get(name, "chassis"),
            material=material,
            density=assembly.DENSITY.get(material) if material else None,
            description=name,
        )
    return resolve


def load(
    db: Database,
    step_path: str,
    author: str = "demo",
    ref: str = "main",
    with_contracts: bool = True,
    with_chains: bool = True,
    ingestor: Ingestor | None = None,
) -> BootstrapReport:
    """Ingest a WINCH-100 STEP file and author every contract and chain."""
    for team_id, name in assembly.TEAMS.items():
        ensure_team(db, team_id, name)

    ing = ingestor or Ingestor(db)
    commit_id = begin_commit(db, author, f"import {step_path}", "chassis")
    report_in = ing.ingest_file(
        step_path, "chassis", commit_id, status="released", resolver=attrs_resolver()
    )
    db.execute(
        "UPDATE commit_log SET new_root = ? WHERE commit_id = ?",
        (report_in.root_hash, commit_id),
    )

    out = BootstrapReport(
        commit_id=commit_id,
        root_revision=report_in.root_revision,
        root_hash=report_in.root_hash,
        parts=int(db.scalar("SELECT COUNT(*) FROM part") or 0),
        ingest_summary=report_in.summary(),
        problems=list(report_in.warnings),
    )

    if with_contracts:
        out.contracts, problems = attach_contracts(db, report_in.root_revision, ing, author)
        out.problems.extend(problems)

    if with_chains:
        out.chains = attach_chains(db)

    # Contracts are part of what a node hashes, so the root moves once they are
    # attached. Recompute before the ref is published.
    tree = merkle.build_from_occurrences(db, report_in.root_revision)
    store_hashes(db, tree)
    out.root_hash = tree.node_hash
    db.execute("UPDATE commit_log SET new_root = ? WHERE commit_id = ?", (tree.node_hash, commit_id))
    db.set_ref(ref, report_in.root_revision, tree.node_hash, author)
    return out


def attach_contracts(
    db: Database, root_revision: str, ingestor: Ingestor, author: str = "demo"
) -> tuple[int, list[str]]:
    """Author the declared contract for every part in the configuration.

    Each declaration is re-derived against the part's own geometry, so the stored
    interface points are measured rather than asserted.
    """
    from ..query import traversal as q

    declarations = assembly.declarations()
    problems: list[str] = []
    written = 0

    # Leaves first: a socket may read a sibling's interface, and that interface
    # has to exist before the socket can be resolved.
    instances = q.configuration(db, root_revision)
    by_depth = sorted(instances, key=lambda i: -i.path.count("/"))
    done: set[str] = set()

    for inst in by_depth:
        if inst.revision_id in done:
            continue
        done.add(inst.revision_id)
        decl = declarations.get(inst.part_number)
        if decl is None:
            continue
        if db.one("SELECT 1 FROM contract WHERE revision_id = ?", (inst.revision_id,)):
            continue

        identity = solid = None
        if inst.fingerprint:
            identity, solid = _reidentify(db, ingestor, inst.fingerprint)

        derived = ct.derive_contract(decl, identity, solid)
        socket_problems = ct.resolve_derived_sockets(db, inst.revision_id, derived.sockets)
        for p in derived.problems + socket_problems:
            problems.append(f"{inst.part_number}: {p}")
        ct.store_contract(db, inst.revision_id, derived, declared_by=author)
        written += 1
    return written, problems


def _reidentify(db: Database, ingestor: Ingestor, fingerprint: str):
    """Recover the identity of a stored shape from its blob.

    Interface points are measured from the geometry, so authoring a contract
    after ingest needs the solid back. It comes from the content-addressed blob
    store rather than from the source file, which is the point of keeping it.
    """
    shape = db.read_blob(fingerprint)
    if shape is None:
        return None, None
    solid = ingestor.backend.solid_record_from_shape(shape, fingerprint)
    if solid is None or not solid.valid:
        return None, None
    from ..geometry import fingerprint as fp

    try:
        return fp.identify(solid, deflection=ingestor.deflection), solid
    except ValueError:
        return None, solid


def attach_chains(db: Database) -> int:
    """Author the tolerance chains. Chains are authored, never discovered."""
    made = 0
    for spec in assembly.CHAINS:
        if db.one("SELECT 1 FROM chain WHERE name = ?", (spec["name"],)):
            continue
        members = []
        missing = []
        for part_number, dim_name, direction in spec["members"]:
            row = db.one(
                """SELECT d.dimension_id FROM dimension d
                   JOIN revision r ON r.revision_id = d.revision_id
                   JOIN part p ON p.part_id = r.part_id
                   WHERE p.part_number = ? AND d.name = ?
                   ORDER BY r.revision_index DESC LIMIT 1""",
                (part_number, dim_name),
            )
            if row is None:
                missing.append(f"{part_number}.{dim_name}")
                continue
            members.append((row["dimension_id"], direction))
        if missing or not members:
            continue
        tolerance.create_chain(
            db, spec["name"], members,
            target_low=spec["target_low"], target_high=spec["target_high"],
            method=spec["method"], owning_team=spec["owning_team"],
            description=spec["description"],
        )
        made += 1
    return made


def subscribe_defaults(db: Database) -> int:
    """Declare the cross-team dependencies that make notification meaningful.

    A subscription is a team stating that it depends on somebody else's contract.
    Without them a breaking change has nobody to name, and the impact query falls
    back to containment alone.
    """
    pairs = [
        ("drivetrain", "BASE-PLATE", None),
        ("chassis", "BEARING-BLOCK", "foot"),
        ("controls", "BASE-PLATE", None),
        ("drivetrain", "GEARBOX-COVER", "mount"),
        ("standards", "BOLT-M6X32", None),
    ]
    made = 0
    for team, part_number, interface in pairs:
        part_id = db.scalar("SELECT part_id FROM part WHERE part_number = ?", (part_number,))
        if not part_id:
            continue
        cur = db.execute(
            """INSERT OR IGNORE INTO subscription (team_id, part_id, interface_name)
               VALUES (?,?,?)""",
            (team, part_id, interface),
        )
        made += cur.rowcount
    return made
