"""Render the N3005Q flight board to a 600x400 six-color PNG.

Layout bands (600x400):
  0-40    header: tail, type, station, flight category, battery, obs time
  46-158  density altitude hero  |  current weather column
  168-248 density altitude forecast, next 12 hours
  257-306 runways, favored first, with wind components
  316-338 TFR / NOTAM line
  341-397 bottom band: frequencies, or currency chips if config says so

Outputs:
  card_device.png  -- nominal driver primaries, exactly 6 colors
  card_preview.png -- remapped to measured-ish Spectra 6 inks

Design rules forced by the hardware:
  * 15-30s refresh, scaling with color complexity -> color is an accent.
  * No partial refresh -> nothing that needs to tick. No clock.
  * ~180 PPI at 4in -> nothing below 12px, nothing that matters below 17px.
"""
import json
import os
import sys
from datetime import datetime, timezone

from PIL import Image, ImageDraw, ImageFont

import aviation as av

W, H = 600, 400

K = (0, 0, 0)
WH = (255, 255, 255)
R = (255, 0, 0)
Y = (255, 255, 0)
B = (0, 0, 255)
G = (0, 255, 0)
PALETTE = [K, WH, R, Y, B, G]

INK = {K: (40, 40, 42), WH: (232, 230, 221), R: (150, 48, 44),
       Y: (201, 168, 62), B: (52, 70, 122), G: (62, 106, 78)}

FD = "/usr/share/fonts/truetype/dejavu"


def font(name, size):
    return ImageFont.truetype(os.path.join(FD, name), size)


F_HERO = font("DejaVuSans-Bold.ttf", 62)
F_HERO_UNIT = font("DejaVuSans-Bold.ttf", 22)
F_RWY = font("DejaVuSans-Bold.ttf", 28)
F_H1 = font("DejaVuSans-Bold.ttf", 20)
F_LABEL = font("DejaVuSansCondensed-Bold.ttf", 13)
F_TINY = font("DejaVuSansCondensed-Bold.ttf", 13)
# 10px regular text does not survive 6-color quantization: Pillow
# anti-aliases to gray, the snap-to-palette erodes thin strokes, and
# the glyphs come apart. 12px condensed BOLD is the practical floor.
F_MICRO = font("DejaVuSansCondensed-Bold.ttf", 12)
F_VAL = font("DejaVuSans-Bold.ttf", 18)
F_VAL_SM = font("DejaVuSans-Bold.ttf", 17)
F_CHIP_L = font("DejaVuSansCondensed-Bold.ttf", 12)
F_CHIP_V = font("DejaVuSans-Bold.ttf", 21)


def text(d, xy, s, f, fill=K, anchor="la"):
    d.text(xy, s, font=f, fill=fill, anchor=anchor)


def fit(d, s, f, max_w, sep=" "):
    """Shorten to fit max_w on a word boundary. Guessing pixel widths from
    character counts is what produced every overlap in the first three drafts."""
    if d.textlength(s, font=f) <= max_w:
        return s
    parts = s.split(sep)
    while len(parts) > 1:
        parts.pop()
        cand = sep.join(parts) + "\u2026"
        if d.textlength(cand, font=f) <= max_w:
            return cand
    while s and d.textlength(s + "\u2026", font=f) > max_w:
        s = s[:-1]
    return s + "\u2026" if s else ""


def da_band(da, th):
    return (R if da >= th["da_alert_ft"]
            else Y if da >= th["da_caution_ft"] else G)


def status_days(days, warn):
    return R if days < 0 else Y if days <= warn else G


def fmt_days(days):
    return f"EXPIRED {abs(days)}d" if days < 0 else f"{days} days"


