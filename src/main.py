# Old Time Radio Wav - Tiny2040 + DFPlayer Mini time-aligned playback
# Original concept: zionbrock (retro radio w/ DFPlayer Mini).
#
# This firmware expands the baseline into a time-aware, stateful player:
# - DS3231 RTC alignment to a weekly schedule (schedule.csv).
# - Synced AM intro + DFPlayer fade-in with BUSY confirmation.
# - Robust resume: SD-state file + EEPROM fallback.
# - EEPROM metadata: schedule checksum/mtime, last album/track, RTC-set flag.
# - Button UX: tap/dual/triple/long with album wrap behavior.
#
# Design notes for advanced readers:
# - PWM audio is intentionally simple (8-bit mono WAV) to keep timing stable.
# - DFPlayer BUSY edges are debounced and ignored after manual skips.
# - EEPROM writes are rate-limited to reduce wear.
# - Schedule alignment uses ISO week semantics for deterministic playback.

from machine import Pin, PWM, Timer, UART, I2C, ADC
import neopixel, ustruct, time, sys, uos, math
try:
    import uselect
except ImportError:
    uselect = None

# ===========================
#      CONFIGURATION
# ===========================

PIN_AUDIO       = 3
PIN_BUTTON      = 2
PIN_NEOPIX      = 16
PIN_UART_TX     = 0
PIN_UART_RX     = 1
PIN_SENSE       = 14      # power sense from Rail 2
PIN_BUSY        = 15      # DFPlayer BUSY (0 = playing, 1 = idle)
PIN_POT_ADC     = 26      # ADC0 (Tiny2040 GP26) for volume pot

VOLUME          = 1.0
WAV_FILE        = "AMradioSound.wav"
PWM_CARRIER     = 125_000
DFPLAYER_VOL    = 28

FADE_IN_S       = 2.4            # time to ramp DF volume up while AM plays
DF_BOOT_MS      = 2000           # time after GP14 HIGH before DF reset/play

LONG_PRESS_MS   = 1000           # hold for NEXT ALBUM
TAP_WINDOW_MS   = 800            # time after last tap to decide 1/2/3 taps

ALBUM_FILE      = "album_state.txt"
MAX_ALBUM_NUM   = 99             # folders 01..99 available

# BUSY behavior
BUSY_CONFIRM_MS = 1800           # how long we wait for BUSY low to confirm a track started
POST_CMD_GUARD_MS = 120          # small pause between stop and play commands

# RTC / Schedule
I2C_ID          = 0
PIN_I2C_SDA     = 4
PIN_I2C_SCL     = 5
I2C_FREQ        = 400_000
# DS3231 RTC is fixed at 0x68. A0/A1/A2 pads on many modules are for the onboard
# AT24C32 EEPROM (base 0x50): A2 A1 A0 = 000->0x50 ... 111->0x57 (default often 0x57).
RTC_ADDR        = 0x68

# If set, we will write this time to the RTC when OSF is set (or when forced).
# Format: (YYYY, MM, DD, HH, MM, SS)
RTC_BOOTSTRAP_TIME = None
RTC_FORCE_BOOTSTRAP = False
RTC_SERIAL_SET_MS = 5000         # window to accept "SET YYYY-MM-DD HH:MM:SS" over USB serial

SCHEDULE_FILE   = "schedule.csv" # folder,track,duration_s (chronological from Monday 00:00:00)
ALIGN_ON_POWER_ON = True

# EEPROM (AT24C32 on common DS3231 modules)
EEPROM_ADDR_DEFAULT = 0x57
EEPROM_BASE_ADDR = 0x50
EEPROM_MAX_ADDR = 0x57
EEPROM_PAGE_SIZE = 32
EEPROM_STATE_ADDR = 0x0000
EEPROM_SAVE_MIN_MS = 60000

# Volume pot behavior
POT_ENABLED     = True
POT_LOG_GAMMA   = 2.0     # >1.0 gives audio-taper feel (mid = less than half)
POT_UPDATE_MS   = 150
POT_DEADBAND    = 1

# ===========================
#   NeoPixel + Pins
# ===========================

np = neopixel.NeoPixel(Pin(PIN_NEOPIX), 1)
np[0] = (4, 4, 4)
np.write()

button      = Pin(PIN_BUTTON, Pin.IN, Pin.PULL_UP)
power_sense = Pin(PIN_SENSE, Pin.IN, Pin.PULL_DOWN)
pin_busy    = Pin(PIN_BUSY, Pin.IN)

