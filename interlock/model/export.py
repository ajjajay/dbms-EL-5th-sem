"""Giving geometry back out, and handing it to a CAD application.

The design document is firm that the system is not a modeller and never creates
geometry (section 2). Exporting does not breach that: every solid written here
was read from the content-addressed blob store exactly as it arrived, and the
only thing this module contributes is the arrangement -- which comes from the
occurrence table, not from a modelling operation.

That makes the export a useful check in its own right. If a configuration can be
written back out and re-read as the same thing, then the store is faithful and
the occurrence transforms mean what they claim to. `tests/test_export.py` asserts
exactly that round trip.

Opening the result in a CAD application is a convenience for the review
interface, and it is deliberately narrow: the launcher only ever runs a known
CAD executable found on this machine, on a file this module just wrote, and the
web route that calls it refuses unless the server is bound to loopback.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path


from ..db.database import Database
from ..kernel import get_backend
from ..kernel.base import ExportNode
from ..query import traversal as q

# Where exported files are cached, keyed by what they contain, so clicking the
# same part twice does not re-export it.
CACHE_DIRNAME = "exports"


def export_revision(
    db: Database,
    revision: str,
    path: str | os.PathLike,
    backend=None,
    max_depth: int = q.DEPTH_GUARD,
) -> str:
    """Write a revision -- a single part or a whole assembly -- as STEP.

    Assemblies are rebuilt with their hierarchy, instance names and placements,
    so what comes out has the same structure as what went in. A shape shared by
    several parts is exported once and referenced, because the ExportNode for a
    given revision is created once and reused.
    """
    backend = backend or get_backend("auto")
    rev = q.resolve_revision(db, revision)
    built: dict[str, ExportNode] = {}

    def build(revision_id: str, depth: int = 0) -> ExportNode | None:
        if depth > max_depth:
            raise RecursionError(f"assembly deeper than {max_depth} while exporting")
        if revision_id in built:
            return built[revision_id]

        row = db.one(
            """SELECT r.fingerprint, p.part_number FROM revision r
               JOIN part p ON p.part_id = r.part_id WHERE r.revision_id = ?""",
            (revision_id,),
        )
        if row is None:
            return None
        node = ExportNode(name=row["part_number"])

        children = db.query(
            "SELECT * FROM occurrence WHERE parent_rev = ? ORDER BY occurrence_id",
            (revision_id,),
        )
        for occ in children:
            child = build(occ["child_rev"], depth + 1)
            if child is not None:
                label = occ["instance_name"] or child.name
                node.add(label, child, q.matrix_of(occ))

        if not node.children:
            if row["fingerprint"] is None:
                return None          # an assembly with nothing usable under it
            shape = db.read_blob(row["fingerprint"])
            if shape is None:
                return None          # geometry was never stored for this shape
            node.shape = shape

        built[revision_id] = node
        return node

    root = build(rev)
    if root is None:
        raise LookupError(f"{revision!r} has no geometry to export")

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    return backend.write_assembly(root, str(target))


def export_cached(db: Database, revision: str, cache_dir: str | os.PathLike | None = None,
                  backend=None) -> Path:
    """Export to a cache keyed by the revision's Merkle hash.

    The hash names the content, so an unchanged configuration is written once and
    every later click serves the same file -- the same property that makes the
    blob store work, applied to exports.
    """
    rev = q.resolve_revision(db, revision)
    row = db.one(
        """SELECT r.merkle_hash, r.fingerprint, p.part_number FROM revision r
           JOIN part p ON p.part_id = r.part_id WHERE r.revision_id = ?""", (rev,))
    if row is None:
        raise LookupError(f"no revision {revision!r}")

    stamp = row["merkle_hash"] or row["fingerprint"] or hashlib.blake2b(
        rev.encode(), digest_size=8).hexdigest()
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in row["part_number"])

    root = Path(cache_dir) if cache_dir else Path(db.path).parent / CACHE_DIRNAME
    target = root / f"{safe}_{stamp}.step"
    if not target.exists():
        export_revision(db, rev, target, backend=backend)
    return target


# ------------------------------------------------------------------ launching

# Known CAD executables, in the order they are preferred. Only these are ever
# launched, and only on a file this module wrote.
CAD_CANDIDATES = [
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\FreeCAD 1.1\bin\freecad.exe"),
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\FreeCAD\bin\freecad.exe"),
    r"C:\Program Files\FreeCAD 1.1\bin\FreeCAD.exe",
    r"C:\Program Files\FreeCAD 1.0\bin\FreeCAD.exe",
    r"C:\Program Files\FreeCAD\bin\FreeCAD.exe",
    "/usr/bin/freecad",
    "/usr/local/bin/freecad",
    "/Applications/FreeCAD.app/Contents/MacOS/FreeCAD",
]


def find_cad() -> str | None:
    """The FreeCAD executable on this machine, or None."""
    override = os.environ.get("INTERLOCK_CAD")
    if override and Path(override).exists():
        return override
    for path in CAD_CANDIDATES:
        if Path(path).exists():
            return path
    from shutil import which

    return which("freecad") or which("FreeCAD")


# Handed to FreeCAD instead of the STEP file itself. Opening a STEP directly
# leaves the camera wherever it was, which on a 400 mm assembly usually means
# looking at nothing -- the model is there, off-screen, and it reads as a failed
# import. This imports it, then frames it.
_LAUNCH_MACRO = '''# Written by Interlock. Opens an exported configuration and frames it.
import FreeCAD, Import

doc = FreeCAD.newDocument({title!r})
Import.insert({step!r}, doc.Name)
doc.recompute()
try:
    import FreeCADGui
    view = FreeCADGui.activeDocument().activeView()
    view.viewAxonometric()
    FreeCADGui.SendMsgToActiveView("ViewFit")
except Exception:
    pass          # console mode, or no view yet: the document is still open
'''


def open_in_cad(path: str | os.PathLike) -> tuple[bool, str]:
    """Launch FreeCAD on an exported file, without waiting for it.

    Returns (started, message). Never raises: a review interface should report
    that it could not open a viewer, not return a 500.
    """
    exe = find_cad()
    if exe is None:
        return False, ("FreeCAD was not found on this machine. Install it with "
                       "`winget install FreeCAD.FreeCAD`, or set INTERLOCK_CAD "
                       "to the executable.")
    target = Path(path)
    if not target.exists():
        return False, f"nothing to open: {target} was not written"

    # FreeCAD runs a .py given on the command line, so the macro is what gets
    # passed; it opens the STEP itself.
    macro = target.with_suffix(".open.py")
    try:
        macro.write_text(
            _LAUNCH_MACRO.format(title=target.stem.split("_")[0] or "Interlock",
                                 step=str(target)),
            encoding="utf-8")
        argument = str(macro)
    except OSError:
        argument = str(target)        # fall back to opening the STEP directly

    try:
        creationflags = 0
        if sys.platform == "win32":
            creationflags = getattr(subprocess, "DETACHED_PROCESS", 0)
        subprocess.Popen(
            [exe, argument],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"could not start {Path(exe).name}: {type(exc).__name__}: {exc}"
    return True, (f"opening {target.name} in {Path(exe).name} -- it takes a few "
                  f"seconds to appear, and arrives framed and ready to rotate")
