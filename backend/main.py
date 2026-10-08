"""
Alpine Edge Carpentry — Kitchen Quoting Engine
Backend API: accepts a Winner Flex IFC export, parses it with ifcopenshell,
and returns a structured breakdown of cabinets/units and trim (kickboard,
cornice, light pelmet, filler panels).
"""

import tempfile
import os
import re
import gc
from collections import defaultdict

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import ifcopenshell
import ifcopenshell.geom

import catalogue

app = FastAPI(title="Alpine Edge Quoting Engine")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten to your frontend domain once deployed
    allow_methods=["*"],
    allow_headers=["*"],
)

SETTINGS = ifcopenshell.geom.settings()
SETTINGS.set(SETTINGS.USE_WORLD_COORDS, True)
SETTINGS.set(SETTINGS.WELD_VERTICES, True)  # merges duplicate vertices — notably cuts mesh memory


def bbox_from_verts(verts):
    """World-space bounding box from a flat (x,y,z,x,y,z,...) vertex list, in metres."""
    xs, ys, zs = verts[0::3], verts[1::3], verts[2::3]
    return {
        "w": max(xs) - min(xs),
        "d": max(ys) - min(ys),
        "h": max(zs) - min(zs),
        "z0": min(zs),
    }


def bbox(element):
    """
    World-space bounding box for a SINGLE element, in metres. Used only
    for the handful of elements (the worktop) that need individual
    geometry access outside the bulk cabinet/trim pass — that pass uses
    the memory-efficient iterator in parse_ifc instead of this, since
    calling create_shape element-by-element for a whole real job (often
    hundreds of parts) is what was pushing real files over a 512MB
    hosting limit.
    """
    shape = ifcopenshell.geom.create_shape(SETTINGS, element)
    return bbox_from_verts(shape.geometry.verts)


def classify_trim(z0: float) -> str:
    """
    Classify an unnamed trim proxy by its height off the floor.
    Bands were derived from a real Winner Flex export (Winner Design 12.0)
    and may need tuning against further sample files:
      - z ~ 0.00m, thin strip            -> Kickboard
      - z ~ 1.0-1.6m, thin strip         -> Light pelmet
      - z > 1.9m, thin strip             -> Cornice
      - anything else thin               -> Filler panel

    Business rules confirmed against real jobs (do not relax without
    checking with Marcus first):
      - Kickboard only ever runs along the FRONT of the kitchen —
        never sides or returns.
      - Pelmet and cornice run along the FRONT and SIDE of cabinets,
        but never the back (that's against the wall).
    TODO: the front/side/back distinction above is not yet
    auto-detected from placement orientation — totals to date have
    been manually verified against real jobs. If a future job's trim
    pieces include a genuine side/back piece that shouldn't count,
    this will need an orientation-based filter (using each element's
    IfcLocalPlacement direction vectors) rather than height bands alone.
    """
    if z0 < 0.05:
        return "Kickboard"
    if 1.0 < z0 < 1.6:
        return "Light pelmet"
    if z0 > 1.9:
        return "Cornice"
    return "Filler panel"


CORNER_UNIT_FILLER_M = 1.5  # standard trade allowance, not measured from the model


def is_corner_unit(cabinet: dict) -> bool:
    """
    Winner Flex doesn't export a filler panel for corner units — the
    corner filler is a fitting-time addition, not a modelled part.
    Flag corner units by code/description so a standard allowance can
    be added to the quote automatically instead of relying on someone
    remembering to add it by hand.
    """
    text = f"{cabinet['code']} {cabinet['description']}".lower()
    return "cnr" in text or "corner" in text


def top_face_area(element) -> float:
    """
    True plan (footprint) area of a flat slab, in m^2, computed from
    the actual mesh rather than its bounding box. A bounding box badly
    overstates area for any L-shaped or notched worktop (confirmed on
    a real job: bbox said 5.56 m^2, true area was 1.95 m^2 — 185% too
    high). Sums the area of upward-facing triangles only, so a sink
    or hob cutout is correctly excluded.
    """
    shape = ifcopenshell.geom.create_shape(SETTINGS, element)
    verts = shape.geometry.verts
    faces = shape.geometry.faces
    total = 0.0
    for i in range(0, len(faces), 3):
        i0, i1, i2 = faces[i], faces[i + 1], faces[i + 2]
        p0 = verts[i0 * 3:i0 * 3 + 3]
        p1 = verts[i1 * 3:i1 * 3 + 3]
        p2 = verts[i2 * 3:i2 * 3 + 3]
        ux, uy, uz = p1[0] - p0[0], p1[1] - p0[1], p1[2] - p0[2]
        vx, vy, vz = p2[0] - p0[0], p2[1] - p0[1], p2[2] - p0[2]
        nx = uy * vz - uz * vy
        ny = uz * vx - ux * vz
        nz = ux * vy - uy * vx
        if nz > 0:  # upward-facing triangle only
            total += (nx**2 + ny**2 + nz**2) ** 0.5 / 2
    return total


def is_worktop(cabinet: dict) -> bool:
    return "worktop" in cabinet["description"].lower()


