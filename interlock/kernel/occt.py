"""OCCT backend.

Open CASCADE is the kernel FreeCAD itself wraps; binding to it directly gives the
same geometry with no GUI process to start. Nothing here runs outside ingestion.

Everything this module produces is a plain record from ``kernel.base``. The one
rule: no OCCT type may escape past ``read()`` except inside ``SolidRecord.handle``,
which only the blob writer and the interference stage are allowed to touch.
"""

from __future__ import annotations

import math
import os
from typing import Sequence

import numpy as np

from .base import (
    ExportNode,
    FaceRecord,
    IngestResult,
    MassProperties,
    OccurrenceNode,
    SolidRecord,
    TopologyCounts,
    identity_transform,
)

_OCCT_IMPORT_ERROR: Exception | None = None
try:  # pragma: no cover - import guard
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.BRepBndLib import BRepBndLib
    from OCP.BRepCheck import BRepCheck_Analyzer
    from OCP.BRepGProp import BRepGProp
    from OCP.Bnd import Bnd_Box
    from OCP.GProp import GProp_GProps
    from OCP.GeomAbs import GeomAbs_SurfaceType
    from OCP.IFSelect import IFSelect_ReturnStatus
    from OCP.STEPCAFControl import STEPCAFControl_Reader
    from OCP.TCollection import TCollection_AsciiString, TCollection_ExtendedString
    from OCP.TDataStd import TDataStd_Name
    from OCP.TDF import TDF_Label, TDF_LabelSequence, TDF_Tool
    from OCP.TDocStd import TDocStd_Document
    from OCP.TopAbs import TopAbs_Orientation, TopAbs_ShapeEnum
    from OCP.TopExp import TopExp
    from OCP.TopTools import TopTools_IndexedMapOfShape
    from OCP.TopoDS import TopoDS, TopoDS_Shape
    from OCP.XCAFDoc import XCAFDoc_DocumentTool, XCAFDoc_ShapeTool
    from OCP.gp import gp_Pnt, gp_Vec

    _HAVE_OCCT = True
except Exception as exc:  # pragma: no cover
    _HAVE_OCCT = False
    _OCCT_IMPORT_ERROR = exc


_SURFACE_MAP: dict = {}


def _init_surface_map() -> None:
    if _SURFACE_MAP or not _HAVE_OCCT:
        return
    t = GeomAbs_SurfaceType
    _SURFACE_MAP.update(
        {
            t.GeomAbs_Plane: "plane",
            t.GeomAbs_Cylinder: "cylinder",
            t.GeomAbs_Cone: "cone",
            t.GeomAbs_Sphere: "sphere",
            t.GeomAbs_Torus: "torus",
            t.GeomAbs_BezierSurface: "bspline",
            t.GeomAbs_BSplineSurface: "bspline",
            t.GeomAbs_SurfaceOfRevolution: "revolution",
            t.GeomAbs_SurfaceOfExtrusion: "other",
            t.GeomAbs_OffsetSurface: "other",
            t.GeomAbs_OtherSurface: "other",
        }
    )


def _xyz(p) -> tuple[float, float, float]:
    return (p.X(), p.Y(), p.Z())


def _trsf_to_matrix(trsf) -> np.ndarray:
    m = np.eye(4, dtype=float)
    for i in range(3):
        for j in range(3):
            m[i, j] = trsf.Value(i + 1, j + 1)
        m[i, 3] = trsf.Value(i + 1, 4)
    return m


def _label_name(label) -> str:
    # A null label is dereferenced without a check inside OCCT and takes the whole
    # process down with an access violation, not a Python exception. Real AP242
    # files with semantic PMI produce them, so every entry point guards.
    if label is None or label.IsNull():
        return "unnamed"
    # FindAttribute on a label that has no name crashes the interpreter (access
    # violation inside the binding), so presence is tested first. Files whose
    # every label is named never expose this; real AP242 files do.
    if not label.IsAttribute(TDataStd_Name.GetID_s()):
        return "unnamed"
    attr = TDataStd_Name()
    if label.FindAttribute(TDataStd_Name.GetID_s(), attr):
        return str(attr.Get().ToExtString())
    return "unnamed"


