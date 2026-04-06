import os
import re
import time
import threading
import json
import hashlib
import socket
from concurrent.futures import ThreadPoolExecutor
from collections import OrderedDict, deque
from pathlib import Path
from urllib.parse import quote

import psutil
import requests
from flask import Flask, jsonify, request, send_from_directory

app = Flask(__name__)

# --- CONFIGURATION ---
CIDER_API_URL = "http://127.0.0.1:10767/api/v1/playback"
LRCLIB_BASE_URL = "https://lrclib.net/api"
MUSIXMATCH_BASE = "https://apic-desktop.musixmatch.com/ws/1.1/"
APP_HOST = os.getenv("DASHY_HOST", "0.0.0.0")
APP_PORT = int(os.getenv("DASHY_PORT", "5000"))
APP_DIR = Path(__file__).resolve().parent
CONFIG_DIR = Path(os.getenv("DASHY_CONFIG_DIR", os.path.expanduser("~/.config/dashy")))
CONFIG_PATH = CONFIG_DIR / "config.json"

DEFAULT_UI_CONFIG = {
    "lyrics_font_scale": 1.0,
    "album_art_scale": 1.0,
    "active_lyric_scale": 1.03,
    "control_mode": "buttons",
    "swipe_start_threshold": 6.0,
    "swipe_commit_threshold": 22.0,
}

# --- CACHE ---
current_track_id = None
cached_lyrics_payload = {"text": None, "is_synced": False, "source": None}
musixmatch_token = None
lyrics_lock = threading.RLock()
config_lock = threading.RLock()
http_session = requests.Session()
lyrics_result_cache = OrderedDict()
cider_request_pool = ThreadPoolExecutor(max_workers=4)
ui_config = dict(DEFAULT_UI_CONFIG)

LYRICS_CACHE_MAX_BYTES = int(os.getenv("LYRICS_CACHE_MAX_BYTES", str(32 * 1024 * 1024)))
LYRICS_CACHE_MAX_ENTRIES = int(os.getenv("LYRICS_CACHE_MAX_ENTRIES", "512"))
LYRICS_CACHE_TTL_SECONDS = int(os.getenv("LYRICS_CACHE_TTL_SECONDS", str(7 * 24 * 60 * 60)))
LYRICS_DISK_CACHE_DIR = Path(os.getenv("DASHY_CACHE_DIR", os.path.expanduser("~/.cache/dashy/lyrics")))
LYRICS_DISK_CACHE_MAX_BYTES = int(os.getenv("DASHY_LYRICS_DISK_CACHE_MAX_BYTES", str(256 * 1024 * 1024)))
LYRICS_DISK_CACHE_CLEANUP_INTERVAL_SECONDS = int(os.getenv("DASHY_LYRICS_DISK_CACHE_CLEANUP_INTERVAL_SECONDS", "300"))
LYRICS_CACHE_SCHEMA_VERSION = 1
lyrics_cache_bytes = 0
next_disk_cache_cleanup_at = 0
stats_cache = {
    "cpu": {"value": 0.0, "updated_at": 0.0},
    "gpu": {"value": 0.0, "updated_at": 0.0},
    "disk": {"value": 0.0, "updated_at": 0.0},
    "cpu_temp": {"value": None, "updated_at": 0.0},
    "gpu_temp": {"value": None, "updated_at": 0.0},
    "ram": {"value": 0.0, "updated_at": 0.0},
}
cpu_samples = deque(maxlen=4)

STATS_INTERVALS = {
    "gpu": 2.0,
    "disk": 2.0,
    "cpu_temp": 3.0,
    "gpu_temp": 3.0,
    "ram": 5.0,
}

# Init non-blocking CPU check
psutil.cpu_percent(interval=None)

try:
    _io = psutil.disk_io_counters()
    last_disk_bytes = (_io.read_bytes + _io.write_bytes) if _io else 0
except Exception:
    last_disk_bytes = 0
last_disk_time = time.time()


def cpu_sampler_loop():
    while True:
        try:
            sample = psutil.cpu_percent(interval=None)
            cpu_samples.append(sample)
            averaged = round(sum(cpu_samples) / len(cpu_samples), 1) if cpu_samples else sample
            with lyrics_lock:
                stats_cache["cpu"]["value"] = averaged
                stats_cache["cpu"]["updated_at"] = time.time()
        except Exception:
            pass
        time.sleep(1.0)


