# Copyright 2026 Joseph D. Skehan
#
# Licensed under the Apache License, Version 2.0 (the "License");
# see LICENSE in this repository for the full text.
#!/usr/bin/env python3
"""Life HUD: a home TV dashboard (weather, tasks, system, music).
Pure Python stdlib — no pip/node needed.
Pure stdlib, no pip/node needed. Serves on port 8080.
Endpoints: /  /api/stats  /api/weather  /api/tasks  /api/music/index  /api/music/file/<id>
Weather: Open-Meteo, no API key. Set LIFE_HUD_LAT/LIFE_HUD_LON/LIFE_HUD_TZ/LIFE_HUD_CITY
to your location (defaults below are an example city, not a real user).
System stats: /proc/stat, /proc/meminfo, /sys/class/thermal (x86_pkg_temp).
Tasks: loaded from tasks.json next to this file; edit that file to update.
Music: MP3s under MUSIC_DIR (default ./music — point this at your music
folder, a mount, or a symlink). Artist/album/title from ID3 tags, falling back to the
Artist/Album/filename path layout. The browser <audio> element does playback,
so sound comes out of the stick's own audio output (HDMI to the TV).
"""
import json, os, re, time, threading, urllib.request, urllib.error, urllib.parse
import subprocess, hashlib
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("LIFE_HUD_PORT", "8080"))
HOST = os.environ.get("LIFE_HUD_HOST", "127.0.0.1")  # safe default: TV/local only.
# Set LIFE_HUD_HOST=0.0.0.0 only on a trusted home network if you want
# other devices on your LAN to reach it. Never port-forward it to the internet.
MUSIC_DIR = os.environ.get("LIFE_HUD_MUSIC", os.path.join(HERE, "music"))
NAS_CONFIG_FILE = os.path.join(HERE, "nas.json")
CACHE_DIR = os.path.join(HERE, "music-cache")
os.makedirs(CACHE_DIR, exist_ok=True)
LAT = float(os.environ.get("LIFE_HUD_LAT", "39.74"))  # example: Denver, CO — set your own
LON = float(os.environ.get("LIFE_HUD_LON", "-104.99"))
CITY_NAME = os.environ.get("LIFE_HUD_CITY", "Denver, CO")
TIMEZONE = os.environ.get("LIFE_HUD_TZ", "America/Denver")
WEATHER_URL = (
    "https://api.open-meteo.com/v1/forecast?latitude=%s&longitude=%s"
    "&current=temperature_2m,relative_humidity_2m,apparent_temperature,weather_code,wind_speed_10m"
    "&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code"
    "&temperature_unit=fahrenheit&wind_speed_unit=mph&precipitation_unit=inch"
    "&timezone=%s&forecast_days=7" % (LAT, LON, urllib.parse.quote(TIMEZONE, safe=""))
)
WMO = {0: "Clear", 1: "Mostly clear", 2: "Partly cloudy", 3: "Overcast",
       45: "Fog", 48: "Fog", 51: "Light drizzle", 53: "Drizzle", 55: "Drizzle",
       61: "Light rain", 63: "Rain", 65: "Heavy rain", 66: "Freezing rain",
       67: "Freezing rain", 71: "Light snow", 73: "Snow", 75: "Heavy snow",
       77: "Snow grains", 80: "Light showers", 81: "Showers", 82: "Heavy showers",
       85: "Snow showers", 86: "Snow showers", 95: "Thunderstorm",
       96: "Storm + hail", 99: "Storm + hail"}

_lock = threading.Lock()
_prev_cpu = None
_weather_cache = {"at": 0, "data": None}

def read_cpu():
    global _prev_cpu
    with open("/proc/stat") as f:
        parts = f.readline().split()[1:]
    vals = [float(x) for x in parts]
    total = sum(vals); idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
    with _lock:
        if _prev_cpu is None:
            _prev_cpu = (total, idle); return 0.0
        pt, pi = _prev_cpu; _prev_cpu = (total, idle)
    dt, di = total - pt, idle - pi
    return round(100 * (1 - di / dt), 1) if dt > 0 else 0.0

def read_mem():
    info = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, v = line.split(":", 1); info[k.strip()] = int(v.split()[0])
    total = info["MemTotal"]; avail = info.get("MemAvailable", info["MemFree"])
    used = total - avail
    return {"total_gb": round(total / 1048576, 2), "used_gb": round(used / 1048576, 2),
            "pct": round(100 * used / total, 1)}

def read_temp():
    # Prefer the x86 package sensor, fall back to the hottest zone.
    try:
        base = "/sys/class/thermal"
        for zone in sorted(os.listdir(base)):
            if not zone.startswith("thermal_zone"): continue
            try:
                with open(os.path.join(base, zone, "type")) as f: typ = f.read().strip()
                with open(os.path.join(base, zone, "temp")) as f: t = int(f.read().strip()) / 1000.0
                if typ == "x86_pkg_temp": return t
            except OSError: pass
        temps = []
        for zone in sorted(os.listdir(base)):
            if zone.startswith("thermal_zone"):
                try:
                    with open(os.path.join(base, zone, "temp")) as f: temps.append(int(f.read().strip()) / 1000.0)
                except OSError: pass
        return max(temps) if temps else None
    except OSError:
        return None

def get_weather():
    now = time.time()
    with _lock:
        if _weather_cache["data"] and now - _weather_cache["at"] < 900:
            return _weather_cache["data"], False
    try:
        with urllib.request.urlopen(WEATHER_URL, timeout=10) as r:
            data = json.loads(r.read().decode())
        with _lock:
            _weather_cache.update(at=now, data=data)
        return data, False
    except Exception:
        with _lock:
            return _weather_cache["data"], True

def get_tasks():
    try:
        with open(os.path.join(HERE, "tasks.json")) as f: return json.load(f)
    except Exception:
        return []

# ---- Music: MP3 index, album art, lyrics (pure stdlib ID3 parsing) ----
_music_cache = {"at": 0, "tracks": []}

def _synchsafe(b):
    return (b[0] << 21) | (b[1] << 14) | (b[2] << 7) | b[3]

def _decode_text(raw, enc):
    try:
        if enc == 1: return raw.decode("utf-16", errors="replace")
        if enc == 2: return raw.decode("utf-16-be", errors="replace")
        if enc == 3: return raw.decode("utf-8", errors="replace")
        return raw.decode("latin-1", errors="replace")
    except Exception:
        return raw.decode("latin-1", errors="replace")

def _split_null(raw, enc):
    # Returns (before, after) a null terminator for the given encoding.
    if enc in (1, 2):
        for i in range(0, len(raw) - 1, 2):
            if raw[i] == 0 and raw[i + 1] == 0:
                return raw[:i], raw[i + 2:]
        return raw, b""
    idx = raw.find(b"\x00")
    return (raw[:idx], raw[idx + 1:]) if idx >= 0 else (raw, b"")

def parse_mp3(path):
    """ID3v2 tags (title/artist/album/track/art/USLT lyrics), ID3v1 fallback."""
    info = {"title": None, "artist": None, "album": None, "track": None,
            "art": None, "art_mime": None, "lyrics": None}
    try:
        with open(path, "rb") as f:
            head = f.read(10)
            if head[:3] == b"ID3":
                ver = head[3]; flags = head[5]; size = _synchsafe(head[6:10])
                tag = f.read(size)
                pos = 0
                if flags & 0x40:  # extended header: skip it
                    ext = _synchsafe(tag[0:4]) if ver == 4 else int.from_bytes(tag[0:4], "big")
                    pos = ext
                while pos + 10 <= len(tag):
                    fid = tag[pos:pos + 4]
                    if fid == b"\x00\x00\x00\x00" or not fid.isalnum(): break
                    try: fid = fid.decode("latin-1")
                    except Exception: break
                    fsize = _synchsafe(tag[pos + 4:pos + 8]) if ver == 4 else int.from_bytes(tag[pos + 4:pos + 8], "big")
                    body = tag[pos + 10:pos + 10 + fsize]
                    if fid in ("TIT2", "TPE1", "TALB", "TRCK") and body:
                        txt = _decode_text(body[1:], body[0]).strip("\x00 \t\r\n")
                        if fid == "TIT2": info["title"] = txt
                        elif fid == "TPE1": info["artist"] = txt
                        elif fid == "TALB": info["album"] = txt
                        elif fid == "TRCK":
                            try: info["track"] = int(txt.split("/")[0])
                            except Exception: pass
                    elif fid == "APIC" and body and info["art"] is None:
                        enc = body[0]
                        rest = body[1:]
                        mime, rest = _split_null(rest, 0)
                        rest = rest[1:]  # picture type byte
                        _desc, img = _split_null(rest, enc)
                        if img:
                            info["art"] = img
                            info["art_mime"] = mime.decode("latin-1") or "image/jpeg"
                    elif fid == "USLT" and body and info["lyrics"] is None:
                        enc = body[0]
                        rest = body[1 + 3:]  # language
                        _desc, txt = _split_null(rest, enc)
                        info["lyrics"] = _decode_text(txt, enc).strip()
                    pos += 10 + fsize
            else:
                # ID3v1, last 128 bytes
                try:
                    f.seek(-128, 2); tail = f.read(128)
                    if tail[:3] == b"TAG":
                        def s(b): return b.decode("latin-1", errors="replace").strip("\x00 ").strip()
                        info["title"] = s(tail[3:33]) or None
                        info["artist"] = s(tail[33:63]) or None
                        info["album"] = s(tail[63:93]) or None
                        if tail[125] == 0 and tail[126] != 0: info["track"] = tail[126]
                except Exception: pass
    except Exception:
        pass
    # Folder art fallback (cover.jpg / folder.jpg / album.jpg|png)
    if info["art"] is None:
        d = os.path.dirname(path)
        for name in ("cover.jpg", "folder.jpg", "album.jpg", "cover.png", "folder.png"):
            p = os.path.join(d, name)
            if os.path.isfile(p):
                try:
                    with open(p, "rb") as f: info["art"] = f.read()
                    info["art_mime"] = "image/png" if name.endswith(".png") else "image/jpeg"
                except Exception: pass
                break
    return info

