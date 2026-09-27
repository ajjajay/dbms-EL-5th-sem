"""The invariant vector, its canonical frame, and chirality.

Design document section 5 proposes hashing volume, area, sorted principal
moments, a surface histogram and topology counts. Three things are wrong with
that as stated, and this module is where they are fixed.

1. The quantities have mixed dimensions. Volume scales as L^3, area as L^2,
   inertia as L^5. Rounding them all to "a fixed decimal precision" applies a
   different relative tolerance to each. They are nondimensionalised here, so
   one tolerance means one thing.

2. The principal frame is undefined whenever two principal moments are equal,
   which is the normal case for mechanical parts -- washers, shafts, flanges,
   square plates. Anything derived from that frame is then arbitrary. The
   degeneracy is detected, an attempt is made to break it with higher moments,
   and what cannot be broken is reported rather than silently used.

3. Every second-order quantity is mirror-symmetric, so a left-hand and a
   right-hand bracket collide. The document lists this as OPEN. Third moments
   resolve it: reflection preserves the moment along a reflected axis, so after
   the eigenvector signs are fixed by skewness the frame's handedness flips.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from .moments import VolumeMoments, fourth_moment_along, third_moment_along

# Relative gap below which two principal moments count as equal.
DEGENERACY_TOLERANCE = 1e-4
# Normalised skewness below which an eigenvector's sign is undetermined.
#
# This was 1e-6, which is far below the noise floor and made chirality a coin
# toss on near-symmetric parts. Measured on the NIST test models (the same part
# modelled independently in different CAD systems), an axis with normalised
# skewness of ~5e-4 changed sign between exporters and flipped the frame's
# handedness, so the same part came back with chirality -1 from one tool and +1
# from another and the bounded match vetoed it. Mesh discretisation alone puts
# roughly deflection/char_length (~4e-4) of noise on this quantity, and real
# modelling differences between tools are larger. 5e-3 clears that floor while
# still resolving the deliberately chiral demo parts (skewness ~1e-2 to 2e-1).
# Below it the axis is reported as undetermined and chirality falls back to the
# hole pattern, or to 0 (unknown), which the match treats as a wildcard.
SKEWNESS_TOLERANCE = 5e-3
# Normalised fourth-moment spread below which a degenerate subspace is
# genuinely rotationally symmetric and cannot be canonicalised at all.
SUBSPACE_TOLERANCE = 1e-6


@dataclass
class PrincipalFrame:
    """A canonical orthonormal frame for a solid, plus an honest account of how
    well determined it actually is."""

    axes: np.ndarray                      # (3, 3), columns are e1, e2, e3
    second_moments: np.ndarray            # (3,) ascending eigenvalues
    degenerate_pairs: tuple[tuple[int, int], ...] = ()
    signs_determined: tuple[bool, bool, bool] = (True, True, True)
    rotationally_symmetric: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def stable(self) -> bool:
        """True when the frame is uniquely determined by the geometry, so
        anything expressed in it (a principal bounding box, a handedness) is
        reproducible."""
        return (
            not self.degenerate_pairs
            and all(self.signs_determined)
            and not self.rotationally_symmetric
        )

    def handedness(self) -> int:
        if not all(self.signs_determined) or self.rotationally_symmetric:
            return 0
        return 1 if float(np.linalg.det(self.axes)) > 0 else -1


@dataclass
class InvariantVector:
    """Pose-independent description of a shape.

    ``char_length`` carries the size and is dimensional; every other continuous
    component is dimensionless, so a single relative tolerance on the first and a
    single absolute tolerance on the rest is a coherent comparison rule.
    """

    char_length: float          # V^(1/3), in model units
    sphericity: float           # (pi^(1/3))(6V)^(2/3) / A, in (0, 1]
    j1: float                   # normalised second moments, ascending
    j2: float
    j3: float
    chirality: int              # -1, 0 (achiral or undetermined), +1
    face_histogram: dict[str, int]
    topology: tuple[int, ...]
    frame_stable: bool
    principal_extents: tuple[float, float, float] | None = None

    def continuous(self) -> np.ndarray:
        """The part of the vector that varies continuously with the shape."""
        return np.array([self.char_length, self.sphericity, self.j1, self.j2, self.j3])

    def dimensionless(self) -> np.ndarray:
        return np.array([self.sphericity, self.j1, self.j2, self.j3])

    def volume(self) -> float:
        return self.char_length**3

    def as_row(self) -> dict:
        return {
            "char_length": self.char_length,
            "sphericity": self.sphericity,
            "j1": self.j1,
            "j2": self.j2,
            "j3": self.j3,
            "chirality": self.chirality,
            "frame_stable": int(self.frame_stable),
        }


def principal_frame(moments: VolumeMoments, mesh=None) -> PrincipalFrame:
    """Build a canonical frame from the second and third moments.

    The eigenvectors of the second-moment tensor give the axes up to sign and,
    when eigenvalues repeat, up to a rotation inside the repeated subspace. Both
    ambiguities are resolved here or reported.
    """
    notes: list[str] = []
    tensor = np.asarray(moments.second_central, dtype=float)
    eigenvalues, eigenvectors = np.linalg.eigh(tensor)

    order = np.argsort(eigenvalues)
    eigenvalues = eigenvalues[order]
    axes = eigenvectors[:, order]

    scale = float(abs(eigenvalues).max())
    degenerate: list[tuple[int, int]] = []
    if scale > 0:
        for k in range(2):
            if abs(eigenvalues[k + 1] - eigenvalues[k]) / scale < DEGENERACY_TOLERANCE:
                degenerate.append((k, k + 1))

    rotationally_symmetric = False
    if degenerate and mesh is not None:
        axes, resolved, note = _break_degeneracy(
            axes, degenerate, moments, mesh
        )
        if note:
            notes.append(note)
        if resolved:
            degenerate = []
        else:
            rotationally_symmetric = True
    elif degenerate:
        notes.append("degenerate principal moments and no mesh available to break them")
        rotationally_symmetric = True

    # Fix each axis sign by the skewness of the mass distribution along it.
    volume = abs(moments.volume)
    normaliser = volume**2 if volume > 0 else 1.0
    signs_determined = []
    for k in range(3):
        skew = third_moment_along(moments, axes[:, k]) / normaliser
        if abs(skew) < SKEWNESS_TOLERANCE:
            signs_determined.append(False)
        else:
            signs_determined.append(True)
            if skew < 0:
                axes[:, k] = -axes[:, k]

    if not all(signs_determined):
        undetermined = [k + 1 for k, ok in enumerate(signs_determined) if not ok]
        notes.append(
            f"axis sign undetermined for {undetermined}: the body is symmetric "
            f"about {'that plane' if len(undetermined) == 1 else 'those planes'}"
        )

    return PrincipalFrame(
        axes=axes,
        second_moments=eigenvalues,
        degenerate_pairs=tuple(degenerate),
        signs_determined=tuple(signs_determined),
        rotationally_symmetric=rotationally_symmetric,
        notes=notes,
    )


def _break_degeneracy(axes, degenerate, moments, mesh):
    """Try to canonicalise a repeated-eigenvalue subspace with fourth moments.

    Inside a two-dimensional degenerate subspace the second moment is the same in
    every direction, so it selects nothing. The fourth moment usually is not: a
    square plate has four-fold variation, and its maximum picks out a diagonal.
    A truly round part -- a washer, a plain shaft -- has no variation at all, and
    that is reported rather than papered over.
    """
    (i, j) = degenerate[0]
    if len(degenerate) > 1:
        return axes, False, "all three principal moments equal; frame is arbitrary"

    u = axes[:, i]
    v = axes[:, j]
    samples = 180
    thetas = np.linspace(0.0, math.pi, samples, endpoint=False)
    values = np.array(
        [
            fourth_moment_along(mesh, moments.centroid, math.cos(t) * u + math.sin(t) * v)
            for t in thetas
        ]
    )

    spread = float(values.max() - values.min())
    reference = float(abs(values).max())
    if reference <= 0 or spread / reference < SUBSPACE_TOLERANCE:
        return axes, False, "degenerate subspace is rotationally symmetric"

    best = thetas[int(np.argmax(values))]
    new_u = math.cos(best) * u + math.sin(best) * v
    new_v = np.cross(axes[:, 3 - i - j], new_u)
    new_v = new_v / max(float(np.linalg.norm(new_v)), 1e-15)

    axes = axes.copy()
    axes[:, i] = new_u
    axes[:, j] = new_v
    # Restore a right-handed basis before the sign pass runs.
    k = 3 - i - j
    axes[:, k] = np.cross(axes[:, i], axes[:, j])
    return axes, True, "degenerate subspace broken by fourth moment"


def build_invariants(
    solid,
    moments: VolumeMoments,
    mesh=None,
    feature_chirality: int = 0,
) -> InvariantVector:
    """Assemble the invariant vector for one solid.

    ``feature_chirality`` is the handedness derived from the part's hole pattern.
    It is used only when the mass distribution is too symmetric to decide, which
    is the case for parts whose asymmetry lives entirely in small features.
    """
    volume = abs(moments.volume)
    if volume <= 0:
        raise ValueError(f"cannot fingerprint {solid.source_name!r}: volume is {volume}")

    area = float(solid.mass.area)
    char_length = volume ** (1.0 / 3.0)

    # Sphericity: the ratio of a sphere's surface area at this volume to the
    # actual surface area. Dimensionless, 1.0 for a sphere, smaller for anything
    # with corners or holes. This replaces raw area, which is dimensional and so
    # cannot share a tolerance with the rest of the vector.
    sphericity = (math.pi ** (1.0 / 3.0)) * ((6.0 * volume) ** (2.0 / 3.0)) / max(area, 1e-15)

    frame = principal_frame(moments, mesh=mesh)
    # Second moments have units of L^5; V^(5/3) restores dimensionlessness.
    normaliser = volume ** (5.0 / 3.0)
    j1, j2, j3 = (float(x) / normaliser for x in frame.second_moments)

    chirality = frame.handedness()
    if chirality == 0 and feature_chirality != 0:
        chirality = feature_chirality

    extents = None
    if frame.stable and mesh is not None and len(mesh) > 0:
        local = (mesh.vertices - moments.centroid) @ frame.axes
        extents = tuple(
            float(x) / char_length for x in (local.max(axis=0) - local.min(axis=0))
        )

    return InvariantVector(
        char_length=float(char_length),
        sphericity=float(sphericity),
        j1=j1,
        j2=j2,
        j3=j3,
        chirality=int(chirality),
        face_histogram=solid.face_histogram(),
        topology=solid.topology.as_tuple(),
        frame_stable=frame.stable,
        principal_extents=extents,
    )