def _label_key(label) -> str:
    """Stable per-document key for a label, used so every occurrence that refers
    to the same geometry shares one SolidRecord."""
    if label is None or label.IsNull():
        return ""
    entry = TCollection_AsciiString()
    TDF_Tool.Entry_s(label, entry)
    return entry.ToCString()


class OcctBackend:
    """Reads STEP AP203/AP214/AP242 and native BREP through Open CASCADE."""

    name = "occt"

    def __init__(self) -> None:
        _init_surface_map()

    def available(self) -> bool:
        return _HAVE_OCCT

    def import_error(self) -> Exception | None:
        return _OCCT_IMPORT_ERROR

    def supported_suffixes(self) -> Sequence[str]:
        return (".step", ".stp", ".brep")

    # ------------------------------------------------------------------ read

    def read(self, path: str) -> IngestResult:
        if not _HAVE_OCCT:
            raise RuntimeError(f"OCCT unavailable: {_OCCT_IMPORT_ERROR}")
        suffix = os.path.splitext(path)[1].lower()
        if suffix in (".step", ".stp"):
            return self._read_step(path)
        if suffix == ".brep":
            return self._read_brep(path)
        raise ValueError(f"unsupported source for {self.name}: {path}")

    def _read_step(self, path: str) -> IngestResult:
        warnings: list[str] = []
        doc = TDocStd_Document(TCollection_ExtendedString("interlock"))
        reader = STEPCAFControl_Reader()
        reader.SetNameMode(True)
        reader.SetColorMode(True)
        reader.SetLayerMode(True)

        status = reader.ReadFile(path)
        # IFSelect_RetDone is 1, not 0 (RetVoid is 0 and means nothing happened).
        if status != IFSelect_ReturnStatus.IFSelect_RetDone:
            raise IOError(f"STEP read failed for {path} (status {status})")
        if not reader.Transfer(doc):
            raise IOError(f"STEP transfer produced nothing for {path}")

        tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
        free = TDF_LabelSequence()
        tool.GetFreeShapes(free)
        if free.Length() == 0:
            raise IOError(f"STEP file contains no root shape: {path}")

        solids: list[SolidRecord] = []
        index_by_label: dict[str, int] = {}

        def solid_index_for(label) -> int | None:
            if label.IsNull():
                return None
            key = _label_key(label)
            if key in index_by_label:
                return index_by_label[key]
            shape = XCAFDoc_ShapeTool.GetShape_s(label)
            if shape is None or shape.IsNull():
                return None
            record = self._solid_record(shape, _label_name(label))
            if record is None:
                warnings.append(f"label {key} carried no usable solid")
                return None
            solids.append(record)
            index_by_label[key] = len(solids) - 1
            return index_by_label[key]

        def build(label, name: str, transform: np.ndarray, instance: str = "") -> OccurrenceNode:
            node = OccurrenceNode(
                name=name,
                transform=transform,
                instance_name=instance,
                definition_key=_label_key(label),
            )
            if XCAFDoc_ShapeTool.IsAssembly_s(label):
                comps = TDF_LabelSequence()
                XCAFDoc_ShapeTool.GetComponents_s(label, comps)
                for i in range(1, comps.Length() + 1):
                    comp = comps.Value(i)
                    child_t = _trsf_to_matrix(
                        XCAFDoc_ShapeTool.GetLocation_s(comp).Transformation()
                    )
                    referred = TDF_Label()
                    target = comp
                    if XCAFDoc_ShapeTool.GetReferredShape_s(comp, referred):
                        target = referred
                    if target.IsNull():
                        warnings.append(
                            f"component {_label_key(comp)} refers to a null label; skipped"
                        )
                        continue
                    child_name = _label_name(target)
                    instance_name = _label_name(comp)
                    if child_name == "unnamed":
                        child_name = instance_name
                    if instance_name == "unnamed":
                        instance_name = ""
                    node.children.append(build(target, child_name, child_t, instance_name))
            else:
                node.solid_index = solid_index_for(label)
            return node

        roots = [
            build(free.Value(i), _label_name(free.Value(i)), identity_transform())
            for i in range(1, free.Length() + 1)
            if not free.Value(i).IsNull()
        ]

        if len(roots) == 1:
            root = roots[0]
        else:
            root = OccurrenceNode(
                name=os.path.splitext(os.path.basename(path))[0],
                transform=identity_transform(),
                children=roots,
            )
            warnings.append(f"{len(roots)} root shapes wrapped in a synthetic container")

        return IngestResult(
            root=root,
            solids=solids,
            source_path=path,
            backend=self.name,
            warnings=warnings,
        )

    def _read_brep(self, path: str) -> IngestResult:
        from OCP.BRep import BRep_Builder
        from OCP.BRepTools import BRepTools

        shape = TopoDS_Shape()
        builder = BRep_Builder()
        if not BRepTools.Read_s(shape, path, builder):
            raise IOError(f"BREP read failed: {path}")
        name = os.path.splitext(os.path.basename(path))[0]
        record = self._solid_record(shape, name)
        solids = [record] if record else []
        root = OccurrenceNode(
            name=name,
            transform=identity_transform(),
            solid_index=0 if record else None,
        )
        return IngestResult(root=root, solids=solids, source_path=path, backend=self.name)

    # --------------------------------------------------------------- records

    # --------------------------------------------------------------- writing

    def write_assembly(self, root: ExportNode, path: str) -> str:
        """Write an ExportNode tree as a STEP assembly, hierarchy and names intact.

        The counterpart of ``read``. Reusing one ExportNode in several places
        makes those appearances of a single definition, so forty bolts leave as
        one product definition and forty usage occurrences -- the same shape the
        reader expects to find coming back in.
        """
        if not _HAVE_OCCT:
            raise RuntimeError(f"OCCT unavailable: {_OCCT_IMPORT_ERROR}")
        from OCP.IFSelect import IFSelect_ReturnStatus
        from OCP.STEPCAFControl import STEPCAFControl_Writer
        from OCP.STEPControl import STEPControl_AsIs
        from OCP.TopLoc import TopLoc_Location
        from OCP.XCAFApp import XCAFApp_Application
        from OCP.gp import gp_Trsf

        app = XCAFApp_Application.GetApplication_s()
        doc = TDocStd_Document(TCollection_ExtendedString("MDTV-XCAF"))
        app.NewDocument(TCollection_ExtendedString("MDTV-XCAF"), doc)
        tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
        labels: dict[int, object] = {}

        def trsf(m: np.ndarray) -> gp_Trsf:
            t = gp_Trsf()
            t.SetValues(*[float(v) for v in np.asarray(m, dtype=float)[:3, :].reshape(12)])
            return t

        def define(node: ExportNode):
            key = id(node)
            if key in labels:
                return labels[key]
            if node.children:
                label = tool.NewShape()
                for instance, child, matrix in node.children:
                    comp = tool.AddComponent(label, define(child),
                                             TopLoc_Location(trsf(matrix)))
                    TDataStd_Name.Set_s(comp, TCollection_ExtendedString(instance))
            else:
                if node.shape is None:
                    raise ValueError(f"leaf {node.name!r} has no geometry to export")
                label = tool.AddShape(node.shape, False)
            TDataStd_Name.Set_s(label, TCollection_ExtendedString(node.name))
            labels[key] = label
            return label

        define(root)
        tool.UpdateAssemblies()

        writer = STEPCAFControl_Writer()
        writer.SetNameMode(True)
        if not writer.Transfer(doc, STEPControl_AsIs):
            raise IOError(f"STEP transfer produced nothing for {path}")
        if writer.Write(path) != IFSelect_ReturnStatus.IFSelect_RetDone:
            raise IOError(f"STEP write failed: {path}")
        return path

    # --------------------------------------------------------------- records

    def solid_record_from_shape(self, shape, name: str) -> SolidRecord | None:
        """Public entry for geometry built in-process rather than read from disk."""
        return self._solid_record(shape, name)

    def _solid_record(self, shape, name: str) -> SolidRecord | None:
        notes: list[str] = []
        valid = True

        solid_map = TopTools_IndexedMapOfShape()
        TopExp.MapShapes_s(shape, TopAbs_ShapeEnum.TopAbs_SOLID, solid_map)
        if solid_map.Extent() == 0:
            # A shell or a sheet body. Mass properties of an open body are
            # meaningless, so the fingerprint must refuse it rather than hash
            # zeros. The design document has no such stage; real STEP needs one.
            valid = False
            notes.append("no closed solid in shape; mass properties are undefined")

        if not BRepCheck_Analyzer(shape).IsValid():
            valid = False
            notes.append("BRepCheck reported an invalid shape")

        vprops = GProp_GProps()
        BRepGProp.VolumeProperties_s(shape, vprops)
        sprops = GProp_GProps()
        BRepGProp.SurfaceProperties_s(shape, sprops)

        volume = float(vprops.Mass())
        area = float(sprops.Mass())
        if volume <= 0.0:
            valid = False
            notes.append(f"non-positive volume ({volume:.6g})")

        matrix = vprops.MatrixOfInertia()
        tensor = tuple(
            tuple(float(matrix.Value(i, j)) for j in (1, 2, 3)) for i in (1, 2, 3)
        )
        mass = MassProperties(
            volume=volume,
            area=area,
            centre_of_mass=_xyz(vprops.CentreOfMass()),
            inertia=tensor,
        )

        return SolidRecord(
            source_name=name,
            mass=mass,
            faces=self._faces(shape),
            topology=self._topology(shape),
            bbox_axis_aligned=self._bbox(shape),
            valid=valid,
            validity_notes=notes,
            handle=shape,
        )

    def _topology(self, shape) -> TopologyCounts:
        def count(kind) -> int:
            m = TopTools_IndexedMapOfShape()
            TopExp.MapShapes_s(shape, kind, m)
            return m.Extent()

        e = TopAbs_ShapeEnum
        return TopologyCounts(
            faces=count(e.TopAbs_FACE),
            edges=count(e.TopAbs_EDGE),
            vertices=count(e.TopAbs_VERTEX),
            shells=count(e.TopAbs_SHELL),
            solids=count(e.TopAbs_SOLID),
            wires=count(e.TopAbs_WIRE),
        )

    def _bbox(self, shape) -> tuple[float, float, float, float, float, float]:
        box = Bnd_Box()
        BRepBndLib.Add_s(shape, box, True)
        if box.IsVoid():
            return (0.0,) * 6
        return tuple(float(v) for v in box.Get())

    def _faces(self, shape) -> list[FaceRecord]:
        records: list[FaceRecord] = []
        fmap = TopTools_IndexedMapOfShape()
        TopExp.MapShapes_s(shape, TopAbs_ShapeEnum.TopAbs_FACE, fmap)
        for i in range(1, fmap.Extent() + 1):
            record = self._face_record(TopoDS.Face_s(fmap.FindKey(i)))
            if record is not None:
                records.append(record)
        return records

    def _face_record(self, face) -> FaceRecord | None:
        adaptor = BRepAdaptor_Surface(face)
        kind = _SURFACE_MAP.get(adaptor.GetType(), "other")

        props = GProp_GProps()
        BRepGProp.SurfaceProperties_s(face, props)
        area = float(props.Mass())

        reversed_face = face.Orientation() == TopAbs_Orientation.TopAbs_REVERSED

        u0, u1 = self._finite(adaptor.FirstUParameter(), adaptor.LastUParameter())
        v0, v1 = self._finite(adaptor.FirstVParameter(), adaptor.LastVParameter())
        um, vm = 0.5 * (u0 + u1), 0.5 * (v0 + v1)

        normal = self._material_normal(adaptor, um, vm, reversed_face)

        if kind == "plane":
            plane = adaptor.Plane()
            return FaceRecord(
                kind=kind, area=area, normal=normal, origin=_xyz(plane.Location())
            )

        if kind == "cylinder":
            cyl = adaptor.Cylinder()
            axis_dir = _xyz(cyl.Axis().Direction())
            axis_pt = _xyz(cyl.Axis().Location())
            return FaceRecord(
                kind=kind,
                area=area,
                axis=axis_dir,
                axis_point=axis_pt,
                radius=float(cyl.Radius()),
                internal=self._is_internal(adaptor, um, vm, axis_dir, axis_pt, normal),
                extent=abs(v1 - v0),
                sweep=abs(u1 - u0),
                normal=normal,
            )

        if kind == "cone":
            cone = adaptor.Cone()
            axis_dir = _xyz(cone.Axis().Direction())
            axis_pt = _xyz(cone.Axis().Location())
            return FaceRecord(
                kind=kind,
                area=area,
                axis=axis_dir,
                axis_point=axis_pt,
                radius=float(cone.RefRadius()),
                half_angle=float(cone.SemiAngle()),
                internal=self._is_internal(adaptor, um, vm, axis_dir, axis_pt, normal),
                extent=abs(v1 - v0),
                sweep=abs(u1 - u0),
                normal=normal,
            )

        if kind == "sphere":
            sph = adaptor.Sphere()
            return FaceRecord(
                kind=kind,
                area=area,
                radius=float(sph.Radius()),
                origin=_xyz(sph.Location()),
                normal=normal,
            )

        if kind == "torus":
            tor = adaptor.Torus()
            return FaceRecord(
                kind=kind,
                area=area,
                axis=_xyz(tor.Axis().Direction()),
                axis_point=_xyz(tor.Axis().Location()),
                radius=float(tor.MinorRadius()),
                normal=normal,
            )

        return FaceRecord(kind=kind, area=area, normal=normal)

    @staticmethod
    def _finite(a: float, b: float, span: float = 1000.0) -> tuple[float, float]:
        """OCCT reports infinite parameter ranges for unbounded surfaces."""
        if not math.isfinite(a):
            a = -span
        if not math.isfinite(b):
            b = span
        return a, b

    @staticmethod
    def _material_normal(adaptor, u: float, v: float, reversed_face: bool):
        """Outward (material-exterior) normal, taken from the real tangent frame
        rather than assumed from the surface definition."""
        try:
            point = gp_Pnt()
            du = gp_Vec()
            dv = gp_Vec()
            adaptor.D1(u, v, point, du, dv)
            n = np.cross([du.X(), du.Y(), du.Z()], [dv.X(), dv.Y(), dv.Z()])
            norm = float(np.linalg.norm(n))
            if norm < 1e-12:
                return None
            n = n / norm
            if reversed_face:
                n = -n
            return (float(n[0]), float(n[1]), float(n[2]))
        except Exception:
            return None

    @staticmethod
    def _is_internal(adaptor, u, v, axis_dir, axis_point, normal) -> bool | None:
        """A bore is a face whose material-outward normal points back toward its
        own axis. Computed rather than read off face orientation, because a
        reversed parametrisation would otherwise invert the answer."""
        if normal is None:
            return None
        try:
            point = gp_Pnt()
            adaptor.D0(u, v, point)
            p = np.array(_xyz(point), dtype=float)
            axis = np.array(axis_dir, dtype=float)
            axis = axis / max(float(np.linalg.norm(axis)), 1e-12)
            radial = p - np.array(axis_point, dtype=float)
            radial = radial - float(np.dot(radial, axis)) * axis
            r_norm = float(np.linalg.norm(radial))
            if r_norm < 1e-9:
                return None
            radial = radial / r_norm
            return bool(float(np.dot(np.asarray(normal, dtype=float), radial)) < 0.0)
        except Exception:
            return None