def _clean_title(stem):
    import re
    t = re.sub(r"^\d{1,3}\s*[-.]+\s*", "", stem)
    return t.strip() or stem

INDEX_CACHE_FILE = os.path.join(HERE, "music-index.json")

_scan_lock = threading.Lock()

def get_music_index(force=False):
    now = time.time()
    with _lock:
        if not force and _music_cache["tracks"] and now - _music_cache["at"] < 300:
            return _music_cache["tracks"]
    # A full scan takes minutes over AFP; don't let concurrent scan
    # requests pile up and DoS the HUD/NAS — return the cached index
    # instead of starting a second scan.
    if force:
        if not _scan_lock.acquire(blocking=False):
            with _lock:
                if _music_cache["tracks"]:
                    return _music_cache["tracks"]
            try:
                with open(INDEX_CACHE_FILE) as f:
                    return json.load(f)
            except Exception:
                return []
    if not force:
        # Persisted index: a full AFP scan of 2,455 files takes ~2m45s, so
        # do not rescan on every page load — only on Rescan.
        # Validate: a disk cache without a file path per track is useless
        # (the public index strips paths) — discard it and rescan instead
        # of serving titles that can never play.
        try:
            with open(INDEX_CACHE_FILE) as f:
                disk = json.load(f)
            if disk and all((t.get("path") or t.get("remote_path")) for t in disk):
                with _lock: _music_cache.update(at=now, tracks=disk)
                return disk
        except Exception:
            pass
    tracks = []
    if os.path.isdir(MUSIC_DIR):
        files = []
        for root, _dirs, names in os.walk(MUSIC_DIR):
            for n in names:
                if n.lower().endswith(".mp3"): files.append(os.path.join(root, n))
        files.sort(key=lambda p: p.lower())
        for p in files:
            rel = os.path.relpath(p, MUSIC_DIR)
            parts = rel.split(os.sep)
            info = parse_mp3(p)
            artist = info["artist"] or (parts[-3] if len(parts) >= 3 else "Unknown artist")
            album = info["album"] or (parts[-2] if len(parts) >= 2 else "Unknown album")
            title = info["title"] or _clean_title(os.path.splitext(os.path.basename(p))[0])
            # Flat "Artist - Title.mp3" libraries: split the
            # filename when the tags do not already give artist/title.
            stem = _clean_title(os.path.splitext(os.path.basename(p))[0])
            if " - " in stem:
                fa, ft = stem.split(" - ", 1)
                if not info["artist"]: artist = fa.strip()
                if not info["title"]: title = ft.strip()
            tracks.append({"path": p, "source": "local", "artist": artist, "album": album,
                           "title": title, "track": info["track"],
                           "has_art": info["art"] is not None, "has_lyrics": bool(info["lyrics"])})
    cfg = load_nas_config()
    if cfg.get("share") and cfg.get("music_folder"):
        nas_files, _err, _rc = smb_list_mp3s(cfg, cfg["music_folder"])
        base = cfg["music_folder"].strip("/")
        for rel in sorted(nas_files, key=lambda s: s.lower()):
            sub = rel[len(base):].strip("/") if rel.startswith(base) else rel
            parts = sub.split("/")
            artist = parts[-3] if len(parts) >= 3 else "Unknown artist"
            album = parts[-2] if len(parts) >= 2 else "Unknown album"
            title = _clean_title(os.path.splitext(parts[-1])[0])
            tracks.append({"remote_path": rel, "source": "nas", "artist": artist, "album": album,
                           "title": title, "track": None, "has_art": False, "has_lyrics": False})
    for i, t in enumerate(tracks): t["id"] = i
    # Never overwrite a good index with an empty one (e.g. the AFP mount
    # dropped mid-scan) — that leaves the player listing nothing at all.
    if tracks:
        try:
            with open(INDEX_CACHE_FILE, "w") as f: json.dump(tracks, f)
        except Exception:
            pass
    with _lock:
        _music_cache.update(at=now, tracks=tracks)
    if force:
        try: _scan_lock.release()
        except Exception: pass
    return tracks

def music_track_by_id(tid):
    for t in get_music_index():
        if t["id"] == tid: return t
    return None

def music_file_path(t):
    if not t: return None
    if t.get("source") == "nas": return nas_download(t)
    p = t.get("path")
    if not p: return None
    # A .mp3 symlink inside the music folder pointing elsewhere (e.g. a
    # private key renamed to .mp3) must not be served or embedded into.
    try:
        base = os.path.realpath(MUSIC_DIR)
        if os.path.commonpath([os.path.realpath(p), base]) != base:
            return None
    except Exception:
        return None
    return p

def music_art(tid):
    t = music_track_by_id(tid)
    if not t: return None, None
    art, mime, _lyr, _syn = _enrich(t)
    return art, mime

def music_lyrics(tid):
    t = music_track_by_id(tid)
    if not t: return None, None
    _art, _mime, lyrics, synced = _enrich(t)
    return lyrics, synced

# ---- NAS explorer (SMB via smbclient, user-level, no sudo/mount needed) ----
# Config lives only on the stick, in nas.json with owner-only (0600) permissions.
# It is never sent to Muse/chat. The player streams NAS tracks by downloading
# each requested file into music-cache/ and serving that local copy.
def load_nas_config():
    try:
        with open(NAS_CONFIG_FILE) as f: return json.load(f)
    except Exception:
        return {}

def save_nas_config(cfg):
    try:
        with open(NAS_CONFIG_FILE, "w") as f: json.dump(cfg, f)
        os.chmod(NAS_CONFIG_FILE, 0o600)
        _ensure_smb_authfile(cfg)
        return True
    except Exception:
        return False

SMB_AUTH_FILE = os.path.join(HERE, ".smb-auth")

def _valid_smb_host_share(host, share):
    # Host/share come from a POST body, so they must not contain path
    # separators, spaces or shell/smbclient metacharacters — otherwise an
    # unauthenticated LAN caller could point smbclient at an arbitrary
    # server (and hand it the NAS credentials) or break out of the
    # //host/share URL.
    return bool(re.match(r"^[A-Za-z0-9._-]+$", host or "")) and \
        bool(re.match(r"^[A-Za-z0-9._$-]+$", share or ""))

def _ensure_smb_authfile(cfg):
    """Write the smbclient authfile (0600) so the password never appears
    on a command line (visible in ps / /proc to any local process)."""
    user = cfg.get("username", "")
    if not user:
        return None
    try:
        with open(SMB_AUTH_FILE, "w") as f:
            f.write("username = %s\npassword = %s\n" % (user, cfg.get("password", "")))
        os.chmod(SMB_AUTH_FILE, 0o600)
        return SMB_AUTH_FILE
    except Exception:
        return None

def _smb_args(cfg):
    auth = _ensure_smb_authfile(cfg) if cfg.get("username") else None
    if auth:
        return ["-A", auth]
    return ["-N"]

