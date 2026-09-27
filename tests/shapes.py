"""Reusable test geometry."""

from __future__ import annotations


from interlock.geometry import fingerprint as fp
from interlock.kernel import get_backend
from interlock.synthetic import cad


def identify(shape, name: str = "part", deflection: float = 0.2):
    backend = get_backend("occt")
    solid = backend.solid_record_from_shape(shape, name)
    assert solid is not None and solid.valid, solid.validity_notes if solid else "no record"
    return fp.identify(solid, deflection=deflection), solid


def hole_plate(holes=((10, 10), (50, 10), (10, 30), (50, 30)), r=3.3):
    return cad.plate(60, 40, 6, holes=holes, hole_r=r)


def l_bracket():
    """A deliberately chiral part: asymmetric hole pattern on an L-shape."""
    base = cad.box(60, 40, 5)
    wall = cad.box(60, 5, 30)
    part = cad.fuse(base, wall)
    part = cad.drill(part, [(10, 15), (50, 15), (10, 33)], 3.3, -1, 6)
    # a hole through the upright, along y
    tool = cad.cylinder(3.3, 12, (30, -1, 20), (0, 1, 0))
    return cad.cut(part, tool)


def moved(shape, rotation_deg=37.0, axis=(0.3, 0.7, 0.2), shift=(812.5, -300.25, 40.0)):
    return cad.place(shape, cad.compose(cad.translation(*shift), cad.rotation(axis, rotation_deg)))
