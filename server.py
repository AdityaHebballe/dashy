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
ADMIN_CONFIG_PATH = CONFIG_DIR / "admin.json"
MANGOHUD_LOG_DIR = Path(os.getenv("DASHY_MANGOHUD_LOG_DIR", os.path.expanduser("~/.local/state/dashy/mangohud")))
GAME_ART_CACHE_DIR = Path(os.getenv("DASHY_GAME_ART_CACHE_DIR", os.path.expanduser("~/.cache/dashy/game_art")))
GAME_ART_META_DIR = GAME_ART_CACHE_DIR / "games"
GAME_ART_IMAGE_DIR = GAME_ART_CACHE_DIR / "images"
GAME_ART_THUMB_DIR = GAME_ART_CACHE_DIR / "thumbs"
SGDB_API_BASE = "https://www.steamgriddb.com/api/v2"

DEFAULT_UI_CONFIG = {
    "lyrics_font_scale": 1.0,
    "album_art_scale": 1.0,
    "active_lyric_scale": 1.03,
    "stats_bg_blur": 1.0,
    "stats_bg_dim": 0.79,
    "stats_card_opacity": 0.36,
    "control_mode": "buttons",
    "swipe_start_threshold": 6.0,
    "swipe_commit_threshold": 72.0,
    "stats_theme": "slate",
}

DEFAULT_ADMIN_CONFIG = {
    "steamgriddb_api_key": "",
    "game_art_overrides": {},
}

# --- CACHE ---
current_track_id = None
cached_lyrics_payload = {"text": None, "is_synced": False, "source": None}
musixmatch_token = None
lyrics_lock = threading.RLock()
stats_lock = threading.RLock()
config_lock = threading.RLock()
admin_config_lock = threading.RLock()
game_art_lock = threading.RLock()
http_session = requests.Session()
lyrics_result_cache = OrderedDict()
cider_request_pool = ThreadPoolExecutor(max_workers=4)
ui_config = dict(DEFAULT_UI_CONFIG)
admin_config = dict(DEFAULT_ADMIN_CONFIG)
admin_config_mtime = 0.0
active_game_art_refreshes = set()
game_art_refresh_failures = {}

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
    "fps": {"value": None, "updated_at": 0.0},
}
cpu_samples = deque(maxlen=4)

STATS_INTERVALS = {
    "fps": 1.0,
    "gpu": 2.0,
    "disk": 2.0,
    "cpu_temp": 3.0,
    "gpu_temp": 3.0,
    "ram": 5.0,
}

MANGOHUD_FPS_STALE_SECONDS = 4.0
MANGOHUD_LOG_MAX_AGE_SECONDS = 24 * 60 * 60
MANGOHUD_CLEANUP_INTERVAL_SECONDS = 6 * 60 * 60
MANGOHUD_IDLE_POLL_SECONDS = 10.0
MANGOHUD_ACTIVE_POLL_SECONDS = 1.0
GAME_ART_CACHE_SCHEMA_VERSION = 3
GAME_ART_TTL_SECONDS = int(os.getenv("DASHY_GAME_ART_TTL_SECONDS", str(30 * 24 * 60 * 60)))
GAME_ART_CACHE_MAX_BYTES = int(os.getenv("DASHY_GAME_ART_CACHE_MAX_BYTES", str(256 * 1024 * 1024)))
GAME_ART_CLEANUP_INTERVAL_SECONDS = int(os.getenv("DASHY_GAME_ART_CLEANUP_INTERVAL_SECONDS", "21600"))
GAME_ART_REFRESH_BACKOFF_SECONDS = int(os.getenv("DASHY_GAME_ART_REFRESH_BACKOFF_SECONDS", "900"))
GAME_ART_LAST_SEEN_WRITE_SECONDS = int(os.getenv("DASHY_GAME_ART_LAST_SEEN_WRITE_SECONDS", "300"))
GAME_ART_HERO_LIMIT = int(os.getenv("DASHY_GAME_ART_HERO_LIMIT", "8"))
GAME_ART_LOGO_LIMIT = int(os.getenv("DASHY_GAME_ART_LOGO_LIMIT", "10"))
next_mangohud_cleanup_at = 0.0
mangohud_last_probe_at = 0.0
mangohud_next_probe_delay = MANGOHUD_IDLE_POLL_SECONDS
mangohud_last_fps_value = None
mangohud_last_game_info = None
last_primed_game_key = None
cached_mangohud_log_path = None
cached_mangohud_log_mtime = 0.0
next_mangohud_rescan_at = 0.0
next_game_art_cleanup_at = 0.0
game_art_state_generation = 0
game_art_games_cache = {"generation": -1, "active_game_key": None, "payload": None}

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
            with stats_lock:
                stats_cache["cpu"]["value"] = averaged
                stats_cache["cpu"]["updated_at"] = time.time()
        except Exception:
            pass
        time.sleep(1.0)


cpu_sampler_thread = threading.Thread(target=cpu_sampler_loop, daemon=True)
cpu_sampler_thread.start()


def get_active_game_info():
    now = time.time()
    info = mangohud_last_game_info
    if info and (now - float(info.get("mtime") or 0.0)) <= MANGOHUD_FPS_STALE_SECONDS:
        return dict(info)
    return None


