from flask import Flask, request, jsonify, send_from_directory, Response, stream_with_context
from flask_cors import CORS
import subprocess
import json
import os
import sys
import urllib.request
import urllib.error
import urllib.parse
import time
import random
import re

app = Flask(__name__, static_folder='static')
CORS(app)

# ─────────────────────────────────────────────────────────────
# Browser User-Agent pool — rotated on every yt-dlp call.
# Mix of desktop Chrome, Firefox, Safari, Edge, and mobile.
# ─────────────────────────────────────────────────────────────
_UA_POOL = [
    # Chrome Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    # Chrome Mac
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    # Firefox Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    # Firefox Mac
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.4; rv:126.0) Gecko/20100101 Firefox/126.0",
    # Safari Mac
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
    # Edge Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0",
    # Smart TV (works great with tv_embedded client)
    "Mozilla/5.0 (SMART-TV; Linux; Tizen 5.0) AppleWebKit/538.1 (KHTML, like Gecko) Version/5.0 TV Safari/538.1",
    "Mozilla/5.0 (SMART-TV; Linux; Tizen 6.0) AppleWebKit/538.1 (KHTML, like Gecko) Version/6.0 TV Safari/538.1",
]

# yt-dlp extractor client strategies — tried in order, first success wins.
# tv_embedded is a real YouTube client that's hard to block and returns audio fine.
_PLAYER_CLIENTS = [
    "tv_embedded",
    "mweb",
    None,  # None = use yt-dlp default (ANDROID_VR), as last resort
]

_ACCEPT_LANGS = [
    "en-US,en;q=0.9",
    "en-GB,en;q=0.9",
    "en-US,en;q=0.9,hi;q=0.8",
    "en-IN,en;q=0.9",
]


def _rand_ua() -> str:
    return random.choice(_UA_POOL)


def _build_cmd(query: str, flat: bool = False, client: str = None) -> list:
    """
    Build the yt-dlp command list with anti-detection settings.
    All anti-bot work is done through yt-dlp's own supported flags only.
    """
    ua = _rand_ua()

    cmd = [
        sys.executable, "-m", "yt_dlp",
        "--user-agent", ua,
        "--no-warnings",
        "--socket-timeout", "15",
        "--retries", "3",
        "--no-playlist",
    ]

    # Use a specific YouTube player client if requested
    if client:
        cmd += ["--extractor-args", f"youtube:player_client={client}"]

    if flat:
        cmd += ["--dump-json", "--flat-playlist"]
    else:
        cmd += ["--dump-json", "-f", "bestaudio/best"]

    cmd.append(query)
    return cmd, ua


# ─────────────────────────────────────────────────────────────
# yt-dlp helper — fetch audio URL + metadata
# Tries multiple player clients automatically on failure
# ─────────────────────────────────────────────────────────────
def fetch_song_data(query: str) -> dict:
    """
    Run yt-dlp to get the direct audio URL and metadata.
    Tries tv_embedded → mweb → default, returns first success.
    """
    # Direct YouTube URL → use it as-is, no ytsearch prefix
    if query.startswith("http://") or query.startswith("https://"):
        search_query = query
    else:
        search_query = f"ytsearch1:{query}"

    last_error = "Unknown error"

    for client in _PLAYER_CLIENTS:
        cmd, ua = _build_cmd(search_query, flat=False, client=client)

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=60
            )
        except subprocess.TimeoutExpired:
            last_error = "Search timed out."
            continue
        except Exception as e:
            last_error = str(e)
            continue

        if result.returncode != 0 or not result.stdout.strip():
            err = result.stderr.strip() or "Could not find the song."
            err = re.sub(r'\x1b\[[0-9;]*m', '', err)
            last_error = err[:300]
            # If this client failed, try the next one
            continue

        # Parse JSON output
        try:
            lines = [l.strip() for l in result.stdout.strip().splitlines() if l.strip()]
            data = None
            for line in lines:
                try:
                    data = json.loads(line)
                    break
                except json.JSONDecodeError:
                    continue
            if data is None:
                last_error = "No valid JSON in yt-dlp output."
                continue
        except Exception as e:
            last_error = f"Parse error: {e}"
            continue

        # Extract audio URL
        audio_url = (
            data.get("url")
            or (data.get("requested_formats") or [{}])[0].get("url")
        )

        if not audio_url and data.get("formats"):
            formats = data["formats"]
            audio_only = [
                f for f in formats
                if f.get("acodec") not in (None, "none")
                and f.get("vcodec") in (None, "none", "")
            ]
            best = sorted(audio_only or formats, key=lambda f: f.get("abr") or 0, reverse=True)
            audio_url = best[0].get("url") if best else None

        if not audio_url:
            last_error = "No playable audio URL found."
            continue

        # Get the actual http_headers yt-dlp recommends for THIS url
        http_headers = data.get("http_headers", {})
        # Make sure User-Agent in http_headers matches what we used
        if "User-Agent" not in http_headers:
            http_headers["User-Agent"] = ua

        info = {
            "title":        data.get("title", "Unknown Track"),
            "artist":       data.get("uploader", data.get("channel", "Unknown Artist")),
            "duration":     data.get("duration", 0),
            "thumbnail":    data.get("thumbnail", ""),
            "view_count":   data.get("view_count", 0),
            "webpage_url":  data.get("webpage_url", ""),
            "album":        data.get("album", ""),
            "http_headers": http_headers,
        }

        return {"audio_url": audio_url, "info": info, "error": None}

    # All clients failed
    return {"audio_url": None, "info": None, "error": last_error}


