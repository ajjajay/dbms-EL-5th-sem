"""Feature extraction: bores, hole patterns, mounting planes, and registration.

Design document section 4 walks each solid face by face and keeps the cylinders,
because a through-hole is the common mechanical interface. Two practical
corrections are applied here.

First, a bore does not arrive as one face. Exchange formats routinely split a
cylindrical surface at its seam, so the same hole appears as two half-cylinders,
and a counterbore appears as cylinder / cone / cylinder at different radii.
Counting raw cylindrical faces therefore counts the exporter's habits, not the
part. Faces are merged onto their underlying axis before anything downstream
looks at them.

Second, section 8 describes matching a hole pattern by comparing pairwise
spacings. A multiset of pairwise distances does not determine a point set, and it
is identical for a pattern and its mirror image. Spacing comparison is kept as a
cheap filter, and the match is then confirmed by actually solving for the
placement -- which the document treats as an afterthought but which is the step
that makes the answer trustworthy.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

AXIS_ANGLE_TOLERANCE = math.radians(0.5)
AXIS_DISTANCE_TOLERANCE = 1e-3
RADIUS_TOLERANCE = 1e-3


def canonical_direction(d: np.ndarray) -> np.ndarray:
    """A direction and its negative describe the same axis. Pick one."""
    d = np.asarray(d, dtype=float)
    n = float(np.linalg.norm(d))
    if n < 1e-15:
        return np.array([0.0, 0.0, 1.0])
    d = d / n
    for component in d:
        if abs(component) > 1e-9:
            return d if component > 0 else -d
    return d


def point_on_axis_nearest(axis_point, direction, target) -> np.ndarray:
    """The point of a line closest to a target point.

    Used to give each bore a single representative position that does not depend
    on where the exporter happened to put the cylinder's origin.
    """
    p = np.asarray(axis_point, dtype=float)
    d = canonical_direction(direction)
    t = np.asarray(target, dtype=float)
    return p + float(np.dot(t - p, d)) * d


@dataclass
class Bore:
    """One physical hole, assembled from however many faces describe it."""

    axis: np.ndarray
    axis_point: np.ndarray       # closest point on the axis to the part centroid
    radius: float
    depth: float
    sweep: float                 # total angular sweep; 2*pi is a full bore
    face_count: int
    through: bool = False
    counterbore_radii: tuple[float, ...] = ()
    chamfered: bool = False
    # Depth summed over every radius on this axis: the narrow section plus any
    # counterbore. ``depth`` alone is only the narrow section, which is not what
    # decides whether the hole goes all the way through the part.
    total_depth: float = 0.0

    @property
    def diameter(self) -> float:
        return 2.0 * self.radius

    @property
    def complete(self) -> bool:
        """A sweep short of a full turn means a slot or a broken-out hole, not a
        round bore, and it must not be matched against a fastener."""
        return self.sweep >= 2.0 * math.pi - 1e-3


@dataclass
class MountingPlane:
    """A planar face large enough to be a seating surface."""

    normal: np.ndarray
    origin: np.ndarray
    area: float


@dataclass
class HolePattern:
    """A set of parallel bores of equal radius: a candidate interface."""

    bores: list[Bore]
    axis: np.ndarray
    radius: float
    centroid: np.ndarray
    plane_normal: np.ndarray | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.bores)

    def positions(self) -> np.ndarray:
        return np.vstack([b.axis_point for b in self.bores])

    def pairwise_spacings(self) -> np.ndarray:
        p = self.positions()
        if len(p) < 2:
            return np.zeros(0)
        d = np.linalg.norm(p[:, None, :] - p[None, :, :], axis=-1)
        return np.sort(d[np.triu_indices(len(p), k=1)])


def merge_bores(faces, centroid) -> list[Bore]:
    """Collapse cylindrical faces onto the distinct axes they lie on.

    Faces sharing an axis line and a radius are one hole however the exporter
    split them; faces sharing an axis line at different radii are one
    counterbored hole, recorded at its smallest radius with the larger ones kept
    as a note.
    """
    centroid = np.asarray(centroid, dtype=float)
    cylinders = [f for f in faces if f.kind == "cylinder" and f.internal]
    cones = [f for f in faces if f.kind == "cone" and f.internal]

    groups: list[dict] = []
    for face in cylinders:
        axis = canonical_direction(np.asarray(face.axis, dtype=float))
        anchor = point_on_axis_nearest(face.axis_point, axis, centroid)
        radius = float(face.radius or 0.0)

        target = None
        for group in groups:
            if _angle_between(axis, group["axis"]) > AXIS_ANGLE_TOLERANCE:
                continue
            if np.linalg.norm(anchor - group["anchor"]) > AXIS_DISTANCE_TOLERANCE:
                continue
            target = group
            break

        if target is None:
            groups.append(
                {
                    "axis": axis,
                    "anchor": anchor,
                    "radii": {},
                    "faces": 0,
                }
            )
            target = groups[-1]

        key = round(radius / RADIUS_TOLERANCE)
        entry = target["radii"].setdefault(key, {"radius": radius, "sweep": 0.0, "depth": 0.0})
        entry["sweep"] += float(face.sweep or 0.0)
        entry["depth"] = max(entry["depth"], float(face.extent or 0.0))
        target["faces"] += 1

    cone_axes = [
        (canonical_direction(np.asarray(c.axis, dtype=float)),
         point_on_axis_nearest(c.axis_point, c.axis, centroid))
        for c in cones
    ]

    bores: list[Bore] = []
    for group in groups:
        radii = sorted(group["radii"].values(), key=lambda e: e["radius"])
        primary = radii[0]
        chamfered = any(
            _angle_between(group["axis"], a) <= AXIS_ANGLE_TOLERANCE
            and np.linalg.norm(group["anchor"] - p) <= AXIS_DISTANCE_TOLERANCE
            for a, p in cone_axes
        )
        bores.append(
            Bore(
                axis=group["axis"],
                axis_point=group["anchor"],
                radius=primary["radius"],
                depth=primary["depth"],
                total_depth=sum(e["depth"] for e in radii),
                sweep=primary["sweep"],
                face_count=group["faces"],
                counterbore_radii=tuple(e["radius"] for e in radii[1:]),
                chamfered=chamfered,
            )
        )
    return bores


def mounting_planes(faces, min_area: float = 0.0) -> list[MountingPlane]:
    planes = []
    for f in faces:
        if f.kind != "plane" or f.normal is None or f.area < min_area:
            continue
        planes.append(
            MountingPlane(
                normal=canonical_direction(np.asarray(f.normal, dtype=float)),
                origin=np.asarray(f.origin, dtype=float),
                area=float(f.area),
            )
        )
    return sorted(planes, key=lambda p: -p.area)


def hole_patterns(bores: list[Bore], min_count: int = 2) -> list[HolePattern]:
    """Group parallel bores of equal radius. Each group is a candidate interface."""
    patterns: list[HolePattern] = []
    used: set[int] = set()

    for i, bore in enumerate(bores):
        if i in used or not bore.complete:
            continue
        members = [bore]
        used.add(i)
        for j in range(i + 1, len(bores)):
            if j in used:
                continue
            other = bores[j]
            if not other.complete:
                continue
            if _angle_between(bore.axis, other.axis) > AXIS_ANGLE_TOLERANCE:
                continue
            if abs(other.radius - bore.radius) > RADIUS_TOLERANCE:
                continue
            members.append(other)
            used.add(j)

        if len(members) < min_count:
            continue
        positions = np.vstack([m.axis_point for m in members])
        patterns.append(
            HolePattern(
                bores=members,
                axis=bore.axis,
                radius=bore.radius,
                centroid=positions.mean(axis=0),
            )
        )

    return sorted(patterns, key=lambda p: (-p.count, p.radius))


def feature_chirality(bores: list[Bore], centroid) -> int:
    """Handedness derived from the hole pattern, computed without a frame.

    Four points ordered by a key that is invariant under both rotation and
    reflection will appear in the same order for a part and for its mirror. The
    sign of the determinant they span therefore flips under reflection, which is
    exactly the signal needed. Ties in the ordering key, or four coplanar points,
    mean the construction cannot decide, and it says so with a zero.

    This is the fallback for parts whose mass distribution is too symmetric for
    the third-moment test -- a symmetric plate carrying an asymmetric hole
    pattern, for instance.
    """
    if len(bores) < 4:
        return 0
    centroid = np.asarray(centroid, dtype=float)
    points = np.vstack([b.axis_point for b in bores])

    radial = np.linalg.norm(points - centroid, axis=1)
    mutual = np.linalg.norm(points[:, None, :] - points[None, :, :], axis=-1).sum(axis=1)

    # Ordering keys must survive being computed by a different exporter, so they
    # are coarse: radii to 10 micrometres, positions to a thousandth of the
    # pattern's own size. Keys at full float precision made the ordering (and so
    # the sign) depend on the last digits of each tool's cylinder parameters.
    span = max(float(np.ptp(points, axis=0).max()), 1.0)
    quantum = 1e-3 * span
    keys = [
        (round(b.radius / 0.01), round(float(r) / quantum), round(float(m) / (quantum * len(bores))), round(b.depth / 0.05))
        for b, r, m in zip(bores, radial, mutual)
    ]

    order = sorted(range(len(bores)), key=lambda k: keys[k])
    chosen = order[:4]
    distinct = len(set(keys[k] for k in chosen)) == 4
    # The fourth pick must also differ from the fifth, or a different exporter
    # could legitimately choose a different fourth point.
    boundary_clear = len(order) == 4 or keys[order[3]] != keys[order[4]]
    if not (distinct and boundary_clear):
        return 0  # the ordering is not unique, so neither is the answer

    p = points[chosen]
    determinant = float(np.linalg.det(np.vstack([p[1] - p[0], p[2] - p[0], p[3] - p[0]])))
    scale = float(np.linalg.norm(p - p.mean(axis=0), axis=1).mean()) ** 3
    if scale <= 0 or abs(determinant) / scale < 1e-6:
        return 0  # coplanar: no handedness to read
    return 1 if determinant > 0 else -1


# --------------------------------------------------------------- registration


@dataclass
class Registration:
    rotation: np.ndarray        # (3, 3), proper rotation
    translation: np.ndarray     # (3,)
    rmsd: float
    permutation: tuple[int, ...]
    reflected: bool = False

    def transform(self) -> np.ndarray:
        m = np.eye(4)
        m[:3, :3] = self.rotation
        m[:3, 3] = self.translation
        return m

    def apply(self, points: np.ndarray) -> np.ndarray:
        return np.asarray(points, dtype=float) @ self.rotation.T + self.translation


def kabsch(source: np.ndarray, target: np.ndarray, allow_reflection: bool = False):
    """Least-squares rigid alignment of two ordered point sets.

    Returns the rotation and translation carrying ``source`` onto ``target``,
    with the reflection branch of the SVD suppressed unless explicitly allowed.
    Suppressing it is what makes the result a physical placement rather than an
    impossible one, and it is why a mirrored hole pattern fails to register.
    """
    p = np.asarray(source, dtype=float)
    q = np.asarray(target, dtype=float)
    if p.shape != q.shape or p.ndim != 2 or p.shape[1] != 3:
        raise ValueError("kabsch needs two matching (n, 3) arrays")

    pc = p.mean(axis=0)
    qc = q.mean(axis=0)
    h = (p - pc).T @ (q - qc)
    u, _, vt = np.linalg.svd(h)
    d = float(np.sign(np.linalg.det(vt.T @ u.T)))
    reflected = d < 0

    correction = np.eye(3)
    if reflected and not allow_reflection:
        correction[2, 2] = -1.0
    rotation = vt.T @ correction @ u.T
    translation = qc - rotation @ pc

    aligned = p @ rotation.T + translation
    rmsd = float(np.sqrt(((aligned - q) ** 2).sum(axis=1).mean()))
    return Registration(
        rotation=rotation,
        translation=translation,
        rmsd=rmsd,
        permutation=tuple(range(len(p))),
        reflected=reflected and not allow_reflection,
    )


def best_registration(source: np.ndarray, target: np.ndarray, max_permutations: int = 5040):
    """Register two unordered point sets by searching correspondences.

    Hole patterns arrive in whatever order the face walk produced, so a
    correspondence has to be found before the alignment means anything. Candidate
    orderings are generated from a rotation-invariant signature first, which
    reduces the search to a handful of orderings for the patterns that occur in
    practice; an exhaustive search is the fallback for small counts.
    """
    import itertools

    p = np.asarray(source, dtype=float)
    q = np.asarray(target, dtype=float)
    if p.shape != q.shape:
        return None
    n = len(p)
    if n == 0:
        return None
    if n == 1:
        return kabsch(p, q)

    def signature(points: np.ndarray) -> list[tuple]:
        centre = points.mean(axis=0)
        radial = np.linalg.norm(points - centre, axis=1)
        mutual = np.sort(
            np.linalg.norm(points[:, None, :] - points[None, :, :], axis=-1), axis=1
        )
        return [
            (round(float(r), 6), tuple(np.round(m, 6))) for r, m in zip(radial, mutual)
        ]

    sig_p = signature(p)
    sig_q = signature(q)

    buckets: dict[tuple, list[int]] = {}
    for index, key in enumerate(sig_q):
        buckets.setdefault(key, []).append(index)

    candidates: list[tuple[int, ...]] = []
    if all(key in buckets for key in sig_p) and sorted(sig_p) == sorted(sig_q):
        choices = [buckets[key] for key in sig_p]
        total = 1
        for c in choices:
            total *= len(c)
        if total <= max_permutations:
            for combo in itertools.product(*choices):
                if len(set(combo)) == n:
                    candidates.append(combo)

    if not candidates:
        if math.factorial(n) > max_permutations:
            return None
        candidates = list(itertools.permutations(range(n)))

    best = None
    for perm in candidates:
        result = kabsch(p, q[list(perm)])
        result.permutation = perm
        if best is None or result.rmsd < best.rmsd:
            best = result
    return best


def _angle_between(a: np.ndarray, b: np.ndarray) -> float:
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    na = float(np.linalg.norm(a))
    nb = float(np.linalg.norm(b))
    if na < 1e-15 or nb < 1e-15:
        return math.pi
    cos = float(np.clip(np.dot(a, b) / (na * nb), -1.0, 1.0))
    return math.acos(abs(cos))
