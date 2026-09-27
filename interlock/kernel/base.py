"""Kernel-facing data records and the backend protocol.

Everything in this module is plain data. The solid modelling kernel is allowed to
run only while producing these records; no other layer of Interlock may import a
kernel. That boundary is what keeps queries off the modeller (design doc S3).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, Sequence, runtime_checkable

import numpy as np

# Surface classifications we care about. Anything the kernel reports that is not
# in this list is folded into "other" and counted but not detailed (S4).
SURFACE_KINDS = ("plane", "cylinder", "cone", "sphere", "torus", "bspline", "revolution", "other")


@dataclass(frozen=True)
class FaceRecord:
    """One face of one solid, reduced to numbers at ingest time."""

    kind: str
    area: float
    # Orientation of the face relative to the solid's own frame.
    normal: tuple[float, float, float] | None = None
    origin: tuple[float, float, float] | None = None
    # Analytic parameters, populated for the kinds that have them.
    axis: tuple[float, float, float] | None = None
    axis_point: tuple[float, float, float] | None = None
    radius: float | None = None
    half_angle: float | None = None
    # True when the solid's material lies outside the surface, i.e. a bore.
    internal: bool | None = None
    # Length of the face along its axis, for cylinders and cones.
    extent: float | None = None
    # Angular sweep in radians; a full bore is 2*pi, a seam-split half is pi.
    sweep: float | None = None

    def as_row(self) -> dict:
        return {
            "kind": self.kind,
            "area": self.area,
            "normal": self.normal,
            "origin": self.origin,
            "axis": self.axis,
            "axis_point": self.axis_point,
            "radius": self.radius,
            "half_angle": self.half_angle,
            "internal": self.internal,
            "extent": self.extent,
            "sweep": self.sweep,
        }


@dataclass(frozen=True)
class MassProperties:
    """Kernel mass properties, computed at unit density.

    ``inertia`` is the 3x3 tensor taken about the centre of mass, in the solid's
    own frame. Interlock never stores the tensor itself -- only its eigenvalues,
    which are the rotation-invariant part (S5).
    """

    volume: float
    area: float
    centre_of_mass: tuple[float, float, float]
    inertia: tuple[tuple[float, float, float], ...]

    @property
    def inertia_matrix(self) -> np.ndarray:
        return np.asarray(self.inertia, dtype=float).reshape(3, 3)


@dataclass(frozen=True)
class TopologyCounts:
    faces: int
    edges: int
    vertices: int
    shells: int
    solids: int
    wires: int = 0

    def as_tuple(self) -> tuple[int, ...]:
        return (self.faces, self.edges, self.vertices, self.shells, self.solids)


@dataclass
class SolidRecord:
    """The per-solid record produced at ingest (design doc S4 table).

    ``valid`` is false when the kernel handed back something that is not a closed
    solid -- a shell, a compound of sheets, a self-intersecting body. Mass
    properties of a non-solid are meaningless, so the fingerprint must refuse to
    consume it rather than silently hashing zeros. The design document does not
    have this stage; it is required in practice because a large fraction of real
    STEP files arrive as shells.
    """

    source_name: str
    mass: MassProperties
    faces: list[FaceRecord]
    topology: TopologyCounts
    bbox_axis_aligned: tuple[float, float, float, float, float, float]
    valid: bool = True
    validity_notes: list[str] = field(default_factory=list)
    # Opaque, backend-specific handle. Used only to hand geometry to the blob
    # store and to the interference stage; never read by the query layer.
    handle: object | None = None

    @property
    def cylinders(self) -> list[FaceRecord]:
        return [f for f in self.faces if f.kind == "cylinder"]

    @property
    def planes(self) -> list[FaceRecord]:
        return [f for f in self.faces if f.kind == "plane"]

    def face_histogram(self) -> dict[str, int]:
        hist = {k: 0 for k in SURFACE_KINDS}
        for f in self.faces:
            hist[f.kind if f.kind in hist else "other"] += 1
        return hist


@dataclass
class ExportNode:
    """One node of a tree on its way *out* to an exchange file.

    The mirror of ``OccurrenceNode``: that one carries what a file yielded, this
    one carries what a file is about to be given. A leaf holds a kernel shape
    (read back from the blob store); a node holds named, placed children.

    Exporting is not modelling. The system still never creates geometry -- it
    hands back the solids it was given, arranged the way the occurrence table
    says they are arranged, which is exactly what the content-addressed store
    exists to make possible.
    """

    name: str
    shape: object | None = None
    children: list[tuple[str, "ExportNode", np.ndarray]] = field(default_factory=list)

    def add(self, instance: str, node: "ExportNode", transform: np.ndarray) -> "ExportNode":
        self.children.append((instance, node, np.asarray(transform, dtype=float)))
        return self

    def walk(self):
        yield self
        for _, child, _ in self.children:
            yield from child.walk()


@dataclass
class OccurrenceNode:
    """One node of the tree a source file yields.

    ``transform`` is the placement of this node *relative to its parent*, as a
    4x4 homogeneous matrix. Relative rather than absolute, because that is what
    makes a subassembly's hash independent of where the parent puts it (S6).
    """

    name: str
    transform: np.ndarray
    children: list["OccurrenceNode"] = field(default_factory=list)
    solid_index: int | None = None
    quantity: int = 1
    # The name of this particular appearance ("bolt_17"), as opposed to ``name``,
    # which is the name of the part definition it refers to ("BOLT-M6x20").
    instance_name: str = ""
    # Identifies the part definition this node refers to. Every appearance of the
    # same definition carries the same key, which is what lets ingest create one
    # revision for forty bolts instead of forty.
    definition_key: str = ""

    def walk(self):
        yield self
        for child in self.children:
            yield from child.walk()

    def leaf_count(self) -> int:
        if not self.children:
            return 1
        return sum(c.leaf_count() for c in self.children)


@dataclass
class IngestResult:
    """What one source file yields: a tree, the distinct solids it references,
    and whatever the reader wants to say about how the read went."""

    root: OccurrenceNode
    solids: list[SolidRecord]
    source_path: str
    backend: str
    warnings: list[str] = field(default_factory=list)

    def solid_for(self, node: OccurrenceNode) -> SolidRecord | None:
        if node.solid_index is None:
            return None
        return self.solids[node.solid_index]


@runtime_checkable
class KernelBackend(Protocol):
    """The only interface through which Interlock may touch a solid modeller."""

    name: str

    def available(self) -> bool:
        """True when this backend can actually run on this machine."""

    def supported_suffixes(self) -> Sequence[str]:
        ...

    def read(self, path: str) -> IngestResult:
        """Parse one source file into a tree plus distinct solids."""


def identity_transform() -> np.ndarray:
    return np.eye(4, dtype=float)


def make_transform(rotation: np.ndarray | None = None,
                   translation: Sequence[float] | None = None) -> np.ndarray:
    m = np.eye(4, dtype=float)
    if rotation is not None:
        m[:3, :3] = np.asarray(rotation, dtype=float).reshape(3, 3)
    if translation is not None:
        m[:3, 3] = np.asarray(translation, dtype=float).reshape(3)
    return m