uart = UART(0, baudrate=9600, tx=Pin(PIN_UART_TX), rx=Pin(PIN_UART_RX))

pwm = None
tim = None
MID = 32768

current_album = 1
current_track = 1
df_volume = DFPLAYER_VOL
fade_active = False

# album -> highest track index confirmed to play
KNOWN_TRACKS = {}

# ignore BUSY edges after manual skips so they don't look like "track finished"
ignore_busy_until = 0

i2c = None
eeprom_addr = None
last_eeprom_save_ms = 0
eeprom_flags = 0

EEPROM_MAGIC = b"OTR1"
EEPROM_VERSION = 1
EEPROM_FLAG_RTC_SET = 0x01
EEPROM_STRUCT_FMT = "<4sBBBBHIIH"
EEPROM_STRUCT_LEN = ustruct.calcsize(EEPROM_STRUCT_FMT)

pot_adc = ADC(PIN_POT_ADC) if POT_ENABLED else None
last_pot_update_ms = 0

# ===========================
#   RTC (DS3231) + Schedule
# ===========================

def bcd_to_int(value):
    return ((value >> 4) * 10) + (value & 0x0F)

def int_to_bcd(value):
    return ((value // 10) << 4) | (value % 10)

def iso_weekday(year, month, day):
    t = (0, 3, 2, 5, 0, 3, 5, 1, 4, 6, 2, 4)
    y = year - 1 if month < 3 else year
    dow = (y + y // 4 - y // 100 + y // 400 + t[month - 1] + day) % 7
    return 7 if dow == 0 else dow  # ISO: Monday=1..Sunday=7

def seconds_into_week(dt):
    year, month, day, hour, minute, second = dt
    dow = iso_weekday(year, month, day)
    return ((dow - 1) * 86400) + (hour * 3600) + (minute * 60) + second

def rtc_init():
    global i2c
    try:
        i2c = I2C(I2C_ID, scl=Pin(PIN_I2C_SCL), sda=Pin(PIN_I2C_SDA), freq=I2C_FREQ)
        devices = i2c.scan()
        if RTC_ADDR not in devices:
            print("RTC: DS3231 not found on I2C bus:", devices)
            i2c = None
            return False
        detect_eeprom_addr(devices)
        return True
    except Exception as e:
        print("RTC: I2C init failed:", e)
        i2c = None
        return False

def rtc_osf_set():
    if i2c is None:
        return True
    try:
        status = i2c.readfrom_mem(RTC_ADDR, 0x0F, 1)[0]
        return (status & 0x80) != 0
    except Exception as e:
        print("RTC: status read failed:", e)
        return True

def rtc_read_datetime():
    if i2c is None:
        return None
    try:
        data = i2c.readfrom_mem(RTC_ADDR, 0x00, 7)
    except Exception as e:
        print("RTC: read failed:", e)
        return None

    sec = bcd_to_int(data[0] & 0x7F)
    minute = bcd_to_int(data[1] & 0x7F)
    hour_raw = data[2]
    if hour_raw & 0x40:  # 12h mode
        hour = bcd_to_int(hour_raw & 0x1F)
        if hour_raw & 0x20:
            if hour != 12:
                hour += 12
        else:
            if hour == 12:
                hour = 0
    else:
        hour = bcd_to_int(hour_raw & 0x3F)

    day = bcd_to_int(data[4] & 0x3F)
    month = bcd_to_int(data[5] & 0x1F)
    year = 2000 + bcd_to_int(data[6])
    return (year, month, day, hour, minute, sec)

def rtc_write_datetime(dt):
    if i2c is None:
        return False
    year, month, day, hour, minute, second = dt
    if not (2000 <= year <= 2099):
        print("RTC: year out of range:", year)
        return False
    dow = iso_weekday(year, month, day)
    payload = bytes([
        int_to_bcd(second),
        int_to_bcd(minute),
        int_to_bcd(hour),          # 24h mode
        int_to_bcd(dow),
        int_to_bcd(day),
        int_to_bcd(month),
        int_to_bcd(year - 2000),
    ])
    try:
        i2c.writeto_mem(RTC_ADDR, 0x00, payload)
        status = i2c.readfrom_mem(RTC_ADDR, 0x0F, 1)[0]
        i2c.writeto_mem(RTC_ADDR, 0x0F, bytes([status & 0x7F]))
        return True
    except Exception as e:
        print("RTC: write failed:", e)
        return False

def detect_eeprom_addr(devices=None):
    global eeprom_addr
    if i2c is None:
        return None
    if devices is None:
        try:
            devices = i2c.scan()
        except Exception:
            devices = []
    for addr in range(EEPROM_BASE_ADDR, EEPROM_MAX_ADDR + 1):
        if addr in devices:
            eeprom_addr = addr
            print("EEPROM: found at", hex(addr))
            return addr
    eeprom_addr = None
    print("EEPROM: not found on I2C bus")
    return None

def eeprom_read(addr, length):
    if i2c is None or eeprom_addr is None:
        return None
    try:
        i2c.writeto(eeprom_addr, bytes([addr >> 8, addr & 0xFF]))
        return i2c.readfrom(eeprom_addr, length)
    except Exception as e:
        print("EEPROM: read failed:", e)
        return None

def eeprom_write(addr, payload):
    if i2c is None or eeprom_addr is None:
        return False
    try:
        offset = 0
        total = len(payload)
        while offset < total:
            page_off = (addr + offset) % EEPROM_PAGE_SIZE
            chunk = min(EEPROM_PAGE_SIZE - page_off, total - offset)
            header = bytes([((addr + offset) >> 8) & 0xFF, (addr + offset) & 0xFF])
            i2c.writeto(eeprom_addr, header + payload[offset:offset + chunk])
            time.sleep_ms(6)
            offset += chunk
        return True
    except Exception as e:
        print("EEPROM: write failed:", e)
        return False

def checksum16(data):
    total = 0
    for b in data:
        total = (total + b) & 0xFFFF
    return total

def get_schedule_checksum():
    try:
        with open(SCHEDULE_FILE, "rb") as f:
            data = f.read()
        return checksum16(data)
    except Exception:
        return 0

def get_schedule_mtime():
    try:
        stat = uos.stat(SCHEDULE_FILE)
        if len(stat) >= 9:
            return int(stat[8])
    except Exception:
        pass
    return 0

def eeprom_load_state():
    if i2c is None or eeprom_addr is None:
        return None
    raw = eeprom_read(EEPROM_STATE_ADDR, EEPROM_STRUCT_LEN)
    if not raw or len(raw) != EEPROM_STRUCT_LEN:
        return None
    try:
        (magic, version, flags, album, track,
         sched_sum, sched_mtime, week_sec, crc) = ustruct.unpack(EEPROM_STRUCT_FMT, raw)
    except Exception:
        return None
    if magic != EEPROM_MAGIC or version != EEPROM_VERSION:
        return None
    if checksum16(raw[:-2]) != crc:
        print("EEPROM: checksum mismatch")
        return None
    return {
        "flags": flags,
        "album": album,
        "track": track,
        "schedule_checksum": sched_sum,
        "schedule_mtime": sched_mtime,
        "week_seconds": week_sec,
    }

def eeprom_save_state(flags, album, track):
    global last_eeprom_save_ms
    if i2c is None or eeprom_addr is None:
        return False
    now = time.ticks_ms()
    if last_eeprom_save_ms and time.ticks_diff(now, last_eeprom_save_ms) < EEPROM_SAVE_MIN_MS:
        return False
    sched_sum = get_schedule_checksum()
    sched_mtime = get_schedule_mtime()
    week_sec = 0
    dt = rtc_read_datetime()
    if dt:
        week_sec = seconds_into_week(dt)
    payload = ustruct.pack(
        EEPROM_STRUCT_FMT,
        EEPROM_MAGIC,
        EEPROM_VERSION,
        flags & 0xFF,
        album & 0xFF,
        track & 0xFF,
        sched_sum & 0xFFFF,
        sched_mtime & 0xFFFFFFFF,
        week_sec & 0xFFFFFFFF,
        0,
    )
    crc = checksum16(payload[:-2])
    payload = payload[:-2] + ustruct.pack("<H", crc)
    if eeprom_write(EEPROM_STATE_ADDR, payload):
        last_eeprom_save_ms = now
        return True
    return False

def read_serial_line(timeout_ms):
    if uselect is None:
        return None
    poller = uselect.poll()
    poller.register(sys.stdin, uselect.POLLIN)
    buf = ""
    deadline = time.ticks_add(time.ticks_ms(), timeout_ms)
    while time.ticks_diff(time.ticks_ms(), deadline) < 0:
        events = poller.poll(50)
        if not events:
            continue
        try:
            ch = sys.stdin.read(1)
        except Exception:
            ch = None
        if not ch:
            continue
        if isinstance(ch, bytes):
            ch = ch.decode()
        if ch in ("\n", "\r"):
            if buf:
                return buf.strip()
            buf = ""
        else:
            if len(buf) < 128:
                buf += ch
    return None

def parse_datetime_line(line):
    if not line:
        return None
    raw = line.strip()
    if raw.upper().startswith("SET"):
        raw = raw[3:].strip()
        if raw.startswith("="):
            raw = raw[1:].strip()
    raw = raw.replace("T", " ")
    parts = raw.split()
    if not parts:
        return None
    date_part = parts[0]
    time_part = parts[1] if len(parts) > 1 else "00:00:00"
    sep = "-" if "-" in date_part else "/"
    date_bits = date_part.split(sep)
    if len(date_bits) != 3:
        return None
    try:
        year = int(date_bits[0])
        month = int(date_bits[1])
        day = int(date_bits[2])
        time_bits = time_part.split(":")
        if len(time_bits) == 2:
            hour = int(time_bits[0])
            minute = int(time_bits[1])
            second = 0
        elif len(time_bits) == 3:
            hour = int(time_bits[0])
            minute = int(time_bits[1])
            second = int(time_bits[2])
        else:
            return None
    except ValueError:
        return None
    if not (2000 <= year <= 2099):
        return None
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59):
        return None
    return (year, month, day, hour, minute, second)

def maybe_set_rtc(force_serial=False):
    global eeprom_flags
    if i2c is None:
        return False
    if RTC_FORCE_BOOTSTRAP and RTC_BOOTSTRAP_TIME:
        if rtc_write_datetime(RTC_BOOTSTRAP_TIME):
            print("RTC: set from bootstrap (forced)")
            eeprom_flags |= EEPROM_FLAG_RTC_SET
            eeprom_save_state(eeprom_flags, current_album, current_track)
            return True
    osf = rtc_osf_set()
    if osf:
        print("RTC: OSF set (time may be invalid)")
        if RTC_BOOTSTRAP_TIME:
            if rtc_write_datetime(RTC_BOOTSTRAP_TIME):
                print("RTC: set from bootstrap (OSF)")
                eeprom_flags |= EEPROM_FLAG_RTC_SET
                eeprom_save_state(eeprom_flags, current_album, current_track)
                return True
    if RTC_SERIAL_SET_MS > 0 and (force_serial or osf):
        print("RTC: send 'SET YYYY-MM-DD HH:MM:SS' over USB serial within", RTC_SERIAL_SET_MS, "ms")
        line = read_serial_line(RTC_SERIAL_SET_MS)
        if line:
            dt = parse_datetime_line(line)
            if dt and rtc_write_datetime(dt):
                print("RTC: set from serial:", dt)
                eeprom_flags |= EEPROM_FLAG_RTC_SET
                eeprom_save_state(eeprom_flags, current_album, current_track)
                return True
            print("RTC: invalid serial time:", line)
    return False

def parse_duration(value):
    value = value.strip()
    if ":" in value:
        bits = value.split(":")
        try:
            parts = [int(p) for p in bits]
        except ValueError:
            return None
        if len(parts) == 2:
            return parts[0] * 60 + parts[1]
        if len(parts) == 3:
            return parts[0] * 3600 + parts[1] * 60 + parts[2]
        return None
    try:
        return int(value)
    except ValueError:
        return None

def parse_schedule_line(line):
    raw = line.strip()
    if not raw or raw.startswith("#"):
        return None
    if "#" in raw:
        raw = raw.split("#", 1)[0].strip()
    if not raw:
        return None
    parts = [p.strip() for p in raw.split(",")]
    if len(parts) < 3:
        return None
    try:
        folder = int(parts[0])
        track = int(parts[1])
    except ValueError:
        return None
    duration = parse_duration(parts[2])
    if duration is None or duration <= 0:
        return None
    if folder < 1 or track < 1:
        return None
    return folder, track, duration

def scan_schedule(target_sec):
    folder_counts = {}
    total = 0
    found = None
    try:
        with open(SCHEDULE_FILE, "r") as f:
            for line in f:
                entry = parse_schedule_line(line)
                if not entry:
                    continue
                folder, track, duration = entry
                if folder > MAX_ALBUM_NUM:
                    print("Schedule folder out of range:", folder)
                    continue
                if found is None and target_sec < (total + duration):
                    found = (folder, track)
                prev = folder_counts.get(folder, 0)
                if track > prev:
                    folder_counts[folder] = track
                total += duration
    except Exception as e:
        print("Schedule read error:", e)
        return None, {}, 0
    return found, folder_counts, total

def find_track_for_time(target_sec):
    found, counts, total = scan_schedule(target_sec)
    if total <= 0:
        return None, {}, 0
    if found is None:
        wrapped = target_sec % total
        found, _, _ = scan_schedule(wrapped)
    return found, counts, total

def align_to_time(reason=""):
    global current_album, current_track, KNOWN_TRACKS
    dt = rtc_read_datetime()
    if not dt:
        print("RTC: no valid time, skipping alignment")
        return False
    target = seconds_into_week(dt)
    found, counts, total = find_track_for_time(target)
    if not found:
        print("Schedule align failed; keeping saved state")
        return False
    current_album, current_track = found
    if counts:
        KNOWN_TRACKS = counts
    print("Aligned to time", dt, "-> album", current_album, "track", current_track, "(schedule seconds:", total, ")")
    save_state("time align" + (":" + reason if reason else ""))
    return True

# ===========================
#   DFPlayer helpers
# ===========================

def df_send(cmd, p1=0, p2=0):
    pkt = bytearray([0x7E, 0xFF, 0x06, cmd, 0x00, p1 & 0xFF, p2 & 0xFF])
    csum = -sum(pkt[1:7]) & 0xFFFF
    pkt.append((csum >> 8) & 0xFF)
    pkt.append(csum & 0xFF)
    pkt.append(0xEF)
    uart.write(pkt)
    time.sleep_ms(30)

def df_reset():
    print("DF: RESET")
    df_send(0x3F, 0x00, 0x00)
    time.sleep_ms(800)

def df_set_vol(v):
    v = max(0, min(30, v))
    print("DF: set volume", v)
    df_send(0x06, 0x00, v)

def df_play_folder_track(folder, track):
    print("DF: play folder", folder, "track", track)
    df_send(0x0F, folder, track)

def df_stop():
    print("DF: stop")
    df_send(0x16, 0, 0)

# ===========================
#   Volume pot helpers
# ===========================

def pot_target_volume():
    if pot_adc is None:
        return DFPLAYER_VOL
    raw = pot_adc.read_u16()
    x = raw / 65535
    if x < 0:
        x = 0
    if x > 1:
        x = 1
    gamma = POT_LOG_GAMMA if POT_LOG_GAMMA > 0 else 1.0
    y = math.pow(x, gamma)
    return int(round(y * DFPLAYER_VOL))

def update_volume_from_pot(force=False):
    global df_volume, last_pot_update_ms
    if pot_adc is None or fade_active:
        return
    now = time.ticks_ms()
    if not force and time.ticks_diff(now, last_pot_update_ms) < POT_UPDATE_MS:
        return
    last_pot_update_ms = now
    target = pot_target_volume()
    if force or abs(target - df_volume) >= POT_DEADBAND:
        df_volume = target
        df_set_vol(df_volume)

# ===========================
#   Album state save / load
# ===========================

def load_state():
    global current_album, current_track, KNOWN_TRACKS
    try:
        with open(ALBUM_FILE, "r") as f:
            raw = f.read().strip()
        print("Loaded raw album_state:", raw)

        parts = raw.split(";")
        a_str, t_str = parts[0].split(",")
        current_album = int(a_str)
        current_track = int(t_str)

        KNOWN_TRACKS = {}
        if len(parts) > 1 and parts[1].startswith("tracks="):
            track_part = parts[1][7:]
            if track_part:
                for pair in track_part.split(","):
                    if not pair:
                        continue
                    a, c = pair.split(":")
                    KNOWN_TRACKS[int(a)] = int(c)

        print("Loaded album", current_album, "track", current_track)
        print("Loaded KNOWN_TRACKS:", KNOWN_TRACKS)
        return True

    except Exception as e:
        print("No valid album_state.txt, starting fresh. Reason:", e)
        current_album = 1
        current_track = 1
        KNOWN_TRACKS = {}
        return False

def save_state(reason=""):
    global current_album, current_track, KNOWN_TRACKS
    try:
        track_str = ",".join("%d:%d" % (a, c) for a, c in sorted(KNOWN_TRACKS.items()))
        payload = f"{current_album},{current_track};tracks={track_str}"
        with open(ALBUM_FILE, "w") as f:
            f.write(payload)
        print("Saved state", ("[" + reason + "]" if reason else ""), ":", payload)
        eeprom_save_state(eeprom_flags, current_album, current_track)
    except Exception as e:
        print("State save error:", e)

# ===========================
#       WAV Loader
# ===========================

def load_wav_u8(path):
    with open(path, "rb") as f:
        if f.read(4) != b"RIFF":
            raise ValueError("Not RIFF")
        f.read(4)
        if f.read(4) != b"WAVE":
            raise ValueError("Not WAVE")
        samplerate = 8000
        fmt_ok = False
        while True:
            cid = f.read(4)
            if not cid:
                raise ValueError("No data chunk")
            clen = ustruct.unpack("<I", f.read(4))[0]
            if cid == b"fmt ":
                fmt = f.read(clen)
                audio_format = ustruct.unpack("<H", fmt[0:2])[0]
                channels = ustruct.unpack("<H", fmt[2:4])[0]
                samplerate = ustruct.unpack("<I", fmt[4:8])[0]
                bits_per_sample = ustruct.unpack("<H", fmt[14:16])[0]
                if audio_format != 1 or channels != 1 or bits_per_sample != 8:
                    raise ValueError(
                        "AMradioSound.wav must be a mono 8-bit PCM WAV. "
                        "Please convert the file and try again."
                    )
                fmt_ok = True
            elif cid == b"data":
                if not fmt_ok:
                    raise ValueError(
                        "AMradioSound.wav is missing a valid WAV header. "
                        "Please re-export the file as a standard WAV."
                    )
                data = f.read(clen)
                break
            else:
                f.seek(clen, 1)
    return data, samplerate

print("Loading WAV:", WAV_FILE)
data, SR = load_wav_u8(WAV_FILE)

lut = [0] * 256
scale = int(256 * VOLUME)
for i in range(256):
    d = MID + (i - 128) * scale
    d = max(0, min(65535, d))
    lut[i] = d

# ===========================
#   BUSY helpers
# ===========================

def wait_for_busy_low(timeout_ms=BUSY_CONFIRM_MS):
    start = time.ticks_ms()
    while time.ticks_diff(time.ticks_ms(), start) < timeout_ms:
        if pin_busy.value() == 0:
            return True
        time.sleep_ms(25)
    return False

def note_track_learned(album, track):
    global KNOWN_TRACKS
    prev = KNOWN_TRACKS.get(album, 0)
    if track > prev:
        KNOWN_TRACKS[album] = track
        print("Learned track", track, "for album", album, "-> KNOWN_TRACKS =", KNOWN_TRACKS)
        save_state("learned track")

# ===========================
#   Synced AM playback + DF fade + confirm
# ===========================

def play_am_and_fade_df_confirming(folder, track):
    """
    Start DF play immediately, then play AM WAV while fading DF volume up.
    During the fade, we watch BUSY for a confirmation that DF actually started.
    Returns True if we confirm BUSY LOW at any point during the AM window.
    """
    global pwm, tim, fade_active

    # Start DF track immediately (synced start)
    df_stop()
    time.sleep_ms(POST_CMD_GUARD_MS)
    df_play_folder_track(folder, track)

    np[0] = (0, 10, 0)
    np.write()

    print("RP: starting AM WAV (synced)")

    p = Pin(PIN_AUDIO)
    pwm = PWM(p)
    pwm.freq(PWM_CARRIER)
    pwm.duty_u16(MID)

    state = {"idx": 0, "n": len(data), "done": False}

    # fade AM out at the very end
    fade_out_s = 0.8
    fo = int(SR * fade_out_s)
    if fo > state["n"]:
        fo = state["n"]
    state["fade_out_samples"] = fo

    tim = Timer()

    def isr_cb(_t):
        idx = state["idx"]
        n = state["n"]
        if idx >= n:
            pwm.duty_u16(MID)
            state["done"] = True
            return

        raw_duty = lut[data[idx]]
        fo2 = state["fade_out_samples"]
        if fo2 > 0 and idx >= n - fo2:
            into = idx - (n - fo2)
            remaining = fo2 - into
            if remaining < 0:
                remaining = 0
            scale_val = (remaining * 256) // fo2
            duty = MID + ((raw_duty - MID) * scale_val) // 256
        else:
            duty = raw_duty

        pwm.duty_u16(duty)
        state["idx"] = idx + 1

    tim.init(freq=SR, mode=Timer.PERIODIC, callback=isr_cb)

    # DF fade steps spread across FADE_IN_S (or the AM length, whichever is shorter)
    fade_total_ms = int(FADE_IN_S * 1000)
    am_ms = int((len(data) * 1000) / SR) if SR > 0 else fade_total_ms
    if am_ms > 0 and am_ms < fade_total_ms:
        fade_total_ms = am_ms
    fade_steps = 20
    max_steps = max(1, fade_total_ms // 40) if fade_total_ms > 0 else 1
    if fade_steps > max_steps:
        fade_steps = max_steps
    fade_delay = int(fade_total_ms / fade_steps) if fade_steps > 0 else 40
    if fade_delay < 10:
        fade_delay = 10

    confirmed = False
    confirm_deadline = time.ticks_add(time.ticks_ms(), BUSY_CONFIRM_MS)

    fade_active = True
    try:
        for step in range(fade_steps + 1):
            df_set_vol(int((step / fade_steps) * df_volume))

            # while we wait between volume steps, keep checking BUSY
            t_start = time.ticks_ms()
            while time.ticks_diff(time.ticks_ms(), t_start) < fade_delay:
                if (not confirmed) and (time.ticks_diff(time.ticks_ms(), confirm_deadline) <= 0):
                    if pin_busy.value() == 0:
                        confirmed = True
                        print("BUSY went LOW -> playback started (confirmed during AM)")
                if state["done"]:
                    break
                time.sleep_ms(10)

            if state["done"]:
                break

        # wait until AM finishes if it hasn't yet
        while not state["done"]:
            if (not confirmed) and (time.ticks_diff(time.ticks_ms(), confirm_deadline) <= 0):
                if pin_busy.value() == 0:
                    confirmed = True
                    print("BUSY went LOW -> playback started (confirmed during AM)")
            time.sleep_ms(20)

    finally:
        fade_active = False
        try:
            tim.deinit()
        except:
            pass
        try:
            pwm.duty_u16(MID)
        except:
            pass
        np[0] = (0, 0, 0)
        np.write()
        print("RP: AM WAV done")

    return confirmed

# ===========================
#       Start Sequence
# ===========================

def start_sequence_synced():
    """
    On power up or pot ON:
    - Reset DF
    - Start DF play immediately (synced with AM)
    - Fade DF volume up while AM plays
    - If not confirmed, do a quick second-chance re-trigger
    """
    global current_album, current_track

    df_reset()
    df_set_vol(0)

    print("Start sequence (synced): album", current_album, "track", current_track)

    confirmed = play_am_and_fade_df_confirming(current_album, current_track)

    if confirmed:
        note_track_learned(current_album, current_track)
        save_state("boot start")
        return

    print("No BUSY LOW in confirm window (will second-chance after AM ends).")
    print("Second-chance: re-trigger DF after AM")
    df_reset()
    df_set_vol(df_volume)
    df_stop()
    time.sleep_ms(POST_CMD_GUARD_MS)
    df_play_folder_track(current_album, current_track)

    if wait_for_busy_low(1500):
        print("Second-chance confirmed (BUSY LOW).")
        note_track_learned(current_album, current_track)
        save_state("boot start (2nd chance)")
        return

    print("Second-chance still not confirmed. (Possible BUSY wiring issue or DF not playing.)")

# ===========================
#   Play current + learn
# ===========================

def play_current(label=""):
    global ignore_busy_until, current_album, current_track
    print("Play request", ("[" + label + "]" if label else ""), "album", current_album, "track", current_track)

    df_stop()
    time.sleep_ms(POST_CMD_GUARD_MS)
    df_play_folder_track(current_album, current_track)

    if wait_for_busy_low():
        print("BUSY went LOW -> playback started")
        note_track_learned(current_album, current_track)
        ignore_busy_until = time.ticks_add(time.ticks_ms(), 2000)
        return True

    print("No BUSY LOW -> not confirmed")
    return False

# ===========================
#     MAIN BOOT LOGIC
# ===========================

print("Booting Retro Radio Baseline 5.8")

print("Waiting for GP14 HIGH (power sense)...")
last_hint = time.ticks_ms()
while power_sense.value() == 0:
    if time.ticks_diff(time.ticks_ms(), last_hint) > 1500:
        print("...still waiting for GP14 HIGH")
        last_hint = time.ticks_ms()
    time.sleep_ms(20)

print("GP14 HIGH detected.")
loaded_file = load_state()

if rtc_init():
    force_serial = (button.value() == 0)
    maybe_set_rtc(force_serial=force_serial)
    align_to_time("boot")
else:
    print("RTC: init failed, using saved state")

saved = eeprom_load_state()
if saved:
    eeprom_flags = int(saved.get("flags", 0)) & 0xFF
    if not loaded_file:
        current_album = max(1, int(saved.get("album", 1)))
        current_track = max(1, int(saved.get("track", 1)))
        print("Loaded state from EEPROM:", current_album, current_track)

print("Giving DFPlayer time to boot:", DF_BOOT_MS, "ms")
time.sleep_ms(DF_BOOT_MS)

df_volume = pot_target_volume()
start_sequence_synced()

# ===========================
#     BUTTON + BUSY LOOP
# ===========================

print("Button active. tap=next, double=prev, triple=restart album, long=next album")

tap_count = 0
press_start = 0
last_button = 1
last_release_time = 0
prev_busy = pin_busy.value()
last_sense = power_sense.value()
rail2_on = (last_sense == 1)

while True:
    curr = button.value()
    now = time.ticks_ms()

    # 1) Button press edge
    if last_button == 1 and curr == 0:
        press_start = now

    # 2) Button release edge
    elif last_button == 0 and curr == 1:
        press_dur = time.ticks_diff(now, press_start)

        if press_dur >= LONG_PRESS_MS:
            # Long press -> next album (Option A wrap if missing)
            print("Long press: request next album")
            candidate = current_album + 1
            if candidate > MAX_ALBUM_NUM:
                candidate = 1

            current_album = candidate
            current_track = 1
            save_state("long press album change")

            if not play_current("next album"):
                # OPTION A: wrap to album 1 track 1
                print("Album", candidate, "did not confirm. Wrapping to album 1 track 1.")
                current_album = 1
                current_track = 1
                save_state("wrap to album 1 after missing album")
                play_current("wrapped album 1")

            tap_count = 0
            last_release_time = 0

        else:
            tap_count += 1
            last_release_time = now
            print("Short tap detected, tap_count =", tap_count)

        time.sleep_ms(40)

    # 3) Decide 1 / 2 / 3 taps after quiet period
    if tap_count > 0 and time.ticks_diff(now, last_release_time) >= TAP_WINDOW_MS:
        max_known = KNOWN_TRACKS.get(current_album, max(current_track, 1))

        if tap_count >= 3:
            current_track = 1
            save_state("triple tap restart")
            play_current("restart album")

        elif tap_count == 2:
            if max_known < 1:
                max_known = 1
            current_track -= 1
            if current_track < 1:
                current_track = max_known
            save_state("double tap prev")
            play_current("previous track")

        else:
            candidate = current_track + 1
            if candidate <= max_known:
                current_track = candidate
                save_state("single tap next inside known")
                play_current("next track known")
            else:
                current_track = candidate
                if not play_current("probe new track"):
                    current_track = 1
                    save_state("wrap to 1 after silent new track")
                    play_current("wrap to track 1")
                else:
                    save_state("extended known range")

        tap_count = 0
        last_release_time = 0

    # 4) Detect track finished via BUSY edge (0 -> 1)
    if rail2_on:
        b = pin_busy.value()
        now_ts = time.ticks_ms()
        if time.ticks_diff(now_ts, ignore_busy_until) >= 0:
            if prev_busy == 0 and b == 1:
                max_known = KNOWN_TRACKS.get(current_album, max(current_track, 1))
                candidate = current_track + 1
                print("BUSY edge: track finished. Auto advance from", current_track, "->", candidate)

                if candidate <= max_known:
                    current_track = candidate
                    save_state("auto next inside known")
                    play_current("auto next known")
                else:
                    current_track = candidate
                    if not play_current("auto probe new track"):
                        current_track = 1
                        save_state("auto wrap to 1")
                        play_current("auto wrap to track 1")
                    else:
                        save_state("auto extended known range")

        prev_busy = b

    # 5) Watch power sense line for pot OFF / ON
    sense = power_sense.value()
    if sense != last_sense:
        if sense == 0:
            print("GP14 LOW - Rail 2 power OFF (pot turned OFF)")
            rail2_on = False
            save_state("pot turned off")
            df_stop()
        else:
            print("GP14 HIGH - Rail 2 power ON (pot turned ON)")
            rail2_on = True
            save_state("pot turned back on")
            if ALIGN_ON_POWER_ON:
                align_to_time("power on")
            print("Giving DFPlayer time to boot:", DF_BOOT_MS, "ms")
            time.sleep_ms(DF_BOOT_MS)
            start_sequence_synced()
        last_sense = sense

    last_button = curr
    update_volume_from_pot()
    time.sleep_ms(10)
