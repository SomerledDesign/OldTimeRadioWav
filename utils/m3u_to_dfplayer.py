#!/usr/bin/env python3
"""
Convert an M3U playlist into a DFPlayer-friendly folder layout and optionally
generate a schedule.csv for the Tiny2040 time-aligned playback.

Example:
  python utils/m3u_to_dfplayer.py --m3u playlist.m3u --out /Volumes/DFPLAYER \\
      --bitrate 64k --sample-rate 44100 --channels 1

  python utils/m3u_to_dfplayer.py --m3u playlist.m3u --out /Volumes/DFPLAYER \\
      -o /path/to/output/dir

  python utils/m3u_to_dfplayer.py --m3u playlist.m3u --out /Volumes/DFPLAYER \\
      -o stdout --verbose
"""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple
from urllib.parse import unquote


def parse_extinf_duration(line: str) -> Optional[int]:
    """
    Parse a single line from an M3U playlist for an EXTINF duration field.

    Returns an integer value representing the duration in seconds, or None if the
    line does not contain a valid EXTINF duration field.

    Example:

    #EXTINF:123.456,Some comment text
    #EXTINF:123.456
    #EXTINF:123.456,Some comment text, with a comma
    #EXTINF:123.456, Some comment text, without a comma

    Valid input lines will be parsed and returned as an integer value in seconds.
    Invalid input lines will return None.

    :param line: The line from the M3U playlist to parse.
    :return: An optional integer value representing the duration in seconds.
    """
    line = line.strip()
    if not line.upper().startswith("#EXTINF:"):
        return None
    try:
        payload = line.split(":", 1)[1]
    except IndexError:
        return None
    if "," in payload:
        payload = payload.split(",", 1)[0]
    payload = payload.strip()
    if not payload:
        return None
    try:
        seconds = float(payload)
    except ValueError:
        return None
    if seconds <= 0:
        return None
    return max(1, int(round(seconds)))


def normalize_m3u_path(entry: str) -> str:
    value = entry.strip()
    lower = value.lower()
    if lower.startswith("file://localhost/"):
        value = value[len("file://localhost/"):]
    elif lower.startswith("file:///"):
        value = value[len("file:///"):]
    elif lower.startswith("file://"):
        value = value[len("file://"):]
    return unquote(value)


def load_m3u_entries(m3u_path: Path) -> List[Tuple[Path, Optional[int]]]:
    entries: List[Tuple[Path, Optional[int]]] = []
    pending_duration: Optional[int] = None
    base_dir = m3u_path.parent

    with m3u_path.open("r", errors="ignore") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith("\ufeff"):
                line = line.lstrip("\ufeff")
            if line.startswith("#"):
                duration = parse_extinf_duration(line)
                if duration:
                    pending_duration = duration
                continue
            normalized = normalize_m3u_path(line)
            src = Path(normalized)
            if not src.is_absolute():
                src = base_dir / src
            entries.append((src, pending_duration))
            pending_duration = None

    return entries


def ensure_tool(name: str, override: Optional[str] = None) -> str:
    if override:
        return override
    resolved = shutil.which(name)
    if not resolved:
        raise RuntimeError(f"Required tool not found: {name}")
    return resolved


def run_ffmpeg(ffmpeg: str, src: Path, dst: Path, bitrate: str, sample_rate: int, channels: int) -> None:
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(src),
        "-ac",
        str(channels),
        "-ar",
        str(sample_rate),
        "-b:a",
        bitrate,
        "-minrate",
        bitrate,
        "-maxrate",
        bitrate,
        "-bufsize",
        bitrate,
        "-codec:a",
        "libmp3lame",
        str(dst),
    ]
    subprocess.run(cmd, check=True)


def probe_duration(ffprobe: str, src: Path) -> Optional[int]:
    cmd = [
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(src),
    ]
    try:
        result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError:
        return None
    value = result.stdout.strip()
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    if seconds <= 0:
        return None
    return max(1, int(round(seconds)))


def log(message: str, verbose: bool) -> None:
    if verbose:
        print(message, file=sys.stderr)


