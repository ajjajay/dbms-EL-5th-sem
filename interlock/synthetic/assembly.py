"""The demonstration assembly: WINCH-100.

Section 17 names the metadata tax as the risk that matters. The mitigation it
prescribes is deliberate scoping: one assembly of fifteen to thirty parts, split
across three or four notional teams, with its contracts authored properly. A
small fully-specified assembly demonstrates every behaviour in the design
document; a large half-specified one demonstrates none of them.

This is that assembly. Twenty distinct parts, four teams, high occurrence counts
on the standard fasteners, and every part carrying a real declaration.

Three things are built in on purpose:

  * **A chiral pair.** MOTOR-BRACKET-RH and -LH are mirror images. Every
    second-order quantity is identical, so they are the case the design document
    lists as OPEN in section 15, and they must not collide.

  * **The section 11 worked example, physically.** CLAMP-STACK is a bolt through
    two plates and a spacer, with the exact dimensions and tolerances printed in
    the document: grip 32.20 +-0.10 over 6.00 +-0.10, 20.00 +-0.20, 6.00 +-0.10.
    The gap is 0.20 mm nominal, worst case reaches -0.30 mm and the statistical
    band reaches -0.06 mm. The chain is authored so the engine can be checked
    against numbers that were written down before it existed.

  * **A hole that can move two millimetres.** The `hole_moved` variant shifts one
    of the four holes that carry the forward bearing block. The spacings barely
    change, so the cheap checks pass; the placement fit is what catches it, and
    the rejection names the bearing block and the team that owns it.

Nothing here is imported by the running system. It generates STEP files, which
are then ingested through exactly the same path as a downloaded model.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


from ..model.contracts import Declaration, DimensionDecl, InterfaceDecl, SocketDecl
from . import cad

# ------------------------------------------------------------------- teams

TEAMS = {
    "chassis": "Chassis and structure",
    "drivetrain": "Drivetrain",
    "standards": "Standard parts library",
    "controls": "Controls and electronics",
}

OWNER = {
    "WINCH-100": "chassis",
    "BASE-PLATE": "chassis",
    "DRUM-ASSY": "chassis",
    "DRUM": "chassis",
    "DRUM-FLANGE": "chassis",
    "BEARING-BLOCK": "chassis",
    "SHAFT": "chassis",
    "DRIVE-ASSY": "drivetrain",
    "MOTOR-BRACKET-RH": "drivetrain",
    "MOTOR-BRACKET-LH": "drivetrain",
    "MOTOR": "drivetrain",
    "GEARBOX": "drivetrain",
    "GEARBOX-HOUSING": "drivetrain",
    "GEARBOX-COVER": "drivetrain",
    "OUTPUT-PINION": "drivetrain",
    "CLAMP-STACK": "drivetrain",
    "PLATE-A": "drivetrain",
    "PLATE-B": "drivetrain",
    "SPACER-20": "standards",
    "BOLT-M6X20": "standards",
    "BOLT-M6X32": "standards",
    "WASHER-M6": "standards",
    "NUT-M6": "standards",
    "CONTROL-BOX": "controls",
    "CONTROL-ENCLOSURE": "controls",
    "CONTROL-PCB": "controls",
}

# g/mm^3
DENSITY = {
    "steel": 7.85e-3,
    "aluminium": 2.70e-3,
    "polycarbonate": 1.20e-3,
    "fr4": 1.85e-3,
}

MATERIAL = {
    "BASE-PLATE": "aluminium", "DRUM": "steel", "DRUM-FLANGE": "aluminium",
    "BEARING-BLOCK": "aluminium", "SHAFT": "steel", "MOTOR-BRACKET-RH": "aluminium",
    "MOTOR-BRACKET-LH": "aluminium", "MOTOR": "steel", "GEARBOX-HOUSING": "aluminium",
    "GEARBOX-COVER": "aluminium", "OUTPUT-PINION": "steel", "PLATE-A": "aluminium",
    "PLATE-B": "aluminium", "SPACER-20": "steel", "BOLT-M6X20": "steel",
    "BOLT-M6X32": "steel", "WASHER-M6": "steel", "NUT-M6": "steel",
    "CONTROL-ENCLOSURE": "polycarbonate", "CONTROL-PCB": "fr4",
}

# --------------------------------------------------------------- geometry

# The deck, and the two bearing blocks that carry the drum. The layout is laid
# out with real clearances rather than roughly, so the advisory interference
# check reports a clean baseline and a genuine clash stands out.
DECK = (400.0, 300.0, 8.0)
# The four holes that carry each bearing block, in BASE-PLATE coordinates.
FORWARD_PATTERN = [(50.0, 120.0), (90.0, 120.0), (50.0, 180.0), (90.0, 180.0)]
AFT_PATTERN = [(250.0, 120.0), (290.0, 120.0), (250.0, 180.0), (290.0, 180.0)]
# Where each block sits, so its own holes land on the plate's.
FORWARD_ORIGIN = (40.0, 110.0, 8.0)
AFT_ORIGIN = (240.0, 110.0, 8.0)
DECK_HOLES = [(20.0, 20.0), (380.0, 20.0), (20.0, 280.0), (380.0, 280.0)]
# The drum axis: along x, carried by both blocks at the same height.
AXIS_Y, AXIS_Z = 150.0, 38.0
M6_CLEARANCE_R = 3.3


def base_plate(
    hole_shift: tuple[float, float] = (0.0, 0.0),
    pattern_shift: tuple[float, float] = (0.0, 0.0),
) -> object:
    """300 x 200 x 8 deck.

    Two different ways to break the forward bearing mount, because they are
    caught by different steps:

    `hole_shift` moves *one* hole, which changes the pairwise spacings and is
    caught by the cheap pattern comparison.

    `pattern_shift` moves *all four* holes together. Every spacing is unchanged,
    the hole sizes are unchanged and the envelope is unchanged, so steps one
    through four all pass. Only measuring the holes against their partners under
    the placement the assembly actually applies catches it -- which is why this
    implementation treats section 8's step five as a verification rather than as
    the afterthought the document calls it.
    """
    holes = [(x + pattern_shift[0], y + pattern_shift[1]) for x, y in FORWARD_PATTERN]
    holes[3] = (holes[3][0] + hole_shift[0], holes[3][1] + hole_shift[1])
    holes += AFT_PATTERN + DECK_HOLES
    return cad.plate(*DECK, holes=holes, hole_r=M6_CLEARANCE_R)


def bearing_block() -> object:
    """60 x 80 x 45 block, bored for the shaft, on four M6 clearance holes."""
    body = cad.box(60, 80, 45)
    # Bored along x: both blocks sit on the same axis and one shaft runs through
    # them. A bore along y would give each block its own axis, which no single
    # shaft could satisfy.
    bore = cad.cylinder(15.5, 70, (-5, 40, 30), (1, 0, 0))
    body = cad.cut(body, bore)
    return cad.drill(body, [(10, 10), (50, 10), (10, 70), (50, 70)], M6_CLEARANCE_R, -1, 46)


def drum() -> object:
    # Radius 26 keeps the drum clear of the deck: the axis sits 38 mm up and the
    # deck is 8 mm thick, so anything over 30 would foul it.
    body = cad.cylinder(26, 110, (0, 0, 0), (1, 0, 0))
    return cad.cut(body, cad.cylinder(15.2, 130, (-10, 0, 0), (1, 0, 0)))


def drum_flange() -> object:
    body = cad.cylinder(28, 10, (0, 0, 0), (1, 0, 0))
    body = cad.cut(body, cad.cylinder(15.2, 20, (-5, 0, 0), (1, 0, 0)))
    tools = [
        cad.cylinder(2.6, 20, (-5, 18 * math.cos(a), 18 * math.sin(a)), (1, 0, 0))
        for a in (0.0, math.pi / 2, math.pi, 3 * math.pi / 2)
    ]
    return cad.cut(body, *tools)


def shaft() -> object:
    return cad.cylinder(15, 320, (0, 0, 0), (1, 0, 0))


def motor_bracket_rh() -> object:
    """Deliberately chiral: an L with a hole pattern that has no mirror symmetry."""
    base = cad.box(90, 70, 8)
    wall = cad.box(90, 8, 75, (0, 62, 0))
    part = cad.fuse(base, wall)
    part = cad.drill(part, [(15, 15), (75, 15), (15, 50)], M6_CLEARANCE_R, -1, 9)
    face = cad.cylinder(20.5, 20, (45, 56, 34), (0, 1, 0))
    part = cad.cut(part, face)
    # Kept clear of the 20.5 mm motor bore centred at (45, 34): a hole inside it
    # is swallowed by the cut, and the declaration then names a hole that is not
    # there. Contract derivation catches that, which is the point, but the
    # demonstration part should be right.
    # Clear of the 20.5 mm motor bore centred at (45, 34): a hole nearer than
    # 20.5 + 2.6 mm merges with it and stops being a full circle, and the
    # declaration would then name three holes where the part offers two. Contract
    # derivation catches that -- which is the point -- but the demonstration part
    # should be right in the first place.
    for x, z in ((20, 34), (70, 34), (45, 62)):
        part = cad.cut(part, cad.cylinder(2.6, 20, (x, 56, z), (0, 1, 0)))
    return part


def motor() -> object:
    body = cad.cylinder(30, 45, (0, 0, 0), (0, 1, 0))
    boss = cad.cylinder(20, 12, (0, -12, 0), (0, 1, 0))
    body = cad.fuse(body, boss)
    return cad.fuse(body, cad.cylinder(6, 30, (0, -38, 0), (0, 1, 0)))


def gearbox_housing() -> object:
    body = cad.box(110, 80, 70)
    pocket = cad.box(94, 64, 58, (8, 8, 8))
    body = cad.cut(body, pocket)
    body = cad.cut(body, cad.cylinder(20.5, 40, (55, -5, 40), (0, 1, 0)))
    return cad.drill(body, [(12, 12), (98, 12), (12, 68), (98, 68)], M6_CLEARANCE_R, -1, 12)


def gearbox_cover(thickness: float = 8.0) -> object:
    body = cad.box(110, 80, thickness)
    return cad.drill(body, [(12, 12), (98, 12), (12, 68), (98, 68)], M6_CLEARANCE_R,
                     -1, thickness + 1)


def output_pinion() -> object:
    body = cad.cylinder(24, 28, (0, 0, 0), (0, 1, 0))
    return cad.cut(body, cad.cylinder(8.1, 40, (0, -5, 0), (0, 1, 0)))


def bolt(length: float) -> object:
    """Socket head cap screw, simplified to a shank plus a head."""
    shank = cad.cylinder(3.0, length)
    head = cad.cylinder(5.0, 5.0, (0, 0, length))
    body = cad.fuse(shank, head)
    return cad.cut(body, cad.cylinder(2.5, 3.0, (0, 0, length + 2.0)))


def washer() -> object:
    return cad.cut(cad.cylinder(6.5, 1.6), cad.cylinder(3.2, 4.0, (0, 0, -1)))


def nut() -> object:
    return cad.cut(cad.cylinder(5.5, 5.0), cad.cylinder(2.6, 8.0, (0, 0, -1)))


def spacer(length: float = 20.0) -> object:
    return cad.cut(cad.cylinder(6.0, length), cad.cylinder(3.2, length + 4, (0, 0, -2)))


def clamp_plate(thickness: float = 6.0) -> object:
    return cad.plate(40, 30, thickness, holes=[(20, 15)], hole_r=M6_CLEARANCE_R)


def control_enclosure() -> object:
    body = cad.box(120, 90, 50)
    body = cad.cut(body, cad.box(110, 80, 44, (5, 5, 5)))
    return cad.drill(body, [(10, 10), (110, 10), (10, 80), (110, 80)], M6_CLEARANCE_R, -1, 12)


def control_pcb(thickness: float = 1.6) -> object:
    return cad.plate(100, 70, thickness, holes=[(6, 6), (94, 6), (6, 64), (94, 64)], hole_r=1.7)


# ------------------------------------------------------------- the assembly


@dataclass
class Variant:
    """A buildable configuration of the demonstration assembly."""

    name: str = "baseline"
    hole_shift: tuple[float, float] = (0.0, 0.0)
    pattern_shift: tuple[float, float] = (0.0, 0.0)
    cover_thickness: float = 8.0
    spacer_length: float = 20.0
    left_hand_bracket: bool = False


def build(variant: Variant | str = "baseline") -> cad.Node:
    """Build the WINCH-100 tree. Reusing a Node makes it one definition."""
    if isinstance(variant, str):
        variant = {
            "baseline": Variant(),
            "hole_moved": Variant("hole_moved", hole_shift=(2.0, 0.0)),
            "pattern_shifted": Variant("pattern_shifted", pattern_shift=(2.0, 0.0)),
            "thicker_cover": Variant("thicker_cover", cover_thickness=9.0),
            "long_spacer": Variant("long_spacer", spacer_length=20.4),
            "left_hand": Variant("left_hand", left_hand_bracket=True),
        }[variant]

    n_bolt20 = cad.Node("BOLT-M6X20", bolt(20.0))
    n_bolt32 = cad.Node("BOLT-M6X32", bolt(32.0))
    n_washer = cad.Node("WASHER-M6", washer())
    n_nut = cad.Node("NUT-M6", nut())

    # -- drum subassembly
    n_drum = cad.Node("DRUM", drum())
    n_flange = cad.Node("DRUM-FLANGE", drum_flange())
    n_shaft = cad.Node("SHAFT", shaft())
    # Local origin is the forward end of the drum barrel; the shaft runs through.
    drum_assy = cad.Node("DRUM-ASSY")
    drum_assy.add("shaft", n_shaft, cad.translation(-100, 0, 0))
    drum_assy.add("drum", n_drum, cad.translation(0, 0, 0))
    drum_assy.add("flange_fwd", n_flange, cad.translation(-10, 0, 0))
    drum_assy.add("flange_aft", n_flange, cad.translation(110, 0, 0))
    for i in range(4):
        a = i * math.pi / 2
        drum_assy.add(f"flange_bolt_{i}", n_bolt20,
                      cad.compose(cad.translation(-14, 18 * math.cos(a), 18 * math.sin(a)),
                                  cad.rotation((0, 1, 0), 90)))

    # -- gearbox subassembly
    n_housing = cad.Node("GEARBOX-HOUSING", gearbox_housing())
    n_cover = cad.Node("GEARBOX-COVER", gearbox_cover(variant.cover_thickness))
    n_pinion = cad.Node("OUTPUT-PINION", output_pinion())
    gearbox = cad.Node("GEARBOX")
    gearbox.add("housing", n_housing)
    gearbox.add("cover", n_cover, cad.translation(0, 0, 70))
    gearbox.add("pinion", n_pinion, cad.translation(55, 20, 40))
    for k, (x, y) in enumerate([(12, 12), (98, 12), (12, 68), (98, 68)]):
        gearbox.add(f"cover_bolt_{k}", n_bolt20, cad.translation(x, y, 70))
        gearbox.add(f"cover_washer_{k}", n_washer, cad.translation(x, y, 70))

    # -- drive subassembly
    bracket_shape = motor_bracket_rh()
    if variant.left_hand_bracket:
        bracket_shape = cad.mirror(bracket_shape, point=(45, 0, 0), normal=(1, 0, 0))
    n_bracket = cad.Node(
        "MOTOR-BRACKET-LH" if variant.left_hand_bracket else "MOTOR-BRACKET-RH", bracket_shape
    )
    n_motor = cad.Node("MOTOR", motor())
    drive = cad.Node("DRIVE-ASSY")
    drive.add("bracket", n_bracket)
    drive.add("motor", n_motor, cad.translation(45, 70, 34))
    # Alongside the bracket rather than behind it: behind it would put the
    # gearbox under the bearing blocks.
    drive.add("gearbox", gearbox, cad.translation(100, 0, 0))
    for k, (x, y) in enumerate([(15, 15), (75, 15), (15, 50)]):
        drive.add(f"bracket_bolt_{k}", n_bolt20, cad.translation(x, y, 8))
        drive.add(f"bracket_washer_{k}", n_washer, cad.translation(x, y, 8))

    # -- the clamp stack: section 11's worked example, built
    n_plate_a = cad.Node("PLATE-A", clamp_plate(6.0))
    n_plate_b = cad.Node("PLATE-B", clamp_plate(6.0))
    n_spacer = cad.Node("SPACER-20", spacer(variant.spacer_length))
    clamp = cad.Node("CLAMP-STACK")
    clamp.add("plate_a", n_plate_a, cad.translation(0, 0, 0))
    clamp.add("spacer", n_spacer, cad.translation(20, 15, 6))
    clamp.add("plate_b", n_plate_b, cad.translation(0, 0, 6 + variant.spacer_length))
    clamp.add("bolt", n_bolt32, cad.translation(20, 15, 0))
    clamp.add("nut", n_nut, cad.translation(20, 15, 6 + variant.spacer_length + 6))

    # -- control box
    n_enclosure = cad.Node("CONTROL-ENCLOSURE", control_enclosure())
    n_pcb = cad.Node("CONTROL-PCB", control_pcb())
    control = cad.Node("CONTROL-BOX")
    control.add("enclosure", n_enclosure)
    control.add("pcb", n_pcb, cad.translation(10, 10, 6))
    for k, (x, y) in enumerate([(10, 10), (110, 10), (10, 80), (110, 80)]):
        control.add(f"mount_bolt_{k}", n_bolt20, cad.translation(x, y, 0))

    # -- root
    n_block = cad.Node("BEARING-BLOCK", bearing_block())
    root = cad.Node("WINCH-100")
    root.add("base_plate",
             cad.Node("BASE-PLATE", base_plate(variant.hole_shift, variant.pattern_shift)))
    root.add("bearing_fwd", n_block, cad.translation(*FORWARD_ORIGIN))
    root.add("bearing_aft", n_block, cad.translation(*AFT_ORIGIN))
    root.add("drum_assy", drum_assy, cad.translation(112, AXIS_Y, AXIS_Z))
    root.add("drive_assy", drive, cad.translation(140, 5, 8))
    root.add("clamp_stack", clamp, cad.translation(330, 200, 8))
    root.add("control_box", control, cad.translation(60, 200, 8))

    # The fasteners that carry the bearing blocks: high occurrence counts on one
    # definition, which is the point of the occurrence table.
    for k, (x, y) in enumerate(FORWARD_PATTERN + AFT_PATTERN):
        root.add(f"block_bolt_{k}", n_bolt20, cad.translation(x, y, 53))
        root.add(f"block_washer_{k}", n_washer, cad.translation(x, y, 53))
        root.add(f"block_nut_{k}", n_nut, cad.translation(x, y, -5))
    for k, (x, y) in enumerate(DECK_HOLES):
        root.add(f"deck_bolt_{k}", n_bolt20, cad.translation(x, y, 8))
        root.add(f"deck_washer_{k}", n_washer, cad.translation(x, y, 8))
    return root


def write(path: str, variant: Variant | str = "baseline", shuffle_seed: int | None = None) -> str:
    return cad.write_step(build(variant), path, shuffle_seed=shuffle_seed)


# ---------------------------------------------------------- the declarations

# Every part's public face, authored. This is the metadata tax being paid in
# full for a deliberately small assembly, exactly as section 17 prescribes.


def _bolt_pattern(name: str, radius: float, count: int, fastener: str = "M6",
                  fit: str = "clearance", **extra) -> InterfaceDecl:
    return InterfaceDecl(
        name=name, kind="bolt_pattern", fastener=fastener, fit_class=fit,
        select={"radius": radius, "radius_tol": 0.05, "count": count, **extra},
    )


def declarations() -> dict[str, Declaration]:
    d: dict[str, Declaration] = {}

    d["BASE-PLATE"] = Declaration(
        envelope=[400.5, 300.5, 8.5],
        mass_max=2700.0,
        attributes={"finish": "anodised", "class": "structural"},
        metadata={"supplier": "in-house", "stock": "6082-T6 plate", "notes": "deburr all holes"},
        interfaces=[
            _bolt_pattern("bearing_fwd_mount", M6_CLEARANCE_R, 4,
                          region={"center": [70.0, 150.0, 8.0], "within": 45.0}),
            _bolt_pattern("bearing_aft_mount", M6_CLEARANCE_R, 4,
                          region={"center": [270.0, 150.0, 8.0], "within": 45.0}),
        ],
        dimensions=[DimensionDecl("deck_thickness", 8.0, 0.1, 0.1)],
    )

    d["BEARING-BLOCK"] = Declaration(
        envelope=[60.5, 80.5, 45.5],
        mass_max=470.0,
        attributes={"bore": 31.0, "class": "structural"},
        metadata={"supplier": "Acme Machining", "part_ref": "BB-6031"},
        interfaces=[
            _bolt_pattern("foot", M6_CLEARANCE_R, 4, seat=[0, 0, -1], seat_offset=0.0),
        ],
        dimensions=[DimensionDecl("bore_height", 30.0, 0.05, 0.05)],
    )

    d["MOTOR-BRACKET-RH"] = Declaration(
        envelope=[90.5, 70.5, 75.5],
        mass_max=260.0,
        attributes={"hand": "right"},
        interfaces=[
            _bolt_pattern("foot", M6_CLEARANCE_R, 3, seat=[0, 0, -1], seat_offset=0.0),
            _bolt_pattern("motor_face", 2.6, 3, fastener="M5", axis=[0, 1, 0]),
        ],
    )
    d["MOTOR-BRACKET-LH"] = Declaration(
        envelope=[90.5, 70.5, 75.5], mass_max=260.0, attributes={"hand": "left"},
        interfaces=[
            _bolt_pattern("foot", M6_CLEARANCE_R, 3, seat=[0, 0, -1], seat_offset=0.0),
            _bolt_pattern("motor_face", 2.6, 3, fastener="M5", axis=[0, 1, 0]),
        ],
    )

    d["MOTOR"] = Declaration(
        envelope=[60.5, 96.0, 60.5], mass_max=1500.0,
        attributes={"power_w": 750.0, "thermal_w": 95.0, "voltage": 48},
        metadata={"supplier": "Maxonic", "model": "BL-750-48", "lead_time_weeks": 6},
        # No pattern interface: the motor locates on its boss, not on a bolt
        # circle, and declaring a pattern it does not have would be exactly the
        # stale interface document this system exists to replace.
    )

    d["GEARBOX-HOUSING"] = Declaration(
        envelope=[110.5, 80.5, 70.5], mass_max=900.0,
        attributes={"ratio": 12.0, "thermal_w": 18.0},
        # The housing offers the face. The socket that requires a cover belongs to
        # GEARBOX, the assembly that holds both, because a socket is a parent's
        # requirement and the housing is not the cover's parent.
        interfaces=[_bolt_pattern("cover_face", M6_CLEARANCE_R, 4, seat=[0, 0, 1])],
    )

    d["GEARBOX-COVER"] = Declaration(
        envelope=[110.5, 80.5, 12.0], mass_max=320.0,
        interfaces=[_bolt_pattern("mount", M6_CLEARANCE_R, 4, seat=[0, 0, -1], seat_offset=0.0)],
        dimensions=[DimensionDecl("cover_thickness", 8.0, 0.1, 0.1)],
    )

    d["GEARBOX"] = Declaration(
        envelope=[130.0, 110.0, 90.0], mass_max=1800.0,
        attributes={"thermal_w": 18.0},
        sockets=[
            SocketDecl(
                name="cover", fills="cover", interface_name="mount",
                fastener="M6", fit_class="clearance", spacing_tol=0.20,
                allow=[130.0, 100.0, 14.0], mass_budget=400.0,
                derive_from={"instance": "housing", "interface": "cover_face"},
            )
        ],
    )

    d["OUTPUT-PINION"] = Declaration(envelope=[48.5, 28.5, 48.5], mass_max=420.0)

    d["DRUM"] = Declaration(
        envelope=[110.5, 52.5, 52.5], mass_max=1400.0,
        attributes={"capacity_m": 30.0},
    )
    d["DRUM-FLANGE"] = Declaration(envelope=[10.5, 56.5, 56.5], mass_max=300.0)
    d["SHAFT"] = Declaration(
        envelope=[320.5, 30.5, 30.5], mass_max=2100.0,
        dimensions=[DimensionDecl("journal_diameter", 30.0, 0.0, 0.021)],
    )

    d["DRUM-ASSY"] = Declaration(envelope=[340.0, 60.0, 60.0], mass_max=4200.0)

    d["DRIVE-ASSY"] = Declaration(
        envelope=[215.0, 130.0, 95.0], mass_max=4000.0,
        attributes={"power_w": 750.0, "thermal_w": 113.0},
    )

    # -- the clamp stack, carrying section 11's numbers verbatim
    d["PLATE-A"] = Declaration(
        envelope=[40.5, 30.5, 6.5], mass_max=30.0,
        interfaces=[_bolt_pattern("through", M6_CLEARANCE_R, 1)],
        dimensions=[DimensionDecl("thickness", 6.00, 0.10, 0.10, sigma_span=3.0)],
    )
    d["PLATE-B"] = Declaration(
        envelope=[40.5, 30.5, 6.5], mass_max=30.0,
        interfaces=[_bolt_pattern("through", M6_CLEARANCE_R, 1)],
        dimensions=[DimensionDecl("thickness", 6.00, 0.10, 0.10, sigma_span=3.0)],
    )
    d["SPACER-20"] = Declaration(
        envelope=[12.5, 12.5, 24.0], mass_max=40.0,
        dimensions=[DimensionDecl("length", 20.00, 0.20, 0.20, sigma_span=3.0)],
    )
    d["BOLT-M6X32"] = Declaration(
        envelope=[10.5, 10.5, 40.0], mass_max=18.0,
        attributes={"fastener": "M6", "grade": "12.9"},
        dimensions=[DimensionDecl("grip", 32.20, 0.10, 0.10, sigma_span=3.0)],
    )
    d["CLAMP-STACK"] = Declaration(envelope=[45.0, 35.0, 50.0], mass_max=130.0)

    d["BOLT-M6X20"] = Declaration(
        envelope=[10.5, 10.5, 26.0], mass_max=12.0,
        attributes={"fastener": "M6", "grade": "12.9"},
        metadata={"standard": "ISO 4762", "supplier": "Fastenal", "pack_size": 100},
    )
    d["WASHER-M6"] = Declaration(
        envelope=[13.5, 13.5, 2.0], mass_max=3.0,
        attributes={"fastener": "M6"}, metadata={"standard": "ISO 7089"},
    )
    d["NUT-M6"] = Declaration(
        envelope=[11.5, 11.5, 5.5], mass_max=4.0,
        attributes={"fastener": "M6"}, metadata={"standard": "ISO 4032"},
    )

    d["CONTROL-ENCLOSURE"] = Declaration(
        envelope=[120.5, 90.5, 50.5], mass_max=420.0,
        attributes={"ip_rating": "IP54", "thermal_w": 4.0},
        metadata={"supplier": "BoxCo", "colour": "RAL 7035"},
        interfaces=[_bolt_pattern("deck_mount", M6_CLEARANCE_R, 4, seat=[0, 0, -1],
                                  seat_offset=0.0)],
    )
    d["CONTROL-PCB"] = Declaration(
        envelope=[100.5, 70.5, 2.0], mass_max=90.0,
        attributes={"power_w": 22.0, "thermal_w": 22.0},
        metadata={"revision": "C", "assembly_house": "PCBWay"},
        interfaces=[_bolt_pattern("standoffs", 1.7, 4, fastener="M3")],
    )
    d["CONTROL-BOX"] = Declaration(
        envelope=[125.0, 95.0, 60.0], mass_max=600.0,
        attributes={"power_w": 22.0, "thermal_w": 26.0},
    )

    # -- the root, and the sockets that make the bearing mounts checkable
    d["WINCH-100"] = Declaration(
        envelope=[405.0, 305.0, 130.0],
        mass_max=16000.0,
        cg_window={"min": [140.0, 90.0, 0.0], "max": [250.0, 190.0, 80.0]},
        attributes={"power_w": 800.0, "thermal_w": 160.0},
        metadata={"programme": "WINCH", "review": "PDR passed 2026-08-14"},
        sockets=[
            SocketDecl(
                name="bearing_fwd", fills="bearing_fwd", interface_name="foot",
                fastener="M6", fit_class="clearance", spacing_tol=0.20,
                allow=[70.0, 90.0, 50.0], mass_budget=480.0,
                derive_from={"instance": "base_plate", "interface": "bearing_fwd_mount"},
            ),
            SocketDecl(
                name="bearing_aft", fills="bearing_aft", interface_name="foot",
                fastener="M6", fit_class="clearance", spacing_tol=0.20,
                allow=[70.0, 90.0, 50.0], mass_budget=480.0,
                derive_from={"instance": "base_plate", "interface": "bearing_aft_mount"},
            ),
            SocketDecl(
                name="drive_assy", fills="drive_assy",
                mass_budget=4000.0, power_budget=800.0, thermal_budget=130.0,
            ),
            SocketDecl(
                name="control_box", fills="control_box",
                mass_budget=700.0, power_budget=40.0, thermal_budget=30.0,
            ),
        ],
    )
    return d


# ------------------------------------------------------------------ chains

# Section 11's example, authored as a chain. The requirement is that the gap does
# not go negative: the bolt must clamp before it bottoms out.
BOLT_GRIP_CHAIN = {
    "name": "bolt_grip_clearance",
    "description": (
        "M6x32 grip minus the three parts it clamps. The gap is 0.20 mm nominal. "
        "Worst case reaches -0.30 mm; the statistical band reaches -0.06 mm."
    ),
    "target_low": 0.0,
    "target_high": None,
    "method": "worst_case",
    "owning_team": "drivetrain",
    "members": [
        ("BOLT-M6X32", "grip", +1),
        ("PLATE-A", "thickness", -1),
        ("SPACER-20", "length", -1),
        ("PLATE-B", "thickness", -1),
    ],
}

# A second chain that crosses three teams, which is the case section 11 says
# nobody checks because no single person can see the whole path.
BEARING_HEIGHT_CHAIN = {
    "name": "drum_centreline_height",
    "description": (
        "Height of the drum centreline above the deck: plate thickness plus the "
        "bearing bore height, against the shaft journal it must receive."
    ),
    # The statistical band is +-0.112 and the worst case is +-0.15, so this
    # requirement is met on the RSS reading and missed on the worst-case one.
    # That disagreement is the point of carrying both numbers.
    "target_low": 37.88,
    "target_high": 38.12,
    "method": "rss",
    "owning_team": "chassis",
    "members": [
        ("BASE-PLATE", "deck_thickness", +1),
        ("BEARING-BLOCK", "bore_height", +1),
    ],
}

CHAINS = [BOLT_GRIP_CHAIN, BEARING_HEIGHT_CHAIN]