# ─────────────────────────────────────────────────────────────
# yt-dlp helper — fast search list (no stream URL, just metadata)
# ─────────────────────────────────────────────────────────────
def fetch_search_results(query: str, count: int = 10) -> list:
    """
    Run yt-dlp --flat-playlist to get a list of results quickly.
    Tries tv_embedded → mweb → default.
    """
    search_query = f"ytsearch{count}:{query}"

    for client in _PLAYER_CLIENTS:
        cmd, _ = _build_cmd(search_query, flat=True, client=client)
        # flat search doesn't need --no-playlist
        cmd = [x for x in cmd if x != "--no-playlist"]

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=30
            )
        except Exception as e:
            print(f"[search_list] error ({client}): {e}")
            continue

        if not result.stdout.strip():
            continue

        results = []
        for line in result.stdout.strip().splitlines():
            if not line.strip():
                continue
            try:
                data = json.loads(line)
                vid = data.get("id")
                if not vid:
                    continue
                thumb = data.get("thumbnail") or f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
                results.append({
                    "id":       vid,
                    "title":    data.get("title", "Unknown Track"),
                    "artist":   data.get("uploader", data.get("channel", "Unknown Artist")),
                    "duration": data.get("duration", 0),
                    "thumbnail": thumb,
                })
            except json.JSONDecodeError:
                continue

        if results:
            return results  # got results, done

    return []


# ─────────────────────────────────────────────────────────────
# Stream cache: query -> {url, http_headers, title, ...}
# ─────────────────────────────────────────────────────────────
_stream_cache: dict = {}
CACHE_TTL = 45 * 60  # 45 minutes


def cache_get(key: str):
    entry = _stream_cache.get(key)
    if not entry:
        return None
    if time.time() > entry.get("expires_at", 0):
        del _stream_cache[key]
        return None
    return entry


def cache_set(key: str, audio_url: str, http_headers: dict, info: dict):
    _stream_cache[key] = {
        "url":          audio_url,
        "http_headers": http_headers,
        "expires_at":   time.time() + CACHE_TTL,
        # store info so cached responses still return rich metadata
        "title":     info.get("title", ""),
        "artist":    info.get("artist", ""),
        "duration":  info.get("duration", 0),
        "thumbnail": info.get("thumbnail", ""),
    }


# ─────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────
@app.route('/')
def index():
    return send_from_directory('static', 'index.html')


@app.route('/api/search_list', methods=['POST'])
def search_list():
    body = request.get_json(silent=True) or {}
    query = body.get('query', '').strip()
    if not query:
        return jsonify({"error": "No query provided"}), 400

    results = fetch_search_results(query, count=10)
    return jsonify({"results": results})


