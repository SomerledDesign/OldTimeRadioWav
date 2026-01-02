#!/usr/bin/env python3
"""
Generate an M3U playlist for an AM-style radio schedule from category folders.
"""

import argparse
import json
import random
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple


DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DAY_ALIASES = {
    "mon": 0,
    "monday": 0,
    "tue": 1,
    "tues": 1,
    "tuesday": 1,
    "wed": 2,
    "weds": 2,
    "wednesday": 2,
    "thu": 3,
    "thur": 3,
    "thurs": 3,
    "thursday": 3,
    "fri": 4,
    "friday": 4,
    "sat": 5,
    "saturday": 5,
    "sun": 6,
    "sunday": 6,
    "1": 0,
    "2": 1,
    "3": 2,
    "4": 3,
    "5": 4,
    "6": 5,
    "7": 6,
}


CONFIG_TEMPLATE = """# Radio Playlist Generator config (YAML)
# Paths can be absolute or relative to this file.
songs_dir: /path/to/songs
commercials_dir: /path/to/commercials
newscasts_dir: /path/to/newscasts
ids_dir: /path/to/ids

# If set, playlist entries are written relative to this path when possible.
relative_to: /path/to

# Output playlist file (relative to this file if not absolute).
out: playlist.m3u

# Schedule behavior.
days: 30
start_dow: mon
newscast_hours: 6,12,18,22
station_ids_per_break: 1
commercials_first_half: 2
commercials_second_half: 1

# Audio selection.
extensions: mp3
max_overrun: 60
seed:

# Tools and cache.
ffprobe:
cache: .playlist_durations.json
"""


DEFAULTS = {
    "days": 30,
    "start_dow": "mon",
    "newscast_hours": "6,12,18,22",
    "station_ids_per_break": 1,
    "commercials_first_half": 2,
    "commercials_second_half": 1,
    "max_overrun": 60,
    "extensions": "mp3",
    "out": "playlist.m3u",
    "cache": ".playlist_durations.json",
}


@dataclass
class Track:
    path: Path
    duration: int


class CyclePicker:
    def __init__(self, items: Sequence[Track], rng: random.Random) -> None:
        if not items:
            raise ValueError("No items available for picker.")
        self._items = list(items)
        self._rng = rng
        self._pool: List[Track] = []

    def next(self) -> Track:
        if not self._pool:
            self._pool = list(self._items)
            self._rng.shuffle(self._pool)
        return self._pool.pop(0)


def log(message: str, verbose: bool) -> None:
    if verbose:
        print(message, file=sys.stderr)


def ensure_tool(name: str, override: Optional[str] = None) -> str:
    if override:
        return override
    resolved = shutil.which(name)
    if not resolved:
        raise RuntimeError(f"Required tool not found: {name}")
    return resolved


def parse_extensions(value: str) -> List[str]:
    parts = [p.strip().lower() for p in value.split(",") if p.strip()]
    result = []
    for ext in parts:
        if not ext.startswith("."):
            ext = "." + ext
        result.append(ext)
    return result


def strip_inline_comment(value: str) -> str:
    if "#" in value:
        value = value.split("#", 1)[0]
    return value.strip()


def parse_simple_yaml(text: str) -> Dict[str, object]:
    data: Dict[str, object] = {}
    current_key: Optional[str] = None
    list_acc: Optional[List[str]] = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("-") and current_key:
            value = strip_inline_comment(line[1:].strip())
            if value:
                list_acc.append(value)
                data[current_key] = list_acc
            continue
        if ":" in line:
            key, value = line.split(":", 1)
            key = key.strip()
            value = strip_inline_comment(value.strip())
            if value == "":
                current_key = key
                list_acc = []
                data[key] = list_acc
            else:
                current_key = None
                list_acc = None
                data[key] = value
    return data


def load_config(path: Path) -> Dict[str, object]:
    raw = path.read_text()
    try:
        import yaml  # type: ignore
    except ImportError:
        yaml = None
    if yaml is not None:
        data = yaml.safe_load(raw)
        if data is None:
            return {}
        if not isinstance(data, dict):
            raise RuntimeError("Config must be a mapping of keys to values.")
        return data
    return parse_simple_yaml(raw)


def coerce_int(value: object, fallback: int) -> int:
    if value is None:
        return fallback
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except ValueError:
        return fallback


