# Alpine Edge Carpentry — Quoting Engine

A web app that takes a Winner Flex IFC export and breaks it down into
cabinets/units and linear metres of kickboard, cornice, light pelmet,
and filler panel.

## Easiest way to run it (no command line needed)

1. Unzip this folder if you haven't already.
2. **On a Mac**: double-click **`Start on Mac.command`**. The first
   time only, macOS will block it — instead, right-click the file and
   choose **Open**, then confirm.
3. **On Windows**: double-click **`Start on Windows.bat`**.
4. A window will open showing some setup text, then your browser will
   open automatically at the quoting tool. Leave that window open
   while you're using it — closing it stops the tool.
5. If it says Python isn't installed, install it from
   https://www.python.org/downloads/ (on Windows, tick "Add Python to
   PATH" during install) and double-click the launcher again.

This runs the tool on your own computer — it's not on the internet yet.
To get a real web link you (or anyone else) can open from anywhere,
see "Deploy it to get a public URL" further down.

## How it works

- **`backend/main.py`** — a FastAPI server. Parses the uploaded `.ifc`
  file with `ifcopenshell` (the standard open-source IFC library),
  extracts every `IfcFurnishingElement` (named cabinets/units) and
  `IfcBuildingElementProxy` (unnamed trim strips), computes true
  world-space bounding boxes, and classifies trim into Kickboard /
  Light pelmet / Cornice / Filler panel by height off the floor.
- **`frontend/index.html`** — a single-page upload UI. The FastAPI
  server serves this file directly, so once deployed you get one URL
  that does everything.

This has been tested end-to-end against a real Winner Flex export
(Winner Design 12.0) and correctly identified 35 cabinets/units and
27 trim pieces.

## Run it locally

```bash
cd backend
pip install -r requirements.txt
uvicorn main:app --reload
```

Then open http://127.0.0.1:8000 in your browser.

## Deploy it to get a public URL

The backend needs a Python host (not a static host, since it runs
`ifcopenshell`). Two easy free/cheap options:

### Option A — Render.com
1. Push this folder to a GitHub repo.
2. On Render: New → Web Service → connect the repo.
3. Root directory: `backend`
4. Build command: `pip install -r requirements.txt`
5. Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
6. Deploy. Render gives you a URL like `https://alpine-edge-quoting.onrender.com`.

### Option B — Railway.app
1. Push this folder to a GitHub repo.
2. On Railway: New Project → Deploy from GitHub repo.
3. Set root directory to `backend`.
4. Railway auto-detects Python; set the start command to
   `uvicorn main:app --host 0.0.0.0 --port $PORT` if it doesn't infer it.
5. Deploy — Railway gives you a public URL.

Either way, once deployed, visiting the URL loads the upload page —
that's your web link.

## Known limitation (by design, not a bug)

Winner Flex exports each cabinet as **one merged solid** — carcass,
door, and hardware combined. Doors and handles aren't separate
objects in the IFC file, so they aren't broken out here. The agreed
next step is a **cabinet-code lookup table** (e.g. `DF 60` → 3-drawer
base, implies drawer front count/size) to infer door/handle data from
the codes already being extracted. That's the next build task.

## Business rules encoded so far

- Kickboard only ever runs along the front of the kitchen; pelmet and
  cornice run along the front and side, never the back. Confirmed
  against two real jobs — the height-band classifier isn't yet
  auto-detecting this from placement orientation (see the TODO in
  `classify_trim`), so if a future job includes a genuine side/back
  piece that shouldn't count, that's the trigger to build the
  orientation-based filter.
