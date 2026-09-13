# Device notes: N3005Q board on the M5Paper Color

How this specific install is set up, and what went wrong setting it up.
The README covers the design; this covers operating and fixing it.

## Current setup (2026-09-12)

| | |
|---|---|
| Hardware | M5Stack M5Paper Color, ESP32-S3, MAC `28:84:85:43:de:20` |
| USB port on the Mac | `/dev/cu.usbmodem101` (native USB, no driver needed) |
| Mounting | Vertical frame, so the card is **portrait, 400x600** |
| Card URL on device | `https://raw.githubusercontent.com/finite0001/n3005q-board/output/public/card.png` |
| Wi-Fi | `CCFD31` (2.4 GHz), stored in `firmware/n3005q_board/config.h` (gitignored) |
| Time zone | Pacific (`TZ_STRING` in `config.h`) |
| Flash command | `zsh ~/n3005q-flash.sh` (wraps `bash setup.sh --skip-git`) |

`config.h` holds the Wi-Fi password and exists only on this Mac. Do not
overwrite or delete it. If it is lost, `setup.sh` prompts for the Wi-Fi
settings again.

## Refresh schedule

| Local time | Device wakes |
|---|---|
| 05:00 to 21:00 | every 30 min, aligned to :00 and :30 |
| 21:00 to 05:00 | every 2 hours |

- Each wake sends `If-None-Match`. If the server replies 304 (unchanged), the device goes straight
  back to sleep with no redraw (about 5 s of radio).
- A redraw only happens when the card changed, and takes 15 to 30 s.
- GitHub Actions renders every 30 min, often 10 to 30 min late, and skips
  publishing if the image is identical.
- The KVGT METAR updates hourly (about :53), so **the screen changes about once an
  hour** in practice.
- The header time is the METAR observation time, not the time of the last redraw.
- Battery: about 4 to 5 weeks. Setting `DAY_INTERVAL_MIN` to 60 roughly doubles it (needs a reflash).

## Troubleshooting

**Screen didn't change after a flash.** Check this first. With the default
esptool `hard_reset`, the chip stayed in the ROM bootloader after upload, so
the new firmware never ran. `platformio.ini` now sets
`board_upload.after_reset = watchdog_reset` to fix it. To check:

```
.venv/bin/python -m esptool --chip esp32s3 -p /dev/cu.usbmodem101 \
  --before no_reset --after no_reset chip_id
```

If that connects, the chip is sitting in download mode. Boot it with
`--after watchdog_reset` in place of `--after no_reset`.

**No serial output at all.** The firmware isn't running (see above), or it is in
deep sleep. A sleeping chip also refuses uploads, so wait for the next
:00/:30 wake or hold the side reset button about 3 s for download mode. For startup
checkpoints, build with `PLATFORMIO_BUILD_FLAGS=-DBOOT_TRACE`. It adds a 6 s
startup delay so the Mac can reattach to USB, so don't leave it on the device.

**Old card still showing.** A failed fetch leaves the panel untouched by design,
so an unchanged screen doesn't prove the firmware is running. To tell layouts
apart: the portrait header reads `N3005Q KVGT` and the runways are a 2x2 grid.
The old landscape card reads `N3005Q PA28RT-201T · KVGT` and has 4 runways in a row.

**Card is stale.** Check the Actions tab. Scheduled workflows are disabled after
60 days without repo activity. The STALE banner does not work with
raw.githubusercontent.com hosting because it sends no `Last-Modified` header.

**Upside down.** Add `#define DISPLAY_ROTATION 2` to `config.h` and reflash.

## Hosting notes

- Vercel project `render` (team `daves-projects-e0da43ba`,
  `render-green-zeta.vercel.app`) is configured correctly (production branch `output`,
  output dir `public`, no build, non-`output` branches skipped) but the
  device does not use it. Its certificate chains to GTS Root R1 / GlobalSign.
  Whether that validates on the device was never tested, because the
  download-mode problem above was the real blocker at the time. Switching to Vercel would enable the
  STALE banner. Test it by changing `CARD_URL` and reflashing.
- NOTAMs show "KEY NOT SET" until `FAA_CLIENT_ID` / `FAA_CLIENT_SECRET` repo
  secrets are added (free from api.faa.gov).