def smb_run(cfg, cmd, timeout=30):
    """Run smbclient, return (stdout, stderr, returncode). No shell, and
    the password is passed via an authfile, never as a command-line
    argument (where ps could read it)."""
    share = cfg.get("share", "")
    host = cfg.get("host", "")
    if not _valid_smb_host_share(host, share):
        return "", "Invalid host or share name", 1
    try:
        r = subprocess.run(["smbclient", "//%s/%s" % (host, share)] + _smb_args(cfg) + ["-c", cmd],
                           capture_output=True, text=True, timeout=timeout)
        return r.stdout, r.stderr, r.returncode
    except Exception as e:
        return "", str(e), 1

def smb_list_shares(cfg):
    host = cfg.get("host", "")
    if not re.match(r"^[A-Za-z0-9._-]+$", host or ""):
        return [], "Invalid host name"
    try:
        r = subprocess.run(["smbclient", "-L", host] + _smb_args(cfg),
                           capture_output=True, text=True, timeout=30)
        shares = []
        for line in (r.stdout + r.stderr).splitlines():
            m = re.match(r"^\s+(\S+)\s+Disk\s*$", line)
            if m and m.group(1) not in ("IPC$",): shares.append(m.group(1))
        return shares, (r.stdout + r.stderr)
    except Exception as e:
        return [], str(e)

def _smb_path(p):
    # Explorer paths use / ; smbclient uses \ . Root is "\".
    # Reject smbclient command-language metacharacters: a path containing
    # " or ; or a newline could break out of the quoted cd and run extra
    # smbclient commands (get/put/del) against the share.
    p = (p or "/").strip()
    if any(c in p for c in ('"', ';', '\n', '\r')):
        raise ValueError("Invalid character in NAS path")
    if p in ("", "/"): return "\\"
    return "\\" + p.strip("/").replace("/", "\\")

def smb_browse(cfg, path):
    try:
        cmd = 'cd "%s"; ls' % _smb_path(path)
    except ValueError as e:
        return [], str(e), 1
    out, err, rc = smb_run(cfg, cmd)
    entries = []
    for line in out.splitlines():
        m = re.match(r"^\s+(.+?)\s{2,}([DA])\s+(\d+)\s+", line)
        if not m: continue
        name = m.group(1).strip()
        if name in (".", ".."): continue
        entries.append({"name": name, "type": "dir" if m.group(2) == "D" else "file",
                        "size": int(m.group(3))})
    return entries, err, rc

def smb_list_mp3s(cfg, folder, _depth=0):
    # Recursively browse: one smbclient ls per directory, so full paths
    # are known exactly (recurse+ls output does not carry full paths).
    files = []
    if _depth > 6: return files, "", 0
    entries, err, rc = smb_browse(cfg, folder)
    if rc != 0: return files, err, rc
    for e in entries:
        rel = (folder.strip("/") + "/" + e["name"]) if folder.strip("/") else e["name"]
        if e["type"] == "dir":
            sub, _, _ = smb_list_mp3s(cfg, rel, _depth + 1)
            files.extend(sub)
        elif e["name"].lower().endswith(".mp3"):
            files.append(rel)
        if len(files) > 5000: break
    return files, err, rc

def nas_cache_path(remote_rel):
    h = hashlib.sha1(remote_rel.encode()).hexdigest()[:16]
    return os.path.join(CACHE_DIR, "nas-%s.mp3" % h)

def nas_download(track):
    cfg = load_nas_config()
    if not cfg: return None
    dest = nas_cache_path(track.get("remote_path", ""))
    if os.path.isfile(dest) and os.path.getsize(dest) > 0: return dest
    # Keep the cache under ~2 GB: drop oldest cached NAS files first.
    try:
        cached = sorted([os.path.join(CACHE_DIR, f) for f in os.listdir(CACHE_DIR) if f.startswith("nas-")],
                        key=lambda p: os.path.getmtime(p))
        total = sum(os.path.getsize(p) for p in cached)
        while cached and total > 2 * 1024 * 1024 * 1024:
            p = cached.pop(0); total -= os.path.getsize(p); os.remove(p)
    except Exception: pass
    try:
        remote = _smb_path(track.get("remote_path", ""))
    except ValueError:
        return None
    out, err, rc = smb_run(cfg, 'get "%s" "%s"' % (remote, dest), timeout=300)
    if os.path.isfile(dest) and os.path.getsize(dest) > 0: return dest
    try: os.remove(dest)
    except Exception: pass
    return None

# ---- Missing album art / lyrics: download, cache, and (local files) embed ----
def _http_get(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": "LifeHUD/1.0 (personal use)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()

def _norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())

def _fetch_art(url):
    try:
        url = url.replace("100x100bb", "600x600bb")
        img = _http_get(url)
        if img and len(img) > 1000:
            mime = "image/png" if img[:4] == b"\x89PNG" else "image/jpeg"
            return img, mime
    except Exception:
        pass
    return None, None

def _itunes_search(term, entity, limit=10):
    try:
        q = urllib.parse.urlencode({"term": term, "entity": entity, "limit": limit})
        return json.loads(_http_get("https://itunes.apple.com/search?" + q)).get("results", [])
    except Exception:
        return []

def _album_match(collection, album):
    """True if an iTunes collection name matches a file's album tag in
    either direction. The file tag often carries the artist's name too
    ('Stevie Wonder's Original Musiquarium' vs iTunes 'Original
    Musiquarium'), so checking only album-in-collection misses it —
    that miss is what let a Paul McCartney compilation cover through
    for a Stevie Wonder track."""
    c, a = _norm(collection), _norm(album)
    if not c or not a:
        return False
    return c == a or a in c or (len(c) >= 4 and c in a)

def _pick_art(results, artist, album, title, kind):
    """Best artwork URL from iTunes results, or None. Requires an artist
    match plus an album (kind=album) or track (kind=song) match, so a
    keyword coincidence can't return the wrong artist's cover. A song
    from the file's own album outranks the same song on a compilation."""
    best = None; best_score = 0
    for res in results:
        if _norm(artist) not in _norm(res.get("artistName")):
            continue
        score = 1
        if kind == "album":
            if not _album_match(res.get("collectionName"), album): continue
            score += 2
        else:
            if _norm(res.get("trackName")) == _norm(title): score += 2
            elif title and _norm(title) in _norm(res.get("trackName")): score += 1
            else: continue
            if _album_match(res.get("collectionName"), album): score += 2
        if score > best_score: best_score = score; best = res
    return best.get("artworkUrl100") if best else None

def _musicbrainz_art(artist, title):
    """Fallback when iTunes lacks the track (e.g. 311 'All Mixed Up'):
    MusicBrainz recording -> release group -> Cover Art Archive front."""
    try:
        q = urllib.parse.urlencode({"query": 'artist:"%s" AND recording:"%s"' % (artist, title), "fmt": "json"})
        data = json.loads(_http_get("https://musicbrainz.org/ws/2/recording/?" + q))
        seen = set()
        for rec in data.get("recordings", [])[:5]:
            for rel in rec.get("releases", [])[:3]:
                rg = (rel.get("release-group") or {}).get("id")
                if not rg or rg in seen: continue
                seen.add(rg)
                img, mime = _fetch_art("https://coverartarchive.org/release-group/%s/front" % rg)
                if img: return img, mime
    except Exception:
        pass
    return None, None

def download_art(artist, album, title):
    # iTunes Search (free, no key), then MusicBrainz/Cover Art Archive.
    # The old code only searched artist+album, so it failed whenever the
    # album tag was truncated ("Retrospective, Vol. 2 (1981-19"), unknown,
    # or wrong — even though the song itself is in iTunes. Searching by
    # artist+track finds that song's album art, and the artist-only
    # searches filter locally instead of trusting iTunes' top-10 ranking.
    album_known = bool(album and _norm(album) not in ("unknownalbum", "unknown"))
    bare = re.sub(r"\s*[\(\[].*?[\)\]]\s*$", "", title or "").strip()
    queries = []
    if album_known: queries.append((artist + " " + album, "album"))
    if title: queries.append((artist + " " + title, "song"))
    if bare and bare != title: queries.append((artist + " " + bare, "song"))
    if album_known: queries.append((artist, "album"))
    if title: queries.append((artist, "song"))
    for term, entity in queries:
        results = _itunes_search(term, entity, limit=50 if term == artist else 10)
        url = _pick_art(results, artist, album, title, entity)
        if url:
            img, mime = _fetch_art(url)
            if img: return img, mime
    if title:
        return _musicbrainz_art(artist, title)
    return None, None