- Corner units get a standard 1.5m filler allowance each (not measured
  from the model — Winner Flex doesn't export it). Two corner units on
  a job means 3.0m, scaled automatically. It's folded into the
  kickboard length total, not shown as its own material.
- Doors/fronts aren't in the Winner Flex export at all — sizes come
  from a per-code lookup table (`CABINET_FRONT_LOOKUP` in
  `backend/main.py`), built by walking through a job's cabinet list
  with Marcus and confirming each one. A door is 4mm narrower and 5mm
  shorter than the opening it fills; a tall cabinet splits its doors
  in line with the worktop (bottom opening 720mm minus the 5mm reveal
  = 715mm, top opening is whatever height remains, no extra reveal —
  confirmed against a real 1965mm tall unit: 715mm + 1245mm). These
  are computed live from each cabinet's actual dimensions, not
  hardcoded, so the same code auto-scales at a different size on a
  future job. Two entries are still fixed overrides rather than
  formula-derived — flagged in the code comments for Marcus to
  confirm the convention (a 2-door sink base's centre-gap split, and
  two specific design heights that were given directly rather than
  derived). Any cabinet code not yet in the table shows up explicitly
  under "not yet catalogued" rather than being silently skipped.
- Kickboard, cornice, and light pelmet come in 3m stock lengths.
  The engine bin-packs the individual piece lengths (first-fit-
  decreasing) to work out how many 3m lengths are actually needed —
  short offcuts get reused for other short pieces rather than each
  piece requiring its own fresh length. A piece longer than 3m is
  flagged as needing to be spliced from multiple lengths.
- Worktops come in 4.05m lengths, 650mm deep. An L-shaped worktop is
  split into its two straight leg lengths (using the real IFC face
  boundary, not the bounding box) and each leg is bin-packed into
  4.05m lengths the same way.

- Cabinet width/depth can come out swapped for any unit rotated 90°
  from the main run (e.g. one on a return wall) — the model's raw
  bounding box doesn't know which axis is "width" in kitchen terms.
  Fixed by cross-checking against the nominal size Winner writes into
  the description (e.g. the "800" in "800 Highline Sink Base"): if
  that number matches the depth field much more closely than the
  width field, the two are swapped. Corner units are excluded, since
  their leading number describes the whole footprint, not one edge.
  Confirmed against a real job: SBH 80 was coming back as 615mm wide,
  800mm deep — backwards — and is now correctly 800×615.

## Trim classification bands

Derived from one real export; may need light tuning as more sample
files come in:

| Height off floor (z) | Category      |
|-----------------------|---------------|
| < 0.05m                | Kickboard     |
| 1.0m – 1.6m             | Light pelmet  |
| > 1.9m                  | Cornice       |
| anything else thin      | Filler panel  |

If a future export has a taller or shorter kitchen run and pieces get
misclassified, this is the block in `backend/main.py`
(`classify_trim`) to adjust.

## Supplier door catalogue (new)

`backend/catalogue.py` holds a structured catalogue extracted from OS
Doors' Price and Product Guide PDF: suppliers → door types (5G Vinyl,
5 Piece, Edged, MDF Painted, Timber Painted) → ranges → colours, plus
the standard stock size grid for 5G Vinyl (199 sizes) and a partial
one for Fenwick (5 Piece). The frontend now has Supplier / Door style
/ Range / Colour dropdowns above the upload zone — pick these before
uploading and every computed door front gets checked against the
real catalogue and flagged **Standard** (a genuine stock size) or
**MTM** (Made to Measure, priced next size up).

This was built the same way as the cabinet-front lookup: automated
extraction first, expected to be corrected. In particular:
- Colour lists come from parsing the PDF's layout text column by
  column — mostly clean, but a handful of stray technical-spec words
  may have slipped through the noise filter. Worth a skim before
  treating any one range's list as complete.
- Only 5G Vinyl's size grid is fully extracted (199 sizes, matching
  the catalogue's own printed table exactly). Other door types
  (5 Piece, Edged, Painted) will show every front as unchecked until
  their size grids are filled in the same way — Fenwick has a partial
  sample already in the file to extend from.
- A second supplier's data will slot into the same `SUPPLIERS` dict
  shape once that catalogue is available.

## Trade Mouldings correction (categorisation)

An earlier pass had misclassified Trade Mouldings' door types — Fenton
was filed under "Five Piece" when it's actually their Laser Edged
range, and Berkeley/Boston (vinyl-wrapped) were lumped in with the
genuine five-piece ranges. Corrected against Trade Mouldings' own
website copy: "There are three ranges of five-piece doors available
from stock: Buckingham... Balmoral... Rivington..." and "laser edged
doors: two families... Fenton... Odyssey." Current structure:

- **Vinyl Shaker**: Berkeley, Boston
- **Five Piece**: Balmoral, Buckingham, Rivington
- **Laser Edged**: Fenton (Odyssey is its sibling range, not yet added)
- **Painted**: Larissa, Vogue, Albany
- **Primed & Sanded**: Albany, Hadfield, Malham
- **MTM Collection Vinyl**: a separate set of 31 named door styles
  (Aries, Ascot, Auckland, Bowland, etc.) available Made to Measure —
  this is where the 252-size grid actually belongs; it had been
  wrongly attached to the Stock Collection ranges above in the first
  pass, since it was pulled from a pricing page that names these MTM
  styles explicitly, not Berkeley/Boston/Balmoral/etc.

