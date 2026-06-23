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

app = Flask(__name__, static_folder='static')
CORS(app)

# ─────────────────────────────────────────────────────────────
# yt-dlp helper — fetch audio URL + metadata
# ─────────────────────────────────────────────────────────────
def fetch_song_data(query: str) -> dict:
    """
    Run yt-dlp with --dump-json to get the direct audio URL
    and all metadata. Returns dict with 'audio_url', 'info', 'error'.
    NOTE: Do NOT use --flat-playlist — it skips format resolution.
    """
    search_query = f"ytsearch1:{query}"

    cmd = [
        sys.executable, "-m", "yt_dlp",
        "--no-playlist",
        "--dump-json",          # full metadata including formats + stream URL
        "-f", "bestaudio/best", # best audio, fallback to best
        "--no-warnings",
        "--socket-timeout", "15",
        search_query
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60
        )
    except subprocess.TimeoutExpired:
        return {"audio_url": None, "info": None, "error": "Search timed out. Try again."}
    except Exception as e:
        return {"audio_url": None, "info": None, "error": str(e)}

    if result.returncode != 0 or not result.stdout.strip():
        err = result.stderr.strip() or "Could not find the song."
        # Strip ANSI escape codes for cleaner error messages
        import re
        err = re.sub(r'\x1b\[[0-9;]*m', '', err)
        return {"audio_url": None, "info": None, "error": err[:200]}

    try:
        # yt-dlp may output multiple JSON lines; take the first complete one
        lines = [l.strip() for l in result.stdout.strip().splitlines() if l.strip()]
        data = None
        for line in lines:
            try:
                data = json.loads(line)
                break
            except json.JSONDecodeError:
                continue
        if data is None:
            raise ValueError("No valid JSON found in yt-dlp output")
    except Exception as e:
        return {"audio_url": None, "info": None, "error": f"Parse error: {str(e)}"}

    # Extract the direct playable URL
    audio_url = None

    # 1) Top-level url field (set when -f selects a single format)
    audio_url = data.get("url")

    # 2) From requested_formats list
    if not audio_url and data.get("requested_formats"):
        audio_url = data["requested_formats"][0].get("url")

    # 3) From formats list — pick best audio
    if not audio_url and data.get("formats"):
        formats = data["formats"]
        # Prefer audio-only formats
        audio_formats = [
            f for f in formats
            if f.get("acodec") not in (None, "none")
            and f.get("vcodec") in (None, "none", "")
        ]
        if not audio_formats:
            audio_formats = formats  # fallback: any format

        # Sort by audio quality (abr)
        audio_formats.sort(key=lambda f: f.get("abr") or 0, reverse=True)
        audio_url = audio_formats[0].get("url")

    if not audio_url:
        return {"audio_url": None, "info": None, "error": "No playable audio URL found in response."}

    # Build info dict
    info = {
        "title":        data.get("title", "Unknown Track"),
        "artist":       data.get("uploader", data.get("channel", "Unknown Artist")),
        "duration":     data.get("duration", 0),
        "thumbnail":    data.get("thumbnail", ""),
        "view_count":   data.get("view_count", 0),
        "webpage_url":  data.get("webpage_url", ""),
        "album":        data.get("album", ""),
        # HTTP headers yt-dlp recommends for this URL (e.g. cookies, referer)
        "http_headers": data.get("http_headers", {}),
    }

    return {"audio_url": audio_url, "info": info, "error": None}


# ─────────────────────────────────────────────────────────────
# Stream cache: query -> {url, http_headers, expires_at}
# URLs expire after 45 minutes (YouTube signed URLs are ~6h but
# to be safe we evict after 45 min so we always re-fetch fresh ones)
# ─────────────────────────────────────────────────────────────
_stream_cache: dict[str, dict] = {}
CACHE_TTL = 45 * 60  # 45 minutes in seconds


def cache_get(key: str):
    entry = _stream_cache.get(key)
    if not entry:
        return None
    if time.time() > entry.get("expires_at", 0):
        del _stream_cache[key]
        return None
    return entry


