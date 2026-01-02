# Utils

Helper scripts for building playlists and preparing DFPlayer-compatible SD card
contents for the Old Time Radio project.

### Workflow (end-to-end)

<table border cellpadding = "0"cellspacing = "0">
  <tr>
    <td>1. Generate an M3U playlist with `generate_radio_playlist.py`.</td>
    <td rowspan="3">
      <img src="img/fcoM.gif" alt="FAT copy order Maters" height="120" width = "120" >
    </td>
  </tr>
  <tr>
    <td>2. Convert the M3U into DFPlayer folder/track layout and `schedule.csv` with
   `m3u_to_dfplayer.py`.</td>
  </tr>
  <tr>
    <td>3. Copy the output folders to the SD card in order (FAT copy order matters).</td>
  </tr>
</table>

__generate_radio_playlist.py__
- Builds an M3U playlist for N days using the per-hour structure:
  IDs -> newscast (if scheduled) -> 2 commercials -> songs to :30 ->
  IDs -> 1 commercial -> songs to :00.
- Sources audio from user-selected directories via `--songs-dir`,
  `--commercials-dir`, `--newscasts-dir`, and `--ids-dir`.
- Newscasts can be day-specific by using subfolders named `mon..sun` or `1..7`
  inside the newscasts directory; otherwise all newscasts are used.
- Uses `ffprobe` to calculate durations and writes `#EXTINF` lines.
- Uses a cache file (default `.playlist_durations.json`) to avoid re-probing
  unchanged files.
- Supports `--relative-to` to emit relative M3U paths when possible.
- Supports YAML config via `--config` or `--write-config`.

__m3u_to_dfplayer.py__
- Reads an M3U playlist and converts each entry to DFPlayer-compatible naming:
  folders `01..99` and tracks `001..255`.
- Transcodes audio to CBR MP3 with `ffmpeg` (default 64 kbps, mono).
- Generates `schedule.csv` by default (format: folder,track,duration_s).
- Can write the schedule to stdout with `-o stdout --verbose`.
- Handles `file:///` URLs and URL-encoded paths from common playlist tools.

~radio_playlist_config.example.yaml~
- Example YAML config with comments and all supported fields.
- Paths may be absolute or relative to the config file.
- `relative_to` controls how M3U entries are written.

Notes
- `ffprobe` and `ffmpeg` must be installed and on PATH (or pass
  `--ffprobe`/`--ffmpeg`).
- `schedule.csv` is chronological from Monday 00:00:00 and is used by
  `src/main.py` to align playback to the current time.
- DFPlayer playback order follows FAT copy order; copy files in order to the SD
  card. On macOS, run `dot_clean /Volumes/SDCARD` after copying.