# Door/front sizing rules (confirmed with Marcus):
#   - A door is 4mm narrower than the opening it fills.
#   - A door is 5mm shorter than the opening it fills.
#   - A tall cabinet splits its doors in line with the worktop: the
#     bottom opening is a standard 720mm (matching base-unit height),
#     the top opening is whatever height remains above that. The one
#     reveal between the two doors comes off the bottom door only —
#     confirmed against FF 60/19/05 (1965mm tall): bottom = 720-5 =
#     715mm, top = 1965-720 = 1245mm exactly, not 1245-5.
FRONT_WIDTH_REVEAL_MM = 4
FRONT_HEIGHT_REVEAL_MM = 5
TALL_CABINET_SPLIT_MM = 720  # nominal base-unit height the worktop sits on


def full_door(cabinet, qty=1, front_type="door"):
    """A single front spanning the cabinet's own full width and height."""
    return {
        "type": front_type,
        "width_mm": cabinet["width_mm"] - FRONT_WIDTH_REVEAL_MM,
        "height_mm": cabinet["height_mm"] - FRONT_HEIGHT_REVEAL_MM,
        "qty": qty,
    }


def opening_door(width_mm, height_mm, qty=1, front_type="door"):
    """A front sized from an explicit opening (e.g. a corner unit's
    stated door width from its own description), reveal applied the
    same way as a full-width door."""
    return {
        "type": front_type,
        "width_mm": width_mm - FRONT_WIDTH_REVEAL_MM,
        "height_mm": height_mm - FRONT_HEIGHT_REVEAL_MM,
        "qty": qty,
    }


def tall_cabinet_doors(cabinet):
    """
    Default (larder / fridge-freezer style) split: two doors in line
    with the worktop — bottom door capped at the standard 720mm base
    height (minus reveal), top door takes the remaining height.

    Confirmed against two independent sources: OS Doors' own Door
    Matrix diagram (1970mm tower unit: 715 + 1245) and Trade
    Mouldings' Larder spec, once corrected — their printed "1 x 1245
    (bottom door), 1 x 715 (top door)" has bottom and top the wrong
    way around (confirmed with Marcus), so the real split is bottom
    715 / top 1245, matching OS Doors exactly. This is the split used
    for a plain larder AND a fridge/freezer housing (both are just a
    storage cavity behind two doors, with no appliance cavity gap to
    account for) — see `appliance_housing_doors` for units built
    around an oven, which need a different, fixed split because the
    appliance itself interrupts the door line.
    """
    bottom_h = TALL_CABINET_SPLIT_MM - FRONT_HEIGHT_REVEAL_MM
    top_h = cabinet["height_mm"] - TALL_CABINET_SPLIT_MM
    w = cabinet["width_mm"] - FRONT_WIDTH_REVEAL_MM
    return [
        {"type": "door", "width_mm": w, "height_mm": bottom_h, "qty": 1},
        {"type": "door", "width_mm": w, "height_mm": top_h, "qty": 1},
    ]


# Appliance housing door splits, sourced from Trade Mouldings' Appliance
# Housing Units matrix (pages 68/69 of their August 2026 pricelist) —
# corrected for the same bottom/top mislabelling as the Larder spec
# (confirmed with Marcus; the PDF's own "Bottom"/"Top" labels are
# swapped from the real physical layout). These are FIXED splits, not
# derived from the cabinet's own height, because the appliance cavity
# itself dictates the door layout, not the overall unit height per se.
# Keyed by nominal tall-cabinet height band (1970mm vs 2150mm) and
# appliance type. A third band (e.g. 2400mm) isn't in the source data
# yet — falls back to the 2150mm split, flagged for a real example.
APPLIANCE_HOUSING_SPLITS_MM = {
    1970: {
        "single_oven": {"bottom": 645, "top": 715},
        "oven_microwave": {"bottom": 450, "top": 450},
        "double_oven": {"bottom": 450, "top": 570},
    },
    2150: {
        "single_oven": {"bottom": 645, "top": 715, "filler": 175},
        "oven_microwave": {"bottom": 355, "top": 645, "filler": 140},
        "double_oven": {"bottom": 530, "top": 570, "filler": 110},
    },
}


def classify_appliance_housing(description: str):
    """
    Identifies which Trade Mouldings appliance-housing config a tall
    cabinet's description implies, or None if it's a plain larder /
    fridge-freezer (which uses the generic tall_cabinet_doors split
    instead). Order matters: check "double oven" before generic
    "oven", and "microwave"/"combi" combos before a plain single oven.
    """
    d = description.lower()
    if "fridge" in d or "freezer" in d or "larder" in d or "broom" in d:
        return None
    if "double oven" in d:
        return "double_oven"
    if "oven" in d and ("micro" in d or "combi" in d or "coffee" in d):
        return "oven_microwave"
    if "oven" in d:
        return "single_oven"
    return None