## Panel sizing fix

Panels were showing confusing, non-standard dimensions. Two separate
issues, both fixed:

1. **Axis mix-up**: a panel's 19mm board thickness could land in
   either the width or depth field depending on the panel's rotation
   in the model (confirmed on a real job: MT 23 had it in width_mm,
   MB 87 had it in depth_mm — same panel type). Fixed by always taking
   thickness as whichever of the two is smaller, and the real face
   width as the other one, in `normalize_panel()`.
2. **No standard-size lookup**: panels were shown at their exact
   modelled size rather than a real orderable size. `suggest_panel_order()`
   now checks OS Doors' actual panel stock sizes (Flat Laminated,
   Pressed Plain, Pressed T&G — all 18mm thick) and returns the
   smallest one that covers the panel, per type.

**Tall panel depth**: confirmed with Marcus — a tall unit is often set
back from the wall so a deeper end panel can run out and meet the
worktop's front edge, covering the gap. This can be 650/700/750mm
depending on the appliance behind it. The engine currently assumes
650mm as a default for any panel â‰¥1900mm tall (`DEFAULT_TALL_PANEL_DEPTH_MM`
in `backend/main.py`) — this isn't auto-detected per job yet, so
confirm the right depth before ordering if it isn't the default.

OS Doors standard panel sizes (18mm thick), for reference:
| Panel type | Standard sizes (L x W mm) |
|---|---|
| Flat Laminated Panel | 785x380, 930x650, 965x380, 2400x650, 2620x650 |
| Pressed Plain Panel | 785x360, 900x650, 900x1200, 965x360, 2400x360, 2400x650 |
| Pressed T&G Panel | 785x360, 900x650, 900x1200, 965x360, 2400x360, 2400x650 |

## Worktop bug fix — multiple worktop pieces

A job can have more than one separate worktop object in the model
(confirmed on a real job with 3 — likely a disconnected run or an
island apart from the main run). The engine was only keeping the last
one it processed and silently discarding the others, which on that
job understated the true worktop area by roughly 50x (0.043m² instead
of the real 2.25m²). Fixed — every worktop piece found is now summed
for area and combined for the board-cutting calculation.

## Generic front rules + real-catalogue-first sizing

Two general rules confirmed with Marcus, now built into `generic_fronts()`
as the fallback for any cabinet code not in the confirmed
`CABINET_FRONT_LOOKUP` table:
- Widest standard door is 600mm — anything wider gets 2 side-by-side doors.
- Tallest standard door is ~1250mm — anything taller gets 2 stacked doors.
Corner units and drawer runs are still never guessed generically (too
variable) — they stay in `doors_not_catalogued` unless a corner unit's
own description states its door width (e.g. "450 Door"), which is
used directly.

Each resolved door now carries a `source` field — `"confirmed"` (a
specifically confirmed CABINET_FRONT_LOOKUP entry) or
`"generic_estimate"` (from the rules above) — so it's clear which
numbers are locked in versus provisional.

**Priority order confirmed with Marcus: the 4mm/5mm reveal figures
are a rough guideline, not a hard rule — real catalogue measurements
always win.** When a supplier/door style is selected, every computed
front is now checked against the real catalogue and SNAPPED to the
closest real stocked size (within 3mm) rather than just flagged —
`catalogue.nearest_standard_size()`. Only a front with nothing close
in the catalogue is left as computed and marked Made to Measure.

This caught a genuine per-range convention difference: Trade
Mouldings' **Larissa** range uses a 3mm width reveal, not the general
4mm used elsewhere — confirmed by testing every door on a real job
(Ryan.ifc) against Larissa's own price list, where every cabinet
matched cleanly once that was accounted for. Larissa's own
`size_convention` is set to `"cut"` (already-finished sizes, not
opening sizes) as a per-range override of Painted's `"nominal"`
default — see the per-range override support in
`catalogue.size_convention()`.

Also fixed while testing this: cabinets with non-standard codes (e.g.
`BUILT IN OVEN`, `BUILT IN MICROWAVE` — not Winner's usual
alphanumeric pattern) were falling through appliance detection and
getting priced as cabinets with doors. `classify_cabinet_group()` now
catches "built in X" (space, not hyphen — distinct from "Built-under",
which is a genuine cabinet housing) as Appliance.

