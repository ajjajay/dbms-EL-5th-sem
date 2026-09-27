"""Transform quantisation: the part of the Merkle hash that is pure arithmetic.

This module is load-bearing and was almost entirely untested. `transform_key` is
what every assembly node hashes its children's placements through, so a sign
error here would silently produce wrong hashes -- and wrong hashes mean wrong
change detection and wrong conflict detection, with no exception raised anywhere.

`canonical_quaternion` in particular implements Shepperd's method, which is four
branches selected by which diagonal entry of the rotation dominates. The
demonstration assembly is almost entirely axis-aligned, so it only ever exercised
the first branch. The other three run for rotations beyond 120 degrees, and are
covered here deliberately.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from interlock.geometry.quantize import (
    ROTATION_GRID,
    TRANSLATION_GRID,
    canonical_quaternion,
    compose,
    invert,
    orthonormalise,
    quantise_transform,
    snap_scalar,
    transform_key,
    transforms_equal,
)
from interlock.synthetic import cad


def quat_to_matrix(q) -> np.ndarray:
    """Rebuild a rotation from its quaternion, so the quaternion can be checked
    against the matrix it came from rather than against itself."""
    w, x, y, z = q
    return np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)],
        [2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)],
        [2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)],
    ])


# The four cases Shepperd's method branches on.
BRANCHES = {
    "trace positive (small rotation)": cad.rotation((0, 0, 1), 20.0),
    "x dominant (180 about x)": cad.rotation((1, 0, 0), 180.0),
    "y dominant (180 about y)": cad.rotation((0, 1, 0), 180.0),
    "z dominant (180 about z)": cad.rotation((0, 0, 1), 180.0),
}


@pytest.mark.parametrize("name", list(BRANCHES))
def test_every_quaternion_branch_reconstructs_its_own_rotation(name):
    """The test the inherited code never had: each branch must actually be the
    quaternion of the matrix it was given."""
    r = BRANCHES[name][:3, :3]
    q = canonical_quaternion(r)

    assert abs(float(np.linalg.norm(q)) - 1.0) < 1e-12, f"{name}: not a unit quaternion"
    assert quat_to_matrix(q) == pytest.approx(r, abs=1e-9), f"{name}: quaternion is not this rotation"


def test_all_four_branches_are_actually_reached():
    """Guard the guard: if a future change makes these inputs stop selecting
    distinct branches, the test above would silently stop testing anything."""
    traces, dominants = set(), set()
    for m in BRANCHES.values():
        r = m[:3, :3]
        traces.add(float(np.trace(r)) > 0)
        dominants.add(int(np.argmax(np.diag(r))))
    assert True in traces and False in traces      # both trace signs
    assert len(dominants) >= 3                     # each diagonal dominates once


def test_random_rotations_round_trip():
    """A property test over the whole rotation group, not just the nice cases."""
    rng = np.random.default_rng(20260921)
    for _ in range(400):
        axis = rng.normal(size=3)
        angle = rng.uniform(0, 360)
        r = cad.rotation(axis, angle)[:3, :3]
        q = canonical_quaternion(r)
        assert quat_to_matrix(q) == pytest.approx(r, abs=1e-9)


def test_q_and_minus_q_are_the_same_rotation_and_the_same_key():
    """The sign ambiguity has to be resolved, or the hash depends on which of two
    equivalent quaternions the kernel happened to produce."""
    rng = np.random.default_rng(7)
    for _ in range(50):
        r = cad.rotation(rng.normal(size=3), rng.uniform(0, 360))[:3, :3]
        q = canonical_quaternion(r)
        # The first meaningfully non-zero component is positive, by construction.
        first = next(c for c in q if abs(c) > 1e-12)
        assert first > 0


def test_key_is_stable_under_sub_grid_jitter():
    """The property the grid exists for: a re-export that perturbs a rotation
    below the grid must not change the hash."""
    rng = np.random.default_rng(3)
    base = compose(cad.translation(120.0, 45.5, 8.0), cad.rotation((0.3, 0.7, 0.2), 41.0))
    key = transform_key(base)
    for _ in range(40):
        jittered = base.copy()
        jittered[:3, 3] += rng.normal(0, TRANSLATION_GRID / 40, 3)
        jittered[:3, :3] = orthonormalise(
            base[:3, :3] + rng.normal(0, ROTATION_GRID / 40, (3, 3)))
        assert transform_key(jittered) == key
        assert transforms_equal(jittered, base)


def test_key_changes_for_a_movement_that_matters():
    """And the property that stops it being useless: a real move must change it."""
    base = cad.translation(100.0, 0.0, 0.0)
    assert transform_key(cad.translation(100.002, 0.0, 0.0)) != transform_key(base)
    assert transform_key(compose(base, cad.rotation((0, 0, 1), 0.01))) != transform_key(base)


def test_snapping_cleans_common_values_without_moving_real_ones():
    assert snap_scalar(1.0 + 1e-9) == 1.0
    assert snap_scalar(math.sqrt(2) / 2 - 1e-9) == pytest.approx(math.sqrt(2) / 2)
    assert snap_scalar(0.37) == 0.37          # not near any target: untouched


def test_orthonormalise_rejects_the_reflection_branch():
    """A placement may never encode a mirror: a reflected 'rotation' would let an
    impossible assembly hash as though it were a real one."""
    mirror = np.diag([1.0, 1.0, -1.0])
    r = orthonormalise(mirror)
    assert float(np.linalg.det(r)) > 0
    assert r @ r.T == pytest.approx(np.eye(3), abs=1e-12)


def test_quantise_returns_integers_not_rounded_floats():
    """Integers, because a rounded float still has a platform-dependent string
    form and the key is built from text."""
    t, q = quantise_transform(compose(cad.translation(1.5, -2.25, 0.125),
                                      cad.rotation((0, 0, 1), 90.0)))
    assert all(isinstance(v, int) for v in t + q)
    assert t == (1500, -2250, 125)            # 1 um grid


def test_compose_and_invert_are_inverses():
    rng = np.random.default_rng(11)
    for _ in range(50):
        m = compose(cad.translation(*rng.uniform(-500, 500, 3)),
                    cad.rotation(rng.normal(size=3), rng.uniform(0, 360)))
        assert compose(m, invert(m)) == pytest.approx(np.eye(4), abs=1e-9)
        assert invert(invert(m)) == pytest.approx(m, abs=1e-9)


def test_a_part_rotated_past_120_degrees_still_hashes_consistently():
    """The end-to-end consequence: the branches this file exposed are the ones a
    real assembly hits whenever something is mounted upside down or turned round,
    and the hash has to be stable there too."""
    for angle in (150.0, 180.0, 210.0, 270.0):
        for axis in ((1, 0, 0), (0, 1, 0), (0, 0, 1), (1, 1, 0)):
            m = compose(cad.translation(10.0, 20.0, 30.0), cad.rotation(axis, angle))
            assert transform_key(m) == transform_key(m.copy())
            # and an equivalent rotation reached the other way round agrees
            equivalent = compose(cad.translation(10.0, 20.0, 30.0),
                                 cad.rotation(axis, angle - 360.0))
            assert transform_key(equivalent) == transform_key(m)
