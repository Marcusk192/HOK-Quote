"""
House of Kitchens - quote pricing.

Prices come from price_data.json, built from Marcus's
Supplier_Price_Comparison.xlsx (net trade prices, ex VAT).
Sell price = net cost x (1 + MARKUP). VAT is added on the quote totals.

Anything that can't be matched to a price is returned with
price=None and a reason, so it shows up on screen to be priced by
hand rather than silently going in at zero.
"""
import json
import os
import re

MARKUP = 1.00          # 100% markup on net cost
VAT_RATE = 0.135       # reduced rate on supply-and-fit kitchens

_DATA = None


def data():
    global _DATA
    if _DATA is None:
        with open(os.path.join(os.path.dirname(__file__), "price_data.json")) as f:
            _DATA = json.load(f)
    return _DATA


def sell(net):
    return None if net is None else round(net * (1 + MARKUP), 2)


# ---------------------------------------------------------------- lists

def list_price_ranges(supplier=None):
    out = []
    for key, r in data()["ranges"].items():
        if supplier and r["supplier"] != supplier:
            continue
        out.append({"key": key, "supplier": r["supplier"], "name": r["name"],
                    "finish": r.get("finish"), "group": r.get("group")})
    return sorted(out, key=lambda r: (r["supplier"], r["name"]))


def list_carcass_decors():
    return [{"decor": d, "band": b} for d, b in data()["carcass_bands"].items()]


def list_storage():
    return [{"name": s["name"], "price": sell(s["net"])} for s in data()["storage"]]


# ---------------------------------------------------------------- fronts

def _front_price(rng, kind, h, w):
    """Exact size first, then the nearest size within 10mm either way,
    then the smallest bigger size (cut down / made to measure)."""
    # Pan drawer fronts are often listed as doors (e.g. TM 283x595),
    # so drawers can match either list.
    kinds = ("drawer", "door") if kind == "drawer" else (kind,)
    cands = [f for f in rng["fronts"] if f[0] in kinds]
    if not cands:
        return None, None
    best = min(cands, key=lambda f: abs(f[1] - h) + abs(f[2] - w))
    if abs(best[1] - h) <= 10 and abs(best[2] - w) <= 10:
        return best[3], f"{best[1]}x{best[2]}"
    bigger = [f for f in cands if f[1] >= h - 10 and f[2] >= w - 10]
    if bigger:
        b = min(bigger, key=lambda f: f[1] * f[2])
        return b[3], f"{b[1]}x{b[2]} (nearest bigger size)"
    return None, None


FRONT_KIND = {"door": "door", "drawer_front": "drawer", "dummy_front": "drawer", "filler_panel": "door"}


def _nominal_width(cab):
    m = re.match(r"\s*(\d{3,4})\b", cab.get("description") or "")
    if m:
        return int(m.group(1))
    return int(round((cab.get("width_mm") or 0) / 50.0) * 50)


def _carcass_item(cab, group):
    """Map a Winner cabinet onto an Agetek carcass line."""
    desc = (cab.get("description") or "").lower()
    code = (cab.get("code") or "").upper()
    w = _nominal_width(cab)
    carc = data()["carcasses"]
    if group == "tall":
        if "fridge" in desc or code.startswith("FF"):
            return "Fridge Freezer"
        if "oven" in desc:
            return "Tall Double Oven"
        return f"{w} Larder" if f"{w} Larder" in carc else None
    if group == "wall":
        if "corner" in desc or "cnr" in desc:
            return "diagonal" if "diag" in desc else None
        return f"{w} Wall Unit" if f"{w} Wall Unit" in carc else None
    # base
    if "corner" in desc or "cnr" in desc:
        n = w // 100
        return f"{n} x {n} corner" if f"{n} x {n} corner" in carc else None
    return f"{w} Base" if f"{w} Base" in carc else None


# ---------------------------------------------------------------- job

def price_job(result, price_range_key=None, carcass_decor=None, carcass_band=None):
    """Returns priced lines for the cabinets, fronts, panels and trim."""
    d = data()
    rng = d["ranges"].get(price_range_key) if price_range_key else None
    supplier = rng["supplier"] if rng else None
    band = str(carcass_band or d["carcass_bands"].get(carcass_decor, 1))
    lines = []

    def line(section, name, qty, unit_net, note=None, detail=None):
        lines.append({
            "section": section, "name": name, "qty": qty,
            "unit_net": unit_net,
            "unit_sell": sell(unit_net),
            "total_sell": None if unit_net is None else round(sell(unit_net) * qty, 2),
            "note": note, "detail": detail,
        })

    # Cabinets (carcasses, Agetek, by colour band)
    for group in ("base", "wall", "tall"):
        for cab in result.get("cabinets", {}).get(group, []):
            item = _carcass_item(cab, group)
            net = d["carcasses"].get(item, {}).get(band) if item else None
            line("Cabinets", f"{cab['code']} - {cab['description']}", 1, net,
                 None if net is not None else "No carcass price match - price by hand",
                 f"Agetek {item} (band {band})" if item else None)

    # Fronts
    dt = result.get("doors_and_trim", {})
    for door in dt.get("doors", []):
        for f in door["fronts"]:
            kind = FRONT_KIND.get(f["type"], "door")
            net, matched = (None, None)
            if rng:
                net, matched = _front_price(rng, kind, f["height_mm"], f["width_mm"])
            label = f["type"].replace("_", " ")
            line("Fronts", f"{door['cabinet_code']} {label} {f['height_mm']}x{f['width_mm']}",
                 f["qty"], net,
                 None if net is not None else ("Pick a door price range" if not rng else "No size match - price by hand"),
                 f"{rng['name']} {matched}" if matched else None)

    # Panels - priced as the matching supplier's standard end panel
    acc = d["matched_accessories"].get("Shaker, wood-grain foil", {}).get(supplier or "", {})
    for p in dt.get("panels", []):
        po = p.get("panel_order") or {}
        h = po.get("actual_height_mm") or p.get("height_mm") or 0
        kind = "Tall end panel" if h >= 1500 else ("Base end panel" if h >= 750 else "Wall end panel")
        a = acc.get(kind)
        line("Panels", f"{p['code']} - {p['description']}", 1, a["price"] if a else None,
             None if a else "Pick a door price range", a["product"] if a else None)

    # Trim (kickboard / cornice / pelmet) by stock length
    trim_map = {"Kickboard": "Plinth", "Cornice": "Cornice", "Light pelmet": "Light pelmet"}
    for cat, t in (dt.get("trim_cutting") or {}).items():
        a = acc.get(trim_map.get(cat, cat))
        line("Trim", f"{cat} - {t['lengths_required']} lengths ({t['total_length_mm']/1000:.2f}m)",
             t["lengths_required"], a["price"] if a else None,
             None if a else "Pick a door price range", a["product"] if a else None)

    return {"lines": lines, "markup": MARKUP, "vat_rate": VAT_RATE,
            "price_range": rng["name"] if rng else None, "supplier": supplier,
            "carcass_band": band}
