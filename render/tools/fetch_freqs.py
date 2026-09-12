"""Regenerate the frequency block in config.json from OurAirports.

OurAirports publishes a CC0 dataset of airport frequencies. It is community
maintained and CAN lag the FAA Chart Supplement, so this is a convenience for
refreshing config, not an authority. Cross-check anything that changes.

    python3 tools/fetch_freqs.py KVGT
"""
import csv, io, json, os, sys, urllib.request
from datetime import date

URL = "https://davidmegginson.github.io/ourairports-data/airport-frequencies.csv"
# OurAirports type code -> (card label, sort order)
WANT = {"ATIS": ("ATIS", 0), "CLD": ("CLNC DEL", 1),
        "GND": ("GROUND", 2), "TWR": ("TOWER", 3)}
EXTRA = {"UNIC": "UNICOM", "A/D": "APP/DEP", "APP": "APP", "CTAF": "CTAF"}


def main(icao):
    raw = urllib.request.urlopen(URL, timeout=60).read().decode()
    rows = [r for r in csv.DictReader(io.StringIO(raw))
            if r["airport_ident"].upper() == icao.upper()]
    if not rows:
        sys.exit(f"no frequencies found for {icao}")

    primary, extras = [], []
    for r in rows:
        t, mhz = r["type"].upper(), r["frequency_mhz"]
        if t in WANT:
            label, order = WANT[t]
            primary.append({"label": label, "mhz": mhz, "_o": order})
        elif t in EXTRA:
            extras.append(f'{EXTRA[t]} {mhz}')
    primary.sort(key=lambda f: f.pop("_o"))

    here = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    path = os.path.join(here, "config.json")
    cfg = json.load(open(path))
    cfg["frequencies"] = {
        "_comment": "From OurAirports (CC0). Community data, can lag the Chart "
                    "Supplement. Verify before relying on it in the airplane.",
        "primary": primary,
        "extras": "  ·  ".join(dict.fromkeys(extras)),
        "verified": date.today().isoformat(),
    }
    json.dump(cfg, open(path, "w"), indent=2)
    for f in primary:
        print(f'{f["label"]:10s} {f["mhz"]}')
    print("extras:", cfg["frequencies"]["extras"])


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "KVGT")
