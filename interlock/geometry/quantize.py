"""Canonical quantisation of rigid transforms.

Section 6 hashes each assembly node from its children's hashes paired with their
placements, and reports that a re-export leaves untouched siblings bit-identical.
That result holds only because the test wrote and read the transforms through one
kernel, where the floats happen to survive exactly.

Placements are floating point. A real re-export perturbs a rotation in the
seventh decimal, and an unquantised hash then reports that every part in the
assembly changed -- which destroys the one property the Merkle tree exists to
provide. Transforms are therefore snapped to a canonical form before hashing.

Two stages. Common exact values are snapped first, because the overwhelming
majority of assembly placements are axis-aligned or at a familiar angle and
snapping removes their jitter entirely. Whatever survives is then rounded onto a
fixed grid.
"""

from __future__ import annotations

import math

import numpy as np

# Grid spacings applied after snapping. A micrometre and ten microradians are
# well below any manufacturing significance (the tightest chain in the demo is
# 0.01 mm) and comfortably above the ~1e-7 jitter that other CAD tools' STEP
# writers introduce. The grids were originally a nanometre and a nanoradian; that
# is *below* exchange jitter, which defeats the purpose of quantising at all.
# Any grid still has a boundary, so a value sitting on one can flip; the chance
# of that per component is roughly jitter / grid, which is why the grid is coarse.
TRANSLATION_GRID = 1e-3
ROTATION_GRID = 1e-5

# Values that appear in hand-built and axis-aligned placements.
_SNAP_TARGETS = np.array(
    [
        0.0, 1.0, -1.0,
        0.5, -0.5,
        math.sqrt(2) / 2, -math.sqrt(2) / 2,
        math.sqrt(3) / 2, -math.sqrt(3) / 2,
    ]
)
SNAP_TOLERANCE = 1e-6


def snap_scalar(value: float, tolerance: float = SNAP_TOLERANCE) -> float:
    deltas = np.abs(_SNAP_TARGETS - value)
    index = int(np.argmin(deltas))
    if deltas[index] <= tolerance:
        return float(_SNAP_TARGETS[index])
    return float(value)


def orthonormalise(rotation: np.ndarray) -> np.ndarray:
    """Project a matrix onto the nearest proper rotation.

    Snapping individual entries can leave a matrix slightly non-orthogonal; this
    puts it back on the rotation group, and rejects the reflection branch so a
    placement can never encode a mirror.
    """
    u, _, vt = np.linalg.svd(np.asarray(rotation, dtype=float))
    r = u @ vt
    if np.linalg.det(r) < 0:
        u[:, -1] *= -1.0
        r = u @ vt
    return r


def canonical_quaternion(rotation: np.ndarray) -> np.ndarray:
    """Unit quaternion for a rotation, with the sign ambiguity resolved.

    q and -q are the same rotation, so a hash over raw components would depend on
    which one the kernel produced.
    """
    r = np.asarray(rotation, dtype=float)
    trace = float(np.trace(r))
    if trace > 0.0:
        s = math.sqrt(trace + 1.0) * 2.0
        w = 0.25 * s
        x = (r[2, 1] - r[1, 2]) / s
        y = (r[0, 2] - r[2, 0]) / s
        z = (r[1, 0] - r[0, 1]) / s
    elif r[0, 0] > r[1, 1] and r[0, 0] > r[2, 2]:
        s = math.sqrt(1.0 + r[0, 0] - r[1, 1] - r[2, 2]) * 2.0
        w = (r[2, 1] - r[1, 2]) / s
        x = 0.25 * s
        y = (r[0, 1] + r[1, 0]) / s
        z = (r[0, 2] + r[2, 0]) / s
    elif r[1, 1] > r[2, 2]:
        s = math.sqrt(1.0 + r[1, 1] - r[0, 0] - r[2, 2]) * 2.0
        w = (r[0, 2] - r[2, 0]) / s
        x = (r[0, 1] + r[1, 0]) / s
        y = 0.25 * s
        z = (r[1, 2] + r[2, 1]) / s
    else:
        s = math.sqrt(1.0 + r[2, 2] - r[0, 0] - r[1, 1]) * 2.0
        w = (r[1, 0] - r[0, 1]) / s
        x = (r[0, 2] + r[2, 0]) / s
        y = (r[1, 2] + r[2, 1]) / s
        z = 0.25 * s

    q = np.array([w, x, y, z], dtype=float)
    norm = float(np.linalg.norm(q))
    if norm < 1e-15:
        return np.array([1.0, 0.0, 0.0, 0.0])
    q = q / norm

    # Fix the sign on the first component that is meaningfully non-zero.
    for component in q:
        if abs(component) > 1e-12:
            if component < 0:
                q = -q
            break
    return q


def quantise_transform(
    transform: np.ndarray,
    translation_grid: float = TRANSLATION_GRID,
    rotation_grid: float = ROTATION_GRID,
) -> tuple[tuple[int, int, int], tuple[int, int, int, int]]:
    """Reduce a 4x4 placement to integers suitable for hashing.

    Returns the quantised translation and the quantised canonical quaternion.
    Integers rather than rounded floats, because a rounded float still has a
    string representation that varies between platforms.
    """
    m = np.asarray(transform, dtype=float)
    if m.shape != (4, 4):
        raise ValueError(f"expected a 4x4 transform, got {m.shape}")

    rotation = np.array([[snap_scalar(m[i, j]) for j in range(3)] for i in range(3)])
    rotation = orthonormalise(rotation)
    quaternion = canonical_quaternion(rotation)

    translation = np.array([snap_scalar(m[i, 3], tolerance=translation_grid) for i in range(3)])

    t_int = tuple(int(round(float(v) / translation_grid)) for v in translation)
    q_int = tuple(int(round(float(v) / rotation_grid)) for v in quaternion)
    return t_int, q_int


def transform_key(transform: np.ndarray) -> str:
    """Stable text form of a placement, for hashing and for debugging output."""
    t, q = quantise_transform(transform)
    return "t:{}|{}|{};q:{}|{}|{}|{}".format(*t, *q)


def transforms_equal(a: np.ndarray, b: np.ndarray) -> bool:
    return transform_key(a) == transform_key(b)


def compose(parent: np.ndarray, child: np.ndarray) -> np.ndarray:
    """Compose two placements, parent then child."""
    return np.asarray(parent, dtype=float) @ np.asarray(child, dtype=float)


def invert(transform: np.ndarray) -> np.ndarray:
    m = np.asarray(transform, dtype=float)
    r = m[:3, :3]
    t = m[:3, 3]
    out = np.eye(4)
    out[:3, :3] = r.T
    out[:3, 3] = -r.T @ t
    return out