def _lrclib_json(url):
    # LRCLIB is free but flaky: it answers 503 "ServerOverloaded" fairly
    # often, so retry those (and network blips) instead of failing once.
    # A 404 is definitive for that exact query — return None so the caller
    # can try a looser query.
    for attempt in range(3):
        try:
            return json.loads(_http_get(url))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if e.code in (503, 429) and attempt < 2:
                time.sleep(1 + attempt); continue
            return None
        except Exception:
            if attempt < 2:
                time.sleep(1 + attempt); continue
            return None
    return None

def _lyrics_entry(entry):
    """Return (plain_text, synced_lines) for an LRCLIB entry.
    synced_lines is [{t: seconds, text: line}, ...] or None."""
    if not isinstance(entry, dict):
        return None, None
    synced = None
    if entry.get("syncedLyrics"):
        synced = []
        for ln in entry["syncedLyrics"].splitlines():
            m = re.match(r"^\[(\d+):(\d+(?:\.\d+)?)\]\s*(.*)$", ln)
            if m:
                synced.append({"t": int(m.group(1)) * 60 + float(m.group(2)),
                               "text": m.group(3)})
        if not synced:
            synced = None
    txt = entry.get("plainLyrics") or ""
    if not txt and synced:
        txt = "\n".join(s["text"] for s in synced)
    txt = txt.strip()
    return (txt or None), synced

def download_lyrics(artist, title, album):
    # LRCLIB (free, no key) — returns embedded-style plain lyrics if known.
    # /api/get is an exact match, so it 404s whenever the file's album tag
    # is wrong ("Unknown album" on 98 tracks, remixes, reissues, etc.).
    # Fall back, in order: exact get with album, get without album,
    # then /api/search, picking the closest title/artist match with lyrics.
    base = "https://lrclib.net/api/get?"
    if album and _norm(album) not in ("unknownalbum", "unknown"):
        txt, synced = _lyrics_entry(_lrclib_json(base + urllib.parse.urlencode(
            {"artist_name": artist, "track_name": title, "album_name": album})))
        if txt: return txt, synced
    txt, synced = _lyrics_entry(_lrclib_json(base + urllib.parse.urlencode(
        {"artist_name": artist, "track_name": title})))
    if txt: return txt, synced
    results = _lrclib_json("https://lrclib.net/api/search?" + urllib.parse.urlencode(
        {"artist_name": artist, "track_name": title}))
    if isinstance(results, list):
        def score(e):
            s = 0
            if _norm(e.get("trackName")) == _norm(title): s += 2
            elif _norm(title) in _norm(e.get("trackName")): s += 1
            if _norm(artist) in _norm(e.get("artistName")): s += 1
            return s
        for entry in sorted(results, key=score, reverse=True):
            txt, synced = _lyrics_entry(entry)
            if txt: return txt, synced
    # Last resort: remix/mix titles ("Take On Me (Techno Mix)") rarely
    # match verbatim — search the bare title instead.
    bare = re.sub(r"\s*[\(\[].*?[\)\]]\s*$", "", title).strip()
    if bare and bare != title:
        results = _lrclib_json("https://lrclib.net/api/search?" + urllib.parse.urlencode(
            {"artist_name": artist, "track_name": bare}))
        if isinstance(results, list):
            for entry in results:
                txt, synced = _lyrics_entry(entry)
                if txt: return txt, synced
    return None, None

def _cache_key(track):
    p = track.get("remote_path") or track.get("path")
    if not p:
        p = "%s|%s|%s" % (track.get("artist", ""), track.get("album", ""), track.get("title", ""))
        return hashlib.sha1(p.encode()).hexdigest()[:16]
    rel = p if track.get("remote_path") else os.path.relpath(p, HERE)
    return hashlib.sha1(rel.encode()).hexdigest()[:16]

def _frame(fid, body):
    return fid.encode() + len(body).to_bytes(4, "big") + b"\x00\x00" + body

def _text_frame(fid, txt):
    return _frame(fid, b"\x03" + txt.encode("utf-8"))

def embed_into_mp3(path, art, art_mime, lyrics):
    """Rewrite the ID3v2 tag, preserving all original frames and the audio,
    adding APIC/USLT only if missing. Atomic replace; False on any failure
    (e.g. read-only NAS file) — the cache still makes the player show it."""
    try:
        with open(path, "rb") as f: data = f.read()
        old_len = 0; old_frames = b""
        if data[:3] == b"ID3":
            old_len = 10 + _synchsafe(data[6:10])
            old_frames = data[10:old_len]
        audio = data[old_len:]
        # Parse original frames so we keep them verbatim, dropping any
        # APIC/USLT we are about to replace.
        frames = b""; pos = 0; ver = 3
        if data[:3] == b"ID3": ver = data[3]
        while pos + 10 <= len(old_frames):
            fid = old_frames[pos:pos + 4]
            if fid == b"\x00\x00\x00\x00" or not fid.isalnum(): break
            fsize = _synchsafe(old_frames[pos + 4:pos + 8]) if ver == 4 else int.from_bytes(old_frames[pos + 4:pos + 8], "big")
            if fid.decode("latin-1") not in ("APIC", "USLT"):
                frames += old_frames[pos:pos + 10 + fsize]
            pos += 10 + fsize
        if art: frames += _frame("APIC", b"\x03" + (art_mime or "image/jpeg").encode() + b"\x00\x03\x00" + art)
        if lyrics: frames += _frame("USLT", b"\x03eng\x00" + lyrics.encode("utf-8"))
        size = len(frames)
        tag = b"ID3" + bytes([3, 0, 0]) + bytes([(size >> 21) & 0x7f, (size >> 14) & 0x7f, (size >> 7) & 0x7f, size & 0x7f]) + frames
        tmp = path + ".lifehud-tmp"
        with open(tmp, "wb") as f: f.write(tag + audio)
        os.replace(tmp, path)
        return True
    except Exception:
        return False

def _enrich(track):
    """Return (art, art_mime, lyrics, synced) for a track: embedded, then
    cached, then downloaded. synced is [{t, text}] or None. Local files
    also get the download embedded (plain lyrics only)."""
    local = track.get("path")
    if track.get("source") == "nas":
        local = nas_download(track)
        if not local: return None, None, None, None
    info = parse_mp3(local) if local else {"art": None, "art_mime": None, "lyrics": None}
    art, mime, lyrics = info["art"], info["art_mime"], info["lyrics"]
    synced = None
    key = _cache_key(track)
    art_cache = os.path.join(CACHE_DIR, key + ".jpg")
    lyr_cache = os.path.join(CACHE_DIR, key + ".txt")
    syn_cache = os.path.join(CACHE_DIR, key + ".lrc")
    if not art and os.path.isfile(art_cache):
        try:
            with open(art_cache, "rb") as f: art = f.read(); mime = "image/jpeg"
        except Exception: pass
    if not lyrics and os.path.isfile(lyr_cache):
        try:
            with open(lyr_cache) as f: lyrics = f.read().strip() or None
        except Exception: pass
    if os.path.isfile(syn_cache):
        try:
            with open(syn_cache) as f: synced = json.load(f) or None
        except Exception: pass
    if not art:
        art, mime = download_art(track["artist"], track["album"], track["title"])
        if art:
            try:
                with open(art_cache, "wb") as f: f.write(art)
            except Exception: pass
    if not lyrics:
        lyrics, synced = download_lyrics(track["artist"], track["title"], track["album"])
        if lyrics:
            try:
                with open(lyr_cache, "w") as f: f.write(lyrics)
            except Exception: pass
        if synced:
            try:
                with open(syn_cache, "w") as f: json.dump(synced, f)
            except Exception: pass
    elif not synced:
        # Lyrics were already cached/embedded, but synced timing wasn't
        # yet — one extra lookup upgrades this track to scrolling lyrics.
        _txt, _syn = download_lyrics(track["artist"], track["title"], track["album"])
        if _syn:
            synced = _syn
            try:
                with open(syn_cache, "w") as f: json.dump(synced, f)
            except Exception: pass
    if local and track.get("source") != "nas" and (art or lyrics):
        embed_into_mp3(local, art if not info["art"] else None,
                       mime, lyrics if not info["lyrics"] else None)
    return art, mime, lyrics, synced