def appliance_housing_doors(cabinet):
    """
    Fixed door split for a tall oven-related housing, picked by
    nominal height band (1970 vs 2150) and appliance type. Falls back
    to the generic tall_cabinet_doors split if the appliance type
    isn't recognised, so an unusual description doesn't silently
    produce no fronts at all.
    """
    housing_type = classify_appliance_housing(cabinet["description"])
    if housing_type is None:
        return tall_cabinet_doors(cabinet)

    band = 1970 if cabinet["height_mm"] < 2060 else 2150
    split = APPLIANCE_HOUSING_SPLITS_MM[band][housing_type]
    w = cabinet["width_mm"] - FRONT_WIDTH_REVEAL_MM
    fronts = [
        {"type": "door", "width_mm": w, "height_mm": split["bottom"], "qty": 1},
        {"type": "door", "width_mm": w, "height_mm": split["top"], "qty": 1},
    ]
    if "filler" in split:
        fronts.append({"type": "filler_panel", "width_mm": w, "height_mm": split["filler"], "qty": 1})
    return fronts


def tall_unit_doors(cabinet):
    """
    Entry point for any Tall-grouped cabinet: routes to the
    appliance-specific split if the description names an oven-type
    appliance, otherwise the generic larder/fridge-freezer split.
    """
    if classify_appliance_housing(cabinet["description"]):
        return appliance_housing_doors(cabinet)
    return tall_cabinet_doors(cabinet)


def split_doors(cabinet, n_doors, front_type="door"):
    """
    n equal-width doors sharing one opening, e.g. a double-door sink
    base. Confirmed with Marcus: the 4mm reveal is taken per door,
    always — regardless of how many doors share the opening. So each
    door's own share of the width (cabinet width / n_doors) gets its
    own 4mm taken off, the same as a single-door front.
    """
    w = (cabinet["width_mm"] / n_doors) - FRONT_WIDTH_REVEAL_MM
    h = cabinet["height_mm"] - FRONT_HEIGHT_REVEAL_MM
    return [{"type": front_type, "width_mm": round(w), "height_mm": h, "qty": n_doors}]


# Cabinet-code -> front layout, built from confirmed real answers
# against bens.ifc, computed from the ACTUAL cabinet dimensions rather
# than hardcoded numbers, so the same code auto-scales if the same
# code appears at a slightly different size on a future job. Keyed on
# the exact Winner Flex code. Grows over time as more jobs get walked
# through — codes repeat across jobs, so this should need less
# confirmation each time.
#
# One entry is still a fixed override rather than fully
# formula-derived, flagged for Marcus to confirm the convention:
#   - DF 60 drawer front heights (140/283/283mm) were given as
#     specific design heights, not derived from cabinet height —
#     kept as literal values. (BUO 60's 115mm dummy front is a
#     standard size, not a one-off — same 115mm regardless of
#     cabinet, so it stays a constant here rather than needing
#     per-cabinet confirmation.)
CABINET_FRONT_LOOKUP = {
    "SBH 80":      lambda c: split_doors(c, 2),
    "BH 40":       lambda c: [full_door(c)],
    "BCD 90/45":   lambda c: [opening_door(450, c["height_mm"])],
    "DF 60":       lambda c: [
        {"type": "drawer_front", "width_mm": c["width_mm"] - FRONT_WIDTH_REVEAL_MM, "height_mm": 140, "qty": 1},
        {"type": "drawer_front", "width_mm": c["width_mm"] - FRONT_WIDTH_REVEAL_MM, "height_mm": 283, "qty": 2},
    ],
    "BUO 60":      lambda c: [{"type": "dummy_front", "width_mm": c["width_mm"] - FRONT_WIDTH_REVEAL_MM, "height_mm": 115, "qty": 1}],
    "W 60/57":     lambda c: [full_door(c)],
    "W 40/72":     lambda c: [full_door(c)],
    "W 60/29":     lambda c: [full_door(c)],
    "W 60/72":     lambda c: [full_door(c)],
    "FF 60/19/05": tall_unit_doors,
    "AP 60/19/04": tall_unit_doors,  # "600 Double Oven Housing 1965 High" — routes to double_oven split
    "WDC 60/72": lambda c: [
        {**opening_door(451, c["height_mm"]), "note": "45\u00b0 corner hinge (special hardware, not standard)"}
    ],  # diagonal corner wall unit — confirmed: standard 447mm-wide door, 45\u00b0 corner hinges
    "W 80/72": lambda c: [
        opening_door(300, c["height_mm"]),
        opening_door(400, c["height_mm"]),
    ],  # custom-narrowed to 700mm wide for this job (its own name says 800) —
       # confirmed as a 300mm + 400mm door split, not an even 2-way split.
       # This is specific to this job's resize decision, not a general rule
       # for every W 80/72 — if a future job's W 80/72 is a different width,
       # reconfirm the split rather than assuming 300/400 again.
}


WIDTH_SPLIT_THRESHOLD_MM = 600   # widest standard single door — wider gets 2 side-by-side doors
HEIGHT_SPLIT_THRESHOLD_MM = 1250  # tallest standard single door — taller gets 2 stacked doors