def write_schedule(schedule_path: Path, schedule_to_stdout: bool,
                   rows: List[Tuple[int, int, int]]) -> None:
    lines = ["# folder,track,duration_s"]
    for folder, track, duration in rows:
        lines.append(f"{folder:02d},{track:03d},{duration}")
    payload = "\n".join(lines) + "\n"
    if schedule_to_stdout:
        sys.stdout.write(payload)
        return
    schedule_path.write_text(payload)


def main() -> int:
    class CustomHelpFormatter(argparse.RawTextHelpFormatter, argparse.ArgumentDefaultsHelpFormatter):
        pass

    description = (
        "Convert an M3U playlist into a DFPlayer Mini folder/track layout and generate schedule.csv.\n"
        "Each playlist entry becomes a sequential track (001..255) inside numbered folders (01..99).\n"
        "Schedule order matches playlist order and is intended for Tiny2040 time alignment."
    )
    epilog = (
        "Examples:\n"
        "  python utils/m3u_to_dfplayer.py --m3u playlist.m3u --out /Volumes/DFPLAYER\n"
        "  python utils/m3u_to_dfplayer.py --m3u playlist.m3u --out /Volumes/DFPLAYER -o /tmp/output\n"
        "  python utils/m3u_to_dfplayer.py --m3u playlist.m3u --out /Volumes/DFPLAYER -o stdout --verbose\n"
        "  python utils/m3u_to_dfplayer.py --m3u playlist.m3u --out /Volumes/DFPLAYER --dry-run\n"
        "\n"
        "Notes:\n"
        "  - M3U entries may be relative paths or file:/// URLs; URL-encoded characters are decoded.\n"
        "  - schedule.csv uses the format: folder,track,duration_s (week starts Monday 00:00:00).\n"
        "  - Duration comes from #EXTINF or ffprobe on the source file.\n"
        "  - Stdout schedule output requires --verbose.\n"
        "  - DFPlayer playback order depends on FAT copy order; copy files in order to the SD card.\n"
        "    If you re-copy, format the card and copy sequentially to preserve order.\n"
        "  - Copy example (macOS/Linux): rsync -a --inplace /Volumes/DFPLAYER/ /Volumes/SDCARD/\n"
        "    Then run: dot_clean /Volumes/SDCARD\n"
        "  - Copy example (PowerShell): robocopy \"C:\\\\DFPLAYER\" \"E:\\\\SDCARD\" /E /COPY:DAT\n"
        "  - Copy example (CMD): xcopy \"C:\\\\DFPLAYER\" \"E:\\\\SDCARD\" /E /I /Y"
    )
    parser = argparse.ArgumentParser(
        description=description,
        epilog=epilog,
        formatter_class=CustomHelpFormatter,
    )
    parser.add_argument(
        "--m3u",
        required=True,
        type=Path,
        help="Path to the input .m3u playlist; non-comment lines are treated as file paths.",
    )
    parser.add_argument(
        "--out",
        required=True,
        type=Path,
        help="Output root directory for DFPlayer folders (01..99); created if missing.",
    )
    parser.add_argument(
        "--bitrate",
        default="64k",
        help="Target MP3 bitrate (CBR recommended for DFPlayer).",
    )
    parser.add_argument(
        "--sample-rate",
        type=int,
        default=44100,
        help="Target sample rate in Hz (e.g., 44100).",
    )
    parser.add_argument(
        "--channels",
        type=int,
        default=1,
        help="Audio channels (1=mono, 2=stereo).",
    )
    parser.add_argument(
        "--tracks-per-folder",
        type=int,
        default=255,
        help="Tracks per folder (DFPlayer supports 1..255).",
    )
    parser.add_argument(
        "--start-folder",
        type=int,
        default=1,
        help="Starting folder number (01..99).",
    )
    parser.add_argument(
        "-o",
        "--schedule-out",
        default=".",
        help="Output directory for schedule.csv, or '-'/'stdout' for stdout.",
    )
    parser.add_argument(
        "--ffmpeg",
        default=None,
        help="Override ffmpeg executable path (required for transcoding).",
    )
    parser.add_argument(
        "--ffprobe",
        default=None,
        help="Override ffprobe executable path (used for duration probing).",
    )
    parser.add_argument(
        "--no-ffprobe",
        action="store_true",
        help="Disable ffprobe; requires #EXTINF durations in the playlist.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show planned actions without running ffmpeg.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose logging to stderr (also allows stdout schedule output).",
    )
    args = parser.parse_args()

    try:
        ffmpeg = ensure_tool("ffmpeg", args.ffmpeg)
    except RuntimeError as exc:
        print(exc)
        return 1

    schedule_out_value = args.schedule_out if args.schedule_out is not None else "."
    schedule_out_raw = str(schedule_out_value).strip()
    if schedule_out_raw.lower() == "stdout":
        schedule_out_raw = "-"
    if schedule_out_raw == "-" and not args.verbose:
        schedule_out_raw = "."
        print("stdout schedule output requires --verbose; writing ./schedule.csv instead", file=sys.stderr)

    schedule_to_stdout = schedule_out_raw == "-"
    if schedule_to_stdout:
        schedule_out_path = Path("schedule.csv")
    else:
        schedule_out_dir = Path(schedule_out_raw)
        if schedule_out_dir.suffix.lower() == ".csv":
            print(f"schedule output expects a directory; using {schedule_out_dir.parent}", file=sys.stderr)
            schedule_out_dir = schedule_out_dir.parent if str(schedule_out_dir.parent) else Path(".")
        schedule_out_dir.mkdir(parents=True, exist_ok=True)
        schedule_out_path = schedule_out_dir / "schedule.csv"

    ffprobe = None
    if not args.no_ffprobe:
        try:
            ffprobe = ensure_tool("ffprobe", args.ffprobe)
        except RuntimeError as exc:
            print(exc, file=sys.stderr)
            return 1

    if args.tracks_per_folder < 1 or args.tracks_per_folder > 255:
        print("tracks-per-folder must be 1..255", file=sys.stderr)
        return 1
    if args.start_folder < 1 or args.start_folder > 99:
        print("start-folder must be 1..99", file=sys.stderr)
        return 1

    entries = load_m3u_entries(args.m3u)
    if not entries:
        print("No tracks found in M3U.", file=sys.stderr)
        return 1

    out_root = args.out
    out_root.mkdir(parents=True, exist_ok=True)

    schedule_rows: List[Tuple[int, int, int]] = []
    total = len(entries)
    folder = args.start_folder
    track = 1

    for idx, (src, extinf_duration) in enumerate(entries, start=1):
        if track > args.tracks_per_folder:
            folder += 1
            track = 1
        if folder > 99:
            print("Exceeded folder limit (99).", file=sys.stderr)
            return 1
        if not src.exists():
            print(f"Missing source file: {src}", file=sys.stderr)
            return 1

        folder_name = f"{folder:02d}"
        track_name = f"{track:03d}.mp3"
        dst_dir = out_root / folder_name
        dst_dir.mkdir(parents=True, exist_ok=True)
        dst = dst_dir / track_name

        log(f"[{idx}/{total}] {src} -> {dst}", args.verbose)
        if not args.dry_run:
            try:
                run_ffmpeg(ffmpeg, src, dst, args.bitrate, args.sample_rate, args.channels)
            except subprocess.CalledProcessError as exc:
                print(f"ffmpeg failed for {src}: {exc}", file=sys.stderr)
                return 1

        duration = extinf_duration
        if duration is None and ffprobe:
            duration = probe_duration(ffprobe, src)
        if duration is None:
            print(f"Missing duration for {src}. Provide EXTINF or enable ffprobe.", file=sys.stderr)
            return 1
        schedule_rows.append((folder, track, duration))

        track += 1

    write_schedule(schedule_out_path, schedule_to_stdout, schedule_rows)
    if schedule_to_stdout:
        log("Wrote schedule to stdout.", args.verbose)
    else:
        log(f"Wrote schedule: {schedule_out_path}", args.verbose)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