@app.route('/api/search', methods=['POST'])
def search():
    body = request.get_json(silent=True) or {}
    query = body.get('query', '').strip()
    if not query:
        return jsonify({"error": "No query provided"}), 400

    # Serve from cache if fresh
    cached = cache_get(query)
    if cached:
        stream_path = "/api/stream?q=" + urllib.parse.quote(query)
        return jsonify({
            "stream_url": stream_path,
            "info": {
                "title":     cached.get("title", query),
                "artist":    cached.get("artist", ""),
                "duration":  cached.get("duration", 0),
                "thumbnail": cached.get("thumbnail", ""),
            },
            "cached": True
        })

    result = fetch_song_data(query)
    if result["error"]:
        return jsonify({"error": result["error"]}), 500

    http_headers = result["info"].pop("http_headers", {})
    cache_set(query, result["audio_url"], http_headers, result["info"])

    stream_path = "/api/stream?q=" + urllib.parse.quote(query)
    return jsonify({
        "stream_url": stream_path,
        "info":       result["info"],
    })


@app.route('/api/stream')
def stream_audio():
    """
    Proxy the YouTube audio stream to the browser.
    Supports Range requests for seeking. Auto-refreshes expired URLs.
    """
    query = request.args.get('q', '').strip()
    if not query:
        return jsonify({"error": "Missing query parameter"}), 400

    cached = cache_get(query)
    if not cached:
        result = fetch_song_data(query)
        if result["error"] or not result["audio_url"]:
            return jsonify({"error": "Could not resolve stream."}), 404
        http_headers = result["info"].pop("http_headers", {})
        cache_set(query, result["audio_url"], http_headers, result["info"])
        cached = cache_get(query)

    yt_url = cached["url"]

    # Use the headers yt-dlp gave us + fill in any missing standard fields
    req_headers = {
        "User-Agent":      cached.get("http_headers", {}).get("User-Agent", _rand_ua()),
        "Accept":          "*/*",
        "Accept-Language": random.choice(_ACCEPT_LANGS),
        "Accept-Encoding": "identity",
        "Referer":         "https://www.youtube.com/",
        "Origin":          "https://www.youtube.com",
        "Sec-Fetch-Mode":  "no-cors",
        "Sec-Fetch-Dest":  "audio",
        "Connection":      "keep-alive",
    }
    # Merge any extra headers yt-dlp provided
    req_headers.update(cached.get("http_headers", {}))

    # Forward browser Range header for seekable audio
    if "Range" in request.headers:
        req_headers["Range"] = request.headers["Range"]

    try:
        yt_req  = urllib.request.Request(yt_url, headers=req_headers)
        yt_resp = urllib.request.urlopen(yt_req, timeout=20)
    except urllib.error.HTTPError as e:
        if e.code in (403, 410):
            _stream_cache.pop(query, None)
            return jsonify({"error": "Stream expired.", "expired": True}), 403
        return jsonify({"error": f"Upstream HTTP {e.code}"}), 502
    except Exception as e:
        return jsonify({"error": str(e)}), 502

    content_type    = yt_resp.headers.get("Content-Type",   "audio/webm")
    content_length  = yt_resp.headers.get("Content-Length", None)
    content_range   = yt_resp.headers.get("Content-Range",  None)
    upstream_status = yt_resp.status

    def generate():
        try:
            while True:
                chunk = yt_resp.read(64 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            yt_resp.close()

    resp_headers = {
        "Content-Type":                content_type,
        "Accept-Ranges":               "bytes",
        "Access-Control-Allow-Origin": "*",
        "Cache-Control":               "no-cache",
    }
    if content_length:
        resp_headers["Content-Length"] = content_length
    if content_range:
        resp_headers["Content-Range"] = content_range

    return Response(
        stream_with_context(generate()),
        status=upstream_status,
        headers=resp_headers,
        direct_passthrough=True,
    )


@app.route('/api/health')
def health():
    return jsonify({
        "status": "operational",
        "service": "Medicine",
        "cache_entries": len(_stream_cache),
    })


if __name__ == '__main__':
    os.makedirs('static', exist_ok=True)
    port = int(os.environ.get('PORT', 5000))
    print("============================================")
    print("  Medicine -- Music is the Best Medicine   ")
    print(f"  http://localhost:{port}                    ")
    print("============================================")
    app.run(debug=True, host='0.0.0.0', port=port, threaded=True)