def coerce_optional_int(value: object) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    raw = str(value).strip()
    if raw == "":
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def find_audio_files(root: Path, exts: Sequence[str]) -> List[Path]:
    files: List[Path] = []
    for path in root.rglob("*"):
        if path.is_file() and path.suffix.lower() in exts:
            files.append(path)
    return sorted(files)


def load_cache(path: Path) -> Dict[str, Dict[str, float]]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def save_cache(path: Path, cache: Dict[str, Dict[str, float]]) -> None:
    path.write_text(json.dumps(cache, indent=2, sort_keys=True))


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


def load_tracks(
    files: Sequence[Path],
    ffprobe: str,
    cache: Dict[str, Dict[str, float]],
    verbose: bool,
) -> List[Track]:
    tracks: List[Track] = []
    for path in files:
        resolved = path.resolve()
        cache_key = str(resolved)
        mtime = resolved.stat().st_mtime
        cached = cache.get(cache_key)
        duration = None
        if cached and cached.get("mtime") == mtime:
            duration = int(cached.get("duration", 0))
        if not duration:
            duration = probe_duration(ffprobe, resolved)
            if duration is None:
                raise RuntimeError(f"Unable to read duration for {path}")
            cache[cache_key] = {"mtime": mtime, "duration": duration}
            log(f"Probed duration {duration}s for {resolved}", verbose)
        tracks.append(Track(path=path, duration=duration))
    return tracks


def build_newscast_map(
    newscasts_root: Path, exts: Sequence[str]
) -> Tuple[Dict[int, List[Path]], List[Path]]:
    day_map: Dict[int, List[Path]] = {}
    for child in newscasts_root.iterdir():
        if not child.is_dir():
            continue
        alias = DAY_ALIASES.get(child.name.lower())
        if alias is None:
            continue
        files = find_audio_files(child, exts)
        if files:
            day_map[alias] = files
    all_files = find_audio_files(newscasts_root, exts)
    return day_map, all_files


def parse_day_name(value: str) -> int:
    key = value.strip().lower()
    if key not in DAY_ALIASES:
        raise ValueError(f"Invalid day name: {value}")
    return DAY_ALIASES[key]