# 30-minute system history, sampled in the background every 10 s so the
# graph is already populated when the page opens (not just from page load).
_history = []
def _sampler():
    while True:
        try:
            mem = read_mem()
            _history.append({"t": time.time(), "cpu": read_cpu(),
                             "mem": mem["pct"], "temp": read_temp(),
                             "mem_used": mem["used_gb"], "mem_total": mem["total_gb"]})
            cutoff = time.time() - 1800
            while _history and _history[0]["t"] < cutoff: _history.pop(0)
        except Exception:
            pass
        time.sleep(10)

def stats():
    with open("/proc/uptime") as f: up = float(f.read().split()[0])
    with open("/proc/loadavg") as f: load = f.read().split()[:3]
    if _history:
        last = _history[-1]
        cpu, mem_pct, temp = last["cpu"], last["mem"], last["temp"]
        mem = {"pct": mem_pct, "used_gb": last.get("mem_used"), "total_gb": last.get("mem_total")}
    else:
        mem = read_mem(); cpu, temp = read_cpu(), read_temp()
    return {"cpu_pct": cpu, "mem": mem, "temp_c": temp,
            "temp_f": round(temp * 9 / 5 + 32, 1) if temp is not None else None,
            "uptime_h": round(up / 3600, 1), "load": load, "time": time.strftime("%I:%M:%S %p"),
            "date": time.strftime("%A, %B %d, %Y"),
            "history": list(_history)}