def clamp_config_value(name, value):
    if name == "control_mode":
        return value if value in {"buttons", "swipe"} else DEFAULT_UI_CONFIG[name]
    if name == "stats_theme":
        return value if value in {"macchiato", "mocha", "graphite", "aurora", "slate"} else DEFAULT_UI_CONFIG[name]

    try:
        numeric = float(value)
    except (TypeError, ValueError):
        numeric = DEFAULT_UI_CONFIG[name]

    limits = {
        "lyrics_font_scale": (0.8, 1.5),
        "album_art_scale": (0.85, 1.25),
        "active_lyric_scale": (1.0, 1.12),
        "stats_bg_blur": (0.0, 12.0),
        "stats_bg_dim": (0.45, 1.0),
        "stats_card_opacity": (0.12, 0.72),
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


def normalize_admin_config(candidate):
    overrides = candidate.get("game_art_overrides", {}) if isinstance(candidate, dict) else {}
    if not isinstance(overrides, dict):
        overrides = {}

    normalized_overrides = {}
    for game_key, override in overrides.items():
        if not isinstance(game_key, str) or not isinstance(override, dict):
            continue
        normalized_overrides[game_key] = {
            "lookup_query": (override.get("lookup_query") or "").strip(),
            "matched_game_id": int(override.get("matched_game_id") or 0) or None,
            "selected_hero_id": int(override.get("selected_hero_id") or 0) or None,
            "selected_logo_id": int(override.get("selected_logo_id") or 0) or None,
        }

    return {
        "steamgriddb_api_key": ((candidate.get("steamgriddb_api_key") if isinstance(candidate, dict) else "") or "").strip(),
        "game_art_overrides": normalized_overrides,
    }


def persist_admin_config():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    temp_path = ADMIN_CONFIG_PATH.with_suffix(".tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        json.dump(admin_config, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_path, ADMIN_CONFIG_PATH)


def load_admin_config_if_needed(force=False):
    global admin_config, admin_config_mtime

    try:
        stat = ADMIN_CONFIG_PATH.stat()
        mtime = stat.st_mtime
    except FileNotFoundError:
        with admin_config_lock:
            admin_config = dict(DEFAULT_ADMIN_CONFIG)
            persist_admin_config()
            try:
                admin_config_mtime = ADMIN_CONFIG_PATH.stat().st_mtime
            except FileNotFoundError:
                admin_config_mtime = 0.0
        return
    except Exception:
        return

    if not force and mtime <= admin_config_mtime:
        return

    try:
        with ADMIN_CONFIG_PATH.open("r", encoding="utf-8") as handle:
            loaded = json.load(handle)
    except Exception:
        with admin_config_lock:
            admin_config = dict(DEFAULT_ADMIN_CONFIG)
            persist_admin_config()
            try:
                admin_config_mtime = ADMIN_CONFIG_PATH.stat().st_mtime
            except FileNotFoundError:
                admin_config_mtime = 0.0
        return

    with admin_config_lock:
        admin_config = normalize_admin_config(loaded if isinstance(loaded, dict) else {})
        admin_config_mtime = mtime


def get_admin_config():
    load_admin_config_if_needed()
    with admin_config_lock:
        return json.loads(json.dumps(admin_config))


def update_admin_overrides(game_key, partial_override):
    global admin_config_mtime, game_art_state_generation

    load_admin_config_if_needed()
    with admin_config_lock:
        merged = dict(admin_config)
        overrides = dict(merged.get("game_art_overrides", {}))
        current = dict(overrides.get(game_key, {}))
        current.update(partial_override)
        overrides[game_key] = normalize_admin_config({"game_art_overrides": {game_key: current}})["game_art_overrides"][game_key]
        merged["game_art_overrides"] = overrides
        admin_config.clear()
        admin_config.update(merged)
        persist_admin_config()
        try:
            admin_config_mtime = ADMIN_CONFIG_PATH.stat().st_mtime
        except FileNotFoundError:
            admin_config_mtime = 0.0
        game_art_state_generation += 1
        return dict(overrides[game_key])


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
load_admin_config_if_needed(force=True)


def estimate_lyrics_payload_size(track_id, payload):
    text = payload.get("text") or ""
    source = payload.get("source") or ""
    return len(track_id.encode("utf-8")) + len(text.encode("utf-8")) + len(source.encode("utf-8")) + 64


def slugify_text(value):
    text = (value or "").lower().strip()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-") or hashlib.sha256((value or "unknown").encode("utf-8")).hexdigest()[:16]


def extract_mangohud_game_name(stem):
    cleaned = re.sub(r"_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}$", "", stem or "")
    cleaned = cleaned.replace("_", " ").replace("-", " ")
    cleaned = re.sub(r"(?<=[a-z])(?=[A-Z0-9])", " ", cleaned)
    cleaned = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", cleaned)
    cleaned = re.sub(r"(?<=[0-9])(?=[A-Za-z])", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or "Unknown Game"


def build_game_art_record_path(game_key):
    return GAME_ART_META_DIR / f"{game_key}.json"


def normalize_game_art_record(record):
    def normalize_assets(items, default_style):
        normalized = []
        for item in items or []:
            if not isinstance(item, dict):
                continue
            asset_id = int(item.get("id") or 0)
            if not asset_id:
                continue
            normalized.append({
                "id": asset_id,
                "width": int(item.get("width") or 0),
                "height": int(item.get("height") or 0),
                "style": item.get("style") or default_style,
                "mime": item.get("mime") or "image/png",
                "url": item.get("url"),
                "thumb_url": item.get("thumb_url"),
                "thumb_filename": item.get("thumb_filename"),
                "image_filename": item.get("image_filename"),
            })
        return normalized

    return {
        "game_key": record.get("game_key"),
        "raw_name": record.get("raw_name"),
        "display_name": record.get("display_name"),
        "lookup_query": (record.get("lookup_query") or "").strip(),
        "matched_game_id": int(record.get("matched_game_id") or 0) or None,
        "matched_game_name": record.get("matched_game_name"),
        "selected_hero_id": int(record.get("selected_hero_id") or 0) or None,
        "selected_logo_id": int(record.get("selected_logo_id") or 0) or None,
        "heroes": normalize_assets(record.get("heroes", []), "alternate"),
        "logos": normalize_assets(record.get("logos", []), "official"),
        "updated_at": float(record.get("updated_at") or 0),
        "last_seen_at": float(record.get("last_seen_at") or 0),
    }


def expected_asset_filename(asset_kind, asset_id, url):
    suffix = Path((url or "").split("?", 1)[0]).suffix or ".img"
    return f"{asset_kind}-{asset_id}{suffix}"


def expected_thumb_filename(asset_kind, asset_id, thumb_url):
    suffix = Path((thumb_url or "").split("?", 1)[0]).suffix or ".img"
    return f"thumb-{asset_kind}-{asset_id}{suffix}"


def asset_collection_name(asset_kind):
    return {
        "hero": "heroes",
        "logo": "logos",
    }[asset_kind]


def hydrate_asset_filenames(record):
    changed = False
    for asset_kind in ("hero", "logo"):
        assets = record.get(asset_collection_name(asset_kind), [])
        for asset in assets:
            asset_id = asset.get("id")
            if not asset_id:
                continue

            if not asset.get("thumb_filename"):
                thumb_filename = expected_thumb_filename(asset_kind, asset_id, asset.get("thumb_url"))
                if (GAME_ART_THUMB_DIR / thumb_filename).exists():
                    asset["thumb_filename"] = thumb_filename
                    changed = True

            if not asset.get("image_filename"):
                image_filename = expected_asset_filename(asset_kind, asset_id, asset.get("url"))
                if (GAME_ART_IMAGE_DIR / image_filename).exists():
                    asset["image_filename"] = image_filename
                    changed = True

    return changed


def resolve_asset_filename(asset_kind, asset, filename_field, base_dir, expected_builder):
    filename = asset.get(filename_field)
    if filename and (base_dir / filename).exists():
        return filename

    asset_id = asset.get("id")
    if not asset_id:
        return None

    source_url = asset.get("thumb_url") if filename_field == "thumb_filename" else asset.get("url")
    expected = expected_builder(asset_kind, asset_id, source_url)
    if (base_dir / expected).exists():
        asset[filename_field] = expected
        return expected
    return None


def build_game_art_public_paths(record, asset_kind, asset):
    game_key = record["game_key"]
    thumb_filename = resolve_asset_filename(asset_kind, asset, "thumb_filename", GAME_ART_THUMB_DIR, expected_thumb_filename)
    image_filename = resolve_asset_filename(asset_kind, asset, "image_filename", GAME_ART_IMAGE_DIR, expected_asset_filename)
    result = {
        "thumb_url": f"/api/game-art/thumb/{game_key}/{thumb_filename}" if thumb_filename else None,
        "image_url": f"/api/game-art/image/{game_key}/{image_filename}" if image_filename else None,
        "thumb_path": str((GAME_ART_THUMB_DIR / thumb_filename).resolve()) if thumb_filename else None,
        "image_path": str((GAME_ART_IMAGE_DIR / image_filename).resolve()) if image_filename else None,
    }
    return result


def cleanup_game_art_cache(force=False):
    global next_game_art_cleanup_at

    now = time.time()
    if not force and now < next_game_art_cleanup_at:
        return

    next_game_art_cleanup_at = now + GAME_ART_CLEANUP_INTERVAL_SECONDS

    try:
        GAME_ART_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        GAME_ART_META_DIR.mkdir(parents=True, exist_ok=True)
        GAME_ART_IMAGE_DIR.mkdir(parents=True, exist_ok=True)
        GAME_ART_THUMB_DIR.mkdir(parents=True, exist_ok=True)

        entries = []
        total_size = 0
        for path in GAME_ART_CACHE_DIR.rglob("*"):
            if not path.is_file():
                continue
            try:
                stat = path.stat()
            except FileNotFoundError:
                continue
            total_size += stat.st_size
            entries.append((path, stat.st_size, stat.st_atime, stat.st_mtime))
            if (now - stat.st_mtime) > GAME_ART_TTL_SECONDS:
                path.unlink(missing_ok=True)
                total_size -= stat.st_size

        if total_size <= GAME_ART_CACHE_MAX_BYTES:
            return

        live_entries = []
        for path, size, atime, mtime in entries:
            if not path.exists():
                continue
            live_entries.append((path, size, atime, mtime))
        live_entries.sort(key=lambda item: (item[2], item[3]))

        for path, size, _atime, _mtime in live_entries:
            if total_size <= GAME_ART_CACHE_MAX_BYTES:
                break
            path.unlink(missing_ok=True)
            total_size -= size
    except Exception:
        pass


def get_game_art_record(game_key):
    path = build_game_art_record_path(game_key)
    try:
        with path.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except FileNotFoundError:
        return None
    except Exception:
        path.unlink(missing_ok=True)
        return None

    if raw.get("version") != GAME_ART_CACHE_SCHEMA_VERSION:
        path.unlink(missing_ok=True)
        return None
    if raw.get("expires_at", 0) < time.time():
        path.unlink(missing_ok=True)
        return None

    record = normalize_game_art_record(raw)
    if hydrate_asset_filenames(record):
        store_game_art_record(game_key, record)
    return record


def store_game_art_record(game_key, record):
    global game_art_state_generation
    record_path = build_game_art_record_path(game_key)
    record_payload = normalize_game_art_record(record)
    stored = {
        "version": GAME_ART_CACHE_SCHEMA_VERSION,
        "expires_at": time.time() + GAME_ART_TTL_SECONDS,
        "updated_at": time.time(),
        **record_payload,
    }
    try:
        record_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = record_path.with_suffix(".tmp")
        with temp_path.open("w", encoding="utf-8") as handle:
            json.dump(stored, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, record_path)
    except Exception:
        try:
            temp_path.unlink(missing_ok=True)
        except Exception:
            pass
    cleanup_game_art_cache()
    game_art_state_generation += 1
    return record_payload


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
    with stats_lock:
        cached = stats_cache[stat_name]
        if (now - cached["updated_at"]) < STATS_INTERVALS[stat_name]:
            return cached["value"]

    value = getter()

    with stats_lock:
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


def read_last_text_line(path, block_size=4096):
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            file_size = handle.tell()
            if file_size <= 0:
                return None

            buffer = b""
            offset = file_size
            while offset > 0 and len(buffer) < (block_size * 8):
                read_size = min(block_size, offset)
                offset -= read_size
                handle.seek(offset)
                buffer = handle.read(read_size) + buffer
                lines = buffer.splitlines()
                for raw_line in reversed(lines):
                    line = raw_line.decode("utf-8", "replace").strip()
                    if line:
                        return line
    except Exception:
        return None
    return None


def parse_mangohud_fps_line(line):
    if not line:
        return None

    first_field = line.split(",", 1)[0].strip()
    try:
        value = float(first_field)
    except ValueError:
        return None

    if value < 0 or value > 10000:
        return None
    return round(value, 1)


def cleanup_mangohud_logs(force=False):
    global next_mangohud_cleanup_at

    now = time.time()
    if not force and now < next_mangohud_cleanup_at:
        return

    next_mangohud_cleanup_at = now + MANGOHUD_CLEANUP_INTERVAL_SECONDS

    try:
        if not MANGOHUD_LOG_DIR.is_dir():
            return

        for path in MANGOHUD_LOG_DIR.iterdir():
            if not path.is_file():
                continue
            try:
                if (now - path.stat().st_mtime) > MANGOHUD_LOG_MAX_AGE_SECONDS:
                    path.unlink(missing_ok=True)
            except OSError:
                continue
    except Exception:
        pass


def get_latest_mangohud_log(now=None):
    global cached_mangohud_log_path, cached_mangohud_log_mtime, next_mangohud_rescan_at
    now = now or time.time()
    try:
        if not MANGOHUD_LOG_DIR.is_dir():
            return None

        if cached_mangohud_log_path is not None and now < next_mangohud_rescan_at:
            try:
                stat = cached_mangohud_log_path.stat()
                if stat.st_mtime >= cached_mangohud_log_mtime:
                    cached_mangohud_log_mtime = stat.st_mtime
                if (now - cached_mangohud_log_mtime) <= MANGOHUD_FPS_STALE_SECONDS:
                    display_name = extract_mangohud_game_name(cached_mangohud_log_path.stem)
                    return {
                        "path": cached_mangohud_log_path,
                        "mtime": cached_mangohud_log_mtime,
                        "raw_name": cached_mangohud_log_path.stem,
                        "display_name": display_name,
                        "game_key": slugify_text(display_name),
                    }
            except OSError:
                cached_mangohud_log_path = None
                cached_mangohud_log_mtime = 0.0

        latest_path = None
        latest_mtime = 0.0
        for path in MANGOHUD_LOG_DIR.iterdir():
            if not path.is_file():
                continue
            if path.suffix.lower() != ".csv" or path.name.endswith("_summary.csv"):
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            if stat.st_mtime > latest_mtime:
                latest_mtime = stat.st_mtime
                latest_path = path

        if latest_path is None or (now - latest_mtime) > MANGOHUD_FPS_STALE_SECONDS:
            cached_mangohud_log_path = None
            cached_mangohud_log_mtime = 0.0
            next_mangohud_rescan_at = now + MANGOHUD_IDLE_POLL_SECONDS
            return None

        cached_mangohud_log_path = latest_path
        cached_mangohud_log_mtime = latest_mtime
        next_mangohud_rescan_at = now + 3.0
        display_name = extract_mangohud_game_name(latest_path.stem)
        return {
            "path": latest_path,
            "mtime": latest_mtime,
            "raw_name": latest_path.stem,
            "display_name": display_name,
            "game_key": slugify_text(display_name),
        }
    except Exception:
        return None


def guess_lookup_query(display_name):
    query = (display_name or "").replace(":", " ")
    query = re.sub(r"\s+", " ", query).strip()
    return query


def score_sgdb_match(candidate_name, query):
    target = re.sub(r"[^a-z0-9]+", "", (query or "").lower())
    candidate = re.sub(r"[^a-z0-9]+", "", (candidate_name or "").lower())
    if not target or not candidate:
        return -999
    if candidate == target:
        return 100
    if candidate_name.lower() == query.lower():
        return 90
    if candidate.startswith(target):
        return 70
    if target in candidate:
        return 50
    return -abs(len(candidate) - len(target))


def get_game_art_override(game_key):
    config = get_admin_config()
    return dict(config.get("game_art_overrides", {}).get(game_key, {}))


def sgdb_request(path):
    config = get_admin_config()
    api_key = (config.get("steamgriddb_api_key") or "").strip()
    if not api_key:
        return None
    headers = {
        "Authorization": f"Bearer {api_key}",
        "User-Agent": "dashy/1.0",
    }
    try:
        response = http_session.get(f"{SGDB_API_BASE}{path}", headers=headers, timeout=8)
        if response.status_code != 200:
            return None
        payload = response.json()
        if not isinstance(payload, dict) or not payload.get("success"):
            return None
        return payload
    except Exception:
        return None


def sgdb_select_game(query, forced_game_id=None):
    if forced_game_id:
        payload = sgdb_request(f"/games/id/{int(forced_game_id)}")
        data = payload.get("data") if payload else None
        if isinstance(data, dict):
            return {"id": int(data.get("id") or forced_game_id), "name": data.get("name") or query}

    payload = sgdb_request(f"/search/autocomplete/{quote(query)}")
    entries = payload.get("data", []) if payload else []
    if not entries:
        return None

    best = max(entries, key=lambda item: score_sgdb_match(item.get("name", ""), query))
    return {"id": int(best.get("id") or 0), "name": best.get("name") or query} if best.get("id") else None


def download_cached_file(url, destination_dir, stem_prefix):
    if not url:
        return None
    suffix = Path(url.split("?", 1)[0]).suffix or ".img"
    filename = f"{stem_prefix}{suffix}"
    path = destination_dir / filename
    if path.exists():
        try:
            os.utime(path, None)
        except Exception:
            pass
        return filename

    try:
        destination_dir.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(path.suffix + ".tmp")
        with http_session.get(url, stream=True, timeout=12) as response:
            if response.status_code != 200:
                return None
            with temp_path.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=65536):
                    if chunk:
                        handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
        os.replace(temp_path, path)
        return filename
    except Exception:
        try:
            temp_path.unlink(missing_ok=True)
        except Exception:
            pass
        return None


def fetch_sgdb_asset_group(match, endpoint, thumb_prefix, image_prefix, limit, default_style):
    payload = sgdb_request(f"/{endpoint}/game/{match['id']}")
    items = payload.get("data", []) if payload else []
    if not items:
        return []

    assets = []
    for item in items[:limit]:
        asset_id = int(item.get("id") or 0)
        if not asset_id:
            continue
        assets.append({
            "id": asset_id,
            "width": int(item.get("width") or 0),
            "height": int(item.get("height") or 0),
            "style": item.get("style") or default_style,
            "mime": item.get("mime") or "image/png",
            "url": item.get("url"),
            "thumb_url": item.get("thumb"),
            "thumb_filename": None,
            "image_filename": None,
            "_image_prefix": image_prefix,
            "_thumb_prefix": thumb_prefix,
        })
    return assets


def fetch_sgdb_assets(game_info, query=None, forced_game_id=None):
    lookup_query = query or guess_lookup_query(game_info["display_name"])
    match = sgdb_select_game(lookup_query, forced_game_id=forced_game_id)
    if not match:
        return None

    heroes = fetch_sgdb_asset_group(match, "heroes", "thumb-hero", "hero", GAME_ART_HERO_LIMIT, "alternate")
    logos = fetch_sgdb_asset_group(match, "logos", "thumb-logo", "logo", GAME_ART_LOGO_LIMIT, "official")

    if not heroes and not logos:
        return None

    return {
        "lookup_query": lookup_query,
        "matched_game_id": match["id"],
        "matched_game_name": match["name"],
        "heroes": heroes,
        "logos": logos,
    }


def ensure_asset_download(record, asset_kind, asset_id=None, include_image=True):
    assets = record.get(asset_collection_name(asset_kind), [])
    for asset in assets:
        current_id = asset.get("id")
        if asset_id is not None and current_id != asset_id:
            continue
        if not asset.get("thumb_filename"):
            expected_thumb = expected_thumb_filename(asset_kind, current_id, asset.get("thumb_url"))
            expected_thumb_path = GAME_ART_THUMB_DIR / expected_thumb
            if expected_thumb_path.exists():
                asset["thumb_filename"] = expected_thumb
            else:
                thumb_prefix = asset.get("_thumb_prefix") or f"thumb-{asset_kind}"
                asset["thumb_filename"] = download_cached_file(asset.get("thumb_url"), GAME_ART_THUMB_DIR, f"{thumb_prefix}-{current_id}")
        if include_image and not asset.get("image_filename"):
            image_prefix = asset.get("_image_prefix") or asset_kind
            expected_filename = expected_asset_filename(image_prefix, current_id, asset.get("url"))
            expected_path = GAME_ART_IMAGE_DIR / expected_filename
            if expected_path.exists():
                asset["image_filename"] = expected_filename
            else:
                asset["image_filename"] = download_cached_file(asset.get("url"), GAME_ART_IMAGE_DIR, f"{image_prefix}-{current_id}")
        if asset_id is not None:
            break
    return record


def ensure_selected_asset_download(record, asset_kind):
    selected_id = record.get(f"selected_{asset_kind}_id")
    if not selected_id:
        return record
    return ensure_asset_download(record, asset_kind, asset_id=selected_id, include_image=True)


def ensure_selected_game_art_download(record):
    ensure_selected_asset_download(record, "hero")
    ensure_selected_asset_download(record, "logo")
    return record


def refresh_game_art_record(game_info, query=None, forced_game_id=None):
    override = get_game_art_override(game_info["game_key"])
    fetch_result = fetch_sgdb_assets(
        game_info,
        query=query or override.get("lookup_query") or None,
        forced_game_id=forced_game_id or override.get("matched_game_id"),
    )
    if not fetch_result:
        return None

    selected_hero_id = override.get("selected_hero_id")
    hero_ids = {hero["id"] for hero in fetch_result["heroes"]}
    if hero_ids and selected_hero_id not in hero_ids:
        selected_hero_id = fetch_result["heroes"][0]["id"]

    selected_logo_id = override.get("selected_logo_id")
    logo_ids = {logo["id"] for logo in fetch_result["logos"]}
    if logo_ids and selected_logo_id not in logo_ids:
        selected_logo_id = fetch_result["logos"][0]["id"]

    update_admin_overrides(game_info["game_key"], {
        "selected_hero_id": selected_hero_id,
        "selected_logo_id": selected_logo_id,
    })

    record = {
        "game_key": game_info["game_key"],
        "raw_name": game_info["raw_name"],
        "display_name": game_info["display_name"],
        "lookup_query": fetch_result["lookup_query"],
        "matched_game_id": fetch_result["matched_game_id"],
        "matched_game_name": fetch_result["matched_game_name"],
        "selected_hero_id": selected_hero_id,
        "selected_logo_id": selected_logo_id,
        "heroes": fetch_result["heroes"],
        "logos": fetch_result["logos"],
        "last_seen_at": time.time(),
    }
    for hero in record["heroes"]:
        ensure_asset_download(record, "hero", asset_id=hero["id"], include_image=False)
    for logo in record["logos"]:
        ensure_asset_download(record, "logo", asset_id=logo["id"], include_image=False)
    ensure_selected_game_art_download(record)
    return store_game_art_record(game_info["game_key"], record)


def start_game_art_refresh(game_info, query=None, forced_game_id=None):
    game_key = game_info["game_key"]
    with game_art_lock:
        last_failure = game_art_refresh_failures.get(game_key, 0.0)
        if not (query or forced_game_id) and last_failure and (time.time() - last_failure) < GAME_ART_REFRESH_BACKOFF_SECONDS:
            return
        if game_key in active_game_art_refreshes:
            return
        active_game_art_refreshes.add(game_key)

    def worker():
        try:
            record = refresh_game_art_record(game_info, query=query, forced_game_id=forced_game_id)
            with game_art_lock:
                if record:
                    game_art_refresh_failures.pop(game_key, None)
                else:
                    game_art_refresh_failures[game_key] = time.time()
        finally:
            with game_art_lock:
                active_game_art_refreshes.discard(game_key)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()


def maybe_prime_game_art(game_info, now=None):
    if not game_info:
        return

    now = now or time.time()
    config = get_admin_config()
    if not config.get("steamgriddb_api_key"):
        return

    record = get_game_art_record(game_info["game_key"])
    if record:
        should_store = False
        if record.get("selected_hero_id"):
            selected_hero = next((hero for hero in record.get("heroes", []) if hero.get("id") == record.get("selected_hero_id")), None)
            if selected_hero and not selected_hero.get("image_filename"):
                record = ensure_selected_game_art_download(record)
                should_store = True
        if record.get("selected_logo_id"):
            selected_logo = next((logo for logo in record.get("logos", []) if logo.get("id") == record.get("selected_logo_id")), None)
            if selected_logo and not selected_logo.get("image_filename"):
                record = ensure_selected_game_art_download(record)
                should_store = True
        if (now - float(record.get("last_seen_at") or 0.0)) >= GAME_ART_LAST_SEEN_WRITE_SECONDS:
            record["last_seen_at"] = now
            should_store = True
        if should_store:
            store_game_art_record(game_info["game_key"], record)
        return

    start_game_art_refresh(game_info)


def mangohud_monitor_loop():
    global mangohud_last_probe_at, mangohud_next_probe_delay, mangohud_last_game_info, mangohud_last_fps_value, last_primed_game_key

    while True:
        now = time.time()
        try:
            cleanup_mangohud_logs()
            latest_log = get_latest_mangohud_log(now)
            mangohud_last_probe_at = now
            mangohud_last_game_info = latest_log
            if latest_log:
                line = read_last_text_line(latest_log["path"])
                mangohud_last_fps_value = parse_mangohud_fps_line(line)
                game_key = latest_log["game_key"]
                if game_key != last_primed_game_key:
                    maybe_prime_game_art(latest_log, now=now)
                    last_primed_game_key = game_key
                mangohud_next_probe_delay = MANGOHUD_ACTIVE_POLL_SECONDS
            else:
                mangohud_last_fps_value = None
                last_primed_game_key = None
                mangohud_next_probe_delay = MANGOHUD_IDLE_POLL_SECONDS
        except Exception:
            mangohud_last_fps_value = None
            mangohud_next_probe_delay = MANGOHUD_IDLE_POLL_SECONDS

        time.sleep(max(1.0, mangohud_next_probe_delay))


mangohud_monitor_thread = threading.Thread(target=mangohud_monitor_loop, daemon=True)
mangohud_monitor_thread.start()


def build_game_art_payload(record, active_game_key=None):
    if not record:
        return None

    def build_assets(asset_kind):
        selected_asset = None
        assets = []
        selected_id = record.get(f"selected_{asset_kind}_id")
        for asset in record.get(asset_collection_name(asset_kind), []):
            paths = build_game_art_public_paths(record, asset_kind, asset)
            payload = {
                "id": asset.get("id"),
                "width": asset.get("width"),
                "height": asset.get("height"),
                "style": asset.get("style"),
                "thumb_url": paths["thumb_url"],
                "image_url": paths["image_url"],
                "thumb_path": paths["thumb_path"],
                "image_path": paths["image_path"],
                "selected": asset.get("id") == selected_id,
            }
            if payload["selected"]:
                selected_asset = payload
            assets.append(payload)
        return selected_asset, assets

    selected_hero, heroes = build_assets("hero")
    selected_logo, logos = build_assets("logo")
    override = get_game_art_override(record["game_key"])
    return {
        "game_key": record.get("game_key"),
        "display_name": record.get("display_name"),
        "raw_name": record.get("raw_name"),
        "lookup_query": override.get("lookup_query") or record.get("lookup_query"),
        "matched_game_id": record.get("matched_game_id"),
        "matched_game_name": record.get("matched_game_name"),
        "selected_hero_id": record.get("selected_hero_id"),
        "selected_logo_id": record.get("selected_logo_id"),
        "selected_image_url": selected_hero.get("image_url") if selected_hero else None,
        "selected_thumb_url": selected_hero.get("thumb_url") if selected_hero else None,
        "selected_image_path": selected_hero.get("image_path") if selected_hero else None,
        "selected_thumb_path": selected_hero.get("thumb_path") if selected_hero else None,
        "selected_logo_url": selected_logo.get("image_url") if selected_logo else None,
        "selected_logo_thumb_url": selected_logo.get("thumb_url") if selected_logo else None,
        "selected_logo_path": selected_logo.get("image_path") if selected_logo else None,
        "selected_logo_thumb_path": selected_logo.get("thumb_path") if selected_logo else None,
        "fps_asset_url": selected_logo.get("image_url") if selected_logo else None,
        "fps_asset_thumb_url": selected_logo.get("thumb_url") if selected_logo else None,
        "fps_asset_path": selected_logo.get("image_path") if selected_logo else None,
        "heroes": heroes,
        "logos": logos,
        "active": record.get("game_key") == active_game_key,
        "last_seen_at": record.get("last_seen_at"),
    }


def get_all_game_art_records():
    records = []
    try:
        if not GAME_ART_META_DIR.is_dir():
            return records
        for path in GAME_ART_META_DIR.glob("*.json"):
            record = get_game_art_record(path.stem)
            if record:
                records.append(record)
    except Exception:
        pass
    records.sort(key=lambda item: item.get("last_seen_at", 0), reverse=True)
    return records


def get_mangohud_fps():
    return mangohud_last_fps_value


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
    with stats_lock:
        cpu_value = stats_cache["cpu"]["value"]
    fps_value = get_cached_stat("fps", get_mangohud_fps, now)
    game_info = get_active_game_info()
    game_art = None
    if game_info:
        record = get_game_art_record(game_info["game_key"])
        if record:
            game_art = build_game_art_payload(record, active_game_key=game_info["game_key"])
        else:
            maybe_prime_game_art(game_info, now=now)
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
        "fps": fps_value,
        "mangohud_active": game_info is not None,
        "game_name": game_info["display_name"] if game_info else None,
        "game_key": game_info["game_key"] if game_info else None,
        "game_art": game_art,
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
        cached_payload = None
        with lyrics_lock:
            track_changed = track_id != current_track_id
            if track_changed:
                current_track_id = track_id
                cached_lyrics_payload = {"text": "", "is_synced": False, "source": None}
            else:
                track_changed = False

        if track_changed:
            cached_payload = get_cached_lyrics(track_id)
            with lyrics_lock:
                if track_id == current_track_id:
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


@app.route("/api/game-art/games", methods=["GET"])
def game_art_games():
    global game_art_games_cache
    active_game_info = get_active_game_info()
    active_game_key = active_game_info["game_key"] if active_game_info else None
    cache_entry = game_art_games_cache
    if (
        cache_entry["generation"] == game_art_state_generation
        and cache_entry["active_game_key"] == active_game_key
        and cache_entry["payload"] is not None
    ):
        return jsonify(cache_entry["payload"])
    records = [build_game_art_payload(record, active_game_key=active_game_key) for record in get_all_game_art_records()]
    records = [record for record in records if record is not None]
    if active_game_info and all(record["game_key"] != active_game_key for record in records):
        override = get_game_art_override(active_game_key)
        records.insert(0, {
            "game_key": active_game_info["game_key"],
            "display_name": active_game_info["display_name"],
            "raw_name": active_game_info["raw_name"],
            "lookup_query": override.get("lookup_query") or guess_lookup_query(active_game_info["display_name"]),
            "matched_game_id": override.get("matched_game_id"),
            "matched_game_name": None,
            "selected_hero_id": override.get("selected_hero_id"),
            "selected_logo_id": override.get("selected_logo_id"),
            "selected_image_url": None,
            "selected_thumb_url": None,
            "selected_image_path": None,
            "selected_thumb_path": None,
            "selected_logo_url": None,
            "selected_logo_thumb_url": None,
            "selected_logo_path": None,
            "selected_logo_thumb_path": None,
            "fps_asset_url": None,
            "fps_asset_thumb_url": None,
            "fps_asset_path": None,
            "heroes": [],
            "logos": [],
            "active": True,
            "last_seen_at": active_game_info["mtime"],
        })
    payload = {
        "ok": True,
        "has_api_key": bool(get_admin_config().get("steamgriddb_api_key")),
        "active_game_key": active_game_key,
        "active_game_name": active_game_info["display_name"] if active_game_info else None,
        "games": records,
    }
    game_art_games_cache = {
        "generation": game_art_state_generation,
        "active_game_key": active_game_key,
        "payload": payload,
    }
    return jsonify(payload)


@app.route("/api/game-art/refresh", methods=["POST"])
def refresh_game_art():
    payload = request.get_json(silent=True) or {}
    game_key = payload.get("game_key")
    query = (payload.get("query") or "").strip() or None
    forced_game_id = int(payload.get("matched_game_id") or 0) or None

    if game_key:
        record = get_game_art_record(game_key)
        if record:
            game_info = {
                "game_key": record["game_key"],
                "display_name": record["display_name"],
                "raw_name": record["raw_name"],
            }
        else:
            active_game_info = get_active_game_info()
            if active_game_info and active_game_info["game_key"] == game_key:
                game_info = dict(active_game_info)
            else:
                return jsonify({"ok": False, "error": "Unknown game"}), 404
    else:
        game_info = get_active_game_info()
        if not game_info:
            return jsonify({"ok": False, "error": "No active MangoHud game detected"}), 404

    if query is not None:
        update_admin_overrides(game_info["game_key"], {"lookup_query": query})
    if forced_game_id is not None:
        update_admin_overrides(game_info["game_key"], {"matched_game_id": forced_game_id})

    record = refresh_game_art_record(game_info, query=query, forced_game_id=forced_game_id)
    if not record:
        return jsonify({"ok": False, "error": "No SteamGridDB match or artwork found"}), 404
    active_game_info = get_active_game_info()
    return jsonify({"ok": True, "game": build_game_art_payload(record, active_game_key=active_game_info["game_key"] if active_game_info else None)})


@app.route("/api/game-art/select", methods=["POST"])
def select_game_art():
    payload = request.get_json(silent=True) or {}
    game_key = payload.get("game_key")
    hero_id = int(payload.get("hero_id") or 0) or None
    asset_kind = (payload.get("asset_kind") or "hero").strip().lower()
    if asset_kind not in {"hero", "logo"}:
        return jsonify({"ok": False, "error": "Expected asset_kind to be hero or logo"}), 400
    asset_id = hero_id
    if not game_key or not asset_id:
        return jsonify({"ok": False, "error": "Expected game_key and hero_id"}), 400

    record = get_game_art_record(game_key)
    if not record:
        return jsonify({"ok": False, "error": "Unknown game"}), 404

    asset_field = asset_collection_name(asset_kind)
    if asset_id not in {asset["id"] for asset in record.get(asset_field, [])}:
        return jsonify({"ok": False, "error": f"{asset_kind.title()} not available in cache"}), 404

    update_admin_overrides(game_key, {f"selected_{asset_kind}_id": asset_id})
    record[f"selected_{asset_kind}_id"] = asset_id
    record = ensure_selected_asset_download(record, asset_kind)
    record = store_game_art_record(game_key, record)
    active_game_info = get_active_game_info()
    return jsonify({"ok": True, "game": build_game_art_payload(record, active_game_key=active_game_info["game_key"] if active_game_info else None)})


@app.route("/api/game-art/thumb/<game_key>/<filename>")
def serve_game_art_thumb(game_key, filename):
    record = get_game_art_record(game_key)
    if not record:
        return jsonify({"ok": False, "error": "Unknown game"}), 404
    valid_filenames = set()
    for singular, plural in (("hero", "heroes"), ("logo", "logos")):
        for asset in record.get(plural, []):
            asset_id = asset.get("id")
            if not asset_id:
                continue
            if asset.get("thumb_filename"):
                valid_filenames.add(asset.get("thumb_filename"))
            valid_filenames.add(expected_thumb_filename(singular, asset_id, asset.get("thumb_url")))
    if filename not in valid_filenames:
        return jsonify({"ok": False, "error": "Unknown thumbnail"}), 404
    return send_from_directory(GAME_ART_THUMB_DIR, filename)


@app.route("/api/game-art/image/<game_key>/<filename>")
def serve_game_art_image(game_key, filename):
    record = get_game_art_record(game_key)
    if not record:
        return jsonify({"ok": False, "error": "Unknown game"}), 404
    valid_filenames = set()
    for singular, plural in (("hero", "heroes"), ("logo", "logos")):
        for asset in record.get(plural, []):
            asset_id = asset.get("id")
            if not asset_id:
                continue
            if asset.get("image_filename"):
                valid_filenames.add(asset.get("image_filename"))
            valid_filenames.add(expected_asset_filename(singular, asset_id, asset.get("url")))
    if filename not in valid_filenames:
        return jsonify({"ok": False, "error": "Unknown image"}), 404
    return send_from_directory(GAME_ART_IMAGE_DIR, filename)


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
