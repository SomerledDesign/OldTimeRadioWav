# Old Time Radio Wav

Old-time radio playback on a Tiny2040 + DFPlayer Mini, with a time-aligned
playlist so the device sounds like a live broadcast when it powers on.

## Status

- Time alignment is implemented in `src/main.py` using a DS3231 RTC and
  `schedule.csv`.
- Button controls are implemented (tap, double, triple, long press).
- Helper scripts generate an M3U and convert it into DFPlayer folder/track
  layout plus the `schedule.csv`.

## How It Works

1. `utils/generate_radio_playlist.py` builds an M3U playlist that follows a
   radio-style hour structure (IDs, commercials, songs, newscasts).
2. `utils/m3u_to_dfplayer.py` converts the M3U into DFPlayer folders `01..99`
   and tracks `001..255`, and writes `schedule.csv`.
3. `src/main.py` reads the schedule, checks the RTC, and starts playback at
   the correct point in the week.

## Requirements

- DFPlayer Mini + MicroSD (FAT32, 32GB max recommended).
- Tiny2040 (MicroPython) and DS3231 RTC module.
- `ffmpeg` and `ffprobe` installed for playlist conversion.
- `AMradioSound.wav` must be mono 8-bit PCM WAV (required by PWM playback).

## Project Layout

- `src/main.py` - Tiny2040 firmware (time alignment, playback, controls).
- `utils/` - playlist generation and DFPlayer conversion scripts.
- `Raspberry Pi/` - legacy reference copy (zionbrock baseline).
- `Archive/` - archived files and references.

## Typical Workflow

1. Create a playlist:
   - Use `utils/generate_radio_playlist.py` with a YAML config.
2. Build the DFPlayer layout:
   - Run `utils/m3u_to_dfplayer.py` to create folders and `schedule.csv`.
3. Copy files to the SD card in order (FAT copy order matters).
4. Copy `schedule.csv` and `AMradioSound.wav` to the Tiny2040 filesystem.

See `utils/README.md` for detailed command examples and options.

## Quick Start

1. Create a config file:
   - `python utils/generate_radio_playlist.py --write-config radio_playlist_config.yaml`
2. Edit the paths in `radio_playlist_config.yaml`, then generate a playlist:
   - `python utils/generate_radio_playlist.py --config radio_playlist_config.yaml`
3. Convert the playlist into DFPlayer folders + schedule:
   - `python utils/m3u_to_dfplayer.py --m3u playlist.m3u --out /Volumes/DFPLAYER`

## Notes

- DFPlayer playback order depends on FAT copy order. Format the card and copy
  files sequentially to preserve order.
- `schedule.csv` is chronological from Monday 00:00:00 and must match the SD
  card contents.

## Next Steps

- Deploy `src/main.py` to the Tiny2040 with MicroPico.
- Validate the RTC time and alignment on first boot.
