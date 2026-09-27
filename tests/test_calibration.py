"""Tolerance calibration: the numbers in fingerprint.py must actually separate
"same part from another tool" from "a different part".

The cross-tool tolerance was a guess until it was measured against the NIST test
models (scripts/calibrate_nist.py, docs/CALIBRATION.md). These tests pin the two
properties the measurement established, without needing the NIST files present:
the tolerance must still reject a part whose hole moved or widened.
"""

from __future__ import annotations


from interlock.geometry import fingerprint as fp
from interlock.synthetic import cad

from .shapes import hole_plate, identify, moved

CROSS = (fp.CROSS_TOOL_CHAR_LENGTH_TOLERANCE, fp.CROSS_TOOL_DIMENSIONLESS_TOLERANCE)


def test_cross_tool_tolerance_is_not_looser_than_a_0_2mm_hole():
    """The failure the calibration was meant to rule out: a hole two tenths of a
    millimetre wider on a small part must not be absorbed by cross-tool slack."""
    base, _ = identify(hole_plate())
    wide = cad.cut(hole_plate(holes=[(10, 10), (50, 10), (10, 30)]),
                   cad.cylinder(3.4, 8, (50, 30, -1)))
    other, _ = identify(wide)

    result = fp.compare(base.invariants, other.invariants, *CROSS)
    assert not result.matched, f"0.2 mm hole change absorbed by cross-tool tolerance: {result.reason}"
    assert result.char_length_error > fp.CROSS_TOOL_CHAR_LENGTH_TOLERANCE


def test_cross_tool_tolerance_rejects_a_hole_moved_2mm():
    """Stage Four's demonstration: one hole moved 2 mm. Volume is unchanged, so
    this is caught by the shape components or not at all."""
    base, _ = identify(hole_plate())
    shifted, _ = identify(hole_plate(holes=((10, 10), (50, 10), (10, 30), (50, 32))))
    result = fp.compare(base.invariants, shifted.invariants, *CROSS)
    assert not result.matched, f"2 mm hole move absorbed by cross-tool tolerance: {result.reason}"


def test_cross_tool_tolerance_accepts_the_same_part_re_posed():
    """A part that only moved must match under both tolerance classes."""
    a, _ = identify(hole_plate())
    b, _ = identify(moved(hole_plate()))
    assert fp.compare(a.invariants, b.invariants).matched
    assert fp.compare(a.invariants, b.invariants, *CROSS).matched


def test_cross_tool_size_tolerance_is_not_looser_than_intra_tool():
    """Calibration finding: volume survives an exporter round trip almost
    exactly, so only the shape components need extra room."""
    assert fp.CROSS_TOOL_CHAR_LENGTH_TOLERANCE <= fp.CHAR_LENGTH_RELATIVE_TOLERANCE
    assert fp.CROSS_TOOL_DIMENSIONLESS_TOLERANCE > fp.DIMENSIONLESS_TOLERANCE


def test_chirality_threshold_still_resolves_a_deliberately_chiral_part():
    """Raising SKEWNESS_TOLERANCE to clear the cross-tool noise floor must not
    cost the chirality signal on parts that are genuinely handed."""
    from .shapes import l_bracket

    right, _ = identify(l_bracket())
    left, _ = identify(cad.mirror(l_bracket(), normal=(1, 0, 0)))
    assert right.invariants.chirality != 0
    assert right.invariants.chirality == -left.invariants.chirality