# ---------------------------------------------------------------- chips
def build_chips(cfg, now):
    warn = cfg["thresholds"]["days_warn"]
    twarn = cfg["thresholds"]["tach_warn"]
    p, m = cfg["pilot"], cfg["maintenance"]
    tach = cfg["aircraft"]["tach_hours"]
    items = []

    def dated(label, iso):
        d = av.days_until(iso, now)
        items.append({"label": label, "value": fmt_days(d),
                      "sub": datetime.fromisoformat(iso).strftime("%d%b%y").upper(),
                      "color": status_days(d, warn), "urgency": d})

    def tached(label, target):
        rem = target - tach
        items.append({"label": label, "value": f"{rem:+.1f} hr",
                      "sub": f"AT {target:.1f}",
                      "color": R if rem < 0 else Y if rem <= twarn else G,
                      "urgency": int(rem * 3)})

    dated("FLIGHT REVIEW", p["flight_review_due"])
    dated("MEDICAL " + p["medical_class"].upper(), p["medical_due"])
    dated("IFR CURRENCY", p["ifr_currency_due"])
    dated("NIGHT LANDINGS", p["night_currency_due"])
    dated("PAX LANDINGS", p["pax_currency_due"])
    dated("ANNUAL", m["annual_due"])
    dated("XPDR / STATIC", m["transponder_static_due"])
    dated("ELT BATTERY", m["elt_battery_due"])
    tached("OIL CHANGE", m["oil_change_due_tach"])
    tached("100 HR", m["next_100hr_tach"])
    items.sort(key=lambda i: i["urgency"])
    return items


# ---------------------------------------------------------------- bands
def draw_header(d, wx, cfg, battery_pct):
    ac = cfg["aircraft"]
    d.rectangle([0, 0, W, 40], fill=K)
    text(d, (14, 20), ac["tail"], F_H1, WH, anchor="lm")
    sub = f'{ac["type"]}  ·  {wx["icao"]} {ac["home_name"]}'
    text(d, (106, 21), fit(d, sub, F_TINY, 178), F_TINY, WH, anchor="lm")
    # BATTERY_SLOT x 292..344, y 8..32. Left black on purpose: the firmware
    # draws the battery percentage here after decoding the PNG.

    cat = wx["flt_cat"]
    cat_col = {"VFR": G, "MVFR": B, "IFR": R, "LIFR": R}.get(cat, Y)
    d.rectangle([350, 8, 408, 32], fill=cat_col)
    text(d, (379, 21), cat, F_VAL, K, anchor="mm")

    if battery_pct is not None:      # local preview only; CI leaves it blank
        text(d, (318, 21), f"{battery_pct}%", F_LABEL, WH, anchor="mm")
    stamp = wx["obs_time"].strftime("%d %b %H%MZ").upper()
    text(d, (W - 14, 21), stamp, F_LABEL, WH, anchor="rm")


def draw_hero(d, wx, cfg, y):
    ac, th = cfg["aircraft"], cfg["thresholds"]
    da = wx["density_alt_ft"]
    text(d, (14, y), "DENSITY ALTITUDE  ·  NOW", F_LABEL, K)

    da_s = f"{da:,.0f}"
    text(d, (12, y + 10), da_s, F_HERO, K)
    hw = d.textlength(da_s, font=F_HERO)
    text(d, (18 + hw, y + 56), "FT", F_HERO_UNIT, K, anchor="ls")
    d.rectangle([12, y + 62, 12 + hw + 44, y + 70], fill=da_band(da, th))

    text(d, (14, y + 78),
         f'PA {wx["pressure_alt_ft"]:,.0f}    ISA {wx["isa_dev_c"]:+.0f}°C'
         f'    σ {wx["sigma"]:.3f}', F_VAL_SM, K)
    grf = av.ground_roll_factor(da, ac["engine_turbocharged"])
    text(d, (14, y + 98), f"~{grf:.2f}x SL ROLL · EST ONLY, USE THE POH",
         F_TINY, K)


def draw_wx_column(d, wx, cfg, y):
    th = cfg["thresholds"]
    cx = 316
    d.line([cx - 18, y - 2, cx - 18, y + 106], fill=K, width=2)

    gust = wx["wgst"]
    windy = (wx["wspd"] >= th["wind_alert_kt"]
             or (gust and gust - wx["wspd"] >= th["gust_factor_warn_kt"]))
    wind_s = f'{wx["wdir"]:03d}° {wx["wspd"]}' + (f"G{gust}" if gust else "")
    sky = wx["sky"] + (f' {wx["ceiling_ft"]:,}' if wx["ceiling_ft"] else "")

    rows = [("WIND (TRUE)", wind_s + " KT", windy),
            ("VIS / SKY", f'{wx["visib"]} SM  {sky}', False),
            ("TEMP / DEWPT", f'{wx["temp_c"]:.0f}° / {wx["dewp_c"]:.0f}°C', False),
            ("ALTIMETER", f'{wx["altim_inhg"]:.2f}"', False)]
    ry = y
    for label, val, hot in rows:
        text(d, (cx, ry + 3), label, F_LABEL, K)
        if hot:
            vw = d.textlength(val, font=F_VAL_SM)
            d.rectangle([cx + 100, ry - 3, cx + 111 + vw, ry + 20], fill=Y,
                        outline=K, width=1)
        text(d, (cx + 106, ry), val, F_VAL_SM, K)
        ry += 27


