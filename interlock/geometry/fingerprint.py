"""Geometric identity.

Section 5 asks one hash to do two incompatible jobs. It must collapse the same
part re-saved by the same tool, where the numbers are bit-identical, and it must
also match the same part exported by a different tool, where the numbers differ
in the fifth decimal and the topology counts differ outright. Hashing is
discontinuous and geometric comparison is continuous; rounding does not reconcile
them, it only moves the failure to the boundary of a rounding bucket, where two
identical parts still land on different sides.

Interlock therefore keeps two identities for every shape.

The strict fingerprint is a hash, and it does the job hashes are good at:
constant-time exact dedup within one authoring tool, including the resave case
that ordinary version control gets wrong. It includes the discrete counts,
because within one tool they are stable and they discriminate well.

The bounded match is a range query over the stored invariant vector. It does the
job the strict hash cannot: recognising the same part across tools, within a
stated tolerance, with the discrete counts used as corroboration rather than as a
requirement. It is a database index, not a hash, which is why it can express
"close enough" at all.
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass, field

import numpy as np

from .features import feature_chirality, hole_patterns, merge_bores, mounting_planes
from .invariants import InvariantVector, build_invariants, principal_frame
from .moments import triangulate

# Comparison tolerances for the bounded match. These were guesses until they
# were measured; see docs/CALIBRATION.md and scripts/calibrate_nist.py.
#
# Measured on the NIST MBE PMI test models, which ship the same part exported by
# several different CAD systems -- the only real evidence available about how far
# the invariant vector moves between tools.
#
#   same tool, different export flavour (n=5):  size <= 3.7e-6, shape <= 1.5e-5
#   different tools, same model      (n=9):  size <= 4.5e-5, shape <= 4.3e-4
#   different tools, different model (n=2):  size >= 1.9e-4, shape >= 2.0e-3
#
# The last row is the useful one. Two of the sixteen cases turned out to be
# genuinely different geometry rather than two exports of one model (ctc_03: 139
# vs 120 faces; ftc_11: 6 vs 42 faces), and they sit an order of magnitude away
# from the rest. That leaves a clean gap, and the tolerance goes in the gap.
#
# The asymmetry is the finding worth repeating: volume is almost exactly
# preserved across exporters, while the dimensionless components move ten times
# further, because they carry surface area and the moment integration, both of
# which depend on how the receiving kernel reconstructed the surfaces.
CHAR_LENGTH_RELATIVE_TOLERANCE = 1e-4   # 0.01% of a part's characteristic size
DIMENSIONLESS_TOLERANCE = 1e-4

# Cross-tool: size is no looser (it does not need to be), shape is 10x looser.
# Both were 2e-3 before calibration, which was 20x too loose on size and would
# have merged parts differing by a small hole on a small part.
CROSS_TOOL_CHAR_LENGTH_TOLERANCE = 1e-4
CROSS_TOOL_DIMENSIONLESS_TOLERANCE = 1e-3

FINGERPRINT_DIGITS = 16


@dataclass
class ShapeIdentity:
    """Everything identity-related that ingest produces for one solid."""

    strict: str
    invariants: InvariantVector
    bores: list
    patterns: list
    planes: list
    frame_notes: list[str] = field(default_factory=list)
    mesh_triangles: int = 0
    warnings: list[str] = field(default_factory=list)
    # Kept so ingest can store a display mesh next to the blob. Never read by the
    # query layer; dropped after ingest.
    mesh: object | None = None

    @property
    def char_length(self) -> float:
        return self.invariants.char_length

    def summary(self) -> str:
        v = self.invariants
        return (
            f"{self.strict}  L={v.char_length:.4f}  sph={v.sphericity:.5f}  "
            f"chirality={v.chirality:+d}  bores={len(self.bores)}  "
            f"patterns={len(self.patterns)}"
        )


def _round_significant(value: float, digits: int) -> float:
    if value == 0.0 or not np.isfinite(value):
        return 0.0
    from math import floor, log10

    exponent = floor(log10(abs(value)))
    factor = 10.0 ** (digits - 1 - exponent)
    return float(round(value * factor) / factor)


def strict_fingerprint(
    invariants: InvariantVector,
    significant_digits: int = 9,
    dimensionless_digits: int = 9,
) -> str:
    """Exact-match identity over the rounded invariant vector.

    Rounding is by significant digits rather than decimal places, which keeps the
    tolerance proportional to the size of the quantity -- the relative scheme
    section 5 argues for, applied consistently instead of only to length.
    """
    parts: list[bytes] = [b"interlock/shape/v1"]

    parts.append(struct.pack("<d", _round_significant(invariants.char_length, significant_digits)))
    for value in invariants.dimensionless():
        parts.append(struct.pack("<d", _round_significant(float(value), dimensionless_digits)))

    parts.append(struct.pack("<b", invariants.chirality))

    for kind in sorted(invariants.face_histogram):
        parts.append(kind.encode("ascii"))
        parts.append(struct.pack("<i", int(invariants.face_histogram[kind])))

    for count in invariants.topology:
        parts.append(struct.pack("<i", int(count)))

    digest = hashlib.blake2b(b"".join(parts), digest_size=FINGERPRINT_DIGITS // 2)
    return digest.hexdigest()


def identify(solid, deflection: float = 0.25) -> ShapeIdentity:
    """Compute the full identity of one ingested solid.

    This is the only place the mesh, the moments, the frame and the features come
    together, and it is the last point at which a kernel handle is required.
    """
    warnings: list[str] = []
    if not solid.valid:
        raise ValueError(
            f"refusing to fingerprint {solid.source_name!r}: "
            + "; ".join(solid.validity_notes)
        )

    if solid.handle is None:
        raise ValueError(f"{solid.source_name!r} carries no kernel handle to mesh")

    mesh = triangulate(solid.handle, deflection=deflection)
    from .moments import moments_from_mesh

    moments = moments_from_mesh(mesh)

    if not mesh.closed:
        warnings.append(
            f"mesh does not close (residual {mesh.closure_residual:.2e}); "
            "moments are approximate"
        )

    kernel_volume = float(solid.mass.volume)
    if kernel_volume > 0:
        error = abs(moments.volume - kernel_volume) / kernel_volume
        if error > 1e-3:
            warnings.append(
                f"mesh volume differs from the kernel by {error:.2%}; "
                "reduce the deflection if identity matching is unreliable"
            )

    bores = merge_bores(solid.faces, moments.centroid)
    _mark_through(bores, mesh)
    patterns = hole_patterns(bores)
    planes = mounting_planes(solid.faces)

    chirality_hint = feature_chirality(bores, moments.centroid)
    invariants = build_invariants(
        solid, moments, mesh=mesh, feature_chirality=chirality_hint
    )
    frame = principal_frame(moments, mesh=mesh)

    return ShapeIdentity(
        strict=strict_fingerprint(invariants),
        invariants=invariants,
        bores=bores,
        patterns=patterns,
        planes=planes,
        frame_notes=list(frame.notes),
        mesh_triangles=len(mesh),
        warnings=warnings,
        mesh=mesh,
    )


def _mark_through(bores, mesh) -> None:
    """Flag bores whose depth spans the whole part along their own axis.

    Decided from the mesh extent rather than assumed, because the earlier code
    reused the "sweep is a full turn" flag for this, which says the hole is round
    and says nothing about whether the hole goes through.

    The test is "spans the part's full extent along the hole axis", which is
    exact for plates and flanges -- the overwhelmingly common case -- and
    conservative for an L-bracket, where a hole can pass entirely through the leg
    it is in without spanning the part. Deciding it exactly needs a ray cast
    against the solid, which is ingest-time cost for a flag no constraint reads;
    it is recorded for display and the limitation is stated rather than hidden.
    """
    if len(mesh) == 0:
        return
    for bore in bores:
        axis = np.asarray(bore.axis, dtype=float)
        projection = mesh.vertices @ axis
        extent = float(projection.max() - projection.min())
        if extent > 0:
            bore.through = bore.total_depth >= 0.98 * extent


# ----------------------------------------------------------- bounded matching


@dataclass
class MatchResult:
    matched: bool
    char_length_error: float
    dimensionless_error: float
    chirality_conflict: bool
    topology_agrees: bool
    histogram_agrees: bool
    reason: str = ""

    @property
    def corroborated(self) -> bool:
        """A match that the discrete counts also support. Unmatched counts do not
        veto -- they usually mean two exporters split a seam differently -- but a
        corroborated match is worth more."""
        return self.matched and self.topology_agrees and self.histogram_agrees


def compare(
    a: InvariantVector,
    b: InvariantVector,
    char_length_tolerance: float = CHAR_LENGTH_RELATIVE_TOLERANCE,
    dimensionless_tolerance: float = DIMENSIONLESS_TOLERANCE,
) -> MatchResult:
    """Decide whether two invariant vectors describe the same shape."""
    scale = max(abs(a.char_length), abs(b.char_length), 1e-12)
    length_error = abs(a.char_length - b.char_length) / scale
    dimensionless_error = float(np.max(np.abs(a.dimensionless() - b.dimensionless())))

    # Chirality zero means undetermined, not "achiral and therefore different".
    conflict = a.chirality != 0 and b.chirality != 0 and a.chirality != b.chirality

    topology_agrees = tuple(a.topology) == tuple(b.topology)
    histogram_agrees = a.face_histogram == b.face_histogram

    matched = (
        length_error <= char_length_tolerance
        and dimensionless_error <= dimensionless_tolerance
        and not conflict
    )

    if matched:
        reason = "within tolerance"
    elif conflict:
        reason = "opposite handedness: these are a mirrored pair, not the same part"
    elif length_error > char_length_tolerance:
        reason = f"size differs by {length_error:.2e} (limit {char_length_tolerance:.0e})"
    else:
        reason = (
            f"shape differs by {dimensionless_error:.2e} "
            f"(limit {dimensionless_tolerance:.0e})"
        )

    return MatchResult(
        matched=matched,
        char_length_error=length_error,
        dimensionless_error=dimensionless_error,
        chirality_conflict=conflict,
        topology_agrees=topology_agrees,
        histogram_agrees=histogram_agrees,
        reason=reason,
    )


def similarity(a: InvariantVector, b: InvariantVector) -> float:
    """Distance between two shapes, for the substitution query.

    The components are already dimensionless and of comparable magnitude, so a
    plain Euclidean distance over them is meaningful -- which is not true of the
    raw vector section 5 describes, where volume would dominate every comparison
    simply by being numerically the largest.
    """
    scale = max(abs(a.char_length), abs(b.char_length), 1e-12)
    size_term = (a.char_length - b.char_length) / scale
    shape_term = a.dimensionless() - b.dimensionless()
    return float(np.sqrt(size_term**2 + float(np.dot(shape_term, shape_term))))