def parse_hours(value: str) -> List[int]:
    raw = value.strip().lower()
    if raw in ("", "none", "off"):
        return []
    hours: List[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        hour = int(part)
        if hour < 0 or hour > 23:
            raise ValueError(f"Invalid hour: {hour}")
        hours.append(hour)
    return sorted(set(hours))


def select_song_for_remaining(
    picker: CyclePicker,
    remaining: int,
    max_overrun: int,
    attempts: int,
) -> Optional[Track]:
    for _ in range(attempts):
        track = picker.next()
        if track.duration <= remaining:
            return track
        if remaining <= max_overrun and (track.duration - remaining) <= max_overrun:
            return track
    return None


def format_m3u_path(path: Path, relative_to: Optional[Path], warned: set, verbose: bool) -> str:
    resolved = path.resolve()
    if relative_to:
        try:
            rel = resolved.relative_to(relative_to)
            return rel.as_posix()
        except ValueError:
            if resolved not in warned:
                warned.add(resolved)
                log(f"Path outside relative_to, using absolute: {resolved}", verbose)
            return str(resolved)
    return str(resolved)


def main() -> int:
    class CustomHelpFormatter(argparse.RawTextHelpFormatter, argparse.ArgumentDefaultsHelpFormatter):
        pass

    description = (
        "Generate an M3U playlist for an AM-style radio schedule.\n"
        "Per hour: IDs -> newscast (if scheduled) -> 2 commercials -> songs to :30 -> "
        "IDs -> 1 commercial -> songs to top of hour."
    )
    epilog = (
        "Configuration:\n"
        "  --config loads a YAML file; --write-config writes a commented template.\n"
        "  Paths in config may be absolute or relative to the config file.\n"
        "\n"
        "Newscasts by day of week:\n"
        "  If the newscasts folder contains subfolders named mon..sun (or 1..7),\n"
        "  those are preferred for the matching day. Otherwise all newscasts are used.\n"
        "\n"
        "Examples:\n"
        "  python utils/generate_radio_playlist.py --config radio_playlist.yaml\n"
        "  python utils/generate_radio_playlist.py --songs-dir /Music --commercials-dir /Ads \\\n"
        "      --newscasts-dir /News --ids-dir /IDs --out radio.m3u\n"
        "  python utils/generate_radio_playlist.py --days 7 --seed 42\n"
        "\n"
        "Notes:\n"
        "  - Output M3U entries are absolute unless --relative-to is provided.\n"
        "  - #EXTINF durations come from ffprobe; a cache speeds up re-runs.\n"
        "  - Use the generated M3U with utils/m3u_to_dfplayer.py to build the SD card.\n"
    )
    parser = argparse.ArgumentParser(
        description=description,
        epilog=epilog,
        formatter_class=CustomHelpFormatter,
    )
    parser.add_argument("--config", type=Path, default=None, help="YAML config file path.")
    parser.add_argument(
        "--write-config",
        nargs="?",
        const="radio_playlist_config.yaml",
        default=None,
        help="Write a commented YAML template and exit.",
    )
    parser.add_argument("--out", type=Path, default=None, help="Output M3U file path (default: playlist.m3u).")
    parser.add_argument("--days", type=int, default=None, help="Number of days to generate (default: 30).")
    parser.add_argument("--start-dow", default=None, help="Start day of week (mon..sun or 1..7).")
    parser.add_argument(
        "--songs-dir",
        default=None,
        help="Songs directory path (required).",
    )
    parser.add_argument(
        "--commercials-dir",
        default=None,
        help="Commercials directory path (required if commercials > 0).",
    )
    parser.add_argument(
        "--newscasts-dir",
        default=None,
        help="Newscasts directory path (required if newscasts are scheduled).",
    )
    parser.add_argument(
        "--ids-dir",
        default=None,
        help="Station IDs directory path (required if IDs per break > 0).",
    )
    parser.add_argument(
        "--relative-to",
        default=None,
        help="Write M3U entries relative to this directory when possible.",
    )
    parser.add_argument(
        "--station-ids-per-break",
        type=int,
        default=None,
        help="Station IDs at each break (default: 1).",
    )
    parser.add_argument(
        "--commercials-first-half",
        type=int,
        default=None,
        help="Commercials after the first break (default: 2).",
    )
    parser.add_argument(
        "--commercials-second-half",
        type=int,
        default=None,
        help="Commercials after the second break (default: 1).",
    )
    parser.add_argument(
        "--newscast-hours",
        default=None,
        help="Comma list of hours to insert newscasts (default: 6,12,18,22).",
    )
    parser.add_argument(
        "--max-overrun",
        type=int,
        default=None,
        help="Allowed song overrun near boundaries (default: 60).",
    )
    parser.add_argument(
        "--extensions",
        default=None,
        help="Comma list of audio extensions to include (default: mp3).",
    )
    parser.add_argument("--seed", type=int, default=None, help="Random seed for repeatable output.")
    parser.add_argument("--ffprobe", default=None, help="Override ffprobe executable path.")
    parser.add_argument("--cache", default=None, help="Duration cache JSON path (default: .playlist_durations.json).")
    parser.add_argument("--no-cache", action="store_true", help="Disable duration cache.")
    parser.add_argument("--dry-run", action="store_true", help="Do not write output M3U.")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose logging to stderr.")
    args = parser.parse_args()

    if args.write_config:
        output = Path(args.write_config)
        if not output.is_absolute():
            output = Path.cwd() / output
        if output.exists():
            print(f"Config already exists: {output}", file=sys.stderr)
            return 1
        output.write_text(CONFIG_TEMPLATE)
        print(f"Wrote config template to {output}")
        return 0

    config: Dict[str, object] = {}
    config_base = Path.cwd()
    if args.config:
        if not args.config.exists():
            print(f"Config file not found: {args.config}", file=sys.stderr)
            return 1
        try:
            config = load_config(args.config)
        except RuntimeError as exc:
            print(exc, file=sys.stderr)
            return 1
        config_base = args.config.parent

    def pick_value(key: str, cli_value: object, default: object) -> object:
        if cli_value is not None:
            return cli_value
        if key in config and config[key] is not None:
            return config[key]
        return default

    default_base = config_base if config else Path.cwd()

    def pick_path(key: str, cli_value: object, default: object) -> Tuple[object, Path]:
        if cli_value is not None:
            return cli_value, Path.cwd()
        if key in config and config[key] is not None:
            return config[key], config_base
        return default, default_base

    out_value, out_base = pick_path("out", args.out, DEFAULTS["out"])
    out_path = Path(out_value)
    if not out_path.is_absolute():
        out_path = (out_base / out_path).resolve()
    out_parent = out_path.parent
    out_parent.mkdir(parents=True, exist_ok=True)

    days = coerce_int(pick_value("days", args.days, DEFAULTS["days"]), DEFAULTS["days"])
    if days < 1:
        print("days must be >= 1", file=sys.stderr)
        return 1

    start_dow_value = pick_value("start_dow", args.start_dow, DEFAULTS["start_dow"])
    try:
        start_dow = parse_day_name(str(start_dow_value))
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1

    newscast_hours_value = pick_value("newscast_hours", args.newscast_hours, DEFAULTS["newscast_hours"])
    if isinstance(newscast_hours_value, list):
        newscast_hours_value = ",".join(str(item) for item in newscast_hours_value)
    try:
        newscast_hours = parse_hours(str(newscast_hours_value))
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1

    ids_per_break = coerce_int(
        pick_value("station_ids_per_break", args.station_ids_per_break, DEFAULTS["station_ids_per_break"]),
        DEFAULTS["station_ids_per_break"],
    )
    commercials_first_half = coerce_int(
        pick_value("commercials_first_half", args.commercials_first_half, DEFAULTS["commercials_first_half"]),
        DEFAULTS["commercials_first_half"],
    )
    commercials_second_half = coerce_int(
        pick_value("commercials_second_half", args.commercials_second_half, DEFAULTS["commercials_second_half"]),
        DEFAULTS["commercials_second_half"],
    )
    max_overrun = coerce_int(pick_value("max_overrun", args.max_overrun, DEFAULTS["max_overrun"]), DEFAULTS["max_overrun"])

    if ids_per_break < 0:
        print("station-ids-per-break must be >= 0", file=sys.stderr)
        return 1
    if commercials_first_half < 0 or commercials_second_half < 0:
        print("commercial counts must be >= 0", file=sys.stderr)
        return 1
    if max_overrun < 0:
        print("max-overrun must be >= 0", file=sys.stderr)
        return 1

    extensions_value = pick_value("extensions", args.extensions, DEFAULTS["extensions"])
    if isinstance(extensions_value, list):
        extensions_value = ",".join(str(item) for item in extensions_value)
    exts = parse_extensions(str(extensions_value))

    seed_value = pick_value("seed", args.seed, None)
    seed = coerce_optional_int(seed_value)
    rng = random.Random(seed)

    relative_value, relative_base = pick_path("relative_to", args.relative_to, None)
    relative_to = None
    if relative_value:
        relative_to = Path(relative_value)
        if not relative_to.is_absolute():
            relative_to = (relative_base / relative_to).resolve()
        if not relative_to.exists():
            print(f"relative-to path not found: {relative_to}", file=sys.stderr)
            return 1

    def resolve_path(value: object, base: Path, label: str) -> Optional[Path]:
        if value is None:
            return None
        path = value if isinstance(value, Path) else Path(str(value))
        if not path.is_absolute():
            path = (base / path).resolve()
        if not path.exists():
            print(f"Missing {label} folder: {path}", file=sys.stderr)
            return None
        return path

    songs_value, songs_base = pick_path("songs_dir", args.songs_dir, None)
    commercials_value, commercials_base = pick_path("commercials_dir", args.commercials_dir, None)
    newscasts_value, newscasts_base = pick_path("newscasts_dir", args.newscasts_dir, None)
    ids_value, ids_base = pick_path("ids_dir", args.ids_dir, None)

    songs_dir = resolve_path(songs_value, songs_base, "songs")
    commercials_dir = resolve_path(commercials_value, commercials_base, "commercials")
    newscasts_dir = resolve_path(newscasts_value, newscasts_base, "newscasts")
    ids_dir = resolve_path(ids_value, ids_base, "ids")

    if songs_dir is None:
        return 1
    if (commercials_first_half + commercials_second_half) > 0 and commercials_dir is None:
        return 1
    if ids_per_break > 0 and ids_dir is None:
        return 1
    if newscast_hours and newscasts_dir is None:
        return 1

    try:
        ffprobe = ensure_tool("ffprobe", args.ffprobe)
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 1

    songs_files = find_audio_files(songs_dir, exts)
    commercials_files = find_audio_files(commercials_dir, exts) if commercials_dir else []
    ids_files = find_audio_files(ids_dir, exts) if ids_dir else []
    if newscasts_dir:
        day_newscasts, all_newscasts = build_newscast_map(newscasts_dir, exts)
    else:
        day_newscasts, all_newscasts = {}, []
    if not songs_files:
        print("No songs found.", file=sys.stderr)
        return 1
    if (commercials_first_half + commercials_second_half) > 0 and not commercials_files:
        print("No commercials found.", file=sys.stderr)
        return 1
    if ids_per_break > 0 and not ids_files:
        print("No station IDs found.", file=sys.stderr)
        return 1
    if newscast_hours and not all_newscasts:
        print("No newscasts found.", file=sys.stderr)
        return 1

    cache_value = pick_value("cache", args.cache, DEFAULTS["cache"])
    cache_file = Path(cache_value)
    if not cache_file.is_absolute():
        cache_file = out_parent / cache_file
    cache: Dict[str, Dict[str, float]] = {}
    if not args.no_cache:
        cache = load_cache(cache_file)

    try:
        songs = load_tracks(songs_files, ffprobe, cache, args.verbose)
        commercials = load_tracks(commercials_files, ffprobe, cache, args.verbose)
        ids = load_tracks(ids_files, ffprobe, cache, args.verbose)
        newscasts_all = load_tracks(all_newscasts, ffprobe, cache, args.verbose)
        newscasts_by_day: Dict[int, List[Track]] = {}
        for day_idx, files in day_newscasts.items():
            newscasts_by_day[day_idx] = load_tracks(files, ffprobe, cache, args.verbose)
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 1

    if not args.no_cache:
        save_cache(cache_file, cache)

    songs_picker = CyclePicker(songs, rng)
    commercials_picker = CyclePicker(commercials, rng) if commercials else None
    ids_picker = CyclePicker(ids, rng) if ids else None
    newscasts_picker_all = CyclePicker(newscasts_all, rng) if newscasts_all else None
    newscasts_picker_by_day: Dict[int, CyclePicker] = {}
    for day_idx, items in newscasts_by_day.items():
        if items:
            newscasts_picker_by_day[day_idx] = CyclePicker(items, rng)

    lines: List[str] = ["#EXTM3U"]
    total_seconds = 0
    warned_paths: set = set()

    def append_track(track: Track, label: str, hour_state: List[int]) -> None:
        nonlocal total_seconds
        entry = format_m3u_path(track.path, relative_to, warned_paths, args.verbose)
        lines.append(f"#EXTINF:{track.duration},")
        lines.append(entry)
        hour_state[0] += track.duration
        total_seconds += track.duration
        log(f"{label} {entry} ({track.duration}s)", args.verbose)

    def fill_songs(target_seconds: int, hour_state: List[int], label: str) -> None:
        remaining = target_seconds - hour_state[0]
        if remaining <= 0:
            log(f"{label} already full ({hour_state[0]}s)", args.verbose)
            return
        attempts = max(5, len(songs))
        while remaining > 0:
            selection = select_song_for_remaining(
                songs_picker,
                remaining,
                max_overrun,
                attempts,
            )
            if selection is None:
                log(f"{label} short by {remaining}s", args.verbose)
                break
            append_track(selection, "Song", hour_state)
            remaining = target_seconds - hour_state[0]

    for day in range(days):
        dow = (start_dow + day) % 7
        for hour in range(24):
            hour_state = [0]
            if ids_picker:
                for _ in range(ids_per_break):
                    append_track(ids_picker.next(), "ID", hour_state)

            if newscast_hours and hour in newscast_hours:
                picker = newscasts_picker_by_day.get(dow, newscasts_picker_all)
                if picker:
                    append_track(picker.next(), "Newscast", hour_state)

            if commercials_picker:
                for _ in range(commercials_first_half):
                    append_track(commercials_picker.next(), "Commercial", hour_state)

            fill_songs(1800, hour_state, f"Hour {hour} first half")

            if ids_picker:
                for _ in range(ids_per_break):
                    append_track(ids_picker.next(), "ID", hour_state)

            if commercials_picker:
                for _ in range(commercials_second_half):
                    append_track(commercials_picker.next(), "Commercial", hour_state)

            fill_songs(3600, hour_state, f"Hour {hour} full")

    if not args.dry_run:
        out_path.write_text("\n".join(lines) + "\n")
        log(f"Wrote playlist: {out_path}", args.verbose)
    else:
        log("Dry run; no playlist written.", args.verbose)

    print(f"Generated {days} day(s), total duration ~ {total_seconds} seconds.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