cpu_sampler_thread = threading.Thread(target=cpu_sampler_loop, daemon=True)
cpu_sampler_thread.start()


def clamp_config_value(name, value):
    if name == "control_mode":
        return value if value in {"buttons", "swipe"} else DEFAULT_UI_CONFIG[name]

    try:
        numeric = float(value)
    except (TypeError, ValueError):
        numeric = DEFAULT_UI_CONFIG[name]

    limits = {
        "lyrics_font_scale": (0.8, 1.5),
        "album_art_scale": (0.85, 1.25),
        "active_lyric_scale": (1.0, 1.12),
        "swipe_start_threshold": (2.0, 24.0),
        "swipe_commit_threshold": (8.0, 72.0),
    }
    minimum, maximum = limits[name]
    return round(max(minimum, min(maximum, numeric)), 3)


def normalize_ui_config(candidate):
    return {
        key: clamp_config_value(key, candidate.get(key, DEFAULT_UI_CONFIG[key]))
        for key in DEFAULT_UI_CONFIG
    }


def persist_ui_config():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    temp_path = CONFIG_PATH.with_suffix(".tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        json.dump(ui_config, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_path, CONFIG_PATH)


def load_ui_config():
    global ui_config
    try:
        with CONFIG_PATH.open("r", encoding="utf-8") as handle:
            loaded = json.load(handle)
    except FileNotFoundError:
        with config_lock:
            ui_config = dict(DEFAULT_UI_CONFIG)
            persist_ui_config()
        return
    except Exception:
        with config_lock:
            ui_config = dict(DEFAULT_UI_CONFIG)
            persist_ui_config()
        return

    with config_lock:
        ui_config = normalize_ui_config(loaded if isinstance(loaded, dict) else {})


def get_ui_config():
    with config_lock:
        return dict(ui_config)


def update_ui_config(partial_config):
    global ui_config
    with config_lock:
        merged = dict(ui_config)
        for key in DEFAULT_UI_CONFIG:
            if key in partial_config:
                merged[key] = partial_config[key]
        ui_config = normalize_ui_config(merged)
        persist_ui_config()
        return dict(ui_config)


load_ui_config()


def estimate_lyrics_payload_size(track_id, payload):
    text = payload.get("text") or ""
    source = payload.get("source") or ""
    return len(track_id.encode("utf-8")) + len(text.encode("utf-8")) + len(source.encode("utf-8")) + 64


def normalize_cached_payload(payload):
    return {
        "text": payload.get("text"),
        "is_synced": bool(payload.get("is_synced", False)),
        "source": payload.get("source"),
    }


def build_disk_cache_key(track_id):
    return hashlib.sha256(track_id.encode("utf-8")).hexdigest()


def get_disk_cache_path(track_id):
    cache_key = build_disk_cache_key(track_id)
    return LYRICS_DISK_CACHE_DIR / cache_key[:2] / f"{cache_key}.json"


def delete_disk_cache_path(path):
    try:
        path.unlink(missing_ok=True)
    except Exception:
        pass


def cleanup_disk_cache(force=False):
    global next_disk_cache_cleanup_at

    now = time.time()
    if not force and now < next_disk_cache_cleanup_at:
        return
    next_disk_cache_cleanup_at = now + LYRICS_DISK_CACHE_CLEANUP_INTERVAL_SECONDS

    try:
        if not LYRICS_DISK_CACHE_DIR.exists():
            return

        entries = []
        total_size = 0

        for path in LYRICS_DISK_CACHE_DIR.rglob("*.json"):
            try:
                stat = path.stat()
                total_size += stat.st_size
                entries.append({
                    "path": path,
                    "size": stat.st_size,
                    "atime": stat.st_atime,
                    "mtime": stat.st_mtime,
                })
            except FileNotFoundError:
                continue

        expired = []
        for entry in entries:
            try:
                with entry["path"].open("r", encoding="utf-8") as handle:
                    cached = json.load(handle)
                if cached.get("expires_at", 0) < now:
                    expired.append(entry)
            except Exception:
                expired.append(entry)

        for entry in expired:
            delete_disk_cache_path(entry["path"])
            total_size -= entry["size"]

        if total_size <= LYRICS_DISK_CACHE_MAX_BYTES:
            return

        live_entries = [entry for entry in entries if entry not in expired]
        live_entries.sort(key=lambda item: (item["atime"], item["mtime"]))

        for entry in live_entries:
            if total_size <= LYRICS_DISK_CACHE_MAX_BYTES:
                break
            delete_disk_cache_path(entry["path"])
            total_size -= entry["size"]
    except Exception:
        pass


def get_disk_cached_lyrics(track_id):
    path = get_disk_cache_path(track_id)
    try:
        with path.open("r", encoding="utf-8") as handle:
            cached = json.load(handle)
    except FileNotFoundError:
        return None
    except Exception:
        delete_disk_cache_path(path)
        return None

    if cached.get("version") != LYRICS_CACHE_SCHEMA_VERSION:
        delete_disk_cache_path(path)
        return None

    if cached.get("expires_at", 0) < time.time():
        delete_disk_cache_path(path)
        return None

    payload = normalize_cached_payload(cached.get("payload", {}))

    try:
        os.utime(path, None)
    except Exception:
        pass

    return payload


def store_disk_cached_lyrics(track_id, payload):
    path = get_disk_cache_path(track_id)
    payload_copy = normalize_cached_payload(payload)
    record = {
        "version": LYRICS_CACHE_SCHEMA_VERSION,
        "track_id": track_id,
        "created_at": time.time(),
        "expires_at": time.time() + LYRICS_CACHE_TTL_SECONDS,
        "payload": payload_copy,
    }

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(".tmp")
        with temp_path.open("w", encoding="utf-8") as handle:
            json.dump(record, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except Exception:
        try:
            temp_path.unlink(missing_ok=True)
        except Exception:
            pass
        return payload_copy

    cleanup_disk_cache()
    return payload_copy


def get_cached_lyrics(track_id):
    global lyrics_cache_bytes

    with lyrics_lock:
        cached = lyrics_result_cache.get(track_id)
        if not cached:
            return None

        expires_at = cached["expires_at"]
        if expires_at < time.time():
            lyrics_cache_bytes -= cached["size"]
            lyrics_result_cache.pop(track_id, None)
        else:
            lyrics_result_cache.move_to_end(track_id)
            return dict(cached["payload"])

    disk_payload = get_disk_cached_lyrics(track_id)
    if not disk_payload:
        return None

    return store_memory_cached_lyrics(track_id, disk_payload)


def store_memory_cached_lyrics(track_id, payload):
    global lyrics_cache_bytes

    payload_copy = normalize_cached_payload(payload)
    entry_size = estimate_lyrics_payload_size(track_id, payload_copy)

    # Do not allow one oversized payload to evict the entire cache.
    if entry_size > LYRICS_CACHE_MAX_BYTES:
        return payload_copy

    with lyrics_lock:
        existing = lyrics_result_cache.pop(track_id, None)
        if existing:
            lyrics_cache_bytes -= existing["size"]

        lyrics_result_cache[track_id] = {
            "payload": payload_copy,
            "expires_at": time.time() + LYRICS_CACHE_TTL_SECONDS,
            "size": entry_size,
        }
        lyrics_cache_bytes += entry_size

        while lyrics_result_cache and (
            lyrics_cache_bytes > LYRICS_CACHE_MAX_BYTES or
            len(lyrics_result_cache) > LYRICS_CACHE_MAX_ENTRIES
        ):
            _, removed = lyrics_result_cache.popitem(last=False)
            lyrics_cache_bytes -= removed["size"]

    return payload_copy


def store_cached_lyrics(track_id, payload):
    payload_copy = store_memory_cached_lyrics(track_id, payload)
    store_disk_cached_lyrics(track_id, payload_copy)
    return payload_copy


def get_cached_stat(stat_name, getter, now):
    with lyrics_lock:
        cached = stats_cache[stat_name]
        if (now - cached["updated_at"]) < STATS_INTERVALS[stat_name]:
            return cached["value"]

    value = getter()

    with lyrics_lock:
        stats_cache[stat_name]["value"] = value
        stats_cache[stat_name]["updated_at"] = now

    return value


def get_amd_gpu_stats():
    """Reads AMD GPU usage directly from the Linux kernel."""
    gpu_usage = 0
    try:
        for i in range(2):
            path = f"/sys/class/drm/card{i}/device/gpu_busy_percent"
            if os.path.exists(path):
                with open(path, "r") as f:
                    usage = int(f.read().strip())
                    if usage > gpu_usage:
                        gpu_usage = usage
    except Exception:
        pass
    return gpu_usage


def get_cpu_temp():
    try:
        temps = psutil.sensors_temperatures()
    except Exception:
        temps = {}

    candidates = []
    for name, entries in temps.items():
        if "coretemp" in name.lower() or "k10temp" in name.lower() or "cpu" in name.lower():
            for entry in entries:
                if entry.current is not None:
                    candidates.append(entry.current)
        else:
            for entry in entries:
                label = (entry.label or "").lower()
                if "cpu" in label or "package" in label or "tdie" in label or "core" in label or "tctl" in label:
                    if entry.current is not None:
                        candidates.append(entry.current)

    if candidates:
        return round(max(candidates), 1)
    return None


def get_gpu_temp():
    """Reads AMD GPU temperature directly from hwmon when available."""
    try:
        for i in range(4):
            base_path = f"/sys/class/drm/card{i}/device/hwmon"
            if not os.path.isdir(base_path):
                continue
            for hwmon_name in os.listdir(base_path):
                temp_path = os.path.join(base_path, hwmon_name, "temp1_input")
                if os.path.exists(temp_path):
                    with open(temp_path, "r") as f:
                        value = f.read().strip()
                    return round(int(value) / 1000, 1)
    except Exception:
        pass
    return None


def get_main_disk_usage():
    global last_disk_bytes, last_disk_time
    try:
        io = psutil.disk_io_counters()
        now = time.time()
        if not io: return 0.0
        current = io.read_bytes + io.write_bytes
        dt = now - last_disk_time
        rate = (current - last_disk_bytes) / dt if dt > 0 else 0
        last_disk_bytes = current
        last_disk_time = now
        return round(rate / (1024 * 1024), 2)
    except Exception:
        return 0.0


def cider_get(endpoint):
    return http_session.get(f"{CIDER_API_URL}/{endpoint}", timeout=0.8)


def cider_post(endpoint):
    return http_session.post(f"{CIDER_API_URL}/{endpoint}", timeout=0.8)


def normalize_text(value):
    value = (value or "").lower().strip()
    value = re.sub(r"\(feat\.[^)]+\)", "", value)
    value = re.sub(r"\[feat\.[^\]]+\]", "", value)
    value = re.sub(r"\bfeat\..*$", "", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip(" -")


def lyrics_quality_penalty(text):
    if not text:
        return 999
    return text.upper().count("UNKNOWN")


def build_lrc_line_map(text):
    line_map = {}
    pattern = re.compile(r"\[(\d{2}):(\d{2}(?:\.\d{2,3})?)\](.*)")
    for raw_line in (text or "").splitlines():
        match = pattern.match(raw_line.strip())
        if not match:
            continue
        minutes = int(match.group(1))
        seconds = float(match.group(2))
        lyric = match.group(3).strip()
        if lyric:
            line_map[round(minutes * 60 + seconds, 2)] = lyric
    return line_map


def merge_lrc_versions(primary_text, backup_text):
    if not primary_text:
        return backup_text
    if not backup_text:
        return primary_text

    primary_map = build_lrc_line_map(primary_text)
    backup_map = build_lrc_line_map(backup_text)
    merged_lines = []

    for timestamp in sorted(set(primary_map) | set(backup_map)):
        lyric = primary_map.get(timestamp) or backup_map.get(timestamp)
        if lyric and lyric.upper() != "UNKNOWN":
            minutes = int(timestamp // 60)
            seconds = timestamp - (minutes * 60)
            merged_lines.append(f"[{minutes:02d}:{seconds:05.2f}]{lyric}")

    return "\n".join(merged_lines) if merged_lines else (primary_text or backup_text)


def request_lrclib(url):
    try:
        response = http_session.get(url, timeout=3)
        if response.status_code == 200:
            return response.json()
    except Exception as exc:
        print(f"Lyrics Error: {exc}")
    return None


def payload_from_lrclib_item(item, source):
    synced = item.get("syncedLyrics")
    plain = item.get("plainLyrics")
    lyric_text = synced or plain
    if not lyric_text:
        return None
    return {
        "text": lyric_text,
        "is_synced": bool(synced),
        "source": source,
        "unknown_penalty": lyrics_quality_penalty(lyric_text),
        "plain_text": plain,
        "synced_text": synced,
    }


def clean_lyrics_text(text, is_synced):
    if not text:
        return None

    cleaned_lines = []
    synced_pattern = re.compile(r"(\[\d{2}:\d{2}(?:\.\d{2,3})?\])(.*)")

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        if is_synced:
            match = synced_pattern.match(line)
            if not match:
                continue
            timestamp, lyric = match.groups()
            lyric = lyric.strip()
            if not lyric or lyric.upper() == "UNKNOWN":
                continue
            cleaned_lines.append(f"{timestamp}{lyric}")
        else:
            if line.upper() == "UNKNOWN":
                continue
            cleaned_lines.append(line)

    return "\n".join(cleaned_lines) if cleaned_lines else None


def fetch_lrclib_exact(artist, track, album, duration_sec):
    url = (
        f"{LRCLIB_BASE_URL}/get?artist_name={quote(artist)}"
        f"&track_name={quote(track)}&album_name={quote(album)}&duration={duration_sec}"
    )
    item = request_lrclib(url)
    if item:
        return payload_from_lrclib_item(item, "lrclib-exact")
    return None


def score_search_candidate(item, artist, track, duration_sec):
    score = 0
    candidate_artist = normalize_text(item.get("artistName"))
    candidate_track = normalize_text(item.get("trackName"))
    target_artist = normalize_text(artist)
    target_track = normalize_text(track)

    if candidate_artist == target_artist:
        score += 8
    if candidate_track == target_track:
        score += 10

    candidate_duration = item.get("duration")
    if isinstance(candidate_duration, (int, float)) and duration_sec:
        delta = abs(candidate_duration - duration_sec)
        score += max(0, 6 - min(6, int(delta / 2)))

    if item.get("syncedLyrics"):
        score += 6
    if item.get("plainLyrics"):
        score += 2

    score -= lyrics_quality_penalty(item.get("syncedLyrics") or item.get("plainLyrics"))
    return score


def search_lrclib(artist, track, duration_sec):
    url = f"{LRCLIB_BASE_URL}/search?artist_name={quote(artist)}&track_name={quote(track)}"
    results = request_lrclib(url)
    if not isinstance(results, list):
        return None

    ranked = sorted(
        results,
        key=lambda item: score_search_candidate(item, artist, track, duration_sec),
        reverse=True,
    )
    for item in ranked:
        payload = payload_from_lrclib_item(item, "lrclib-search")
        if payload:
            return payload
    return None


def get_musixmatch_token():
    global musixmatch_token
    url = f"{MUSIXMATCH_BASE}token.get?app_id=web-desktop-app-v1.0"
    try:
        resp = http_session.get(url, headers={"Authority": "apic-desktop.musixmatch.com"}, timeout=2)
        data = resp.json()
        musixmatch_token = data.get("message", {}).get("body", {}).get("user_token")
    except Exception:
        pass


def fetch_musixmatch(artist, track, album, duration_sec):
    global musixmatch_token
    if not musixmatch_token:
        get_musixmatch_token()
    if not musixmatch_token:
        return None

    url = f"{MUSIXMATCH_BASE}macro.subtitles.get"
    params = {
        "app_id": "web-desktop-app-v1.0",
        "format": "json",
        "usertoken": musixmatch_token,
        "q_track": track,
        "q_artist": artist,
        "q_duration": duration_sec,
        "namespace": "lyrics_richsynched",
        "subtitle_format": "lrc",
    }
    if album:
        params["q_album"] = album

    try:
        resp = http_session.get(url, params=params, headers={"Authority": "apic-desktop.musixmatch.com", "Cookie": f"x-mxm-user-id="}, timeout=3)
        data = resp.json()

        status = data.get("message", {}).get("header", {}).get("status_code")
        if status == 401:
            get_musixmatch_token()
            params["usertoken"] = musixmatch_token
            resp = http_session.get(url, params=params, headers={"Authority": "apic-desktop.musixmatch.com", "Cookie": f"x-mxm-user-id="}, timeout=3)
            data = resp.json()

        macro_calls = data.get("message", {}).get("body", {}).get("macro_calls", {})
        
        track_msg = macro_calls.get("matcher.track.get", {}).get("message", {})
        track_body = track_msg.get("body") if isinstance(track_msg.get("body"), dict) else {}
        track_info = track_body.get("track", {})
        if not track_info or track_info.get("track_id") == 115264642:
            return None

        subtitle_msg = macro_calls.get("track.subtitles.get", {}).get("message", {})
        subtitle_body = subtitle_msg.get("body") if isinstance(subtitle_msg.get("body"), dict) else {}
        subtitle_list = subtitle_body.get("subtitle_list", [])
        
        lyrics_msg = macro_calls.get("track.lyrics.get", {}).get("message", {})
        lyrics_body = lyrics_msg.get("body") if isinstance(lyrics_msg.get("body"), dict) else {}
        plain_lyrics = lyrics_body.get("lyrics", {}).get("lyrics_body")

        synced_text = None
        if subtitle_list and isinstance(subtitle_list, list):
            synced_text = subtitle_list[0].get("subtitle", {}).get("subtitle_body")

        if not synced_text and not plain_lyrics:
            return None

        lyric_text = synced_text or plain_lyrics
        return {
            "text": lyric_text,
            "is_synced": bool(synced_text),
            "source": "MusixMatch",
            "unknown_penalty": lyrics_quality_penalty(lyric_text),
            "plain_text": plain_lyrics,
            "synced_text": synced_text,
        }
    except Exception as e:
        print(f"MusixMatch Error: {e}")
        return None


def update_lyrics_background(artist, track, album, duration, job_track_id):
    global cached_lyrics_payload

    with lyrics_lock:
        if job_track_id == current_track_id:
            pass
        else:
            return

    payload = fetch_best_lyrics(artist, track, album, duration)
    payload = store_cached_lyrics(job_track_id, payload)

    with lyrics_lock:
        if job_track_id == current_track_id:
            cached_lyrics_payload = payload

def fetch_best_lyrics(artist, track, album, duration_ms):
    duration_sec = int(duration_ms / 1000) if duration_ms else 0
    candidates = []
    raw_track = (track or "").strip()

    mxm = fetch_musixmatch(artist, raw_track, album, duration_sec)
    if mxm:
        candidates.append(mxm)
        if mxm["is_synced"] and mxm["unknown_penalty"] == 0:
            return {
                "text": clean_lyrics_text(mxm["text"], True),
                "is_synced": True,
                "source": mxm["source"],
            }

    exact = fetch_lrclib_exact(artist, raw_track, album, duration_sec)
    if exact:
        candidates.append(exact)

    normalized_track = normalize_text(raw_track)
    if normalized_track and normalized_track != raw_track.lower():
        normalized_exact = fetch_lrclib_exact(artist, normalized_track, album, duration_sec)
        if normalized_exact:
            candidates.append(normalized_exact)

    search_match = search_lrclib(artist, raw_track, duration_sec)
    if search_match:
        candidates.append(search_match)

    if normalized_track and normalized_track != raw_track.lower():
        normalized_search = search_lrclib(artist, normalized_track, duration_sec)
        if normalized_search:
            candidates.append(normalized_search)

    if not candidates:
        return {"text": None, "is_synced": False, "source": None}

    synced_candidates = [candidate for candidate in candidates if candidate["is_synced"]]
    plain_candidates = [candidate for candidate in candidates if not candidate["is_synced"]]

    best_synced = min(synced_candidates, key=lambda item: item["unknown_penalty"], default=None)
    best_plain = min(plain_candidates, key=lambda item: item["unknown_penalty"], default=None)

    if best_synced and best_plain and best_synced["unknown_penalty"] > best_plain["unknown_penalty"]:
        return {
            "text": clean_lyrics_text(best_plain["text"], False),
            "is_synced": False,
            "source": best_plain["source"],
        }

    chosen = best_synced or best_plain
    return {
        "text": clean_lyrics_text(chosen["text"], chosen["is_synced"]),
        "is_synced": chosen["is_synced"],
        "source": chosen["source"],
    }


def build_stats_payload():
    now = time.time()
    memory = psutil.virtual_memory()
    ram_percent = get_cached_stat("ram", lambda: memory.percent, now)
    with lyrics_lock:
        cpu_value = stats_cache["cpu"]["value"]
    return {
        "mode": "stats",
        "ui_config": get_ui_config(),
        "cpu": cpu_value,
        "ram": ram_percent,
        "ram_used_gb": round((memory.total - memory.available) / (1024 ** 3), 1),
        "ram_total_gb": round(memory.total / (1024 ** 3), 1),
        "gpu": get_cached_stat("gpu", get_amd_gpu_stats, now),
        "cpu_temp": get_cached_stat("cpu_temp", get_cpu_temp, now),
        "gpu_temp": get_cached_stat("gpu_temp", get_gpu_temp, now),
        "disk": get_cached_stat("disk", get_main_disk_usage, now),
    }


@app.route("/api/dashboard")
def dashboard_data():
    global current_track_id, cached_lyrics_payload

    is_playing_future = cider_request_pool.submit(cider_get, "is-playing")
    now_playing_future = cider_request_pool.submit(cider_get, "now-playing")

    try:
        is_playing_req = is_playing_future.result()
        data_json = is_playing_req.json() if is_playing_req.status_code == 200 else {}
        is_playing = data_json.get("is_playing", False) if isinstance(data_json, dict) else False
    except requests.RequestException:
        is_playing = False

    try:
        now_playing_req = now_playing_future.result()
        if now_playing_req.status_code != 200:
            return jsonify(build_stats_payload())

        music_data = now_playing_req.json().get("info", {})
        track_id = music_data.get("playParams", {}).get("id") or (music_data.get("name", "") + "::" + music_data.get("artistName", ""))
        has_active_track = bool(track_id and music_data.get("name"))

        if not has_active_track:
            return jsonify(build_stats_payload())

        should_start_lyrics_job = False
        with lyrics_lock:
            if track_id != current_track_id:
                current_track_id = track_id
                cached_lyrics_payload = {"text": "", "is_synced": False, "source": None}
                cached_payload = get_cached_lyrics(track_id)
                if cached_payload is not None:
                    cached_lyrics_payload = cached_payload
                else:
                    should_start_lyrics_job = True

        if should_start_lyrics_job:
            t = threading.Thread(target=update_lyrics_background, args=(
                music_data.get("artistName", ""),
                music_data.get("name", ""),
                music_data.get("albumName", ""),
                music_data.get("durationInMillis", 0),
                track_id
            ))
            t.daemon = True
            t.start()

        with lyrics_lock:
            lyrics_payload = dict(cached_lyrics_payload)

        payload = {
            "mode": "music",
            "ui_config": get_ui_config(),
            "is_playing": is_playing,
            "has_active_track": True,
            "track": music_data.get("name"),
            "artist": music_data.get("artistName"),
            "album": music_data.get("albumName"),
            "artwork": music_data.get("artwork"),
            "current_time": music_data.get("currentPlaybackTime"),
            "duration": music_data.get("durationInMillis"),
            "lyrics": lyrics_payload.get("text"),
            "lyrics_synced": lyrics_payload.get("is_synced", False),
            "lyrics_source": lyrics_payload.get("source"),
        }
        return jsonify(payload)
    except requests.RequestException:
        return jsonify(build_stats_payload())


@app.route("/api/config", methods=["GET", "POST"])
def config_data():
    if request.method == "GET":
        return jsonify(get_ui_config())

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "Expected JSON object"}), 400

    updated = update_ui_config(payload)
    return jsonify({"ok": True, "config": updated})


@app.route("/api/control/<action>", methods=["POST"])
def control_playback(action):
    allowed_actions = {
        "play": "play",
        "pause": "pause",
        "playpause": "playpause",
        "next": "next",
        "previous": "previous",
    }

    endpoint = allowed_actions.get(action)
    if not endpoint:
        return jsonify({"ok": False, "error": "Unsupported action"}), 400

    try:
        response = cider_post(endpoint)
        return jsonify({"ok": response.ok, "status_code": response.status_code})
    except requests.RequestException as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502


@app.route("/")
def index():
    return send_from_directory(APP_DIR, "index.html")


@app.after_request
def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    return response


if __name__ == "__main__":
    hostname = socket.gethostname()
    print(f"Dashy listening on http://{hostname}.local:{APP_PORT}/")
    app.run(host=APP_HOST, port=APP_PORT, debug=False)
