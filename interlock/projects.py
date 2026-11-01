"""Projects: separate databases for separate products.

A project is one folder under data/projects/<name>/ holding its own database,
geometry blobs, document store and downloaded source files. Nothing is shared
between projects, so a real product can be loaded next to the demonstration
assembly without the two ever touching. Every CLI command takes --project.

The demonstration assembly is the project called 'winch'. It keeps its original
location (data/store/) and is built by `demo`, not by `build-project`.

Real products arrive the way downloaded CAD always does: geometry and names,
nothing else. No owners, no materials, no contracts. `build` therefore assigns
the notional teams and materials declared in the project's spec, and drafts a
contract for every part from its own geometry. Those drafts are stored as
'auto_drafted' and unreviewed, so the status view reports each one as work
still to do rather than passing it off as a human decision.
"""

from __future__ import annotations

import re
import shutil
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from .db.database import DEFAULT_DB, Database
from .model import contracts as ct
from .model import merkle
from .model.ingest import Ingestor, PartAttrs, begin_commit, ensure_team, store_hashes

PROJECTS_DIR = Path("data") / "projects"
DEMO = "winch"


@dataclass
class Source:
    file: str          # name under the project's source/ folder
    url: str
    ref: str           # the ref this assembly is published under


@dataclass
class ProjectSpec:
    name: str
    title: str
    licence: str
    origin: str
    sources: list[Source]
    teams: dict[str, str]                         # team_id -> display name
    team_rules: list[tuple[str, str]]             # (part-name regex, team_id), first match wins
    default_team: str
    materials: list[tuple[str, str, float]] = field(default_factory=list)  # (regex, material, g/mm^3)


_SO100 = "https://raw.githubusercontent.com/TheRobotStudio/SO-ARM100/a758567c3978dfeefe282ede0500085a48fe8f78/STEP/SO100"

PROJECTS: dict[str, ProjectSpec] = {
    "so-arm100": ProjectSpec(
        name="so-arm100",
        title="SO-ARM100 robot arm (follower and leader)",
        licence="Apache-2.0",
        origin="github.com/TheRobotStudio/SO-ARM100 at commit a758567",
        sources=[
            Source("follower_assembly.step", f"{_SO100}/Follower_Specific/SO_5DOF_ARM100_Assembly.step", "main"),
            Source("leader_assembly.step", f"{_SO100}/Leader_Specific/SO_5DOF_ARM100_Assembly.step", "leader"),
        ],
        # Notional teams, as in the demonstration: the source has no owners.
        teams={"actuation": "Actuation (servos)", "base": "Base and mounting",
               "arm": "Arm links", "gripper": "Wrist and gripper"},
        team_rules=[(r"^STS3215|^Passive_Horn", "actuation"),
                    (r"^Base|^WaveShare", "base"),
                    (r"^Wrist|^Moving_Jaw|^Trigger", "gripper"),
                    (r"^Rotation_Pitch|^SO_ARM100", "arm")],
        default_team="arm",
        # Printed parts are PLA. Solid density, so the masses are an upper bound
        # (real prints are partly hollow). Servos are bought in and get none:
        # the status view reports them as missing a density, which is true.
        materials=[(r"^STS3215|^Passive_Horn", "", 0.0),
                   (r".", "PLA (solid)", 0.00124)],
    ),
}


def names() -> list[str]:
    return [DEMO, *PROJECTS]


def folder(name: str) -> Path:
    if name == DEMO:
        return DEFAULT_DB.parent
    if name not in PROJECTS:
        raise KeyError(f"unknown project {name!r}; known: {', '.join(names())}")
    return PROJECTS_DIR / name


def db_path(name: str) -> Path:
    return DEFAULT_DB if name == DEMO else folder(name) / "interlock.db"


def resolver(spec: ProjectSpec):
    def resolve(part_name: str) -> PartAttrs:
        team = next((t for pat, t in spec.team_rules if re.search(pat, part_name)), spec.default_team)
        material, density = None, None
        for pat, mat, dens in spec.materials:
            if re.search(pat, part_name):
                material, density = (mat or None), (dens or None)
                break
        return PartAttrs(team_id=team, material=material, density=density, description=part_name)
    return resolve


def fetch(spec: ProjectSpec, refresh: bool = False, log=print) -> None:
    """Download any source file not already present."""
    src = folder(spec.name) / "source"
    src.mkdir(parents=True, exist_ok=True)
    for s in spec.sources:
        dest = src / s.file
        if dest.exists() and not refresh:
            continue
        log(f"downloading {s.file} from {spec.origin}")
        tmp = dest.with_suffix(".part")
        with urllib.request.urlopen(s.url, timeout=120) as r, open(tmp, "wb") as f:
            shutil.copyfileobj(r, f)
        tmp.replace(dest)


@dataclass
class BuildReport:
    lines: list[str] = field(default_factory=list)
    drafted: int = 0
    problems: list[str] = field(default_factory=list)


def build(name: str, refresh: bool = False, log=print) -> BuildReport:
    """Fetch the sources and build the project's database from scratch."""
    from .synthetic.bootstrap import _reidentify

    spec = PROJECTS[name]
    fetch(spec, refresh=refresh, log=log)
    path = db_path(name)
    for stale in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm"),
                  path.parent / "documents.json"):
        stale.unlink(missing_ok=True)
    shutil.rmtree(path.parent / "blobs", ignore_errors=True)

    db = Database(path)
    out = BuildReport()
    try:
        for team_id, title in spec.teams.items():
            ensure_team(db, team_id, title)
        ing = Ingestor(db)
        roots: list[tuple[Source, str]] = []
        for s in spec.sources:
            commit = begin_commit(db, "import", f"import {s.file}", spec.default_team)
            rep = ing.ingest_file(str(folder(name) / "source" / s.file), spec.default_team, commit,
                                  status="released", resolver=resolver(spec))
            db.execute("UPDATE commit_log SET new_root = ? WHERE commit_id = ?", (rep.root_hash, commit))
            out.lines.append(f"{s.file} -> ref '{s.ref}': {rep.summary()}")
            out.problems.extend(rep.warnings)
            roots.append((s, rep.root_revision))

        # A contract for every part that has geometry, drafted from that geometry.
        for row in db.query("SELECT revision_id, fingerprint FROM revision WHERE fingerprint IS NOT NULL"):
            if db.one("SELECT 1 FROM contract WHERE revision_id = ?", (row["revision_id"],)):
                continue
            identity, solid = _reidentify(db, ing, row["fingerprint"])
            if identity is None or solid is None:
                out.problems.append(f"{row['revision_id']}: geometry could not be re-read; no draft")
                continue
            ct.store_contract(db, row["revision_id"], ct.auto_draft(identity, solid),
                              declared_by="auto-draft", reviewed=False)
            out.drafted += 1

        # Contracts are part of what a node hashes, so hash after drafting.
        for s, root in roots:
            tree = merkle.build_from_occurrences(db, root)
            store_hashes(db, tree)
            db.set_ref(s.ref, root, tree.node_hash, "import")
        out.lines.append(f"contracts auto-drafted (unreviewed): {out.drafted}")
    finally:
        db.close()
    return out
