"""Aviation math + METAR fetch for the N3005Q flight board.

Kept separate from rendering so it can be unit-checked, and so the same
formulas can be ported to C++ for the ESP32 without dragging Pillow along.
"""
import json
import math
import urllib.request
from datetime import datetime, timezone

HPA_PER_INHG = 33.8638866667
AWC = "https://aviationweather.gov/api/data"


def fetch_metar(icao):
    url = f"{AWC}/metar?ids={icao}&format=json"
    with urllib.request.urlopen(url, timeout=20) as r:
        data = json.load(r)
    if not data:
        raise RuntimeError(f"no METAR for {icao}")
    return data[0]


def fetch_taf(icao):
    url = f"{AWC}/taf?ids={icao}&format=json"
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            data = json.load(r)
        return data[0] if data else None
    except Exception:
        return None


def hpa_to_inhg(hpa):
    return hpa / HPA_PER_INHG


def station_pressure_inhg(altimeter_inhg, field_elev_ft):
    """Altimeter setting is reduced to sea level; back it out to absolute
    station pressure using the ISA pressure lapse."""
    return altimeter_inhg * (1.0 - field_elev_ft * 6.8755856e-6) ** 5.2558797


def pressure_altitude_ft(altimeter_inhg, field_elev_ft):
    return field_elev_ft + (29.9213 - altimeter_inhg) * 1000.0


def isa_temp_c(altitude_ft):
    return 15.0 - 1.98 * (altitude_ft / 1000.0)


def density_altitude_ft(altimeter_inhg, field_elev_ft, temp_c):
    """NWS density altitude, using true station pressure (not the altimeter
    setting -- that is the classic off-by-800ft mistake)."""
    p = station_pressure_inhg(altimeter_inhg, field_elev_ft)
    t_f = temp_c * 9.0 / 5.0 + 32.0
    return 145442.16 * (1.0 - (17.326 * p / (459.67 + t_f)) ** 0.235)


def density_ratio(da_ft):
    """sigma = rho / rho_sl, from density altitude in the standard atmosphere."""
    return (1.0 - 6.8755856e-6 * da_ft) ** 4.2558797


def ground_roll_factor(da_ft, turbocharged):
    """Rough multiplier on sea-level ground roll.

    Constant-thrust (turbocharged, below critical altitude): roll ~ 1/sigma,
    because liftoff TAS rises as 1/sqrt(sigma) and thrust holds.
    Normally aspirated: thrust falls with density too, so roughly 1/sigma^2.

    This is a rule-of-thumb sanity check, NOT POH data. Plan with the book.
    """
    sigma = density_ratio(da_ft)
    return 1.0 / sigma if turbocharged else 1.0 / (sigma ** 2)


def days_until(iso_date, now=None):
    now = now or datetime.now(timezone.utc)
    due = datetime.fromisoformat(iso_date).replace(tzinfo=timezone.utc)
    return (due - now).days


def parse_metar(m, field_elev_ft=None):
    """Normalize the AWC JSON into the handful of fields the card needs."""
    elev = field_elev_ft if field_elev_ft is not None else m.get("elev", 0)
    altim_inhg = hpa_to_inhg(m["altim"])
    temp_c = m["temp"]
    da = density_altitude_ft(altim_inhg, elev, temp_c)

    clouds = m.get("clouds") or []
    ceiling = None
    for c in clouds:
        if c.get("cover") in ("BKN", "OVC") and c.get("base") is not None:
            ceiling = c["base"] if ceiling is None else min(ceiling, c["base"])
    sky = m.get("cover") or (clouds[0]["cover"] if clouds else "CLR")

    return {
        "icao": m["icaoId"],
        "obs_time": datetime.fromtimestamp(m["obsTime"], tz=timezone.utc),
        "raw": m["rawOb"],
        "elev_ft": elev,
        "temp_c": temp_c,
        "dewp_c": m.get("dewp"),
        "altim_inhg": altim_inhg,
        "wdir": m.get("wdir"),
        "wspd": m.get("wspd") or 0,
        "wgst": m.get("wgst"),
        "visib": m.get("visib"),
        "sky": sky,
        "ceiling_ft": ceiling,
        "flt_cat": m.get("fltCat", "UNK"),
        "pressure_alt_ft": pressure_altitude_ft(altim_inhg, elev),
        "density_alt_ft": da,
        "isa_dev_c": temp_c - isa_temp_c(pressure_altitude_ft(altim_inhg, elev)),
        "sigma": density_ratio(da),
    }