## Swap tolerance tightened to 8mm

`correct_width_depth()`'s tolerance for treating a cabinet's nominal
description number as confirming a width/depth swap was 25mm; Marcus
confirmed it should be 8mm. Re-tested against all three real jobs on
file — nothing in the 8-25mm range existed on any of them, so this is
a stricter safety margin going forward with no change to current
results.

## Manual size entry when nothing resolves

When a front doesn't match any real catalogue size (MTM), its Width/
Height cells in the Doors & Trim table are now editable — type the
real size in and it's picked up by the CSV export. Cabinets that
couldn't be resolved to a front at all (`doors_not_catalogued`) get
their own small form (type, width, height, qty) to specify the front
by hand, also included in the CSV export (tagged `manual` vs `engine`
in the Source column so it's clear which numbers were typed in versus
computed). This doesn't yet feed back into `CABINET_FRONT_LOOKUP`
permanently — it's a per-job fix, not a new confirmed rule. Promoting
a manually-entered size into the permanent lookup table is still a
code change, the same as every other confirmed entry so far.

## Full catalogue extraction — every range now has real standard sizes

Previously only 5G Vinyl (OS Doors) and the MTM Collection grids had
real size data; everything else was either a partial sample (Fenwick)
or had no size data at all, meaning standard-size checks silently did
nothing for most ranges. Fixed by scanning every page of both PDFs
for size/price grids (a simple density check — any page with 10+
"NNxNN" number pairs) and extracting them properly. Every range in
both catalogues now has its own real size list:

- **OS Doors**: all 8 5G Vinyl styles (199 sizes, shared), all 4 five
  piece ranges (Harlem 184, Dylan 186, Fenwick 186 — replacing the
  15-size partial sample, Bastille 133), both Edged ranges (Phoenix/
  Tribeca, 192 each), all 5 MDF Painted ranges (113-205), all 6
  Timber Painted ranges (79-206).
- **Trade Mouldings**: Berkeley/Boston (61 each), Balmoral (66),
  Buckingham (67), Rivington (65), Vogue (80), Albany-Painted (60),
  on top of Fenton (240) and Larissa (58) already done.

All of these are genuine "cut" sizes (already-finished door sizes
straight from each range's own price list), which surfaced something
worth knowing: **width reveal varies by range, not just by
supplier** — Berkeley/Boston/Albany use 5mm, Balmoral/Buckingham/
Rivington/Vogue/Larissa use 3mm, while OS Doors' ranges generally use
4mm. Since every range's convention is set to "cut", the engine
doesn't need to know which reveal a range uses — it snaps the
formula's estimate straight to the real listed size regardless.

Primed & Sanded ranges (Albany, Hadfield, Malham) still have no size
grid — no price/size page was found for them in the PDF (makes sense:
they're primed-only, painted to order elsewhere, so a fixed size list
may not apply the same way). Worth flagging if that assumption is wrong.

## Fenton corrections (found while running a real job through it)

Two errors caught and fixed while testing Fenton against bens.ifc:
1. **Size data was an assumed full cross-product (240 sizes), not the
   real price grid.** Fenton's actual pricing page has blank cells —
   not every height/width combo is offered. Reparsed by actual grid
   cell position (blank cells excluded): 201 real sizes, not 240
   assumed ones.
2. **Convention was wrongly set to "cut".** Fenton's grid uses NOMINAL
   heights (720, not the cut 715) — confirmed by there being no 715
   row at all in the real data, only 720. Fixed to "nominal".

Also fixed: the nominal-convention reveal-add was being applied
blindly to every front, but not every front's dimensions came from a
reveal formula in the first place — a drawer front's height (140/283mm)
is a fixed design value that was never reveal-adjusted, and the second
door of a tall/appliance split is just "the remaining height". Adding
5mm to those produced false MTM flags (confirmed: Fenton stocks
140x600 directly, but checking 145x600 missed it). Now all four
height/width adjustment combinations are tried and whichever actually
resolves to a real catalogue size is used, rather than assuming reveal
always applies uniformly.

**Panels priced as door**: Fenton's own note says panels use the same
size grid as doors, no separate panel list — implemented via
`panels_priced_as_door()` / the `panel_priced_as_door` flag on a
range. Real finding from testing: Fenton's grid tops out at 1735mm,
so a tall end panel (MT 23, needing 2115mm) genuinely can't be
supplied as a single Fenton panel — flagged as needing Made to
Measure rather than silently guessed.