def draw_da_forecast(d, fc, cfg, y):
    """Hourly DA bars. The afternoon peak is the number that matters in Vegas."""
    th = cfg["thresholds"]
    text(d, (14, y), "DENSITY ALTITUDE  ·  NEXT 12 HOURS  (NWS)",
         F_LABEL, K)
    if not fc:
        text(d, (14, y + 22), "forecast unavailable", F_VAL_SM, K)
        return

    peak = max(fc, key=lambda h: h["da_ft"])
    text(d, (W - 14, y), f'PEAK {peak["da_ft"]:,.0f} FT AT '
         f'{peak["time"].strftime("%H%M")}L', F_LABEL, K, anchor="ra")

    x0, x1, top, bot = 14, W - 52, y + 18, y + 66
    # Adaptive ceiling so a mild day still shows shape, floored so the
    # threshold lines stay on screen.
    scale = max(peak["da_ft"] * 1.25, th["da_caution_ft"] * 1.15)
    slot = (x1 - x0) / len(fc)
    bw = slot - 4

    def ypos(v):
        return bot - int((bot - top) * min(1.0, v / scale))

    for i, h in enumerate(fc):
        bx = int(x0 + i * slot)
        d.rectangle([bx, ypos(h["da_ft"]), bx + int(bw), bot],
                    fill=da_band(h["da_ft"], th), outline=K, width=1)
        if i % 2 == 0:
            text(d, (bx + bw / 2, bot + 4), h["time"].strftime("%H"),
                 F_TINY, K, anchor="ma")
    d.line([x0, bot, x1, bot], fill=K, width=2)

    # Threshold lines go ON TOP of the bars, or the bars swallow them.
    for thresh, col in ((th["da_caution_ft"], Y), (th["da_alert_ft"], R)):
        if thresh > scale:
            continue
        ly = ypos(thresh)
        for gx in range(x0, x1, 8):
            d.line([gx, ly, gx + 3, ly], fill=K, width=2)
        d.rectangle([x1 + 4, ly - 6, x1 + 15, ly + 5], fill=col, outline=K)
        text(d, (x1 + 19, ly - 6), f"{thresh // 1000}k", F_MICRO, K)


def draw_runway_strip(d, wx, cfg, y):
    """One box per runway end, favored first. Each end colored by its own
    crosswind against the demonstrated value, so usability reads at a glance."""
    ac = cfg["aircraft"]
    rwys = av.runway_analysis(wx, ac)
    limit = ac["max_demo_crosswind_kt"]
    text(d, (14, y), "RUNWAYS  ·  FAVORED FIRST", F_LABEL, K)
    text(d, (W - 14, y), f"TRUE->MAG 14E · DEMO XW {limit} KT",
         F_MICRO, K, anchor="ra")
    if not rwys:
        text(d, (14, y + 18), "calm / variable", F_VAL_SM, K)
        return

    bw, gap, by = 138, 8, y + 16
    for i, r in enumerate(rwys[:4]):
        x = 12 + i * (bw + gap)
        peak = r["gust_crosswind"] or r["crosswind"]
        col = R if peak > limit else Y if r["crosswind"] > limit * 0.7 else G
        d.rectangle([x, by, x + bw, by + 38], fill=WH, outline=K,
                    width=3 if i == 0 else 1)
        d.rectangle([x + 1, by + 1, x + 41, by + 37], fill=col)
        text(d, (x + 21, by + 19), r["id"], F_RWY, K, anchor="mm")

        hw = r["headwind"]
        hw_s = (f"HW {hw:.0f}" if hw >= 0 else f"TW {abs(hw):.0f}") + " KT"
        xw_s = f'XW {r["crosswind"]:.0f}'
        if r["gust_crosswind"]:
            xw_s += f' G{r["gust_crosswind"]:.0f}'
        text(d, (x + 49, by + 4), hw_s, F_VAL_SM, K)
        text(d, (x + 49, by + 23), xw_s, F_TINY, K)


