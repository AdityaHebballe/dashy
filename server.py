import os
import re
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from collections import OrderedDict
from urllib.parse import quote

import psutil
import requests
from flask import Flask, jsonify

app = Flask(__name__)

# --- CONFIGURATION ---
CIDER_API_URL = "http://127.0.0.1:10767/api/v1/playback"
CIDER_HEADERS = {"apitoken": ""}  # Add your token here if you didn't disable auth
LRCLIB_BASE_URL = "https://lrclib.net/api"
MUSIXMATCH_BASE = "https://apic-desktop.musixmatch.com/ws/1.1/"

# --- CACHE ---
current_track_id = None
cached_lyrics_payload = {"text": None, "is_synced": False, "source": None}
musixmatch_token = None
lyrics_lock = threading.RLock()
http_session = requests.Session()
lyrics_result_cache = OrderedDict()
cider_request_pool = ThreadPoolExecutor(max_workers=4)

LYRICS_CACHE_MAX_BYTES = int(os.getenv("LYRICS_CACHE_MAX_BYTES", str(32 * 1024 * 1024)))
LYRICS_CACHE_MAX_ENTRIES = int(os.getenv("LYRICS_CACHE_MAX_ENTRIES", "512"))
LYRICS_CACHE_TTL_SECONDS = int(os.getenv("LYRICS_CACHE_TTL_SECONDS", str(7 * 24 * 60 * 60)))
lyrics_cache_bytes = 0

# Init non-blocking CPU check
psutil.cpu_percent(interval=None)

try:
    _io = psutil.disk_io_counters()
    last_disk_bytes = (_io.read_bytes + _io.write_bytes) if _io else 0
except Exception:
    last_disk_bytes = 0
last_disk_time = time.time()


def estimate_lyrics_payload_size(track_id, payload):
    text = payload.get("text") or ""
    source = payload.get("source") or ""
    return len(track_id.encode("utf-8")) + len(text.encode("utf-8")) + len(source.encode("utf-8")) + 64


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
            return None

        lyrics_result_cache.move_to_end(track_id)
        return dict(cached["payload"])


def store_cached_lyrics(track_id, payload):
    global lyrics_cache_bytes

    payload_copy = {
        "text": payload.get("text"),
        "is_synced": bool(payload.get("is_synced", False)),
        "source": payload.get("source"),
    }
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
    return http_session.get(f"{CIDER_API_URL}/{endpoint}", headers=CIDER_HEADERS, timeout=0.8)


def cider_post(endpoint):
    return http_session.post(f"{CIDER_API_URL}/{endpoint}", headers=CIDER_HEADERS, timeout=0.8)


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
    return {
        "mode": "stats",
        "cpu": psutil.cpu_percent(interval=None),
        "ram": psutil.virtual_memory().percent,
        "gpu": get_amd_gpu_stats(),
        "cpu_temp": get_cpu_temp(),
        "gpu_temp": get_gpu_temp(),
        "disk": get_main_disk_usage(),
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
            "mode": "music" if is_playing else "stats",
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


@app.after_request
def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    return response


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
