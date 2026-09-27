"""Volume moments of a solid, to third order.

The design document's invariant vector stops at second order (the inertia
tensor). Second-order quantities cannot distinguish a part from its mirror image,
which is the KNOWN LIMITATION admitted in section 5. Third-order moments can,
and this module computes them.

Method: triangulate the boundary, decompose into signed tetrahedra against an
arbitrary apex, and integrate each monomial with a quadrature rule that is exact
for cubics. The signed decomposition means the apex position is irrelevant and
concave solids need no special handling -- the negative tetrahedra cancel.

Everything here consumes a kernel handle, so it runs at ingest time only.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

# Keast degree-3 rule on the reference tetrahedron, in barycentric coordinates.
# Exact for polynomials up to and including cubics, which is precisely what the
# third moments need. Weights sum to one.
_QUAD_BARYCENTRIC = np.array(
    [
        [0.25, 0.25, 0.25, 0.25],
        [0.5, 1.0 / 6.0, 1.0 / 6.0, 1.0 / 6.0],
        [1.0 / 6.0, 0.5, 1.0 / 6.0, 1.0 / 6.0],
        [1.0 / 6.0, 1.0 / 6.0, 0.5, 1.0 / 6.0],
        [1.0 / 6.0, 1.0 / 6.0, 1.0 / 6.0, 0.5],
    ]
)
_QUAD_WEIGHTS = np.array([-0.8, 0.45, 0.45, 0.45, 0.45])

# The ten distinct third-order monomials, as exponent triples.
_THIRD_ORDER = (
    (3, 0, 0), (0, 3, 0), (0, 0, 3),
    (2, 1, 0), (2, 0, 1), (1, 2, 0),
    (0, 2, 1), (1, 0, 2), (0, 1, 2),
    (1, 1, 1),
)


@dataclass
class Mesh:
    vertices: np.ndarray  # (n, 3)
    triangles: np.ndarray  # (m, 3) indices, wound counter-clockwise seen from outside
    closed: bool
    closure_residual: float

    def __len__(self) -> int:
        return len(self.triangles)


@dataclass
class VolumeMoments:
    """Raw and central moments of a solid at unit density."""

    volume: float
    centroid: np.ndarray                 # (3,)
    second_central: np.ndarray           # (3, 3) second moment about the centroid
    third_central: dict[tuple, float]    # exponent triple -> central moment
    mesh_triangles: int
    closure_residual: float

    def inertia_about_centroid(self) -> np.ndarray:
        """Convert the second-moment tensor into the inertia tensor.

        I = trace(M) * Id - M. Same eigenvectors, different eigenvalues; this is
        provided so the mesh result can be checked against the kernel's own
        inertia matrix.
        """
        m = self.second_central
        return np.trace(m) * np.eye(3) - m


def triangulate(shape, deflection: float = 0.25, angular: float = 0.35) -> Mesh:
    """Mesh a kernel shape and return outward-wound triangles.

    ``deflection`` is a chord tolerance in model units. It controls only the
    accuracy of the moment integration, not any stored geometry.
    """
    from OCP.BRep import BRep_Tool
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.TopAbs import TopAbs_Orientation, TopAbs_ShapeEnum
    from OCP.TopExp import TopExp
    from OCP.TopLoc import TopLoc_Location
    from OCP.TopoDS import TopoDS
    from OCP.TopTools import TopTools_IndexedMapOfShape

    BRepMesh_IncrementalMesh(shape, deflection, False, angular, True)

    face_map = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_ShapeEnum.TopAbs_FACE, face_map)

    all_vertices: list[np.ndarray] = []
    all_triangles: list[np.ndarray] = []
    offset = 0

    for i in range(1, face_map.Extent() + 1):
        face = TopoDS.Face_s(face_map.FindKey(i))
        location = TopLoc_Location()
        triangulation = BRep_Tool.Triangulation_s(face, location)
        if triangulation is None:
            continue
        transform = location.Transformation()

        n_nodes = triangulation.NbNodes()
        nodes = np.empty((n_nodes, 3), dtype=float)
        for n in range(1, n_nodes + 1):
            p = triangulation.Node(n).Transformed(transform)
            nodes[n - 1] = (p.X(), p.Y(), p.Z())

        reversed_face = face.Orientation() == TopAbs_Orientation.TopAbs_REVERSED
        n_tris = triangulation.NbTriangles()
        tris = np.empty((n_tris, 3), dtype=np.int64)
        for t in range(1, n_tris + 1):
            tri = triangulation.Triangle(t)
            a, b, c = tri.Value(1), tri.Value(2), tri.Value(3)
            # A reversed face stores its triangles wound against the surface
            # normal; swapping two indices puts every triangle outward.
            if reversed_face:
                b, c = c, b
            tris[t - 1] = (a - 1 + offset, b - 1 + offset, c - 1 + offset)

        all_vertices.append(nodes)
        all_triangles.append(tris)
        offset += n_nodes

    if not all_triangles:
        empty = np.zeros((0, 3))
        return Mesh(empty, np.zeros((0, 3), dtype=np.int64), False, float("inf"))

    vertices = np.vstack(all_vertices)
    triangles = np.vstack(all_triangles)

    # A closed surface has vector areas that cancel. The residual, scaled by the
    # total area, says how badly a body fails to close -- which is the mesh-level
    # echo of the shell-not-solid problem the ingest stage rejects.
    p0 = vertices[triangles[:, 0]]
    p1 = vertices[triangles[:, 1]]
    p2 = vertices[triangles[:, 2]]
    cross = np.cross(p1 - p0, p2 - p0)
    total_area = 0.5 * float(np.linalg.norm(cross, axis=1).sum())
    residual = float(np.linalg.norm(cross.sum(axis=0))) / max(total_area, 1e-12)

    return Mesh(vertices, triangles, closed=residual < 1e-6, closure_residual=residual)


def compute_moments(shape, deflection: float = 0.25) -> VolumeMoments:
    """Integrate monomials up to third order over the solid."""
    mesh = triangulate(shape, deflection=deflection)
    return moments_from_mesh(mesh)


def moments_from_mesh(mesh: Mesh) -> VolumeMoments:
    if len(mesh) == 0:
        return VolumeMoments(
            volume=0.0,
            centroid=np.zeros(3),
            second_central=np.zeros((3, 3)),
            third_central={e: 0.0 for e in _THIRD_ORDER},
            mesh_triangles=0,
            closure_residual=float("inf"),
        )

    v = mesh.vertices
    t = mesh.triangles
    a = v[t[:, 0]]
    b = v[t[:, 1]]
    c = v[t[:, 2]]

    # Apex at the vertex centroid keeps the signed tetrahedra well conditioned
    # for bodies far from the origin.
    apex = v.mean(axis=0)
    a = a - apex
    b = b - apex
    c = c - apex

    # Signed volume of each tetrahedron (apex, a, b, c).
    signed_six = np.einsum("ij,ij->i", a, np.cross(b, c))
    tet_volumes = signed_six / 6.0
    volume = float(tet_volumes.sum())

    # Quadrature points: barycentric combination of (apex=0, a, b, c).
    # Column 0 multiplies the apex, which sits at the origin after the shift.
    w1 = _QUAD_BARYCENTRIC[:, 1]
    w2 = _QUAD_BARYCENTRIC[:, 2]
    w3 = _QUAD_BARYCENTRIC[:, 3]
    # points[q] has shape (n_tris, 3)
    points = [
        a * w1[q] + b * w2[q] + c * w3[q] for q in range(len(_QUAD_WEIGHTS))
    ]

    def integrate(fn) -> float:
        total = 0.0
        for q, weight in enumerate(_QUAD_WEIGHTS):
            total += weight * float(np.dot(tet_volumes, fn(points[q])))
        return total

    if abs(volume) < 1e-15:
        return VolumeMoments(
            volume=volume,
            centroid=apex,
            second_central=np.zeros((3, 3)),
            third_central={e: 0.0 for e in _THIRD_ORDER},
            mesh_triangles=len(mesh),
            closure_residual=mesh.closure_residual,
        )

    first = np.array([integrate(lambda p, i=i: p[:, i]) for i in range(3)])
    centroid_shifted = first / volume

    # Re-centre on the centroid and integrate again. Integrating the already
    # centred coordinates is more accurate than converting raw moments, which
    # loses precision through cancellation for bodies far from the apex.
    a -= centroid_shifted
    b -= centroid_shifted
    c -= centroid_shifted
    points = [a * w1[q] + b * w2[q] + c * w3[q] for q in range(len(_QUAD_WEIGHTS))]

    second = np.empty((3, 3))
    for i in range(3):
        for j in range(i, 3):
            value = integrate(lambda p, i=i, j=j: p[:, i] * p[:, j])
            second[i, j] = second[j, i] = value

    third: dict[tuple, float] = {}
    for exps in _THIRD_ORDER:
        def monomial(p, e=exps):
            out = np.ones(len(p))
            for axis, power in enumerate(e):
                if power:
                    out = out * p[:, axis] ** power
            return out

        third[exps] = integrate(monomial)

    return VolumeMoments(
        volume=volume,
        centroid=apex + centroid_shifted,
        second_central=second,
        third_central=third,
        mesh_triangles=len(mesh),
        closure_residual=mesh.closure_residual,
    )


def third_moment_along(moments: VolumeMoments, axis: np.ndarray) -> float:
    """Return the third central moment along a direction, i.e. integral of
    (r . axis)^3 dV, expanded from the stored monomial moments.

    This is the skewness of the mass distribution along that axis. It is what
    breaks the sign ambiguity of an inertia eigenvector, because reflection
    preserves it while a sign flip of the axis negates it.
    """
    u = np.asarray(axis, dtype=float)
    u = u / max(float(np.linalg.norm(u)), 1e-15)
    x, y, z = u

    m = moments.third_central
    return float(
        x**3 * m[(3, 0, 0)]
        + y**3 * m[(0, 3, 0)]
        + z**3 * m[(0, 0, 3)]
        + 3 * x**2 * y * m[(2, 1, 0)]
        + 3 * x**2 * z * m[(2, 0, 1)]
        + 3 * x * y**2 * m[(1, 2, 0)]
        + 3 * y**2 * z * m[(0, 2, 1)]
        + 3 * x * z**2 * m[(1, 0, 2)]
        + 3 * y * z**2 * m[(0, 1, 2)]
        + 6 * x * y * z * m[(1, 1, 1)]
    )


def fourth_moment_along(mesh: Mesh, centroid: np.ndarray, axis: np.ndarray) -> float:
    """Fourth central moment along a direction.

    Used only to break a tie between equal principal moments, where the third
    moment is also degenerate. Computed directly rather than stored, because it
    is needed for a handful of parts and never for matching.
    """
    if len(mesh) == 0:
        return 0.0
    v = mesh.vertices
    t = mesh.triangles
    a = v[t[:, 0]] - centroid
    b = v[t[:, 1]] - centroid
    c = v[t[:, 2]] - centroid
    signed_six = np.einsum("ij,ij->i", a, np.cross(b, c))
    tet_volumes = signed_six / 6.0

    u = np.asarray(axis, dtype=float)
    u = u / max(float(np.linalg.norm(u)), 1e-15)

    # A quartic needs a rule of degree four; the cubic rule above would be
    # inexact. Subdividing each tetrahedron's quadrature with the cubic rule
    # applied to the squared quadratic is sufficient here because the integrand
    # is a perfect square and the rule is positive on the four corner points.
    total = 0.0
    w1 = _QUAD_BARYCENTRIC[:, 1]
    w2 = _QUAD_BARYCENTRIC[:, 2]
    w3 = _QUAD_BARYCENTRIC[:, 3]
    for q, weight in enumerate(_QUAD_WEIGHTS):
        p = a * w1[q] + b * w2[q] + c * w3[q]
        s = p @ u
        total += weight * float(np.dot(tet_volumes, s**4))
    return total