TFR_ABBREV = {"SPACE OPERATIONS": "SPACE OPS", "AIR SHOWS": "AIRSHOW",
              "SECURITY (PROHIBITED)": "SECURITY", "HAZARDS": "HAZARD"}


def _tfr_kind(t):
    k = " ".join((t.get("type") or "?").split()[:2]).upper()
    return TFR_ABBREV.get(k, k)


def draw_tfr_line(d, tfrs, notams, cfg, y):
    state = cfg["aircraft"]["tfr_state"]
    if tfrs is None:
        body, col = "FETCH FAILED", Y
    elif not tfrs:
        body, col = f"NONE ACTIVE IN {state}", G
    else:
        kinds = sorted({_tfr_kind(t) for t in tfrs})
        body, col = f'{len(tfrs)} IN {state} · ' + ", ".join(kinds), Y

    d.rectangle([12, y, 58, y + 22], fill=col, outline=K, width=2)
    text(d, (35, y + 11), "TFR", F_LABEL, K, anchor="mm")

    note = "NOTAM KEY NOT SET" if notams is None else f"{len(notams)} NOTAMS"
    avail = (W - 30 - d.textlength(note, font=F_TINY)) - 66
    text(d, (66, y + 3), fit(d, body, F_VAL_SM, avail, ", "), F_VAL_SM, K)
    text(d, (W - 14, y + 6), note, F_TINY, K, anchor="ra")


def fmt_mhz(v):
    """118.05 stays, 124 becomes 124.0. A bare integer on a comm panel looks
    like a truncation bug."""
    return f"{float(v):.1f}" if "." not in str(v) else str(v)


def draw_frequencies(d, cfg, y):
    """KVGT comm frequencies. Static config data, not a live feed: these change
    on the order of years, and no free API serves them."""
    fq = cfg.get("frequencies") or {}
    prim = fq.get("primary") or []
    if not prim:
        text(d, (14, y), "no frequencies configured", F_VAL_SM, K)
        return

    bw, gap = 138, 8
    for i, f in enumerate(prim[:4]):
        x = 12 + i * (bw + gap)
        d.rectangle([x, y, x + bw, y + 38], fill=WH, outline=K, width=2)
        d.rectangle([x, y, x + bw, y + 15], fill=K)
        text(d, (x + bw / 2, y + 7), f["label"], F_CHIP_L, WH, anchor="mm")
        text(d, (x + bw / 2, y + 27), fmt_mhz(f["mhz"]), F_CHIP_V, K,
             anchor="mm")

    extras = fq.get("extras", "")
    ver = fq.get("verified", "")
    if ver:
        extras = (extras + "   ·   " if extras else "") + f"VERIFIED {ver}"
    if extras:
        text(d, (14, y + 42), fit(d, extras, F_MICRO, W - 28, "  ·  "),
             F_MICRO, K)


def draw_chips(d, chips, y):
    cw, ch, gap = 186, 51, 8
    for i, c in enumerate(chips):
        x = 12 + i * (cw + gap)
        expired = c["color"] == R
        d.rectangle([x, y, x + cw, y + ch], fill=R if expired else WH,
                    outline=K, width=2)
        d.rectangle([x + 2, y + 2, x + 12, y + ch - 2], fill=c["color"])
        fg = WH if expired else K
        subw = d.textlength(c["sub"], font=F_CHIP_L)
        text(d, (x + 19, y + 6),
             fit(d, c["label"], F_CHIP_L, cw - 34 - subw), F_CHIP_L, fg)
        text(d, (x + cw - 8, y + 6), c["sub"], F_CHIP_L, fg, anchor="ra")
        text(d, (x + 19, y + 24), c["value"], F_CHIP_V, fg)


# ---------------------------------------------------------------- card
BATTERY_SLOT = (292, 8, 344, 32)  # firmware draws into this box