HTML = r"""<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Life HUD</title><link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css"><style>
*{box-sizing:border-box}body{margin:0;background:#0b1220;color:#e8eefc;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;padding:24px}
h1{margin:0;font-size:42px}h2{margin:0 0 12px;font-size:22px;color:#9fb4e8}
.sub{color:#8fa0c0;font-size:18px;margin-top:4px}
.grid{display:grid;grid-template-columns:1.2fr 1fr;gap:18px;margin-top:20px}
.card{background:#14203a;border:1px solid #24365f;border-radius:16px;padding:20px}
.big{font-size:64px;font-weight:700;line-height:1}.row{display:flex;gap:24px;flex-wrap:wrap;margin-top:10px;font-size:20px}
.bar{height:18px;background:#24365f;border-radius:9px;overflow:hidden;margin:8px 0 4px}.fill{height:100%;background:#4da3ff}
.gauge{font-size:44px;font-weight:700}.task{padding:12px 0;border-top:1px solid #24365f}.task:first-of-type{border-top:none}
.task b{font-size:20px}.task .when{color:#ffb454;font-weight:700}.task .det{color:#aebdd8}
.day{display:flex;justify-content:space-between;padding:8px 0;border-top:1px solid #24365f;font-size:19px}
.day:first-of-type{border-top:none}.tag{background:#2a4a7f;border-radius:20px;padding:2px 10px;font-size:14px;margin-left:8px}
small{color:#8fa0c0}.btn{background:#2a4a7f;border:1px solid #3d5f9e;color:#e8eefc;border-radius:10px;padding:10px 16px;font-size:18px;cursor:pointer;margin:4px 6px 4px 0}
.btn:hover{background:#3a5ea0}#lyrics{white-space:pre-wrap;max-height:360px;overflow:auto;background:#0b1220;border:1px solid #24365f;border-radius:10px;padding:18px;font-size:21px;line-height:1.65;margin-top:12px}
#lyrics.columns{column-count:2;column-gap:32px}
#lyrics .lyric-line{padding:6px 10px;border-radius:8px;color:#aebdd8;white-space:normal}
#lyrics .lyric-line.past{color:#7d8fb3}
#lyrics .lyric-line.cur{color:#ffffff;background:#24407a;font-weight:700;font-size:23px}
@media(max-width:900px){.grid{grid-template-columns:1fr}h1{font-size:32px}.big{font-size:52px}}
</style></head><body>
<h1>Life HUD</h1><div class="sub" id="clock">Loading…</div>
<div class="card" style="margin-top:20px"><h2>🎵 Music — NAS</h2>
<div style="display:flex;gap:20px;flex-wrap:wrap">
<img id="albumart" alt="" style="width:180px;height:180px;object-fit:cover;border-radius:12px;background:#24365f;display:none">
<div style="flex:1;min-width:280px">
<div id="nowplaying" style="font-size:22px;font-weight:700">No music found yet</div>
<div class="sub" id="musicmode"></div>
<audio id="audio" controls preload="none" style="width:100%;margin-top:12px"></audio>
<div id="musicerror" style="color:#ffb454;margin-top:6px"></div>
<div style="margin-top:10px">
<button class="btn" onclick="musicRandom()">▶ Random</button>
<button class="btn" onclick="musicNext()">⏭ Next</button>
<button class="btn" onclick="musicAlbum()">💿 Album</button>
<button class="btn" onclick="musicArtist()">🎤 More Artist</button>
<button class="btn" onclick="loadMusic(true)">↻ Rescan</button>
<button class="btn" id="lyricViewBtn" onclick="cycleLyricView()">📜 Lyrics: Auto</button>
</div>
<small id="musiccount"></small>
</div></div>
<div id="lyrics" style="display:none"></div>
</div>
<button class="btn" onclick="toggleMenu()" style="margin-top:18px">☰ Menu</button>
<div id="menuPanel" style="display:none">
<div class="card" style="margin-top:18px"><h2>🗂 NAS Explorer</h2>
<div id="nasstatus" class="sub">Not connected</div>
<div style="margin-top:12px;display:flex;gap:10px;flex-wrap:wrap">
<input id="nashost" placeholder="Host, e.g. 192.168.1.10" value="" style="padding:10px;border-radius:8px;border:1px solid #3d5f9e;background:#0b1220;color:#e8eefc;font-size:16px">
<input id="nasshare" placeholder="Share, e.g. music" style="padding:10px;border-radius:8px;border:1px solid #3d5f9e;background:#0b1220;color:#e8eefc;font-size:16px">
<input id="nasuser" placeholder="Username" style="padding:10px;border-radius:8px;border:1px solid #3d5f9e;background:#0b1220;color:#e8eefc;font-size:16px">
<input id="naspass" type="password" placeholder="Password" style="padding:10px;border-radius:8px;border:1px solid #3d5f9e;background:#0b1220;color:#e8eefc;font-size:16px">
</div>
<div style="margin-top:10px">
<button class="btn" onclick="nasConnect()">Connect</button>
<button class="btn" onclick="nasBrowse('/')">Browse from root</button>
<button class="btn" onclick="nasDisconnect()">Disconnect</button>
</div>
<small>Credentials are sent only to this HUD on your own network and stored only on the stick (owner-only file) so it can reconnect. They are never sent to Muse or to chat.</small>
<div id="nasbrowse" style="margin-top:14px"></div>
</div>
</div>
<div class="grid">
<div>
<div class="card"><h2>🌤 __CITY__</h2><div id="usmap" style="height:220px;border-radius:10px;overflow:hidden;background:#0b1220;margin-bottom:12px"></div><div class="sub" id="radartime" style="margin-bottom:12px">US precipitation — loading radar…</div><div id="wx">Loading weather…</div><div id="days"></div>
<small id="wxnote"></small></div>
<div class="card" style="margin-top:18px"><h2>📋 Upcoming</h2><div id="tasks">Loading tasks…</div>
<small>Edit tasks.json next to life-hud.py to change this list.</small></div>
</div>
<div>
<div class="card"><h2>🖥 System — CPU / Memory / Temperature</h2>
<div style="display:flex;gap:20px;flex-wrap:wrap">
<div style="display:flex;flex-direction:column;gap:12px">
<div style="text-align:center"><svg width="90" height="90" viewBox="0 0 90 90"><circle cx="45" cy="45" r="38" stroke="#24365f" stroke-width="10" fill="none"/><circle id="dialCpu" cx="45" cy="45" r="38" stroke="#4da3ff" stroke-width="10" fill="none" stroke-linecap="round" transform="rotate(-90 45 45)" stroke-dasharray="238.8" stroke-dashoffset="238.8"/><text id="dialCpuText" x="45" y="50" text-anchor="middle" fill="#e8eefc" font-size="17" font-weight="700">–</text></svg><div>CPU</div></div>
<div style="text-align:center"><svg width="90" height="90" viewBox="0 0 90 90"><circle cx="45" cy="45" r="38" stroke="#24365f" stroke-width="10" fill="none"/><circle id="dialMem" cx="45" cy="45" r="38" stroke="#4ade80" stroke-width="10" fill="none" stroke-linecap="round" transform="rotate(-90 45 45)" stroke-dasharray="238.8" stroke-dashoffset="238.8"/><text id="dialMemText" x="45" y="50" text-anchor="middle" fill="#e8eefc" font-size="17" font-weight="700">–</text></svg><div>Memory</div></div>
<div style="text-align:center"><svg width="90" height="90" viewBox="0 0 90 90"><circle cx="45" cy="45" r="38" stroke="#24365f" stroke-width="10" fill="none"/><circle id="dialTemp" cx="45" cy="45" r="38" stroke="#ffb454" stroke-width="10" fill="none" stroke-linecap="round" transform="rotate(-90 45 45)" stroke-dasharray="238.8" stroke-dashoffset="238.8"/><text id="dialTempText" x="45" y="50" text-anchor="middle" fill="#e8eefc" font-size="17" font-weight="700">–</text></svg><div>Temp °C</div></div>
</div>
<div style="flex:1;min-width:280px">
<canvas id="history" width="600" height="220" style="width:100%;height:220px;background:#0b1220;border-radius:10px"></canvas>
<div class="row" style="font-size:16px"><span style="color:#4da3ff">● CPU %</span><span style="color:#4ade80">● Memory %</span><span style="color:#ffb454">● Temp °C</span><span>Last 30 min</span></div>
<div class="row"><span>Load <b id="load">–</b></span><span>Uptime <b id="up">–</b> hrs</span><span><b id="memused">–</b> / <b id="memtotal">–</b> GB mem</span><span><b id="temp">–</b>°F</span></div>
</div></div></div>
</div></div>
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js"></script>
<script>
const WMO={0:"Clear",1:"Mostly clear",2:"Partly cloudy",3:"Overcast",45:"Fog",48:"Fog",51:"Light drizzle",53:"Drizzle",55:"Drizzle",61:"Light rain",63:"Rain",65:"Heavy rain",71:"Light snow",73:"Snow",75:"Heavy snow",80:"Light showers",81:"Showers",82:"Heavy showers",85:"Snow showers",95:"Thunderstorm",96:"Storm + hail",99:"Storm + hail"};
function fmtDate(s){const d=new Date(s+"T12:00:00");return d.toLocaleDateString(undefined,{weekday:"short",month:"short",day:"numeric"})}
function setDial(id,txt,val){const c=document.getElementById(id);const t=document.getElementById(id+"Text");const v=Math.max(0,Math.min(100,val??0));c.style.strokeDashoffset=(238.8*(1-v/100)).toFixed(1);t.textContent=txt;}
function drawHistory(h){const cv=document.getElementById("history");if(!cv)return;const ctx=cv.getContext("2d");ctx.clearRect(0,0,600,220);
ctx.strokeStyle="#24365f";ctx.lineWidth=1;for(let y=0;y<=100;y+=25){const py=200-(y/100)*190;ctx.beginPath();ctx.moveTo(40,py);ctx.lineTo(600,py);ctx.stroke();ctx.fillStyle="#8fa0c0";ctx.font="12px system-ui";ctx.fillText(y,8,py+4);}
if(!h||h.length<2){ctx.fillStyle="#8fa0c0";ctx.font="16px system-ui";ctx.fillText("Collecting 30-min history…",60,110);return;}
const now=h[h.length-1].t,from=now-1800;
const series=[["cpu","#4da3ff"],["mem","#4ade80"],["temp","#ffb454"]];
for(const [key,color] of series){ctx.strokeStyle=color;ctx.lineWidth=2;ctx.beginPath();let started=false;
for(const p of h){if(p[key]==null)continue;const x=40+((p.t-from)/1800)*560;const y=200-((Math.max(0,Math.min(100,p[key]))/100)*190);if(!started){ctx.moveTo(x,y);started=true;}else ctx.lineTo(x,y);}ctx.stroke();}}
async function stats(){try{const r=await fetch("/api/stats");const s=await r.json();
document.getElementById("clock").textContent=s.date+"  •  "+s.time+"  •  Up "+s.uptime_h+" hrs";
setDial("dialCpu",Math.round(s.cpu_pct)+"%",s.cpu_pct);
setDial("dialMem",Math.round(s.mem.pct)+"%",s.mem.pct);
setDial("dialTemp",Math.round(s.temp_c??0)+"°",s.temp_c??0);
document.getElementById("load").textContent=s.load.join("  ");document.getElementById("up").textContent=s.uptime_h;
document.getElementById("memused").textContent=s.mem.used_gb??"–";document.getElementById("memtotal").textContent=s.mem.total_gb??"–";
document.getElementById("temp").textContent=s.temp_f??"–";
drawHistory(s.history||[]);}catch(e){}}
async function weather(){try{const r=await fetch("/api/weather");const j=await r.json();if(!j.data){document.getElementById("wx").textContent="Weather unavailable";return}
const c=j.data.current,d=j.data.daily;
document.getElementById("wx").innerHTML=`<div class="big">${Math.round(c.temperature_2m)}°F</div><div class="row"><span>${WMO[c.weather_code]||"—"}</span><span>Feels ${Math.round(c.apparent_temperature)}°F</span><span>Humidity ${c.relative_humidity_2m}%</span><span>Wind ${Math.round(c.wind_speed_10m)} mph</span></div>`;
document.getElementById("wxnote").textContent=j.stale?"Weather cache is stale — last fetch failed, will retry.":"Updated from Open-Meteo, refreshes every 15 min.";
let h="";for(let i=0;i<d.time.length;i++){let tag="";
h+=`<div class="day"><span>${fmtDate(d.time[i])}${tag}</span><span>${WMO[d.weather_code[i]]||""} · ${Math.round(d.temperature_2m_min[i])}° / ${Math.round(d.temperature_2m_max[i])}° · ${d.precipitation_probability_max[i]??0}% rain</span></div>`;}
document.getElementById("days").innerHTML=h;}catch(e){document.getElementById("wx").textContent="Weather unavailable";}}
async function tasks(){try{const r=await fetch("/api/tasks");const t=await r.json();
const today=new Date();today.setHours(0,0,0,0);
const box=document.getElementById("tasks");box.textContent="";
if(!t.length){box.textContent="No tasks listed.";return;}
for(const x of t){let when="";if(x.date){const d=new Date(x.date+"T12:00:00");const diff=Math.round((d-today)/86400000);when=diff===0?"Today":diff===1?"Tomorrow":diff>0?`In ${diff} days`:`${-diff} days ago`;when+=x.time?` • ${x.time}`:"";}
const div=document.createElement("div");div.className="task";
const b=document.createElement("b");b.textContent=x.title||"";div.appendChild(b);
const w=document.createElement("span");w.className="when";w.textContent=" "+when;div.appendChild(w);
div.appendChild(document.createElement("br"));
const det=document.createElement("span");det.className="det";det.textContent=x.detail||"";div.appendChild(det);
box.appendChild(div);}}catch(e){}}
let mTracks=[],mCurrent=null,mMode="all",mModeName="All songs",mQueue=[],mQi=-1,mLastId=-1;
const audioEl=document.getElementById("audio");
function shuffle(a){a=[...a];for(let i=a.length-1;i>0;i--){const j=Math.floor(Math.random()*(i+1));[a[i],a[j]]=[a[j],a[i]];}return a;}
function pickRandom(pool){if(!pool.length)return null;let p=pool.filter(t=>t.id!==mLastId);if(!p.length)p=pool;return p[Math.floor(Math.random()*p.length)];}
async function loadMusic(force){try{const r=await fetch("/api/music/index"+(force?"?scan=1":""),force?{headers:{"X-LifeHUD-Rescan":"1"}}:{});mTracks=await r.json();
document.getElementById("musiccount").textContent=mTracks.length?`${mTracks.length} songs indexed`:"No MP3s found — connect the NAS below and pick a music folder, or put MP3s in ./music, then Rescan.";
if(!mTracks.length)document.getElementById("nowplaying").textContent="No music found yet";}catch(e){document.getElementById("musiccount").textContent="Music index unavailable";}}
let lyricSynced=null,lyricPlain="",lyricIdx=-1,lyricView="auto",lastManualScroll=0;
function renderLyrics(){const ly=document.getElementById("lyrics");
if(lyricSynced&&lyricSynced.length&&lyricView!=="columns"){
ly.classList.remove("columns");ly.style.whiteSpace="normal";ly.innerHTML="";
lyricSynced.forEach((ln,i)=>{const d=document.createElement("div");d.className="lyric-line";d.textContent=ln.text||" ";ly.appendChild(d);});
lyricIdx=-1;return;}
ly.classList.add("columns");ly.style.whiteSpace="pre-wrap";ly.textContent=lyricPlain;}
function cycleLyricView(){let next=lyricView==="auto"?"columns":lyricView==="columns"?"scroll":"auto";
if(next==="scroll"&&!lyricSynced)next="columns";lyricView=next;
document.getElementById("lyricViewBtn").textContent="📜 Lyrics: "+(lyricView==="auto"?"Auto":lyricView==="columns"?"Columns":"Scroll");
if(lyricPlain)renderLyrics();}
document.getElementById("lyrics").addEventListener("scroll",()=>{lastManualScroll=Date.now();});
audioEl.addEventListener("timeupdate",()=>{if(!lyricSynced||!lyricSynced.length||lyricView==="columns")return;
const t=audioEl.currentTime;let idx=-1;
for(let i=0;i<lyricSynced.length;i++){if(lyricSynced[i].t<=t)idx=i;else break;}
if(idx===lyricIdx)return;lyricIdx=idx;
const ly=document.getElementById("lyrics");const lines=ly.querySelectorAll(".lyric-line");
lines.forEach((d,i)=>{d.classList.toggle("cur",i===idx);d.classList.toggle("past",i<idx);});
if(idx>=0&&Date.now()-lastManualScroll>10000)lines[idx].scrollIntoView({block:"center",behavior:"smooth"});});
audioEl.addEventListener("seeked",()=>{lyricIdx=-1;});
async function playTrack(t){if(!t)return;mCurrent=t;mLastId=t.id;
document.getElementById("nowplaying").textContent=`${t.title} — ${t.artist}`;
document.title=`${t.title} — ${t.artist}`;
document.getElementById("musicmode").textContent=`${t.album} · ${mModeName}`+(mQueue.length?` · ${mQi+1} of ${mQueue.length}`:"");
const art=document.getElementById("albumart");
art.onerror=()=>{art.style.display="none";};
art.src="/api/music/art/"+t.id;art.style.display="block";
document.getElementById("musicerror").textContent="";
audioEl.onerror=()=>{document.getElementById("musicerror").textContent="Could not play this file — the server could not stream it. Try Next, or Rescan.";};
audioEl.src="/api/music/file/"+t.id;try{await audioEl.play();}catch(e){document.getElementById("musicerror").textContent="Playback blocked/failed: "+(e.message||e);}
const ly=document.getElementById("lyrics");ly.textContent="Looking up lyrics…";ly.style.display="block";
lyricSynced=null;lyricPlain="";lyricIdx=-1;
try{const r=await fetch("/api/music/lyrics/"+t.id);const j=await r.json();
if(j.lyrics){lyricPlain=j.lyrics;lyricSynced=(j.synced&&j.synced.length)?j.synced:null;renderLyrics();}
else{ly.classList.remove("columns");ly.textContent="No lyrics found in the file or online for this track.";}}catch(e){ly.style.display="none";}}
function startShuffle(pool,mode,name){if(!pool.length)return;mMode=mode;mModeName=name;mQueue=shuffle(pool);
if(mQueue.length>1&&mQueue[0].id===mLastId){const tmp=mQueue[0];mQueue[0]=mQueue[1];mQueue[1]=tmp;}
mQi=0;playTrack(mQueue[0]);}
function reshuffleQueue(){let pool=mTracks;
if(mMode==="artist"&&mCurrent)pool=mTracks.filter(t=>t.artist===mCurrent.artist);
mQueue=shuffle(pool);
if(mQueue.length>1&&mQueue[0].id===mLastId){const tmp=mQueue[0];mQueue[0]=mQueue[1];mQueue[1]=tmp;}
mQi=0;}
function musicRandom(){startShuffle(mTracks,"all","All songs — shuffle");}
function musicNext(){
if(mMode==="album"&&mQueue.length){mQi++;if(mQi<mQueue.length){playTrack(mQueue[mQi]);return;}
musicRandom();return;}
if(mQueue.length){mQi++;
if(mQi>=mQueue.length)reshuffleQueue();
playTrack(mQueue[mQi]);return;}
if(mMode==="artist"&&mCurrent){startShuffle(mTracks.filter(t=>t.artist===mCurrent.artist),"artist","More by "+mCurrent.artist+" — shuffle");return;}
musicRandom();}
function musicAlbum(){if(!mCurrent)return;const same=mTracks.filter(t=>t.artist===mCurrent.artist&&t.album===mCurrent.album);
same.sort((a,b)=>(a.track??999)-(b.track??999)||a.title.localeCompare(b.title));
mMode="album";mModeName="Album: "+mCurrent.album;mQueue=same;mQi=0;playTrack(same[0]);}
function musicArtist(){if(!mCurrent)return;startShuffle(mTracks.filter(t=>t.artist===mCurrent.artist),"artist","More by "+mCurrent.artist+" — shuffle");}
audioEl.addEventListener("ended",musicNext);
function toggleMenu(){const p=document.getElementById("menuPanel");p.style.display=(p.style.display==="none"||!p.style.display)?"block":"none";}
let nasPath="/";
async function nasStatus(){try{const r=await fetch("/api/nas/status");const s=await r.json();
document.getElementById("nasstatus").textContent=s.connected?`Connected: ${s.host} / ${s.share} — music folder: ${s.music_folder||"(none picked yet)"} — ${s.track_count} songs`:"Not connected";
if(s.host)document.getElementById("nashost").value=s.host;
if(s.share)document.getElementById("nasshare").value=s.share;
if(s.username)document.getElementById("nasuser").value=s.username;}catch(e){}}
async function nasConnect(){const body={host:document.getElementById("nashost").value,share:document.getElementById("nasshare").value,username:document.getElementById("nasuser").value,password:document.getElementById("naspass").value};
document.getElementById("nasstatus").textContent="Connecting…";
const r=await fetch("/api/nas/connect",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});const j=await r.json();
if(j.ok){document.getElementById("nasstatus").textContent="Connected — shares found: "+(j.shares||[]).join(", ");nasBrowse("/");}else{document.getElementById("nasstatus").textContent="Connect failed: "+(j.error||"check host, share and login");}}
async function nasBrowse(p){nasPath=p;const box=document.getElementById("nasbrowse");box.textContent="Loading…";
const r=await fetch("/api/nas/browse?path="+encodeURIComponent(p));const j=await r.json();
if(j.error){box.textContent="Browse failed: "+j.error;return;}
box.textContent="";
const head=document.createElement("div");head.style.marginBottom="8px";
const hb=document.createElement("b");hb.textContent=j.path;head.appendChild(hb);
const useBtn=document.createElement("button");useBtn.className="btn";useBtn.textContent="Use this folder for music";useBtn.addEventListener("click",nasUseFolder);head.appendChild(useBtn);
box.appendChild(head);
if(p!=="/"){const parent="/"+p.split("/").filter(Boolean).slice(0,-1).join("/");
const upWrap=document.createElement("div");const up=document.createElement("button");up.className="btn";up.textContent="⬆ Up";up.addEventListener("click",()=>nasBrowse(parent==="/"?"/":parent));upWrap.appendChild(up);box.appendChild(upWrap);}
if(!j.entries.length){const d=document.createElement("div");d.textContent="No entries";box.appendChild(d);}
for(const e of j.entries){const wrap=document.createElement("div");
if(e.type==="dir"){const btn=document.createElement("button");btn.className="btn";btn.textContent="📁 "+e.name;const dest=(p==="/"?"/"+e.name:p+"/"+e.name);btn.addEventListener("click",()=>nasBrowse(dest));wrap.appendChild(btn);}
else{wrap.style.padding="6px 0";wrap.style.color="#aebdd8";wrap.textContent="🎵 "+e.name;}
box.appendChild(wrap);}}
async function nasUseFolder(){const r=await fetch("/api/nas/use-folder",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({path:nasPath})});const j=await r.json();
if(j.ok){document.getElementById("nasstatus").textContent=`Music folder set: ${j.music_folder} — ${j.track_count} songs`;loadMusic(true);}else{document.getElementById("nasstatus").textContent="Could not set folder: "+j.error;}}
async function nasDisconnect(){await fetch("/api/nas/disconnect",{method:"POST"});document.getElementById("nasbrowse").innerHTML="";loadMusic(true);nasStatus();}
let radarMap=null,radarLayer=null,radarFrames=[],radarIdx=0,radarTimer=null;
async function usRadar(){try{
if(typeof L==="undefined"){document.getElementById("radartime").textContent="US precipitation radar unavailable (map library did not load).";return;}
if(!radarMap){radarMap=L.map("usmap",{scrollWheelZoom:false}).setView([39.5,-98.35],4);
L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png",{maxZoom:6,attribution:"© OpenStreetMap · Radar: RainViewer"}).addTo(radarMap);}
const r=await fetch("https://api.rainviewer.com/public/weather-maps.json");const j=await r.json();
const now=Math.floor(Date.now()/1000);let frames=(j.radar.past||[]).filter(f=>f.time>=now-1800);
if(frames.length<2)frames=(j.radar.past||[]).slice(-6);
if(!frames.length){document.getElementById("radartime").textContent="US precipitation radar unavailable right now.";return;}
radarFrames=frames;radarIdx=0;
const radarHost=(j.host||"https://tilecache.rainviewer.com").replace(/\/$/,"");
const radarUrl=f=>radarHost+f.path+"/256/{z}/{x}/{y}/2/1_1.png";
if(radarLayer)radarMap.removeLayer(radarLayer);
radarLayer=L.tileLayer(radarUrl(frames[0]),{opacity:0.7,zIndex:10}).addTo(radarMap);
if(radarTimer)clearInterval(radarTimer);
radarTimer=setInterval(()=>{radarIdx=(radarIdx+1)%radarFrames.length;
const f=radarFrames[radarIdx];
radarLayer.setUrl(radarUrl(f));
document.getElementById("radartime").textContent="US precipitation — "+new Date(f.time*1000).toLocaleTimeString([],{hour:"numeric",minute:"2-digit"})+" · looping past 30 min";},900);
document.getElementById("radartime").textContent="US precipitation — looping past 30 min";
}catch(e){document.getElementById("radartime").textContent="US precipitation radar unavailable right now.";}}
stats();weather();tasks();loadMusic();nasStatus();usRadar();setInterval(stats,10000);setInterval(weather,600000);setInterval(tasks,60000);setInterval(usRadar,600000);
</script></body></html>"""

