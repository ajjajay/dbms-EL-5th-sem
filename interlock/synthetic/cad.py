"""Small geometry toolkit for building test and demo parts, and writing STEP.

Interlock is not a modeller (design doc section 2) and nothing in the running
system imports this module. It exists so the demonstration assembly and the test
suite can be generated deterministically, on any machine, without shipping
third-party CAD files. Everything here goes through the same OCCT kernel that
ingestion reads with, which is a limitation: a shape built and read by one kernel
proves little about cross-tool behaviour. That is what the NIST files are for.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Sequence

import numpy as np
from OCP.BRepAlgoAPI import BRepAlgoAPI_Common, BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
from OCP.IFSelect import IFSelect_ReturnStatus
from OCP.STEPCAFControl import STEPCAFControl_Writer
from OCP.STEPControl import STEPControl_AsIs
from OCP.TCollection import TCollection_ExtendedString
from OCP.TDataStd import TDataStd_Name
from OCP.TDocStd import TDocStd_Document
from OCP.TopLoc import TopLoc_Location
from OCP.XCAFApp import XCAFApp_Application
from OCP.XCAFDoc import XCAFDoc_DocumentTool
from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt, gp_Trsf

# --------------------------------------------------------------- transforms


def translation(x: float = 0.0, y: float = 0.0, z: float = 0.0) -> np.ndarray:
    m = np.eye(4)
    m[:3, 3] = (x, y, z)
    return m


def rotation(axis: Sequence[float], degrees: float) -> np.ndarray:
    a = np.asarray(axis, dtype=float)
    a = a / np.linalg.norm(a)
    t = math.radians(degrees)
    c, s = math.cos(t), math.sin(t)
    x, y, z = a
    r = np.array(
        [
            [c + x * x * (1 - c), x * y * (1 - c) - z * s, x * z * (1 - c) + y * s],
            [y * x * (1 - c) + z * s, c + y * y * (1 - c), y * z * (1 - c) - x * s],
            [z * x * (1 - c) - y * s, z * y * (1 - c) + x * s, c + z * z * (1 - c)],
        ]
    )
    m = np.eye(4)
    m[:3, :3] = r
    return m


def compose(*ms: np.ndarray) -> np.ndarray:
    out = np.eye(4)
    for m in ms:
        out = out @ m
    return out


def _trsf(m: np.ndarray) -> gp_Trsf:
    t = gp_Trsf()
    t.SetValues(*[float(v) for v in np.asarray(m, dtype=float)[:3, :].reshape(12)])
    return t


# ---------------------------------------------------------------- primitives


def box(dx: float, dy: float, dz: float, at: Sequence[float] = (0, 0, 0)):
    return BRepPrimAPI_MakeBox(gp_Pnt(*at), dx, dy, dz).Shape()


def cylinder(r: float, h: float, at: Sequence[float] = (0, 0, 0), axis: Sequence[float] = (0, 0, 1)):
    return BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(*at), gp_Dir(*axis)), r, h).Shape()


def cut(a, *tools):
    for t in tools:
        a = BRepAlgoAPI_Cut(a, t).Shape()
    return a


def fuse(a, *others):
    for o in others:
        a = BRepAlgoAPI_Fuse(a, o).Shape()
    return a


def common(a, b):
    return BRepAlgoAPI_Common(a, b).Shape()


def place(shape, matrix: np.ndarray):
    """Rigidly move a shape, returning a copy."""
    return BRepBuilderAPI_Transform(shape, _trsf(matrix), True).Shape()


def mirror(shape, point: Sequence[float] = (0, 0, 0), normal: Sequence[float] = (1, 0, 0)):
    """Reflect a shape through a plane. The result is the opposite-handed part."""
    t = gp_Trsf()
    t.SetMirror(gp_Ax2(gp_Pnt(*point), gp_Dir(*normal)))
    return BRepBuilderAPI_Transform(shape, t, True).Shape()


def drill(shape, points: Sequence[Sequence[float]], radius: float, z0: float, z1: float):
    """Drill vertical holes through a range of z at each (x, y)."""
    tools = [cylinder(radius, z1 - z0, (x, y, z0)) for x, y in points]
    return cut(shape, *tools)


def plate(dx, dy, dz, holes=(), hole_r: float = 3.3, at=(0, 0, 0)):
    """A rectangular plate with vertical through holes at absolute (x, y)."""
    base = box(dx, dy, dz, at)
    if holes:
        base = drill(base, holes, hole_r, at[2] - 1.0, at[2] + dz + 1.0)
    return base


# ------------------------------------------------------------ assembly writer


@dataclass
class Node:
    """A part definition or an assembly, for STEP export.

    Reusing one Node object in several places makes them appearances of the same
    definition, which is how a real CAD system stores forty identical bolts.
    """

    name: str
    shape: object | None = None
    children: list[tuple[str, "Node", np.ndarray]] = field(default_factory=list)

    def add(self, instance: str, node: "Node", matrix: np.ndarray | None = None) -> "Node":
        self.children.append((instance, node, np.eye(4) if matrix is None else matrix))
        return self


def write_step(root: Node, path: str, shuffle_seed: int | None = None) -> str:
    """Write a Node tree as a STEP assembly with hierarchy, names and placements.

    ``shuffle_seed`` reorders every node's components before writing, which is the
    'same assembly re-exported with a different child order' case that the Merkle
    hash is meant to be indifferent to.
    """
    import random

    rng = random.Random(shuffle_seed) if shuffle_seed is not None else None

    app = XCAFApp_Application.GetApplication_s()
    doc = TDocStd_Document(TCollection_ExtendedString("MDTV-XCAF"))
    app.NewDocument(TCollection_ExtendedString("MDTV-XCAF"), doc)
    tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    labels: dict[int, object] = {}

    def define(node: Node):
        key = id(node)
        if key in labels:
            return labels[key]
        if node.children:
            label = tool.NewShape()
            kids = list(node.children)
            if rng is not None:
                rng.shuffle(kids)
            for instance, child, matrix in kids:
                child_label = define(child)
                comp = tool.AddComponent(label, child_label, TopLoc_Location(_trsf(matrix)))
                TDataStd_Name.Set_s(comp, TCollection_ExtendedString(instance))
        else:
            if node.shape is None:
                raise ValueError(f"leaf {node.name!r} has no shape")
            label = tool.AddShape(node.shape, False)
        TDataStd_Name.Set_s(label, TCollection_ExtendedString(node.name))
        labels[key] = label
        return label

    define(root)
    tool.UpdateAssemblies()

    writer = STEPCAFControl_Writer()
    writer.SetNameMode(True)
    if not writer.Transfer(doc, STEPControl_AsIs):
        raise IOError("STEP transfer failed")
    if writer.Write(path) != IFSelect_ReturnStatus.IFSelect_RetDone:
        raise IOError(f"STEP write failed: {path}")
    return path


def write_step_part(shape, name: str, path: str) -> str:
    """Write one solid as a single-part STEP file."""
    return write_step(Node(name=name, shape=shape), path)
