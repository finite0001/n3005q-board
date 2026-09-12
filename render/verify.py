"""Independent checks. Run after any change to aviation.py or render.py.

The density altitude number is the whole point of the card, so it is checked
three ways: the NWS closed form used in production, the ideal gas law from
first principles, and the pilot rule of thumb.
"""
import os
from PIL import Image
import aviation as av
import render as rd

CARD = os.environ.get("N3005Q_CARD", os.path.join("..", "public", "card.png"))

FAIL = []


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + ("  " + detail if detail else ""))
    if not ok:
        FAIL.append(name)


# ---- 1. density altitude, three independent methods -------------------
elev, temp_c, altim = 664.0, 38.3, av.hpa_to_inhg(1010.9)

da_prod = av.density_altitude_ft(altim, elev, temp_c)

# ideal gas: rho = p/(R T), then invert the standard atmosphere for altitude
p_pa = av.station_pressure_inhg(altim, elev) * 3386.389
rho = p_pa / (287.058 * (temp_c + 273.15))
sigma_gas = rho / 1.225
da_gas = (1.0 - sigma_gas ** (1.0 / 4.2558797)) / 6.8755856e-6

pa = av.pressure_altitude_ft(altim, elev)
da_rule = pa + 120.0 * (temp_c - av.isa_temp_c(pa))

print(f"  NWS closed form : {da_prod:,.0f} ft")
print(f"  ideal gas law   : {da_gas:,.0f} ft")
print(f"  rule of thumb   : {da_rule:,.0f} ft")
check("DA vs ideal gas law within 1%", abs(da_prod - da_gas) / da_gas < 0.01,
      f"delta {da_prod - da_gas:+.0f} ft")
check("DA vs rule of thumb within 6%", abs(da_prod - da_rule) / da_rule < 0.06,
      f"delta {da_prod - da_rule:+.0f} ft")
check("sigma agrees with ideal gas",
      abs(av.density_ratio(da_prod) - sigma_gas) < 0.002)

# ---- 2. standard-day sanity ------------------------------------------
da_std = av.density_altitude_ft(29.9213, 0.0, 15.0)
check("ISA sea level gives DA ~= 0", abs(da_std) < 60, f"{da_std:+.0f} ft")

# ---- 3. wind components ----------------------------------------------
# Wind straight down a runway: all headwind, no crosswind.
c = av.wind_components(120 + 14, 20, 120, 14)
check("aligned wind is pure headwind",
      abs(c["headwind"] - 20) < 0.01 and c["crosswind"] < 0.01)
# 90 degrees off: all crosswind.
c = av.wind_components(210 + 14, 20, 120, 14)
check("perpendicular wind is pure crosswind",
      abs(c["crosswind"] - 20) < 0.01 and abs(c["headwind"]) < 0.01)
# The true->magnetic correction must actually change the answer.
a = av.wind_components(190, 22, 120, 14)["crosswind"]
b = av.wind_components(190, 22, 120, 0)["crosswind"]
check("true->mag correction is applied", abs(a - b) > 1.0,
      f"{a:.1f} vs {b:.1f} kt")

# ---- 4. rendered image -----------------------------------------------
img = Image.open(CARD).convert("RGB")
check(f"canvas is {rd.W}x{rd.H}", img.size == (rd.W, rd.H), str(img.size))
colors = {c for _, c in img.getcolors(maxcolors=1 << 24)}
illegal = colors - set(rd.PALETTE)
check("only legal Spectra 6 inks", not illegal, str(illegal) if illegal else
      f"{len(colors)} of 6 used")

# Color coverage drives refresh time, so keep an eye on it.
total = rd.W * rd.H
chroma = sum(n for n, c in img.getcolors(maxcolors=1 << 24)
             if c not in (rd.K, rd.WH))
check("chromatic coverage under 20%", chroma / total < 0.20,
      f"{100 * chroma / total:.1f}%")

# ---- 5. bottom band ---------------------------------------------------
import json
cfg = json.load(open("config.json"))
fq = cfg.get("frequencies", {})
prim = fq.get("primary", [])
check("4 primary frequencies configured", len(prim) >= 4, f"{len(prim)} found")
check("frequencies formatted with a decimal",
      all("." in rd.fmt_mhz(f["mhz"]) for f in prim))
# Nothing should touch the last two pixel rows, or text is clipped off-panel.
px = img.load()
edge = {px[x, y] for y in (rd.H - 2, rd.H - 1) for x in range(rd.W)}
check("no content clipped at bottom edge", edge == {rd.WH}, str(edge))

print()
print("ALL CHECKS PASSED" if not FAIL else f"{len(FAIL)} FAILED: {FAIL}")
raise SystemExit(1 if FAIL else 0)