def render(wx, fc, tfrs, notams, cfg, now, battery_pct=None):
    img = Image.new("RGB", (W, H), WH)
    d = ImageDraw.Draw(img)

    draw_header(d, wx, cfg, battery_pct)
    draw_hero(d, wx, cfg, 46)
    draw_wx_column(d, wx, cfg, 50)

    d.line([12, 162, W - 12, 162], fill=K, width=2)
    draw_da_forecast(d, fc, cfg, 168)

    d.line([12, 252, W - 12, 252], fill=K, width=2)
    draw_runway_strip(d, wx, cfg, 257)

    d.line([12, 312, W - 12, 312], fill=K, width=2)
    draw_tfr_line(d, tfrs, notams, cfg, 316)

    if cfg.get("bottom_band", "frequencies") == "currency":
        draw_chips(d, build_chips(cfg, now)[:cfg["thresholds"]["chips_shown"]],
                   345)
    else:
        draw_frequencies(d, cfg, 341)
    return img


def quantize_to_palette(img):
    pal = Image.new("P", (1, 1))
    flat = [v for c in PALETTE for v in c]
    flat += [0, 0, 0] * (256 - len(PALETTE))
    pal.putpalette(flat)
    return img.quantize(palette=pal, dither=Image.Dither.NONE).convert("RGB")


def to_preview(img):
    out = Image.new("RGB", img.size)
    src, dst = img.load(), out.load()
    for yy in range(img.height):
        for xx in range(img.width):
            dst[xx, yy] = INK.get(src[xx, yy], src[xx, yy])
    return out


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    out = os.environ.get("N3005Q_OUT") or os.path.join(here, "..", "public")
    out = os.path.abspath(out)
    os.makedirs(out, exist_ok=True)
    cfg = json.load(open(os.path.join(here, "config.json")))
    ac = cfg["aircraft"]
    errors = []

    # METAR is the only hard dependency. Without it there is no card worth
    # drawing, so fail the CI run loudly rather than publish a broken image.
    wx = av.parse_metar(av.fetch_metar(ac["home_icao"]))

    try:
        fc = av.fetch_da_forecast(ac["lat"], ac["lon"], wx["altim_inhg"],
                                  wx["elev_ft"], 12)
    except Exception as e:
        errors.append(f"da_forecast: {e}")
        fc = []
    tfrs = av.fetch_tfrs(ac["tfr_state"])
    if tfrs is None:
        errors.append("tfr: fetch failed")
    notams = av.fetch_notams(
        ac["home_icao"],
        os.environ.get("FAA_CLIENT_ID") or cfg.get("faa_api", {}).get("client_id"),
        os.environ.get("FAA_CLIENT_SECRET") or cfg.get("faa_api", {}).get("client_secret"))

    now = datetime.now(timezone.utc)
    img = quantize_to_palette(render(wx, fc, tfrs, notams, cfg, now))
    img.save(os.path.join(out, "card.png"))
    to_preview(img).save(os.path.join(out, "card_preview.png"))

    rwys = av.runway_analysis(wx, ac)
    peak = max(fc, key=lambda h: h["da_ft"]) if fc else None
    status = {
        "generated_at": now.isoformat(timespec="seconds"),
        "metar_obs": wx["obs_time"].isoformat(timespec="seconds"),
        "metar_raw": wx["raw"],
        "flight_category": wx["flt_cat"],
        "density_alt_ft": round(wx["density_alt_ft"]),
        "pressure_alt_ft": round(wx["pressure_alt_ft"]),
        "peak_da_ft": round(peak["da_ft"]) if peak else None,
        "peak_da_local": peak["time"].isoformat(timespec="minutes") if peak else None,
        "favored_runway": rwys[0]["id"] if rwys else None,
        "favored_crosswind_kt": round(rwys[0]["crosswind"], 1) if rwys else None,
        "tfr_count": None if tfrs is None else len(tfrs),
        "notam_count": None if notams is None else len(notams),
        "errors": errors,
    }
    with open(os.path.join(out, "status.json"), "w") as f:
        json.dump(status, f, indent=2)

    print(json.dumps(status, indent=2))
    if errors:
        print(f"\n{len(errors)} soft failure(s); card still published.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