def cache_set(key: str, url: str, headers: dict):
    _stream_cache[key] = {
        "url": url,
        "http_headers": headers,
        "expires_at": time.time() + CACHE_TTL,
    }


# ─────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────
@app.route('/')
def index():
    return send_from_directory('static', 'index.html')


@app.route('/api/search', methods=['POST'])
def search():
    """Resolve a song query and return metadata + a proxy stream path."""
    body = request.get_json(silent=True) or {}
    query = body.get('query', '').strip()

    if not query:
        return jsonify({"error": "No query provided"}), 400

    # Check cache first
    cache_key = query.lower()
    cached = cache_get(cache_key)
    if cached:
        # Return cached stream path without re-calling yt-dlp
        stream_path = "/api/stream?q=" + urllib.parse.quote(cache_key)
        # We don't have info in cache — still return stream path
        return jsonify({
            "stream_url": stream_path,
            "info": {
                "title":    cache_key,
                "artist":   "",
                "duration": 0,
                "thumbnail":"",
            },
            "cached": True
        })

    result = fetch_song_data(query)

    if result["error"]:
        return jsonify({"error": result["error"]}), 500

    # Store in cache
    http_headers = result["info"].pop("http_headers", {})
    cache_set(cache_key, result["audio_url"], http_headers)

    stream_path = "/api/stream?q=" + urllib.parse.quote(cache_key)

    return jsonify({
        "stream_url": stream_path,
        "info":       result["info"],
    })


@app.route('/api/stream')
def stream_audio():
    """
    Proxy endpoint — fetches the YouTube audio server-side and streams
    bytes to the browser, bypassing CORS. Supports Range requests for seeking.
    Auto-refreshes the stream URL if the cache entry is missing.
    """
    query = request.args.get('q', '').strip()
    if not query:
        return jsonify({"error": "Missing query parameter"}), 400

    cached = cache_get(query)

    # If not in cache (expired or never fetched), resolve fresh
    if not cached:
        result = fetch_song_data(query)
        if result["error"] or not result["audio_url"]:
            return jsonify({"error": "Could not resolve stream. Try searching again."}), 404
        http_headers = result["info"].pop("http_headers", {})
        cache_set(query, result["audio_url"], http_headers)
        cached = cache_get(query)

    yt_url = cached["url"]
    extra_headers = cached.get("http_headers", {})

    # Build proxy request headers
    req_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
        "Referer":  "https://www.youtube.com/",
        "Origin":   "https://www.youtube.com",
    }
    req_headers.update(extra_headers)

    # Forward Range header so browser seeking works
    if "Range" in request.headers:
        req_headers["Range"] = request.headers["Range"]

    try:
        yt_req  = urllib.request.Request(yt_url, headers=req_headers)
        yt_resp = urllib.request.urlopen(yt_req, timeout=20)
    except urllib.error.HTTPError as e:
        if e.code in (403, 410):
            # URL expired — evict cache and tell client to retry
            _stream_cache.pop(query, None)
            return jsonify({"error": "Stream URL expired. Please search again.", "expired": True}), 403
        return jsonify({"error": f"Upstream HTTP {e.code}: {e.reason}"}), 502
    except Exception as e:
        return jsonify({"error": str(e)}), 502

    content_type    = yt_resp.headers.get("Content-Type",   "audio/webm")
    content_length  = yt_resp.headers.get("Content-Length", None)
    content_range   = yt_resp.headers.get("Content-Range",  None)
    upstream_status = yt_resp.status

    def generate():
        try:
            while True:
                chunk = yt_resp.read(64 * 1024)  # 64 KB chunks
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
        "service": "Medicine -- Music is the Best Medicine",
        "cache_entries": len(_stream_cache),
    })


if __name__ == '__main__':
    os.makedirs('static', exist_ok=True)
    print("============================================")
    print("  Medicine -- Music is the Best Medicine   ")
    print("  http://localhost:5000                    ")
    print("============================================")
    app.run(debug=True, host='0.0.0.0', port=5000, threaded=True)
