"""Render a configuration from the database, to look at it.

A verification tool, not part of the system. It is worth having because it uses
*only* what the query layer already exposes: the display meshes stored beside the
blobs at ingest, placed by the composed transforms from `query.configuration`.
So if the picture looks right, the stored meshes and the transform composition
are both right -- which is the thing a CAD viewer would otherwise be needed to
confirm.

No kernel is opened. Colours are by owning team, which makes the multi-team
story visible at a glance.

    .venv/Scripts/python.exe scripts/render_assembly.py [--db PATH] [--out PATH]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from interlock.db.database import Database
from interlock.query import traversal as q

TEAM_COLOUR = {
    "chassis": "#4a7fb5",
    "drivetrain": "#c2703d",
    "standards": "#7a7a7a",
    "controls": "#5f9e6e",
}
VIEWS = [
    ("isometric", 22, -60),
    ("front", 0, -90),
    ("top", 89, -90),
    ("side", 0, 0),
]


def main(argv: list[str]) -> int:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from mpl_toolkits.mplot3d.art3d import Poly3DCollection

    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(ROOT / "data" / "store" / "interlock.db"))
    ap.add_argument("--out", default=str(ROOT / "data" / "demo" / "winch_100.png"))
    ap.add_argument("--ref", default="main")
    ap.add_argument("--revision")
    ap.add_argument("--hide-fasteners", action="store_true")
    args = ap.parse_args(argv)

    db = Database(args.db)
    head = db.get_ref(args.ref)
    if head is None and not args.revision:
        print("no configuration loaded; run `python -m interlock.cli demo` first")
        return 1
    root = args.revision or head["revision_id"]

    instances = [i for i in q.configuration(db, root) if not i.is_assembly and i.fingerprint]
    if args.hide_fasteners:
        from interlock.model.interference import is_fastener

        instances = [i for i in instances if not is_fastener(i.part_number)]

    print(f"{len(instances)} placed solids")
    cache: dict[str, tuple] = {}
    drawn = skipped = 0
    per_team: dict[str, int] = {}

    fig = plt.figure(figsize=(16, 13))
    fig.patch.set_facecolor("#fbfaf8")
    axes = []
    for k, (name, elev, azim) in enumerate(VIEWS, start=1):
        ax = fig.add_subplot(2, 2, k, projection="3d")
        ax.view_init(elev=elev, azim=azim)
        ax.set_title(name, fontsize=11, color="#1a1a1a")
        axes.append(ax)

    bounds_lo = np.full(3, np.inf)
    bounds_hi = np.full(3, -np.inf)

    for inst in instances:
        if inst.fingerprint not in cache:
            cache[inst.fingerprint] = db.read_mesh(inst.fingerprint)
        mesh = cache[inst.fingerprint]
        if mesh is None:
            skipped += 1
            continue
        vertices, triangles = mesh
        placed = (inst.world[:3, :3] @ np.asarray(vertices, float).T).T + inst.world[:3, 3]
        bounds_lo = np.minimum(bounds_lo, placed.min(axis=0))
        bounds_hi = np.maximum(bounds_hi, placed.max(axis=0))

        colour = TEAM_COLOUR.get(inst.team_id, "#999999")
        per_team[inst.team_id] = per_team.get(inst.team_id, 0) + 1
        faces = placed[np.asarray(triangles, dtype=int)]
        for ax in axes:
            ax.add_collection3d(Poly3DCollection(
                faces, facecolors=colour, shade=True, alpha=0.95,
                lightsource=matplotlib.colors.LightSource(azdeg=225, altdeg=45),
            ))
        drawn += 1

    centre = (bounds_lo + bounds_hi) / 2.0
    span = float((bounds_hi - bounds_lo).max()) * 0.55
    for ax in axes:
        ax.set_xlim(centre[0] - span, centre[0] + span)
        ax.set_ylim(centre[1] - span, centre[1] + span)
        ax.set_zlim(centre[2] - span, centre[2] + span)
        ax.set_box_aspect((1, 1, 1))
        ax.set_facecolor("#fbfaf8")
        ax.grid(False)
        for pane in (ax.xaxis, ax.yaxis, ax.zaxis):
            pane.pane.set_alpha(0.03)
            pane.line.set_color("#c8c4bc")
        ax.tick_params(colors="#9a958c", labelsize=7)

    handles = [
        plt.Line2D([0], [0], marker="s", color="none", markerfacecolor=c,
                   markersize=11, label=f"{t} ({per_team.get(t, 0)})")
        for t, c in TEAM_COLOUR.items() if t in per_team
    ]
    fig.legend(handles=handles, loc="lower center", ncol=len(handles), frameon=False,
               fontsize=10)
    part = db.scalar(
        "SELECT p.part_number FROM revision r JOIN part p ON p.part_id = r.part_id "
        "WHERE r.revision_id = ?", (root,))
    fig.suptitle(
        f"{part}   -   {drawn} placed solids, coloured by owning team\n"
        f"rendered from stored display meshes and composed transforms; no kernel opened",
        fontsize=13, color="#1a1a1a")
    fig.tight_layout(rect=(0, 0.04, 1, 0.95))

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=110, facecolor=fig.get_facecolor())
    extent = bounds_hi - bounds_lo
    print(f"drew {drawn}, skipped {skipped} (no stored mesh)")
    print(f"bounds {extent[0]:.0f} x {extent[1]:.0f} x {extent[2]:.0f} mm")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
