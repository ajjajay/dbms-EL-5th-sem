"""Verifications that used to be throw-away scripts: moments, fingerprint
invariance, chirality. These are the claims section 15 lists as PROVEN, made
re-runnable."""

from __future__ import annotations

import numpy as np
import pytest

from interlock.geometry import fingerprint as fp
from interlock.geometry.moments import compute_moments
from interlock.synthetic import cad

from .shapes import hole_plate, identify, l_bracket, moved


def test_box_moments_match_the_kernel(backend):
    shape = cad.box(20, 30, 10)
    solid = backend.solid_record_from_shape(shape, "box")
    m = compute_moments(shape, deflection=0.1)

    assert m.volume == pytest.approx(6000.0, rel=1e-9)
    assert m.centroid == pytest.approx([10.0, 15.0, 5.0], abs=1e-9)

    inertia = m.inertia_about_centroid()
    assert inertia == pytest.approx(solid.mass.inertia_matrix, rel=1e-6, abs=1e-3)
    assert sorted(np.diag(inertia)) == pytest.approx([250000.0, 500000.0, 650000.0], rel=1e-6)


def test_third_moments_vanish_for_a_symmetric_body():
    m = compute_moments(cad.box(20, 30, 10), deflection=0.1)
    scale = m.volume ** 2
    assert all(abs(v) / scale < 1e-9 for v in m.third_central.values())


def test_third_moments_flip_sign_under_mirroring():
    bracket = l_bracket()
    mirrored = cad.mirror(bracket, normal=(1, 0, 0))
    a = compute_moments(bracket, deflection=0.1)
    b = compute_moments(mirrored, deflection=0.1)
    # Reflecting in x flips the x-odd third moments.
    assert b.third_central[(3, 0, 0)] == pytest.approx(-a.third_central[(3, 0, 0)], rel=1e-6, abs=1e-6)


def test_fingerprint_is_pose_and_order_invariant():
    a, _ = identify(hole_plate())
    # Same plate: moved a metre away and rotated, holes drilled in another order.
    reordered = hole_plate(holes=((50, 30), (10, 10), (50, 10), (10, 30)))
    b, _ = identify(moved(reordered))
    assert a.strict == b.strict


def test_widening_one_hole_by_0_2mm_changes_the_fingerprint():
    base, _ = identify(hole_plate())
    wide = cad.plate(60, 40, 6, holes=[(10, 10), (50, 10), (10, 30)], hole_r=3.3)
    wide = cad.cut(wide, cad.cylinder(3.4, 8, (50, 30, -1)))
    other, _ = identify(wide)
    assert base.strict != other.strict


def test_mirrored_bracket_has_opposite_chirality_and_different_fingerprint():
    right = l_bracket()
    left = cad.mirror(right, normal=(1, 0, 0))
    r, _ = identify(right)
    l, _ = identify(left)

    assert r.invariants.chirality != 0
    assert r.invariants.chirality == -l.invariants.chirality
    assert r.strict != l.strict

    # Same handedness moved by a rigid motion is still the same part.
    r2, _ = identify(moved(right))
    assert r2.strict == r.strict


def test_bounded_match_refuses_a_mirrored_pair():
    right, _ = identify(l_bracket())
    left, _ = identify(cad.mirror(l_bracket(), normal=(1, 0, 0)))
    result = fp.compare(right.invariants, left.invariants)
    assert not result.matched and result.chirality_conflict


def test_bounded_match_accepts_same_part_and_reports_size_gap_for_a_bigger_hole():
    a, _ = identify(hole_plate())
    b, _ = identify(moved(hole_plate()))
    assert fp.compare(a.invariants, b.invariants).matched

    wide = cad.cut(hole_plate(holes=[(10, 10), (50, 10), (10, 30)]),
                   cad.cylinder(3.4, 8, (50, 30, -1)))
    c, _ = identify(wide)
    assert not fp.compare(a.invariants, c.invariants).matched


def test_through_holes_are_flagged_as_through():
    identity, _ = identify(hole_plate())
    assert len(identity.bores) == 4
    assert all(b.through for b in identity.bores)
    blind = cad.cut(cad.box(20, 20, 10), cad.cylinder(2, 4, (10, 10, 6)))
    blind_identity, _ = identify(blind)
    assert blind_identity.bores and not blind_identity.bores[0].through


def test_a_hole_pattern_is_found_as_one_pattern():
    identity, _ = identify(hole_plate())
    assert len(identity.patterns) == 1
    assert identity.patterns[0].count == 4
    assert identity.patterns[0].radius == pytest.approx(3.3, abs=1e-6)