def generic_fronts(cabinet):
    """
    Fallback for a cabinet code with no specific confirmed entry in
    CABINET_FRONT_LOOKUP, using the general rules confirmed with
    Marcus: widest standard door is 600mm (wider needs 2 side-by-side
    doors), tallest standard door is ~1250mm (taller needs 2 stacked
    doors, in line with the worktop). Corner units and drawer runs
    aren't guessed here — their configuration varies too much to
    assume, so they stay unresolved (flagged in doors_not_catalogued)
    unless a corner unit's own description states a door width (e.g.
    "450 Door"), which is used directly.

    This is a starting estimate, not a confirmed answer — every front
    it produces still gets checked against the real supplier catalogue
    (see resolve_front_size) and only kept as-is if a real stocked
    size is close; otherwise it's marked Made to Measure rather than
    silently trusted.
    """
    desc = cabinet["description"]
    if is_corner_unit(cabinet):
        m = re.search(r"(\d+)\s*(?:mm)?\s*Door", desc, re.IGNORECASE)
        return [opening_door(int(m.group(1)), cabinet["height_mm"])] if m else None
    if "drawer" in desc.lower():
        return None  # drawer counts/heights vary too much to assume generically
    if cabinet["height_mm"] > HEIGHT_SPLIT_THRESHOLD_MM:
        return tall_unit_doors(cabinet)
    if cabinet["width_mm"] > WIDTH_SPLIT_THRESHOLD_MM:
        return split_doors(cabinet, 2)
    return [full_door(cabinet)]


def lookup_fronts(cabinet: dict):
    """
    Computed front list for a cabinet: a confirmed per-code entry if
    one exists, otherwise a generic estimate from the general width/
    height split rules, otherwise None (genuinely unresolved — corner
    units without a stated door width, and drawer runs).
    """
    rule = CABINET_FRONT_LOOKUP.get(cabinet["code"])
    if rule:
        return rule(cabinet)
    return generic_fronts(cabinet)


TRIM_STOCK_LENGTH_MM = 3000
WORKTOP_STOCK_LENGTH_MM = 4050
WORKTOP_STOCK_DEPTH_MM = 650