class H(BaseHTTPRequestHandler):
    server_version = "LifeHUD"
    sys_version = ""
    def log_message(self, *a): pass
    def _send(self, body, ctype):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(200); self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(b)))
        self.send_header("X-Content-Type-Options", "nosniff"); self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.end_headers(); self.wfile.write(b)
    def _send_file_range(self, path):
        try:
            size = os.path.getsize(path)
        except OSError:
            self.send_response(404); self.end_headers(); return
        start, end = 0, size - 1
        status = 200
        rng = self.headers.get("Range")
        if rng:
            try:
                spec = rng.replace("bytes=", "").split("-")
                start = int(spec[0]) if spec[0] else 0
                end = int(spec[1]) if spec[1] else size - 1
                # Clamp: an out-of-bounds Range previously produced a
                # Content-Length larger than the file (e.g. 9 GB for a
                # 4 MB file) and a truncated body — reject it instead.
                if start < 0 or end < start or start >= size:
                    self.send_response(416); self.send_header("Content-Range", f"bytes */{size}"); self.end_headers(); return
                end = min(end, size - 1)
                status = 206
            except Exception:
                start, end, status = 0, size - 1, 200
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if status == 206: self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start); remaining = length
            while remaining > 0:
                chunk = f.read(min(65536, remaining))
                if not chunk: break
                self.wfile.write(chunk); remaining -= len(chunk)

    def do_GET(self):
        from urllib.parse import urlparse, parse_qs
        u = urlparse(self.path); path = u.path; q = parse_qs(u.query)
        if path == "/": self._send(HTML.replace("__CITY__", CITY_NAME), "text/html")
        elif path == "/api/stats": self._send(json.dumps(stats()), "application/json")
        elif path == "/api/weather":
            data, stale = get_weather(); self._send(json.dumps({"data": data, "stale": stale}), "application/json")
        elif path == "/api/tasks": self._send(json.dumps(get_tasks()), "application/json")
        elif path == "/api/music/index":
            # A rescan must come from the HUD page itself, not from an
            # <img> tag on another site (GET CSRF): the page sends this
            # header, a cross-site image load cannot.
            force = ("scan" in q) and (self.headers.get("X-LifeHUD-Rescan") == "1")
            tracks = get_music_index(force=force)
            self._send(json.dumps([{k: v for k, v in t.items() if k not in ("path", "remote_path")} for t in tracks]), "application/json")
        elif path == "/api/nas/status":
            cfg = load_nas_config()
            n = len(get_music_index()) if cfg.get("music_folder") else 0
            self._send(json.dumps({"host": cfg.get("host", ""), "share": cfg.get("share", ""),
                                   "username": cfg.get("username", ""), "music_folder": cfg.get("music_folder", ""),
                                   "connected": bool(cfg.get("share")), "track_count": n}), "application/json")
        elif path == "/api/nas/shares":
            cfg = load_nas_config()
            shares, raw = smb_list_shares(cfg)
            self._send(json.dumps({"shares": shares, "raw": raw[-2000:]}), "application/json")
        elif path == "/api/nas/browse":
            cfg = load_nas_config()
            p = q.get("path", ["/"])[0]
            entries, err, rc = smb_browse(cfg, p) if cfg.get("share") else ([], "Not connected", 1)
            self._send(json.dumps({"path": p, "entries": entries, "error": err[-500:] if rc else "", "rc": rc}), "application/json")
        elif path.startswith("/api/music/file/"):
            try: tid = int(path.rsplit("/", 1)[1])
            except Exception: tid = -1
            t = music_track_by_id(tid)
            fp = music_file_path(t) if t else None
            if fp: self._send_file_range(fp)
            else: self.send_response(404); self.end_headers()
        elif path.startswith("/api/music/art/"):
            try: tid = int(path.rsplit("/", 1)[1])
            except Exception: tid = -1
            art, mime = music_art(tid)
            if art: self._send(art, mime or "image/jpeg")
            else: self.send_response(404); self.end_headers()
        elif path.startswith("/api/music/lyrics/"):
            try: tid = int(path.rsplit("/", 1)[1])
            except Exception: tid = -1
            lyrics, synced = music_lyrics(tid)
            self._send(json.dumps({"lyrics": lyrics, "synced": synced}), "application/json")
        else: self.send_response(404); self.end_headers()

    def do_POST(self):
        from urllib.parse import urlparse
        path = urlparse(self.path).path
        # CSRF: a POST from a different origin (a malicious site visited
        # in a browser on the home network) is rejected. Same-origin
        # HUD fetches and curl (no Origin header) are allowed.
        origin = self.headers.get("Origin")
        if origin:
            ohost = urlparse(origin).netloc
            if ohost and ohost != self.headers.get("Host", ""):
                self.send_response(403); self.end_headers(); return
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length < 0 or length > 65536:
                self.send_response(413); self.end_headers(); return
            body = json.loads(self.rfile.read(length).decode() or "{}")
        except Exception:
            body = {}
        if path == "/api/nas/connect":
            cfg = {"host": (body.get("host") or "").strip(),
                   "share": (body.get("share") or "").strip(),
                   "username": (body.get("username") or "").strip(),
                   "password": body.get("password") or "",
                   "music_folder": (body.get("music_folder") or "").strip()}
            if not cfg["share"]:
                self._send(json.dumps({"ok": False, "error": "Share name is required"}), "application/json"); return
            shares, raw = smb_list_shares(cfg)
            ok = cfg["share"] in shares or (not shares and "NT_STATUS_LOGON_FAILURE" not in raw
                                             and "NT_STATUS_CONNECTION_DISCONNECTED" not in raw)
            if ok: save_nas_config(cfg)
            self._send(json.dumps({"ok": ok, "shares": shares,
                                   "error": "" if ok else "Could not list that share — check host, share, username and password."}), "application/json")
        elif path == "/api/nas/use-folder":
            cfg = load_nas_config()
            folder = (body.get("path") or "").strip()
            if not cfg.get("share") or not folder:
                self._send(json.dumps({"ok": False, "error": "Connect first, then pick a folder"}), "application/json"); return
            cfg["music_folder"] = folder
            save_nas_config(cfg)
            tracks = get_music_index(force=True)
            self._send(json.dumps({"ok": True, "music_folder": folder, "track_count": len(tracks)}), "application/json")
        elif path == "/api/nas/disconnect":
            try: os.remove(NAS_CONFIG_FILE)
            except Exception: pass
            try: os.remove(SMB_AUTH_FILE)
            except Exception: pass
            with _lock: _music_cache.update(at=0, tracks=[])
            self._send(json.dumps({"ok": True}), "application/json")
        else:
            self.send_response(404); self.end_headers()

if __name__ == "__main__":
    threading.Thread(target=_sampler, daemon=True).start()
    ThreadingHTTPServer((HOST, PORT), H).serve_forever()