def wind_components(wind_dir_true, speed_kt, rwy_mag_hdg, magvar_east_deg):
    """METAR wind direction is TRUE-referenced; runway numbers are MAGNETIC.
    Skipping this conversion is a ~14 degree error at KVGT."""
    wind_mag = (wind_dir_true - magvar_east_deg) % 360.0
    angle = (wind_mag - rwy_mag_hdg + 180.0) % 360.0 - 180.0
    rad = math.radians(angle)
    return {
        "rwy_offset_deg": angle,
        "headwind": speed_kt * math.cos(rad),
        "crosswind": abs(speed_kt * math.sin(rad)),
    }


def runway_analysis(wx, ac):
    """Components for every runway end, favored end first (max headwind,
    crosswind as tiebreak)."""
    if wx.get("wdir") is None or not wx.get("wspd"):
        return []
    out = []
    for r in ac["runways"]:
        c = wind_components(wx["wdir"], wx["wspd"], r["mag"],
                            ac["magvar_east_deg"])
        g = (wind_components(wx["wdir"], wx["wgst"], r["mag"],
                             ac["magvar_east_deg"]) if wx.get("wgst") else None)
        out.append({
            "id": r["id"],
            "headwind": c["headwind"],
            "crosswind": c["crosswind"],
            "gust_crosswind": g["crosswind"] if g else None,
        })
    out.sort(key=lambda r: (-r["headwind"], r["crosswind"]))
    return out


NWS = "https://api.weather.gov"
UA = {"User-Agent": "n3005q-flight-board (github.com/n3005q)"}
TFR_URL = "https://tfr.faa.gov/tfrapi/exportTfrList"


def _get_json(url, timeout=20):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def fetch_da_forecast(lat, lon, altimeter_inhg, field_elev_ft, hours=12):
    """Density altitude for the next N hours.

    Uses NWS hourly temperature with the CURRENT altimeter held constant.
    Temperature dominates DA by a wide margin, so the frozen altimeter is
    worth maybe +/- 100 ft. Labelled as an estimate on the card.
    """
    pt = _get_json(f"{NWS}/points/{lat},{lon}")
    periods = _get_json(pt["properties"]["forecastHourly"])
    out = []
    for p in periods["properties"]["periods"][:hours]:
        t_c = ((p["temperature"] - 32) * 5.0 / 9.0
               if p["temperatureUnit"] == "F" else p["temperature"])
        out.append({
            "time": datetime.fromisoformat(p["startTime"]),
            "temp_c": t_c,
            "da_ft": density_altitude_ft(altimeter_inhg, field_elev_ft, t_c),
        })
    return out


def fetch_tfrs(state):
    """Active TFRs, filtered to one state. Public, no API key."""
    try:
        rows = _get_json(TFR_URL, timeout=25)
    except Exception:
        return None
    return [r for r in rows if (r.get("state") or "").upper() == state.upper()]


def fetch_notams(icao, client_id, client_secret, limit=20):
    """FAA NOTAM API. Needs a free key from faa.gov; returns None without one."""
    if not client_id or not client_secret:
        return None
    url = (f"https://external-api.faa.gov/notamapi/v1/notams"
           f"?icaoLocation={icao}&pageSize={limit}")
    req = urllib.request.Request(url, headers={
        **UA, "client_id": client_id, "client_secret": client_secret})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.load(r).get("items", [])
    except Exception:
        return None