def lengths_required(piece_lengths_mm, stock_mm=TRIM_STOCK_LENGTH_MM):
    """
    How many stock lengths are needed to cut a given list of individual
    piece lengths, using first-fit-decreasing bin packing: each piece
    must be one continuous length (can't be joined from two lengths),
    but several short pieces can share one stock length if they fit
    together, reusing the offcut rather than starting a fresh length
    for each one.

    A piece LONGER than stock_mm can't physically come from one length
    at all — it needs multiple lengths joined/spliced. Those spliced
    lengths are counted as fully used (not available to share with
    other pieces), which is the safe assumption for ordering material.
    Returns (count, list_of_bins) where each bin is the list of piece
    lengths cut from that one stock length ("SPLICE" marks a length
    consumed by joining an oversized piece).
    """
    pieces = sorted(piece_lengths_mm, reverse=True)
    bins = []
    bin_contents = []
    spliced_bins = 0
    for length in pieces:
        if length > stock_mm:
            spliced_bins += -(-length // stock_mm)  # ceil division
            bin_contents.append([f"{length} (spliced, needs joining)"])
            continue
        placed = False
        for i, remaining in enumerate(bins):
            if length <= remaining:
                bins[i] -= length
                bin_contents[i].append(length)
                placed = True
                break
        if not placed:
            bins.append(stock_mm - length)
            bin_contents.append([length])
    return len(bins) + spliced_bins, bin_contents


def worktop_board_lengths(element) -> list:
    """
    Splits an L-shaped worktop's true outer boundary into the straight
    run lengths that need to be ordered as separate boards (an L can't
    be cut from one straight length — it's mitred from two).

    Method: walk the raw IFC face boundaries (not the triangulated
    mesh) to get exact edges, group them into connected loops — the
    outer perimeter is the one large loop; any sink/hob cutouts form
    their own small disconnected loops and are discarded. The outer
    loop's one reflex (concave) vertex — the only vertex that doesn't
    touch the shape's overall bounding box on either axis — is where
    the L bends; splitting the polygon there gives two rectangles,
    and the longer side of each is that leg's cut length.

    Verified against a real L-shaped worktop (bens.ifc): correctly
    recovered two legs of 1904mm and 2220mm, matching the cabinet
    runs beneath them.

    Only handles a simple 6-vertex L shape (one reflex vertex). A
    U-shaped or island worktop has more than one reflex vertex and
    isn't handled yet — falls back to a single bounding-box length in
    that case, which will overstate the true requirement.
    """
    rep = element.Representation.Representations[0]
    item = rep.Items[0]
    if not item.is_a("IfcFaceBasedSurfaceModel"):
        return []

    raw_edges = []
    for cfs in item.FbsmFaces:
        for face in cfs.CfsFaces:
            for b in face.Bounds:
                pts = [tuple(round(c, 1) for c in p.Coordinates) for p in b.Bound.Polygon]
                zs = sorted(set(p[2] for p in pts))
                if len(pts) == 4 and len(zs) == 2:
                    xy = tuple(sorted(set((p[0], p[1]) for p in pts)))
                    if len(xy) == 2:
                        raw_edges.append(xy)

    # union-find to group edges into connected loops
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for e in raw_edges:
        union(e[0], e[1])
    loops = defaultdict(list)
    for e in raw_edges:
        loops[find(e[0])].append(e)

    outer_loop = max(loops.values(), key=len)
    pts = [p for e in outer_loop for p in e]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    xmin, xmax, ymin, ymax = min(xs), max(xs), min(ys), max(ys)

    def is_extreme(p):
        return p[0] in (xmin, xmax) or p[1] in (ymin, ymax)

    verts = sorted(set(p for e in outer_loop for p in e))
    reflex = [v for v in verts if not is_extreme(v)]

    if len(reflex) != 1:
        # not a simple L — fall back to the overall bounding box length
        return [round(max(xmax - xmin, ymax - ymin))]

    rx, ry = reflex[0]
    rect1 = (xmax - xmin, ry - ymin) if ry > ymin else (xmax - xmin, ymax - ry)
    rect2 = (rx - xmin, ymax - ymin) if rx > xmin else (xmax - rx, ymax - ymin)
    # take whichever pairing actually reconstructs the two arms correctly:
    # arm A = strip on the short side of the reflex point, arm B = the
    # remaining full-width strip on the other side.
    arm_a = (rx - xmin, ry - ymin)   # narrow arm's own footprint
    arm_b = (xmax - xmin, ymax - ry)  # wide arm including the corner
    if arm_a[0] <= 0 or arm_a[1] <= 0:
        arm_a = (rx - xmin, ymax - ry)
        arm_b = (xmax - xmin, ry - ymin)
    return [round(max(arm_a)), round(max(arm_b))]


def normalize_panel(cab: dict) -> dict:
    """
    An end panel's bounding box gives width_mm/depth_mm in whatever
    order the model's rotation happens to produce — sometimes the
    19mm board thickness lands in width_mm, sometimes in depth_mm
    (confirmed on a real job: MT 23 had it in width_mm, MB 87 had it
    in depth_mm, same panel type). Thickness is reliably whichever of
    the two is smaller; the real face width is the other one. Height
    is a genuine vertical (z-axis) measurement either way, so it's
    left alone.
    """
    thickness = min(cab["width_mm"], cab["depth_mm"])
    face_width = max(cab["width_mm"], cab["depth_mm"])
    return {"height_mm": cab["height_mm"], "face_width_mm": face_width, "thickness_mm": thickness}


TALL_PANEL_MIN_HEIGHT_MM = 1900  # panels this tall sit beside a tall unit, not a base/wall run
DEFAULT_TALL_PANEL_DEPTH_MM = 650  # Marcus's default — confirm 650/700/750 per job (appliance-dependent)


def suggest_panel_order(cab: dict, supplier="OS Doors", door_type=None, range_name=None):
    """
    Suggests the smallest standard panel that covers this end panel's
    actual size, so a real orderable size gets quoted instead of the
    model's raw, sometimes-arbitrary dimensions. Defaults to OS Doors'
    generic panel list; if a supplier/range is given and that range
    prices its panels as doors (see catalogue.panels_priced_as_door —
    confirmed for Trade Mouldings' Fenton), its own door size grid is
    used instead, since there's no separate panel list to check.

    Tall panels (height >= TALL_PANEL_MIN_HEIGHT_MM) get a face width
    of DEFAULT_TALL_PANEL_DEPTH_MM rather than the panel's own modelled
    depth — a tall unit is often set back from the wall specifically so
    a deeper end panel can run out to meet the worktop's front edge,
    covering the gap. This depth varies by job (650/700/750mm
    depending on the appliance behind it) and isn't auto-detected yet;
    650mm is used as the default until told otherwise for a given job.
    """
    norm = normalize_panel(cab)
    is_tall = norm["height_mm"] >= TALL_PANEL_MIN_HEIGHT_MM
    needed_width = DEFAULT_TALL_PANEL_DEPTH_MM if is_tall else norm["face_width_mm"]
    options = catalogue.suggest_standard_panel(supplier, norm["height_mm"], needed_width, door_type, range_name)
    return {
        "actual_height_mm": norm["height_mm"],
        "actual_face_width_mm": norm["face_width_mm"],
        "thickness_mm": norm["thickness_mm"],
        "is_tall_panel": is_tall,
        "assumed_depth_mm": needed_width if is_tall else None,
        "standard_options": {k: (list(v) if v else None) for k, v in options.items()},
    }


def classify_cabinet_group(cabinet: dict) -> str:
    """
    Groups a named Winner Flex item into Base / Wall / Tall / Panel /
    Appliance, derived from patterns confirmed against two real jobs:
      - "Wall" in the description -> Wall cabinet.
      - Winner's internal appliance/fixture codes are formatted like
        'W330 052' (a letter+digits code with NO space right after
        the leading letter) — this reliably separates real wall
        cabinets (coded like 'W 60/57', WITH a space) from appliances
        that happen to also start with 'W'. This is the only signal
        used for Appliance — description keywords like "oven" or
        "sink" are NOT used, because cabinet housings (e.g. a "Sink
        Base" or a "Built-under Oven ... Draw Front" dummy panel) use
        those same words and are cabinets, not appliances.
      - "End Panel" / "End Support Panel" in the description -> Panel
        (grouped with trim/doors, not with cabinets).
      - Height >= 1900mm -> Tall (larders, tall housings, fridge
        freezer housings).
      - Everything else defaults to Base — this is where drawer
        units, sink bases, corner bases, and built-under housings
        land.
    Verified against bens.ifc (20 named items -> 5 Base, 5 Wall,
    1 Tall, 3 Panel, 6 Appliance) and spot-checked against the
    Anne-Marie Cook job without breaking its classification.
    """
    desc = cabinet["description"].lower()
    code = cabinet["code"]

    # "Built in X" (space, not hyphen) names the appliance itself —
    # confirmed gap: a job with codes like "BUILT IN OVEN" (not
    # Winner's usual alphanumeric pattern) fell through to Base
    # without this, wrongly getting cabinet doors costed against an
    # appliance. Distinct from "Built-under" (hyphenated), which is a
    # cabinet housing, not the appliance — e.g. BUO 60's "Built-under
    # Oven ... Draw Front" stays Base as intended.
    if "built in" in desc:
        return "Appliance"
    if "wall" in desc:
        return "Wall"
    if re.match(r"^W\d", code):
        return "Appliance"
    if "end panel" in desc or "end support panel" in desc:
        return "Panel"
    if cabinet["height_mm"] >= 1900:
        return "Tall"
    return "Base"


def correct_width_depth(cab: dict) -> dict:
    """
    The model's bounding box gives raw X/Y extents, which only line
    up with real-world "width" and "depth" when a cabinet happens to
    be oriented the same way as the rest of the run. A cabinet on a
    return wall (rotated 90° from the main run) gets its true width
    reported in the depth field instead — confirmed on a real job:
    SBH 80 ("800 Highline Sink Base") came back as width_mm=615,
    depth_mm=800, when the unit is genuinely 800mm wide.

    Winner's descriptions usually lead with the cabinet's true nominal
    width (e.g. the "800" in "800 Highline Sink Base"). If that number
    matches depth_mm much more closely than width_mm, the two are
    swapped. Corner units are excluded — their nominal number (e.g.
    "900" in "900 Cnr...") describes the overall footprint, not a
    single rectangular width/depth, so it isn't a reliable check for
    them.
    """
    if is_corner_unit(cab):
        return cab
    m = re.match(r"^(\d+)\b", cab["description"].strip())
    if not m:
        return cab
    nominal = int(m.group(1))
    diff_w = abs(nominal - cab["width_mm"])
    diff_d = abs(nominal - cab["depth_mm"])
    if diff_d < diff_w and diff_d <= 8:
        cab["width_mm"], cab["depth_mm"] = cab["depth_mm"], cab["width_mm"]
    return cab


BULK_GEOMETRY_LIBRARY = "manifold"  # lightweight kernel for the bulk pass — see note below


def parse_ifc(path: str) -> dict:
    f = ifcopenshell.open(path)

    cabinets = []
    corner_filler_count = 0
    worktop_pieces = []  # one entry per separate worktop object in the model
    trim_pieces = []

    # Worktops are singled out before the bulk pass: the default
    # OpenCASCADE kernel handles their L-shaped, cut-out geometry
    # correctly, but the lightweight "manifold" kernel used for the
    # bulk pass below silently fails on exactly that kind of complex
    # shape (confirmed: it skipped the one worktop on a real job
    # without raising an error) — so worktops never go through the
    # fast path at all, and get the careful per-element treatment via
    # bbox()/top_face_area()/worktop_board_lengths() instead. There
    # are only ever a few worktops per job, so this costs little.
    worktop_elements = [
        el for el in f.by_type("IfcFurnishingElement")
        if "worktop" in (el.Description or "").lower()
    ]
    worktop_ids = {el.id() for el in worktop_elements}
    for el in worktop_elements:
        try:
            b = bbox(el)
            cab = correct_width_depth({
                "code": el.Name or "",
                "description": el.Description or "",
                "width_mm": round(b["w"] * 1000),
                "depth_mm": round(b["d"] * 1000),
                "height_mm": round(b["h"] * 1000),
            })
            cabinets.append(cab)
            worktop_pieces.append({
                "area_m2": round(top_face_area(el), 3),
                "board_lengths_mm": worktop_board_lengths(el),
            })
        except Exception:
            pass

    # Bulk pass over everything else, using the C++-side iterator with
    # a lightweight geometry kernel rather than one create_shape()
    # Python call per element on the default kernel. Confirmed on a
    # real job (Ryan.ifc, 15.7MB): the original per-element approach
    # on the default kernel peaked at ~360MB just for parsing, which —
    # added to FastAPI's own overhead — was tipping real jobs over a
    # 512MB hosting limit; switching the bulk pass to "manifold"
    # brought that down to ~150MB for the same file, since cabinets
    # and trim strips are simple enough shapes not to need the full
    # CAD-precision kernel.
    iterator = ifcopenshell.geom.iterator(
        SETTINGS, f,
        include=["IfcFurnishingElement", "IfcBuildingElementProxy"],
        geometry_library=BULK_GEOMETRY_LIBRARY,
    )
    if iterator.initialize():
        while True:
            shape = iterator.get()
            try:
                if shape.id not in worktop_ids:
                    b = bbox_from_verts(shape.geometry.verts)
                    el = f.by_id(shape.id)
                    if el.is_a("IfcFurnishingElement"):
                        cab = correct_width_depth({
                            "code": el.Name or "",
                            "description": el.Description or "",
                            "width_mm": round(b["w"] * 1000),
                            "depth_mm": round(b["d"] * 1000),
                            "height_mm": round(b["h"] * 1000),
                        })
                        cabinets.append(cab)
                        if is_corner_unit(cab):
                            corner_filler_count += 1
                    else:  # IfcBuildingElementProxy
                        dims = sorted([b["w"], b["d"], b["h"]])
                        trim_pieces.append({
                            "category": classify_trim(b["z0"]),
                            "length_mm": round(dims[2] * 1000),
                            "thickness_mm": round(dims[0] * 1000),
                        })
            except Exception:
                pass
            if not iterator.next():
                break

    worktop_area_m2 = round(sum(p["area_m2"] for p in worktop_pieces), 3) if worktop_pieces else None
    worktop_board_lens = [l for p in worktop_pieces for l in p["board_lengths_mm"]]

    # Corner filler is bought and cut as part of the kickboard run, not
    # its own material — fold its allowance in as extra kickboard pieces
    # before working out how many 3m lengths are needed.
    kickboard_lengths = [t["length_mm"] for t in trim_pieces if t["category"] == "Kickboard"]
    kickboard_lengths += [round(CORNER_UNIT_FILLER_M * 1000)] * corner_filler_count
    cornice_lengths = [t["length_mm"] for t in trim_pieces if t["category"] == "Cornice"]
    pelmet_lengths = [t["length_mm"] for t in trim_pieces if t["category"] == "Light pelmet"]

    def summarize_cutting(lengths):
        if not lengths:
            return None
        count, bins = lengths_required(lengths)
        return {
            "total_length_mm": sum(lengths),
            "pieces": len(lengths),
            "lengths_required": count,
            "stock_length_mm": TRIM_STOCK_LENGTH_MM,
            "cut_plan_mm": bins,
        }

    trim_cutting = {
        "Kickboard": summarize_cutting(kickboard_lengths),
        "Cornice": summarize_cutting(cornice_lengths),
        "Light pelmet": summarize_cutting(pelmet_lengths),
    }
    trim_cutting = {k: v for k, v in trim_cutting.items() if v}

    worktop_cutting = None
    if worktop_board_lens:
        count, bins = lengths_required(worktop_board_lens, WORKTOP_STOCK_LENGTH_MM)
        worktop_cutting = {
            "board_lengths_mm": worktop_board_lens,
            "lengths_required": count,
            "stock_length_mm": WORKTOP_STOCK_LENGTH_MM,
            "stock_depth_mm": WORKTOP_STOCK_DEPTH_MM,
            "cut_plan_mm": bins,
        }

    # Group cabinets: Base / Wall / Tall go under "cabinets"; Panel
    # joins trim under "doors_and_trim"; Appliance is its own group.
    # The worktop is tracked separately (area, not a bounding box) and
    # doesn't belong in any of these groups.
    groups = {"Base": [], "Wall": [], "Tall": [], "Panel": [], "Appliance": []}
    for cab in cabinets:
        if is_worktop(cab):
            continue
        groups[classify_cabinet_group(cab)].append(cab)

    # Doors/fronts: looked up per cabinet against CABINET_FRONT_LOOKUP.
    # Appliances and panels don't take cabinet fronts, so only Base/
    # Wall/Tall are checked. "not_catalogued" lists codes still
    # needing a confirmed answer, so gaps are visible rather than
    # silently treated as zero.
    doors = []
    not_catalogued = []
    for cab in groups["Base"] + groups["Wall"] + groups["Tall"]:
        confirmed = cab["code"] in CABINET_FRONT_LOOKUP
        fronts = lookup_fronts(cab)
        if fronts is None:
            not_catalogued.append(cab["code"])
        else:
            doors.append({
                "cabinet_code": cab["code"],
                "cabinet_description": cab["description"],
                "fronts": fronts,
                "source": "confirmed" if confirmed else "generic_estimate",
            })

    panels_with_order = [{**cab, "panel_order": suggest_panel_order(cab)} for cab in groups["Panel"]]

    return {
        "cabinets": {
            "base": groups["Base"],
            "wall": groups["Wall"],
            "tall": groups["Tall"],
        },
        "doors_and_trim": {
            "doors": doors,
            "doors_not_catalogued": sorted(set(not_catalogued)),
            "panels": panels_with_order,
            "trim_cutting": trim_cutting,
            "worktop_area_m2": worktop_area_m2,
            "worktop_cutting": worktop_cutting,
        },
        "appliances": groups["Appliance"],
        "summary": {
            "cabinet_count": len(groups["Base"]) + len(groups["Wall"]) + len(groups["Tall"]),
            "panel_count": len(groups["Panel"]),
            "appliance_count": len(groups["Appliance"]),
            "corner_units": corner_filler_count,
        },
    }


@app.post("/api/parse")
async def parse_ifc_upload(
    file: UploadFile = File(...),
    supplier: str = None,
    door_type: str = None,
    door_range: str = None,
):
    if not file.filename.lower().endswith(".ifc"):
        raise HTTPException(400, "Please upload a Winner Flex .ifc export.")

    with tempfile.NamedTemporaryFile(suffix=".ifc", delete=False) as tmp:
        tmp.write(await file.read())
        tmp_path = tmp.name

    try:
        result = parse_ifc(tmp_path)
    except Exception as e:
        raise HTTPException(422, f"Could not parse this IFC file: {e}")
    finally:
        os.unlink(tmp_path)
        gc.collect()  # force the C-level IFC model/geometry memory to actually release between requests

    # If a door style is selected: check each computed front against
    # the real supplier catalogue and, where a real stocked size is
    # close, SNAP to that exact real size rather than trusting the
    # formula's own numbers — confirmed with Marcus: the 4mm/5mm
    # reveal figures are rough guidelines, and real catalogue
    # measurements always take priority when we have them. Only a
    # front with nothing close in the catalogue is left as computed
    # and marked Made to Measure.
    #
    # Size grids come in two conventions: OS Doors' (and Trade
    # Mouldings' Larissa range specifically) list the actual cut door
    # size already net of the reveal; other Trade Mouldings ranges
    # list the cabinet OPENING size before the reveal is taken off —
    # same reveal idea, just quoted against a different reference
    # point. For a "nominal" convention, the reveal is added back on
    # before checking, and subtracted again from any matched size
    # before snapping to it, to stay in "real cut door size" terms
    # throughout.
    if supplier and door_type:
        convention = catalogue.size_convention(supplier, door_type, door_range)
        for door in result["doors_and_trim"]["doors"]:
            for f in door["fronts"]:
                # Not every front's dimensions came from the reveal
                # formula in the first place — a drawer front's height
                # (e.g. 140/283mm) is a fixed design value, and the
                # second door of a tall/appliance split is just "the
                # remaining height", neither ever had a reveal taken
                # off. Applying the nominal reveal-add blindly to those
                # produces a false MTM (confirmed on a real job: Fenton
                # stocks 140x600 directly — adding 5mm missed it
                # entirely). So both interpretations are tried and
                # whichever actually resolves to a real catalogue size
                # wins, rather than assuming reveal always applies.
                candidates = [(f["height_mm"], f["width_mm"])]
                if convention == "nominal":
                    h0, w0 = f["height_mm"], f["width_mm"]
                    candidates = [
                        (h0, w0),
                        (h0 + FRONT_HEIGHT_REVEAL_MM, w0),
                        (h0, w0 + FRONT_WIDTH_REVEAL_MM),
                        (h0 + FRONT_HEIGHT_REVEAL_MM, w0 + FRONT_WIDTH_REVEAL_MM),
                    ]
                match = None
                matched_h_adj = matched_w_adj = False
                for check_h, check_w in candidates:
                    m = catalogue.nearest_standard_size(supplier, door_type, check_h, check_w, door_range)
                    if m:
                        match = m
                        matched_h_adj = check_h != f["height_mm"]
                        matched_w_adj = check_w != f["width_mm"]
                        break
                if match:
                    m_h, m_w = match
                    if matched_h_adj:
                        m_h -= FRONT_HEIGHT_REVEAL_MM
                    if matched_w_adj:
                        m_w -= FRONT_WIDTH_REVEAL_MM
                    f["height_mm"], f["width_mm"] = m_h, m_w
                    f["standard_size"] = True
                else:
                    f["standard_size"] = False

    # Panels: re-check against the selected supplier/range too, if
    # given — defaults to OS Doors' generic panel list otherwise.
    if supplier:
        for p in result["doors_and_trim"]["panels"]:
            p["panel_order"] = suggest_panel_order(
                {"width_mm": p["width_mm"], "depth_mm": p["depth_mm"], "height_mm": p["height_mm"]},
                supplier, door_type, door_range,
            )

    return result


@app.get("/api/catalogue/suppliers")
async def catalogue_suppliers():
    return catalogue.list_suppliers()


@app.get("/api/catalogue/door-types")
async def catalogue_door_types(supplier: str):
    return catalogue.list_door_types(supplier)


@app.get("/api/catalogue/ranges")
async def catalogue_ranges(supplier: str, door_type: str):
    return catalogue.list_ranges(supplier, door_type)


@app.get("/api/catalogue/colours")
async def catalogue_colours(supplier: str, door_type: str, door_range: str = None):
    return catalogue.list_colours(supplier, door_type, door_range)


@app.get("/api/health")
async def health():
    return {"status": "ok"}


# Serve the frontend as static files (index.html at /)
frontend_dir = os.path.join(os.path.dirname(__file__), "..", "frontend")
if os.path.isdir(frontend_dir):
    app.mount("/", StaticFiles(directory=frontend_dir, html=True), name="frontend")
