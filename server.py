#!/usr/bin/env python3
"""
File transfer server — serves both Mac filesystem and Android phone via adb.
Supports both USB and wireless adb (same Wi-Fi network, no cable needed).
Run: python3 server.py [port]
"""

import os
import sys
import html
import io
import json
import shutil
import subprocess
import mimetypes
import socket
import tempfile
import threading
import time
import shlex
import urllib.parse
import zipfile
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True

try:
    import qrcode
    import qrcode.image.svg as qrsvg
    _HAS_QRCODE = True
except ImportError:
    _HAS_QRCODE = False

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
MAC_ROOT = Path.home()
DOWNLOADS = Path.home() / "Downloads"
ANDROID_PREFIX = "/android"
DOWNLOAD_API   = "/api/download"
CANCEL_API     = "/api/cancel"
SPACE_API      = "/api/space"
SEARCH_API     = "/api/search"
CONNECT_API      = "/api/connect"
PAIR_API         = "/api/pair"
ADB_STATUS_API   = "/api/adb_status"
QR_API           = "/api/qr"
MDNS_API         = "/api/mdns_discover"
AUTOCONNECT_PATH = "/android/connect"

# tracks active adb pulls: token -> {"name", "progress": 0-100, "status", "error"}
_jobs = {}
_jobs_lock = threading.Lock()


# ── helpers ──────────────────────────────────────────────────────────────────

def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def free_bytes():
    return shutil.disk_usage(str(DOWNLOADS)).free


def adb_shell(cmd):
    # Pass the command as a single shell string so Android's sh correctly
    # handles quoted paths with spaces (list args would split on spaces).
    shell_cmd = " ".join(cmd)
    result = subprocess.run(["adb", "shell", shell_cmd], capture_output=True, text=True)
    return result.stdout, result.stderr


_adb_connected_cache = {"value": False, "ts": 0}

def adb_connected():
    """Return True if at least one adb device is online (USB or TCP). Cached for 5s."""
    now = time.time()
    if now - _adb_connected_cache["ts"] < 5:
        return _adb_connected_cache["value"]
    result = subprocess.run(["adb", "devices"], capture_output=True, text=True)
    lines = [l for l in result.stdout.splitlines() if l and "List of" not in l]
    val = any("device" in l for l in lines)
    _adb_connected_cache["value"] = val
    _adb_connected_cache["ts"] = now
    return val


def adb_devices_list():
    """Return list of dicts: {serial, type} for all connected devices."""
    result = subprocess.run(["adb", "devices"], capture_output=True, text=True)
    devices = []
    for line in result.stdout.splitlines():
        if not line or "List of" in line:
            continue
        parts = line.split()
        if len(parts) >= 2:
            serial = parts[0]
            kind = "wifi" if ":" in serial else "usb"
            devices.append({"serial": serial, "type": kind, "state": parts[1]})
    return devices


def mdns_find_pairing_port(phone_ip, timeout=6):
    """
    Use dns-sd to find the adb pairing port broadcast by the phone when
    'Pair device with pairing code' screen is open.
    Returns port (str) or None.
    """
    import re
    try:
        # Step 1: browse for pairing service instance name
        browse = subprocess.Popen(
            ["dns-sd", "-B", "_adb-tls-pairing._tcp", "local."],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        time.sleep(timeout / 2)
        browse.terminate()
        bout, _ = browse.communicate()

        instance = None
        for line in bout.splitlines():
            m = re.search(r'_adb-tls-pairing\._tcp\.\s+(\S+)', line)
            if m:
                instance = m.group(1)
                break
        if not instance:
            return None

        # Step 2: resolve instance to get port
        resolve = subprocess.Popen(
            ["dns-sd", "-L", instance, "_adb-tls-pairing._tcp", "local."],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        time.sleep(timeout / 2)
        resolve.terminate()
        rout, _ = resolve.communicate()

        for line in rout.splitlines():
            m = re.search(r':(\d{4,5})\s', line)
            if m:
                return m.group(1)
    except Exception:
        pass
    return None


def adb_connect_wireless(ip, port=5555):
    """
    Connect to an Android device over Wi-Fi via adb.
    Returns (success: bool, message: str).
    """
    _adb_connected_cache["ts"] = 0  # invalidate cache
    target = f"{ip}:{port}"
    result = subprocess.run(["adb", "connect", target], capture_output=True, text=True)
    out = (result.stdout + result.stderr).strip()
    if "connected" in out.lower() and "unable" not in out.lower() and "failed" not in out.lower():
        return True, out
    return False, out


def adb_pair_wireless(ip, pair_port, pair_code):
    """
    Pair with an Android 11+ device using the pairing code flow.
    On Android 11+, adb auto-connects after pairing via mDNS — no separate connect needed.
    Returns (success: bool, message: str).
    """
    target = f"{ip}:{pair_port}"
    # Send code via stdin (most reliable across adb versions)
    try:
        proc = subprocess.Popen(
            ["adb", "pair", target],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True
        )
        out, _ = proc.communicate(input=pair_code + "\n", timeout=20)
        out = out.strip()
        if "successfully" in out.lower() or "paired" in out.lower():
            _adb_connected_cache["ts"] = 0  # invalidate cache
            return True, out
        return False, out
    except subprocess.TimeoutExpired:
        return False, "Pairing timed out — make sure the pairing screen is still open on your phone"


def adb_disconnect_all():
    """Disconnect all wireless adb connections."""
    _adb_connected_cache["ts"] = 0
    subprocess.run(["adb", "disconnect"], capture_output=True, text=True)


def adb_ls(path):
    out, _ = adb_shell(["ls", "-la", shlex.quote(path)])
    entries = []
    for line in out.splitlines():
        if "Permission denied" in line or "opendir failed" in line or "No such file" in line:
            continue
        parts = line.split()
        if len(parts) < 4:
            continue
        perms = parts[0]
        is_dir = perms.startswith("d")
        is_link = perms.startswith("l")

        # Reconstruct full name from everything after the fixed prefix columns
        # Android ls -la: perms links user group size date time name
        # That's 8 columns before the name (indices 0-7), name starts at index 7
        name_part = " ".join(parts[7:]) if len(parts) > 7 else parts[-1]
        if is_link and " -> " in name_part:
            name = name_part.split(" -> ")[0]
        else:
            name = name_part

        if name in (".", ".."):
            continue
        try:
            size = int(parts[4]) if not is_dir else 0
        except (ValueError, IndexError):
            size = 0
        entries.append((name, is_dir, size))
    return sorted(entries, key=lambda x: (not x[1], x[0].lower()))


def adb_file_size(remote_path):
    out, _ = adb_shell(["stat", "-c", "%s", shlex.quote(remote_path)])
    try:
        return int(out.strip())
    except ValueError:
        return 0


def adb_pull_tracked(token, remote_path, dest_path):
    total = adb_file_size(remote_path)

    proc = subprocess.Popen(
        ["adb", "pull", remote_path, str(dest_path)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )
    with _jobs_lock:
        _jobs[token]["proc"] = proc

    dest = Path(dest_path)
    while proc.poll() is None:
        with _jobs_lock:
            if _jobs[token]["status"] == "cancelled":
                proc.kill()
                break
        if total > 0 and dest.exists():
            pct = int(dest.stat().st_size / total * 99)
            with _jobs_lock:
                _jobs[token]["progress"] = min(99, pct)
        time.sleep(0.25)

    proc.wait()

    with _jobs_lock:
        if _jobs[token]["status"] == "cancelled":
            Path(dest_path).unlink(missing_ok=True)
        elif proc.returncode == 0:
            _jobs[token]["progress"] = 100
            _jobs[token]["status"] = "done"
            _jobs[token]["dest"] = str(dest_path)
        else:
            _jobs[token]["status"] = "error"
            _jobs[token]["error"] = "adb pull failed"


def adb_push(local_path, remote_path):
    result = subprocess.run(["adb", "push", local_path, remote_path], capture_output=True)
    return result.returncode == 0


def adb_push_tracked(token, local_path, remote_path, local_size):
    proc = subprocess.Popen(
        ["adb", "push", local_path, remote_path],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )
    with _jobs_lock:
        _jobs[token]["proc"] = proc

    while proc.poll() is None:
        if local_size > 0:
            out, _ = adb_shell(["stat", "-c", "%s", shlex.quote(remote_path)])
            try:
                done = int(out.strip())
                pct = int(done / local_size * 99)
                with _jobs_lock:
                    _jobs[token]["progress"] = min(99, pct)
            except (ValueError, IndexError):
                pass
        time.sleep(0.5)

    proc.wait()
    try:
        os.unlink(local_path)
    except OSError:
        pass

    with _jobs_lock:
        if proc.returncode == 0:
            _jobs[token]["progress"] = 100
            _jobs[token]["status"] = "done"
        else:
            _jobs[token]["status"] = "error"
            _jobs[token]["error"] = "adb push failed"


def _dir_size(path):
    total = 0
    for f in Path(path).rglob("*"):
        if f.is_file():
            try:
                total += f.stat().st_size
            except OSError:
                pass
    return total


def adb_folder_pull_tracked(token, remote_path, folder_name):
    tmp_dir = tempfile.mkdtemp()
    dest_tmp = os.path.join(tmp_dir, folder_name)
    os.makedirs(dest_tmp, exist_ok=True)

    out, _ = adb_shell(["du", "-sb", shlex.quote(remote_path)])
    try:
        total = int(out.strip().split()[0])
    except (ValueError, IndexError):
        total = 0

    proc = subprocess.Popen(
        ["adb", "pull", remote_path, dest_tmp],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )
    with _jobs_lock:
        _jobs[token]["proc"] = proc

    while proc.poll() is None:
        with _jobs_lock:
            if _jobs[token]["status"] == "cancelled":
                proc.kill()
                break
        if total > 0:
            pct = int(_dir_size(dest_tmp) / total * 80)
            with _jobs_lock:
                _jobs[token]["progress"] = min(80, pct)
        time.sleep(0.25)

    proc.wait()

    with _jobs_lock:
        cancelled = _jobs[token]["status"] == "cancelled"

    if cancelled or proc.returncode != 0:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        with _jobs_lock:
            if not cancelled:
                _jobs[token]["status"] = "error"
                _jobs[token]["error"]  = "adb pull failed"
        return

    # Zip it up
    with _jobs_lock:
        _jobs[token]["progress"] = 85

    zip_name = folder_name + ".zip"
    zip_dest = DOWNLOADS / zip_name
    stem, c = folder_name, 1
    while zip_dest.exists():
        zip_dest = DOWNLOADS / f"{stem}_{c}.zip"; c += 1

    # Already-compressed formats gain nothing from deflate — store them raw
    _NO_COMPRESS = {
        ".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic",
        ".mp4", ".mov", ".avi", ".mkv", ".webm",
        ".mp3", ".aac", ".m4a", ".flac", ".wav",
        ".zip", ".gz", ".7z", ".rar",
    }
    with zipfile.ZipFile(str(zip_dest), "w") as zf:
        pulled = Path(dest_tmp)
        for f in pulled.rglob("*"):
            if f.is_file():
                ext = f.suffix.lower()
                compress = zipfile.ZIP_STORED if ext in _NO_COMPRESS else zipfile.ZIP_DEFLATED
                zf.write(f, f.relative_to(pulled.parent), compress_type=compress,
                         compresslevel=None if compress == zipfile.ZIP_STORED else 1)

    shutil.rmtree(tmp_dir, ignore_errors=True)

    with _jobs_lock:
        _jobs[token]["progress"] = 100
        _jobs[token]["status"]   = "done"
        _jobs[token]["dest"]     = str(zip_dest)
        _jobs[token]["name"]     = zip_name


def adb_search(query, root="/sdcard"):
    """Search recursively across all top-level sdcard folders."""
    safe_query = query.replace("'", "").replace('"', "")
    # Get top-level dirs to search each one (Android blocks find on /sdcard directly)
    top_out, _ = adb_shell(["ls", "/sdcard"])
    dirs = [f"/sdcard/{d.strip()}" for d in top_out.splitlines() if d.strip()]
    if not dirs:
        dirs = [root]

    results = []
    for d in dirs:
        result = subprocess.run(
            ["adb", "shell", f"find {d} -iname '*{safe_query}*' 2>/dev/null"],
            capture_output=True, text=True
        )
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line or "Permission denied" in line or "No such file" in line:
                continue
            results.append(line)
        if len(results) >= 300:
            break
    return results[:300]


def format_size(n):
    for unit in ["B", "KB", "MB", "GB"]:
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return f"{n:.1f} TB"


def make_qr_svg(url):
    """Return SVG bytes for a QR code encoding url, or None if unavailable."""
    if not _HAS_QRCODE:
        return None
    qr = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10, border=2,
    )
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(image_factory=qrsvg.SvgPathImage)
    buf = io.BytesIO()
    img.save(buf)
    return buf.getvalue()


def file_icon(name, is_dir):
    if is_dir:
        return "📁"
    ext = Path(name).suffix.lower()
    if ext in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".heic"}:
        return "🖼️"
    if ext in {".mp4", ".mov", ".avi", ".mkv", ".webm"}:
        return "🎬"
    if ext in {".mp3", ".wav", ".flac", ".aac", ".m4a"}:
        return "🎵"
    if ext in {".pdf"}:
        return "📄"
    if ext in {".zip", ".tar", ".gz", ".7z", ".rar"}:
        return "🗜️"
    if ext in {".py", ".js", ".ts", ".html", ".css", ".json", ".sh", ".md"}:
        return "📝"
    return "📄"


# ── CSS / JS ─────────────────────────────────────────────────────────────────

CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
       background: #0f0f0f; color: #e0e0e0; min-height: 100vh; }
header { background: #2e1a1a; padding: 16px 20px; border-bottom: 1px solid #333;
         position: sticky; top: 0; z-index: 10; }
.header-top { display: flex; align-items: center; gap: 16px; margin-bottom: 10px; }
header h1 { font-size: 1.2rem; font-weight: 800; color: #ff6b6b; white-space: nowrap; }
.tabs { display: flex; gap: 8px; }
.tab { padding: 6px 16px; border-radius: 20px; font-size: 0.85rem; font-weight: 600;
       text-decoration: none; border: 1px solid #333; color: #888; transition: all 0.2s; }
.tab.active { background: #ff6b6b; color: #000; border-color: #ff6b6b; }
.tab:hover:not(.active) { border-color: #555; color: #ccc; }
.breadcrumb { font-size: 0.8rem; color: #888; word-break: break-all; }
.breadcrumb a { color: #ff6b6b; text-decoration: none; }
.breadcrumb a:hover { text-decoration: underline; }
.container { padding: 16px; max-width: 900px; margin: 0 auto; }

/* upload box */
.upload-box { background: #2e1a1a; border: 2px dashed #333; border-radius: 12px;
              padding: 20px; margin-bottom: 20px; text-align: center; }
.upload-box input[type=file] { display: none; }
.upload-box label { cursor: pointer; display: inline-block; background: #ff6b6b;
                    color: #000; padding: 10px 20px; border-radius: 8px; font-weight: 600; font-size: 0.9rem; }
.upload-box .hint { margin-top: 8px; font-size: 0.8rem; color: #666; }
.upload-form button { margin-top: 10px; background: #22c55e; color: #000;
                       border: none; padding: 10px 24px; border-radius: 8px;
                       font-weight: 600; cursor: pointer; font-size: 0.9rem; display: none; }
.upload-form button.visible { display: inline-block; }
.upload-progress { margin-top: 12px; display: none; }
.upload-progress.visible { display: block; }

/* file list */
.file-list { background: #2e1a1a; border-radius: 12px; overflow: hidden; border: 1px solid #3e2a2a; }
.file-row { border-bottom: 1px solid #222; }
.file-row:last-child { border-bottom: none; }
.file-item-wrap { display: flex; align-items: center; transition: background 0.15s; }
.file-item-wrap:hover { background: #402525; }
.file-item { display: flex; align-items: center; gap: 12px; flex: 1;
             padding: 14px 16px; text-decoration: none; color: inherit; }
.file-item-wrap .dl-btn { margin-right: 12px; flex-shrink: 0; }
.file-icon { font-size: 1.3rem; flex-shrink: 0; width: 28px; text-align: center; }
.file-name { flex: 1; font-size: 0.95rem; word-break: break-all; }
.file-meta { font-size: 0.75rem; color: #666; flex-shrink: 0; margin-right: 8px; }
.dl-btn { flex-shrink: 0; background: #4a2a2a; border: 1px solid #444; color: #ff6b6b;
           padding: 6px 12px; border-radius: 8px; font-size: 0.8rem; font-weight: 600;
           cursor: pointer; transition: all 0.2s; white-space: nowrap; }
.dl-btn:hover { background: #ff6b6b; color: #000; border-color: #ff6b6b; }
.dl-btn:disabled { background: #2e1a1a; color: #555; border-color: #333; cursor: default; }

/* per-file progress */
.file-progress { padding: 0 16px 12px 56px; display: none; }
.file-progress.visible { display: block; }
.prog-bar { height: 5px; background: #222; border-radius: 3px; overflow: hidden; margin-bottom: 5px; }
.prog-fill { height: 100%; background: #ff6b6b; width: 0%; border-radius: 3px; transition: width 0.3s; }
.prog-fill.done { background: #22c55e; }
.prog-fill.error { background: #ef4444; }
.prog-text { font-size: 0.75rem; color: #888; }

/* shared progress bar */
.progress-bar { height: 6px; background: #333; border-radius: 3px; overflow: hidden; }
.progress-fill { height: 100%; background: #ff6b6b; width: 0%; transition: width 0.2s; border-radius: 3px; }
.progress-text { font-size: 0.8rem; color: #888; margin-top: 6px; }

.empty { text-align: center; padding: 40px; color: #555; }
.badge { font-size: 0.7rem; background: #22c55e; color: #000; padding: 2px 7px;
         border-radius: 10px; font-weight: 700; margin-left: 4px; vertical-align: middle; }
.space-info { font-size: 0.75rem; color: #555; text-align: right; margin-bottom: 8px; }

/* wireless connect panel */
.wifi-panel { background: #2e1a1a; border: 1px solid #3e2a2a; border-radius: 12px;
              padding: 20px; margin-bottom: 16px; }
.wifi-panel h3 { font-size: 0.95rem; font-weight: 700; color: #ff6b6b; margin-bottom: 4px; }
.wifi-panel p  { font-size: 0.82rem; color: #888; margin-bottom: 14px; line-height: 1.5; }
.wifi-row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin-bottom: 10px; }
.wifi-row input { flex: 1; min-width: 140px; background: #1a0f0f; border: 1px solid #333;
                  border-radius: 8px; color: #e0e0e0; padding: 9px 12px;
                  font-size: 0.85rem; outline: none; }
.wifi-row input:focus { border-color: #ff6b6b; }
.wifi-row input::placeholder { color: #555; }
.wifi-row input.short { max-width: 90px; }
.wifi-btn { background: #ff6b6b; color: #000; border: none; padding: 9px 18px;
            border-radius: 8px; font-weight: 700; font-size: 0.85rem; cursor: pointer;
            white-space: nowrap; transition: background 0.2s; }
.wifi-btn:hover { background: #ff9494; }
.wifi-btn.secondary { background: #4a2a2a; color: #ff6b6b; border: 1px solid #444; }
.wifi-btn.secondary:hover { background: #5a2a2a; }
.wifi-btn.danger { background: #2a1a1a; color: #ef4444; border: 1px solid #5a2a2a; }
.wifi-btn.danger:hover { background: #3a1a1a; }
.wifi-status { font-size: 0.8rem; margin-top: 8px; padding: 7px 12px; border-radius: 7px;
               display: none; }
.wifi-status.ok  { background: #0a2a1a; color: #22c55e; border: 1px solid #1a4a2a; display: block; }
.wifi-status.err { background: #2a0a0a; color: #ef4444; border: 1px solid #4a1a1a; display: block; }
.wifi-status.info { background: #2e0a0a; color: #ff6b6b; border: 1px solid #4a1a1a; display: block; }
.wifi-section { margin-bottom: 14px; }
.wifi-section-title { font-size: 0.78rem; font-weight: 700; color: #666; text-transform: uppercase;
                      letter-spacing: 0.05em; margin-bottom: 8px; }
.wifi-divider { border: none; border-top: 1px solid #3e2a2a; margin: 14px 0; }
.wifi-devices { margin-top: 10px; }
.wifi-device-row { display: flex; align-items: center; gap: 10px; padding: 8px 10px;
                   background: #1a0f0f; border-radius: 8px; margin-bottom: 6px;
                   font-size: 0.82rem; }
.wifi-device-icon { font-size: 1rem; }
.wifi-device-name { flex: 1; color: #ccc; }
.wifi-device-type { font-size: 0.72rem; color: #555; }
.wifi-connected-banner { background: #0a2a1a; border: 1px solid #1a4a2a; border-radius: 10px;
                         padding: 10px 14px; margin-bottom: 14px; display: flex;
                         align-items: center; gap: 10px; font-size: 0.82rem; color: #22c55e; }
.wifi-connected-banner span { flex: 1; }
.collapsible-toggle { background: none; border: none; color: #ff6b6b; font-size: 0.8rem;
                      cursor: pointer; padding: 0; text-decoration: underline; }
.pair-section { margin-top: 12px; }

/* search */
.search-wrap { position: relative; flex: 1; max-width: 320px; }
.search-wrap input { width: 100%; background: #1a0f0f; border: 1px solid #333; border-radius: 8px;
                     color: #e0e0e0; padding: 7px 32px 7px 12px; font-size: 0.85rem; outline: none; }
.search-wrap input:focus { border-color: #ff6b6b; }
.search-wrap input::placeholder { color: #555; }
.search-clear { position: absolute; right: 8px; top: 50%; transform: translateY(-50%);
                background: none; border: none; color: #666; cursor: pointer; font-size: 1rem;
                display: none; line-height: 1; }
.search-clear.visible { display: block; }
.search-results { background: #2e1a1a; border-radius: 12px; border: 1px solid #3e2a2a;
                  margin-top: 0; overflow: hidden; }
.search-results .file-row { border-bottom: 1px solid #222; }
.search-results .file-row:last-child { border-bottom: none; }
.search-hint { font-size: 0.78rem; color: #555; padding: 6px 0 10px; }
.hidden { display: none !important; }

/* readme modal */
.modal-backdrop { position: fixed; inset: 0; background: rgba(0,0,0,0.75);
                  z-index: 100; display: flex; align-items: center; justify-content: center;
                  padding: 20px; }
.modal-backdrop.hidden { display: none !important; }
.modal { background: #2e1a1a; border: 1px solid #333; border-radius: 16px;
         max-width: 640px; width: 100%; max-height: 80vh; display: flex; flex-direction: column;
         box-shadow: 0 24px 80px rgba(0,0,0,0.6); }
.modal-header { display: flex; align-items: center; justify-content: space-between;
                padding: 18px 20px; border-bottom: 1px solid #3e2a2a; flex-shrink: 0; }
.modal-header h2 { font-size: 1rem; font-weight: 700; color: #ff6b6b; }
.modal-close { background: none; border: none; color: #888; font-size: 1.4rem;
               cursor: pointer; line-height: 1; padding: 0 4px; }
.modal-close:hover { color: #e0e0e0; }
.modal-body { overflow-y: auto; padding: 20px; font-size: 0.85rem; line-height: 1.7; color: #ccc; }
.modal-body h2 { font-size: 0.95rem; color: #ff6b6b; margin: 18px 0 6px; }
.modal-body h2:first-child { margin-top: 0; }
.modal-body p { margin-bottom: 10px; }
.modal-body ul, .modal-body ol { padding-left: 20px; margin-bottom: 10px; }
.modal-body li { margin-bottom: 4px; }
.modal-body code { background: #1a0f0f; border: 1px solid #3e2a2a; border-radius: 4px;
                   padding: 1px 6px; font-family: monospace; font-size: 0.82rem; color: #ffaaaa; }
.modal-body pre { background: #1a0f0f; border: 1px solid #3e2a2a; border-radius: 8px;
                  padding: 12px; overflow-x: auto; margin-bottom: 12px; }
.modal-body pre code { background: none; border: none; padding: 0; }
.modal-body hr { border: none; border-top: 1px solid #3e2a2a; margin: 16px 0; }
.modal-footer { padding: 14px 20px; border-top: 1px solid #3e2a2a; text-align: right; flex-shrink: 0; }
.modal-footer button { background: #ff6b6b; color: #000; border: none; padding: 9px 24px;
                        border-radius: 8px; font-weight: 700; cursor: pointer; font-size: 0.9rem; }
.modal-footer button:hover { background: #ff9494; }
.help-btn { background: none; border: 1px solid #333; color: #888; padding: 5px 12px;
             border-radius: 8px; font-size: 0.8rem; cursor: pointer; transition: all 0.2s; }
.help-btn:hover { border-color: #ff6b6b; color: #ff6b6b; }

/* QR panel */
.qr-wrap { display: flex; flex-direction: column; align-items: center; gap: 10px;
           padding: 16px 0 8px; }
.qr-wrap canvas { border-radius: 8px; background: #fff; padding: 8px; }
.qr-label { font-size: 0.78rem; color: #666; text-align: center; }
.qr-url   { font-size: 0.72rem; color: #444; word-break: break-all; text-align: center; margin-top: 2px; }
"""

JS = r"""
// ── Upload form ───────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  const input = document.getElementById('fileInput');
  if (input) {
    const btn   = document.getElementById('uploadBtn');
    const label = document.getElementById('fileLabel');
    const prog  = document.getElementById('uploadProgress');
    const fill  = prog && prog.querySelector('.progress-fill');
    const ptext = prog && prog.querySelector('.progress-text');

    input.addEventListener('change', () => {
      if (input.files.length > 0) {
        label.textContent = input.files.length === 1 ? input.files[0].name : `${input.files.length} files selected`;
        btn.classList.add('visible');
      }
    });

    const isAndroid = window.location.pathname.startsWith('/android');

    function pollPushProgress(token) {
      fetch('/api/download?token=' + token)
        .then(r => r.json())
        .then(s => {
          const pct = s.progress || 0;
          if (fill) fill.style.width = pct + '%';
          if (ptext) ptext.textContent = `Pushing to phone… ${pct}%`;
          if (s.status === 'done') {
            window.location.reload();
          } else if (s.status === 'error') {
            if (ptext) ptext.textContent = 'Push failed: ' + (s.error || 'unknown error');
          } else {
            setTimeout(() => pollPushProgress(token), 500);
          }
        })
        .catch(() => setTimeout(() => pollPushProgress(token), 1000));
    }

    document.getElementById('uploadForm').addEventListener('submit', (e) => {
      e.preventDefault();
      const fd = new FormData();
      for (const f of input.files) fd.append('files', f);
      const xhr = new XMLHttpRequest();
      xhr.open('POST', window.location.href);
      if (!isAndroid) {
        xhr.upload.addEventListener('progress', (ev) => {
          if (ev.lengthComputable && fill) {
            const pct = Math.round((ev.loaded / ev.total) * 100);
            fill.style.width = pct + '%';
            ptext.textContent = `Uploading… ${pct}%`;
            prog.classList.add('visible');
          }
        });
      } else {
        if (prog) prog.classList.add('visible');
        if (ptext) ptext.textContent = 'Sending to server…';
      }
      xhr.addEventListener('load', () => {
        if (xhr.status === 200) {
          if (isAndroid) {
            try {
              const res = JSON.parse(xhr.responseText);
              if (res.token) { pollPushProgress(res.token); return; }
            } catch(_) {}
          }
          window.location.reload();
        } else if (ptext) {
          ptext.textContent = 'Upload failed: ' + xhr.statusText;
        }
      });
      xhr.send(fd);
    });
  }

  // ── Live local filter ──────────────────────────────────────────────────────
  const searchInput = document.getElementById('searchInput');
  const clearBtn    = document.getElementById('searchClear');
  const fileList    = document.getElementById('fileList');
  const searchRes   = document.getElementById('searchResults');
  const searchHint  = document.getElementById('searchHint');

  if (!searchInput) return;

  let searchTimer = null;

  searchInput.addEventListener('input', () => {
    const q = searchInput.value.trim();
    clearBtn.classList.toggle('visible', q.length > 0);

    // Hide current folder listing while searching, show it when cleared
    if (fileList) fileList.classList.toggle('hidden', q.length > 0);

    clearTimeout(searchTimer);
    if (q.length === 0) {
      if (searchRes) searchRes.innerHTML = '';
      if (searchHint) searchHint.classList.add('hidden');
      return;
    }
    if (q.length < 2) {
      if (searchRes) searchRes.innerHTML = '<div class="empty">Type at least 2 characters…</div>';
      return;
    }
    if (searchRes) searchRes.innerHTML = '<div class="empty">Searching…</div>';
    searchTimer = setTimeout(() => deepSearch(q), 400);
  });

  clearBtn.addEventListener('click', () => {
    searchInput.value = '';
    clearBtn.classList.remove('visible');
    if (fileList) fileList.classList.remove('hidden');
    if (searchRes) searchRes.innerHTML = '';
    if (searchHint) searchHint.classList.add('hidden');
  });
});

// ── Deep search (Android) ─────────────────────────────────────────────────────
function deepSearch(q) {
  const searchRes  = document.getElementById('searchResults');
  const searchHint = document.getElementById('searchHint');
  if (!searchRes) return;

  searchRes.innerHTML = '<div class="empty">Searching…</div>';
  if (searchHint) searchHint.classList.remove('hidden');

  fetch('/api/search?q=' + encodeURIComponent(q))
    .then(r => r.json())
    .then(data => {
      if (!data.results || data.results.length === 0) {
        searchRes.innerHTML = '<div class="empty">No results found</div>';
        return;
      }
      const rows = data.results.map(item => {
        const name    = item.name;
        const isDir   = item.is_dir;
        const size    = item.size;
        const path    = item.path;
        const urlPath = '/android' + path.replace('/sdcard', '');
        const icon    = isDir ? '📁' : fileIconFor(name);
        const meta    = isDir ? '' : fmtSize(size);
        const dlBtn   = (!isDir)
          ? `<button class="dl-btn" onclick="dlFile(this,${JSON.stringify(path)},${JSON.stringify(name)},${size})">⬇ Save</button>`
          : `<button class="dl-btn" onclick="dlFile(this,${JSON.stringify(path)},${JSON.stringify(name)},0,true)">⬇ Save</button>`;
        const progHtml = `<div class="file-progress"><div class="prog-bar"><div class="prog-fill"></div></div><div class="prog-text"></div></div>`;
        return `<div class="file-row" data-name="${escHtml(name)}">
          <div class="file-item-wrap">
            <a class="file-item" href="${escHtml(urlPath)}">
              <span class="file-icon">${icon}</span>
              <span class="file-name">${escHtml(name)}</span>
              <span class="file-meta">${meta}</span>
            </a>${dlBtn}
          </div>${progHtml}</div>`;
      }).join('\n');
      searchRes.innerHTML = rows;
    })
    .catch(() => { searchRes.innerHTML = '<div class="empty">Search failed</div>'; });
}

function fileIconFor(name) {
  const ext = name.split('.').pop().toLowerCase();
  if (['jpg','jpeg','png','gif','webp','svg','heic'].includes(ext)) return '🖼️';
  if (['mp4','mov','avi','mkv','webm'].includes(ext)) return '🎬';
  if (['mp3','wav','flac','aac','m4a'].includes(ext)) return '🎵';
  if (ext === 'pdf') return '📄';
  if (['zip','tar','gz','7z','rar'].includes(ext)) return '🗜️';
  return '📄';
}

function fmtSize(n) {
  const units = ['B','KB','MB','GB'];
  for (const u of units) { if (n < 1024) return n.toFixed(1) + ' ' + u; n /= 1024; }
  return n.toFixed(1) + ' TB';
}

function escHtml(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// ── README Modal ──────────────────────────────────────────────────────────────
function openModal()  { document.getElementById('readmeModal').classList.remove('hidden'); }
function closeModal() { document.getElementById('readmeModal').classList.add('hidden'); }

// Close on backdrop click
document.getElementById('readmeModal').addEventListener('click', (e) => {
  if (e.target === e.currentTarget) closeModal();
});
// Close on Escape
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeModal(); });

// ── Per-item download with progress ──────────────────────────────────────────
function dlFile(btn, androidPath, fileName, fileSize, isFolder) {
  btn.disabled = true;
  btn.textContent = 'Checking…';

  const row   = btn.closest('.file-row');
  const prog  = row.querySelector('.file-progress');
  const fill  = prog.querySelector('.prog-fill');
  const ptext = prog.querySelector('.prog-text');
  prog.classList.add('visible');

  fetch('/api/space?need=' + fileSize)
    .then(r => r.json())
    .then(info => {
      if (!info.ok) {
        fill.classList.add('error'); fill.style.width = '100%';
        ptext.textContent = `Not enough space. Need ${info.need_fmt}, only ${info.free_fmt} free.`;
        btn.textContent = 'No space';
        return;
      }

      btn.textContent = '⏹ Stop';
      btn.disabled = false;
      ptext.textContent = 'Starting…';

      const url = '/api/download?path=' + encodeURIComponent(androidPath)
                + '&name=' + encodeURIComponent(fileName)
                + (isFolder ? '&folder=1' : '');

      fetch(url).then(r => r.json()).then(job => {
        if (job.error) {
          fill.classList.add('error'); fill.style.width = '100%';
          ptext.textContent = 'Error: ' + job.error;
          btn.textContent = '↩ Again'; btn.disabled = false;
          btn.onclick = () => { resetProgress(fill, ptext); dlFile(btn, androidPath, fileName, fileSize, isFolder); };
          return;
        }
        const token = job.token;

        btn.onclick = () => {
          fetch('/api/cancel?token=' + token);
          clearInterval(iv);
          fill.classList.add('error'); fill.style.width = '100%';
          ptext.textContent = 'Cancelled';
          btn.textContent = '↩ Again'; btn.disabled = false;
          btn.onclick = () => { resetProgress(fill, ptext); dlFile(btn, androidPath, fileName, fileSize, isFolder); };
        };

        const iv = setInterval(() => {
          fetch('/api/download?token=' + token).then(r => r.json()).then(s => {
            const pct = s.progress || 0;
            fill.style.width = pct + '%';
            if (s.status === 'done') {
              clearInterval(iv);
              fill.classList.add('done'); fill.style.width = '100%';
              ptext.textContent = `Saved to ~/Downloads/${s.name || fileName}`;
              btn.textContent = '↩ Again'; btn.disabled = false;
              btn.onclick = () => { resetProgress(fill, ptext); dlFile(btn, androidPath, fileName, fileSize, isFolder); };
            } else if (s.status === 'error' || s.status === 'cancelled') {
              clearInterval(iv);
              fill.classList.add('error'); fill.style.width = '100%';
              ptext.textContent = s.status === 'cancelled' ? 'Cancelled' : 'Download failed';
              btn.textContent = '↩ Again'; btn.disabled = false;
              btn.onclick = () => { resetProgress(fill, ptext); dlFile(btn, androidPath, fileName, fileSize, isFolder); };
            } else {
              btn.textContent = '⏹ Stop';
              ptext.textContent = pct + '%' + (isFolder ? ' (pulling + zipping)' : '');
            }
          });
        }, 500);
      });
    })
    .catch(err => {
      ptext.textContent = 'Error: ' + err;
      btn.textContent = '↩ Again'; btn.disabled = false;
      btn.onclick = () => { resetProgress(fill, ptext); dlFile(btn, androidPath, fileName, fileSize, isFolder); };
    });
}

function resetProgress(fill, ptext) {
  fill.className = 'prog-fill';
  fill.style.width = '0%';
  ptext.textContent = '';
}

// ── Wireless adb connect ──────────────────────────────────────────────────────
function wifiConnect() {
  const ip   = document.getElementById('wifiIp').value.trim();
  const port = document.getElementById('wifiPort').value.trim() || '5555';
  const st   = document.getElementById('wifiConnectStatus');
  if (!ip) { showWifiStatus(st, 'err', 'Enter the phone IP address first.'); return; }
  showWifiStatus(st, 'info', 'Connecting…');
  fetch('/api/connect', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ip, port})
  }).then(r => r.json()).then(d => {
    if (d.success) {
      showWifiStatus(st, 'ok', '✓ ' + d.message);
      setTimeout(() => window.location.reload(), 800);
    } else {
      showWifiStatus(st, 'err', '✗ ' + d.message);
    }
  }).catch(() => showWifiStatus(st, 'err', 'Request failed'));
}

function wifiPair() {
  const ip   = document.getElementById('wifiIp').value.trim();
  const pp   = document.getElementById('pairPort').value.trim();
  const code = document.getElementById('pairCode').value.trim();
  const st   = document.getElementById('wifiPairStatus');
  if (!ip || !pp || !code) { showWifiStatus(st, 'err', 'Fill in all three fields.'); return; }
  showWifiStatus(st, 'info', 'Pairing… (may take a few seconds)');
  fetch('/api/pair', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ip, pair_port: pp, pair_code: code})
  }).then(r => r.json()).then(d => {
    if (d.success) {
      showWifiStatus(st, 'ok', '✓ Paired! Now tap Connect.');
    } else {
      showWifiStatus(st, 'err', '✗ ' + d.message);
    }
  }).catch(() => showWifiStatus(st, 'err', 'Request failed'));
}

function wifiDisconnect() {
  fetch('/api/connect', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({disconnect: true})
  }).then(() => window.location.reload());
}

function showWifiStatus(el, cls, msg) {
  el.className = 'wifi-status ' + cls;
  el.textContent = msg;
}

function togglePairSection() {
  const s = document.getElementById('pairSection');
  const btn = document.getElementById('pairToggle');
  if (s.style.display === 'none' || !s.style.display) {
    s.style.display = 'block';
    btn.textContent = 'Hide pairing';
  } else {
    s.style.display = 'none';
    btn.textContent = 'Android 11+? Pair first';
  }
}

function toggleConnectPanel() {
  const panel = document.getElementById('connectPanel');
  const btn   = document.getElementById('connectPanelToggle');
  if (!panel) return;
  if (panel.style.display === 'none') {
    panel.style.display = 'block';
    if (btn) btn.textContent = 'Hide';
  } else {
    panel.style.display = 'none';
    if (btn) btn.textContent = 'Manage connection';
  }
}

// Auto-fill port hint when IP changes
document.addEventListener('DOMContentLoaded', () => {
  const ipInput = document.getElementById('wifiIp');
  if (ipInput) {
    ipInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') wifiConnect();
    });
  }
});
"""


# ── Page renderer ─────────────────────────────────────────────────────────────

def render_page_error(breadcrumb_html, message):
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FileTransfer</title>
<style>{CSS}</style>
</head>
<body>
<header>
  <div class="header-top">
    <h1>📡 FileTransfer Wireless</h1>
    <div class="tabs">
      <a class="tab" href="/">💻 Mac</a>
      <a class="tab active" href="{ANDROID_PREFIX}">📱 Android <span class="badge">USB</span></a>
    </div>
  </div>
  <div class="breadcrumb">{breadcrumb_html}</div>
</header>
<div class="container">
  <div class="file-list">
    <div class="empty" style="color:#ef4444;">🔒 {html.escape(message)}</div>
  </div>
</div>
<script>{JS}</script>
</body>
</html>""".encode()


def render_wireless_panel(devices, connected, mac_ip, port):
    """Return the HTML for the wireless adb connect panel."""
    device_rows = ""
    for d in devices:
        icon = "📱" if d["type"] == "wifi" else "🔌"
        label = "Wi-Fi" if d["type"] == "wifi" else "USB"
        device_rows += (
            f'<div class="wifi-device-row">'
            f'<span class="wifi-device-icon">{icon}</span>'
            f'<span class="wifi-device-name">{html.escape(d["serial"])}</span>'
            f'<span class="wifi-device-type">{label}</span>'
            f'</div>'
        )

    connected_banner = ""
    if connected:
        wifi_devices = [d for d in devices if d["type"] == "wifi"]
        label = wifi_devices[0]["serial"] if wifi_devices else "device"
        connected_banner = f"""
<div class="wifi-connected-banner">
  <span>📶 Connected wirelessly to <strong>{html.escape(label)}</strong></span>
  <button class="wifi-btn danger" style="padding:5px 12px;font-size:0.78rem;" onclick="wifiDisconnect()">Disconnect</button>
</div>"""

    panel_style  = 'style="display:none"' if connected else ""
    connect_url  = f"http://{mac_ip}:{port}{AUTOCONNECT_PATH}"
    qr_url_js    = json.dumps(connect_url)

    qr_img_src = f"/api/qr?url={urllib.parse.quote(connect_url, safe='')}"
    qr_section = f"""
    <div class="wifi-section">
      <div class="wifi-section-title">Step 1 — Scan QR on your phone</div>
      <div class="qr-wrap">
        <img src="{html.escape(qr_img_src)}" width="180" height="180" alt="QR code" style="border-radius:8px;background:#fff;padding:8px;">
        <div class="qr-label">Scan with your phone camera to connect automatically</div>
        <div class="qr-url">{html.escape(connect_url)}</div>
      </div>
    </div>
    <hr class="wifi-divider">"""

    return f"""
<div class="wifi-panel">
  <h3>📶 Wireless Connection</h3>
  <p>Both devices must be on the same Wi-Fi. No cable, no app, no internet needed.</p>
  {connected_banner}
  {"" if not connected else f'<button class="collapsible-toggle" onclick="toggleConnectPanel()" id="connectPanelToggle">Manage connection</button>'}
  <div id="connectPanel" {panel_style}>
    {qr_section}
    <div class="wifi-section">
      <div class="wifi-section-title">Or enter details manually</div>
      <div class="wifi-row">
        <input id="wifiIp"   type="text" placeholder="Phone IP  e.g. 192.168.1.42" autocomplete="off">
        <input id="wifiPort" type="text" placeholder="Port (5555)" class="short" value="5555">
        <button class="wifi-btn" onclick="wifiConnect()">Connect</button>
      </div>
      <div id="wifiConnectStatus" class="wifi-status"></div>
    </div>
    <hr class="wifi-divider">
    <div class="wifi-section">
      <button class="collapsible-toggle" onclick="togglePairSection()" id="pairToggle">Android 11+? Pair first</button>
      <div class="pair-section" id="pairSection" style="display:none">
        <p style="font-size:0.8rem;color:#888;margin:8px 0;">
          On phone: Wireless debugging → <strong>Pair device with pairing code</strong>.<br>
          Enter the IP, the pairing port shown, and the 6-digit code.
        </p>
        <div class="wifi-row">
          <input id="pairPort" type="text" placeholder="Pair port e.g. 37425" class="short">
          <input id="pairCode" type="text" placeholder="6-digit code" class="short" maxlength="6">
          <button class="wifi-btn secondary" onclick="wifiPair()">Pair</button>
        </div>
        <div id="wifiPairStatus" class="wifi-status"></div>
      </div>
    </div>
    {('<hr class="wifi-divider"><div class="wifi-section"><div class="wifi-section-title">Connected devices</div><div class="wifi-devices">' + device_rows + '</div></div>') if device_rows else ""}
  </div>
</div>"""


def render_page(breadcrumb_html, entries, current_url, mode, upload=True, android_ok=True, devices=None, mac_ip="", port=PORT):
    url_path = current_url.rstrip("/")

    if mode == "android":
        parent_rel = "/".join(url_path.replace(ANDROID_PREFIX, "").strip("/").split("/")[:-1])
        parent_url = None if url_path in (ANDROID_PREFIX, ANDROID_PREFIX + "/") else ANDROID_PREFIX + "/" + parent_rel
    else:
        rel = url_path.lstrip("/")
        parent_url = None if not rel else "/" + "/".join(rel.split("/")[:-1])

    rows = []

    if parent_url is not None:
        rows.append(
            f'<div class="file-row">'
            f'<a class="file-item" href="{html.escape(parent_url) or "/"}">'
            f'<span class="file-icon">⬆️</span>'
            f'<span class="file-name">..</span>'
            f'</a></div>'
        )

    for name, is_dir, size in entries:
        if name.startswith("."):
            continue
        href     = url_path + "/" + urllib.parse.quote(name)
        icon     = file_icon(name, is_dir)
        meta     = "" if is_dir else format_size(size)

        if mode == "android":
            android_path = "/sdcard" + url_path[len(ANDROID_PREFIX):] + "/" + name
            ap_js  = html.escape(json.dumps(android_path))
            nm_js  = html.escape(json.dumps(name))
            if is_dir:
                dl_btn = (
                    f'<button class="dl-btn" '
                    f'onclick="dlFile(this,{ap_js},{nm_js},0,true)">'
                    f'⬇ Save</button>'
                )
            else:
                dl_btn = (
                    f'<button class="dl-btn" '
                    f'onclick="dlFile(this,{ap_js},{nm_js},{size})">'
                    f'⬇ Save</button>'
                )
            prog_html = (
                f'<div class="file-progress">'
                f'<div class="prog-bar"><div class="prog-fill"></div></div>'
                f'<div class="prog-text"></div>'
                f'</div>'
            )
        else:
            dl_btn    = ""
            prog_html = ""

        rows.append(
            f'<div class="file-row" data-name="{html.escape(name.lower())}">'
            f'<div class="file-item-wrap">'
            f'<a class="file-item" href="{html.escape(href)}">'
            f'<span class="file-icon">{icon}</span>'
            f'<span class="file-name">{html.escape(name)}</span>'
            f'<span class="file-meta">{meta}</span>'
            f'</a>'
            f'{dl_btn}'
            f'</div>'
            f'{prog_html}'
            f'</div>'
        )

    file_list = "\n".join(rows) if rows else '<div class="empty">Empty folder</div>'
    mac_active   = "" if mode == "android" else " active"
    and_active   = " active" if mode == "android" else ""
    deep_search  = "true" if mode == "android" else "false"
    android_tab  = f'<a class="tab{and_active}" href="{ANDROID_PREFIX}">📱 Android <span class="badge">Wi-Fi</span></a>'

    free = free_bytes()
    space_line = f'<div class="space-info">Mac free space: {format_size(free)}</div>'

    upload_html = ""
    if upload:
        upload_html = f"""
  <div class="upload-box">
    <form id="uploadForm" class="upload-form" method="POST" enctype="multipart/form-data">
      <input type="file" id="fileInput" name="files" multiple>
      <label for="fileInput" id="fileLabel">Choose files to upload</label>
      <div class="hint">upload to this folder</div>
      <button type="submit" id="uploadBtn">Upload</button>
      <div class="upload-progress" id="uploadProgress">
        <div class="progress-bar"><div class="progress-fill"></div></div>
        <div class="progress-text"></div>
      </div>
    </form>
  </div>"""

    search_results_html = (
        '<div class="search-hint hidden" id="searchHint" style="font-size:0.78rem;color:#555;padding:6px 0 10px;">Deep search results (across entire device):</div>'
        '<div class="search-results" id="searchResults"></div>'
    ) if mode == "android" else ""

    wireless_panel_html = render_wireless_panel(devices or [], android_ok, mac_ip, port) if mode == "android" else ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FileTransfer</title>
<style>{CSS}</style>
</head>
<body>

<!-- README Modal -->
<div class="modal-backdrop hidden" id="readmeModal">
  <div class="modal">
    <div class="modal-header">
      <h2>📡 FileTransfer Wireless — Quick Start</h2>
      <button class="modal-close" onclick="closeModal()">✕</button>
    </div>
    <div class="modal-body">
      <h2>Requirements</h2>
      <ul>
        <li>macOS + Python 3 (pre-installed)</li>
        <li>Android phone on the <strong>same Wi-Fi network</strong></li>
        <li><code>adb</code> installed on Mac: <code>brew install android-platform-tools</code></li>
      </ul>
      <h2>Enable Wireless Debugging (one time)</h2>
      <ol>
        <li><strong>Settings → About Phone</strong> — tap <strong>Build Number</strong> 7 times to unlock Developer Options</li>
        <li><strong>Settings → Developer Options → Wireless Debugging</strong> — turn ON</li>
        <li>Note your phone's <strong>IP address</strong> shown there (e.g. 192.168.1.42) and the port (usually 5555)</li>
      </ol>
      <h2>Connect (Android 10 and below)</h2>
      <p>In the <strong>📱 Android</strong> tab, enter your phone's IP and tap <strong>Connect</strong>. Done.</p>
      <h2>Pair + Connect (Android 11+)</h2>
      <ol>
        <li>In Wireless Debugging, tap <strong>Pair device with pairing code</strong></li>
        <li>Note the pairing port and 6-digit code shown</li>
        <li>In the Android tab, expand <em>Android 11+? Pair first</em>, enter the pairing port and code, tap <strong>Pair</strong></li>
        <li>Then enter the main IP + port 5555 and tap <strong>Connect</strong></li>
      </ol>
      <h2>Features</h2>
      <ul>
        <li><strong>💻 Mac tab</strong> — browse your Mac filesystem, upload files from any device on the network</li>
        <li><strong>📱 Android tab</strong> — browse phone storage wirelessly over Wi-Fi</li>
        <li><strong>⬇ Save</strong> — download any file or folder to <code>~/Downloads</code> with progress bar</li>
        <li><strong>Search</strong> — searches entire phone storage recursively (type 2+ chars)</li>
        <li><strong>Upload</strong> — send files from Mac → Android or vice versa</li>
        <li>Checks free disk space before every download</li>
      </ul>
      <h2>Start / Stop</h2>
      <pre><code>python3 server.py        # start
./start.sh ~/Desktop     # share specific folder
kill $(lsof -ti:8765)    # stop</code></pre>
      <hr>
      <p style="color:#555;font-size:0.78rem;">Everything runs locally — nothing leaves your network. Mac and phone must be on the same Wi-Fi.</p>
    </div>
    <div class="modal-footer">
      <button onclick="closeModal()">Got it, let's go</button>
    </div>
  </div>
</div>

<header>
  <div class="header-top">
    <h1>📡 FileTransfer Wireless</h1>
    <div class="tabs">
      <a class="tab{mac_active}" href="/">💻 Mac</a>
      {android_tab}
    </div>
    <div class="search-wrap">
      <input id="searchInput" type="search" placeholder="Search files &amp; folders…" autocomplete="off">
      <button class="search-clear" id="searchClear" title="Clear">✕</button>
    </div>
    <button class="help-btn" onclick="openModal()">📖 Instructions</button>
  </div>
  <div class="breadcrumb">{breadcrumb_html}</div>
</header>
<div class="container">
{wireless_panel_html}
{upload_html}
{space_line}
{search_results_html}
  <div class="file-list" id="fileList">
{file_list}
  </div>
</div>
<script>{JS}</script>
</body>
</html>""".encode()


# ── Request handler ───────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"  {self.address_string()} → {fmt % args}")

    def do_GET(self):
        raw = urllib.parse.unquote(self.path.split("?")[0])
        qs  = urllib.parse.parse_qs(self.path.split("?")[1] if "?" in self.path else "")

        if raw == SPACE_API:
            self.handle_space(qs)
        elif raw == SEARCH_API:
            self.handle_search(qs)
        elif raw == CANCEL_API:
            self.handle_cancel(qs)
        elif raw == DOWNLOAD_API:
            self.handle_download_api(qs)
        elif raw == ADB_STATUS_API:
            self.handle_adb_status()
        elif raw == QR_API:
            self.handle_qr(qs)
        elif raw == MDNS_API:
            self.handle_mdns_discover(qs)
        elif raw == AUTOCONNECT_PATH:
            self.handle_autoconnect()
        elif raw.startswith(ANDROID_PREFIX):
            self.handle_android_get(raw)
        else:
            self.handle_mac_get(raw)

    def do_POST(self):
        raw = urllib.parse.unquote(self.path.split("?")[0])
        if raw == CONNECT_API:
            self.handle_connect_post()
        elif raw == PAIR_API:
            self.handle_pair_post()
        else:
            self._do_upload_post(raw)

    # ── /api/space ────────────────────────────────────────────

    def handle_space(self, qs):
        need = int(qs.get("need", ["0"])[0])
        free = free_bytes()
        ok   = free >= need
        body = json.dumps({
            "ok":       ok,
            "free":     free,
            "free_fmt": format_size(free),
            "need":     need,
            "need_fmt": format_size(need),
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ── /api/search ───────────────────────────────────────────

    def handle_search(self, qs):
        q = qs.get("q", [""])[0].strip()
        if not q:
            self._json({"results": []})
            return
        paths = adb_search(q)
        results = []
        for p in paths:
            name   = Path(p).name
            out, _ = adb_shell(["test", "-d", shlex.quote(p), "&&", "echo", "DIR", "||", "echo", "FILE"])
            is_dir = "DIR" in out
            size   = 0 if is_dir else adb_file_size(p)
            results.append({"path": p, "name": name, "is_dir": is_dir, "size": size})
        self._json({"results": results})

    def _json(self, data):
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ── /api/download ─────────────────────────────────────────

    def handle_download_api(self, qs):
        # Poll existing job
        if "token" in qs:
            token = qs["token"][0]
            with _jobs_lock:
                raw = _jobs.get(token, {"status": "unknown", "progress": 0})
                job = {k: v for k, v in raw.items() if k != "proc"}
            self._json(job)
            return

        # Start new job
        android_path = qs.get("path", [""])[0]
        name         = qs.get("name", [Path(android_path).name])[0]
        is_folder    = qs.get("folder", ["0"])[0] == "1"
        if not android_path:
            self.send_error(400, "Missing path")
            return

        token = os.urandom(8).hex()
        with _jobs_lock:
            _jobs[token] = {"status": "running", "progress": 0, "name": name}

        if is_folder:
            thread = threading.Thread(
                target=adb_folder_pull_tracked, args=(token, android_path, name), daemon=True
            )
        else:
            dest = DOWNLOADS / name
            stem, suffix = Path(name).stem, Path(name).suffix
            counter = 1
            while dest.exists():
                dest = DOWNLOADS / f"{stem}_{counter}{suffix}"
                counter += 1
            thread = threading.Thread(
                target=adb_pull_tracked, args=(token, android_path, dest), daemon=True
            )

        thread.start()
        self._json({"token": token})

    # ── /api/cancel ───────────────────────────────────────────

    def handle_cancel(self, qs):
        token = qs.get("token", [""])[0]
        with _jobs_lock:
            job = _jobs.get(token)
            if job and job.get("status") == "running":
                job["status"] = "cancelled"
                proc = job.get("proc")
                if proc:
                    proc.kill()
                self._json({"ok": True})
                return
        self._json({"ok": False})

    # ── Android browse ────────────────────────────────────────

    def handle_android_get(self, url_path):
        mac_ip   = get_local_ip()
        port_num = self.server.server_address[1]

        if not adb_connected():
            devices = adb_devices_list()
            crumbs = f'<a href="{ANDROID_PREFIX}">📱 Android</a>'
            body = render_page(crumbs, [], url_path, "android", upload=False,
                               android_ok=False, devices=devices, mac_ip=mac_ip, port=port_num)
            self._send_html(body)
            return

        rel          = url_path[len(ANDROID_PREFIX):]
        android_path = "/sdcard" + (rel if rel else "/")

        out, err = adb_shell(["ls", "-ld", shlex.quote(android_path)])
        first = out.strip()

        devices = adb_devices_list()
        if first.startswith("d"):
            entries = adb_ls(android_path)
            parts   = rel.strip("/").split("/") if rel.strip("/") else []
            crumbs  = [f'<a href="{ANDROID_PREFIX}">📱 Android</a>']
            for i, part in enumerate(parts):
                link = ANDROID_PREFIX + "/" + "/".join(parts[:i+1])
                crumbs.append(f'<a href="{html.escape(link)}">{html.escape(part)}</a>')
            body = render_page(" / ".join(crumbs), entries, url_path, "android",
                               android_ok=True, devices=devices, mac_ip=mac_ip, port=port_num)
            self._send_html(body)
        elif not first or "Permission denied" in out + err or "Permission denied" in first:
            parts  = rel.strip("/").split("/") if rel.strip("/") else []
            crumbs = [f'<a href="{ANDROID_PREFIX}">📱 Android</a>']
            for i, part in enumerate(parts):
                link = ANDROID_PREFIX + "/" + "/".join(parts[:i+1])
                crumbs.append(f'<a href="{html.escape(link)}">{html.escape(part)}</a>')
            body = render_page_error(" / ".join(crumbs), "Permission denied — this folder is restricted by Android.")
            self._send_html(body)
        else:
            # It's a file — direct inline view
            self._serve_android_file(android_path)

    def _serve_android_file(self, android_path):
        tmp_path = tempfile.mktemp(suffix=Path(android_path).suffix)
        result   = subprocess.run(["adb", "pull", android_path, tmp_path], capture_output=True)
        if result.returncode != 0:
            self.send_error(500, "adb pull failed")
            return
        fname = Path(android_path).name
        mime, _ = mimetypes.guess_type(fname)
        mime = mime or "application/octet-stream"
        size = os.path.getsize(tmp_path)
        viewable = mime.startswith(("image/", "video/", "audio/", "text/")) or mime == "application/pdf"
        disposition = "inline" if viewable else f'attachment; filename="{html.escape(fname)}"'
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", disposition)
        self.end_headers()
        with open(tmp_path, "rb") as f:
            while chunk := f.read(65536):
                self.wfile.write(chunk)
        os.unlink(tmp_path)

    # ── Mac browse ────────────────────────────────────────────

    def handle_mac_get(self, raw):
        target = (MAC_ROOT / raw.lstrip("/")).resolve()
        if not str(target).startswith(str(MAC_ROOT)):
            self.send_error(403, "Forbidden")
            return
        if not target.exists():
            self.send_error(404, "Not found")
            return
        if target.is_dir():
            self._serve_mac_dir(target, raw)
        else:
            self._serve_mac_file(target)

    def _serve_mac_dir(self, path, raw):
        try:
            items = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except PermissionError:
            self.send_error(403, "Permission denied")
            return
        entries = [(p.name, p.is_dir(), p.stat().st_size if p.is_file() else 0) for p in items]
        parts  = raw.strip("/").split("/") if raw.strip("/") else []
        crumbs = ['<a href="/">💻 Mac</a>']
        for i, part in enumerate(parts):
            link = "/" + "/".join(parts[:i+1])
            crumbs.append(f'<a href="{html.escape(link)}">{html.escape(part)}</a>')
        self._send_html(render_page(" / ".join(crumbs), entries, raw, "mac", android_ok=adb_connected()))

    def _serve_mac_file(self, path):
        mime, _ = mimetypes.guess_type(str(path))
        mime = mime or "application/octet-stream"
        size = path.stat().st_size
        viewable = mime.startswith(("image/", "video/", "audio/", "text/")) or mime == "application/pdf"
        disposition = "inline" if viewable else f'attachment; filename="{html.escape(path.name)}"'
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", disposition)
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        with open(path, "rb") as f:
            while chunk := f.read(65536):
                self.wfile.write(chunk)

    def _send_html(self, body: bytes):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ── /api/adb_status ───────────────────────────────────────

    def handle_adb_status(self):
        devices = adb_devices_list()
        connected = any(d["state"] == "device" for d in devices)
        self._json({"connected": connected, "devices": devices})

    # ── /api/qr ───────────────────────────────────────────────

    def handle_qr(self, qs):
        url = qs.get("url", [""])[0]
        if not url:
            self.send_error(400, "Missing url parameter")
            return
        svg = make_qr_svg(url)
        if svg is None:
            self.send_error(503, "QR library not available")
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/svg+xml")
        self.send_header("Content-Length", str(len(svg)))
        self.send_header("Cache-Control", "max-age=3600")
        self.end_headers()
        self.wfile.write(svg)

    # ── /api/mdns_discover ───────────────────────────────────

    def handle_mdns_discover(self, qs):
        phone_ip = qs.get("ip", [""])[0].strip()
        port = mdns_find_pairing_port(phone_ip)
        if port:
            self._json({"found": True, "port": port})
        else:
            self._json({"found": False})

    # ── /android/connect  (phone scans QR → hits this) ───────

    def handle_autoconnect(self):
        phone_ip = (
            self.headers.get("X-Forwarded-For", "").split(",")[0].strip()
            or self.client_address[0]
        )
        mac_ip   = get_local_ip()
        port_num = self.server.server_address[1]
        api_base = f"http://{mac_ip}:{port_num}"

        page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FileTransfer — Connect</title>
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
       background: #0f0f0f; color: #e0e0e0; min-height: 100vh;
       display: flex; align-items: center; justify-content: center; padding: 20px; }}
.card {{ background: #2e1a1a; border: 1px solid #3e2a2a; border-radius: 16px;
         max-width: 400px; width: 100%; padding: 28px 20px; text-align: center; }}
h1 {{ font-size: 1.1rem; font-weight: 700; color: #ff6b6b; margin-bottom: 6px; }}
.subtitle {{ font-size: 0.82rem; color: #666; margin-bottom: 24px; line-height: 1.5; }}
.instruction {{ background: #1a0f0f; border: 1px solid #3e2a2a; border-radius: 10px;
                padding: 14px 16px; margin-bottom: 20px; text-align: left; font-size: 0.88rem;
                color: #ccc; line-height: 1.7; }}
.instruction strong {{ color: #e0e0e0; }}
.code-input {{ width: 100%; background: #1a0f0f; border: 2px solid #333; border-radius: 12px;
               color: #ff6b6b; padding: 16px; font-size: 2rem; font-weight: 700;
               text-align: center; letter-spacing: 0.25em; outline: none;
               -webkit-appearance: none; margin-bottom: 8px; }}
.code-input:focus {{ border-color: #ff6b6b; }}
.code-hint {{ font-size: 0.75rem; color: #444; margin-bottom: 16px; }}
.btn {{ width: 100%; background: #ff6b6b; color: #000; border: none; padding: 14px;
        border-radius: 10px; font-weight: 700; font-size: 1rem; cursor: pointer; }}
.btn:active {{ background: #ff9494; }}
.btn:disabled {{ background: #333; color: #666; cursor: default; }}
.status {{ border-radius: 10px; padding: 14px; margin-top: 16px;
           font-size: 0.9rem; line-height: 1.5; display: none; }}
.status.ok   {{ background: #0a2a1a; color: #22c55e; border: 1px solid #1a4a2a; display: block; }}
.status.err  {{ background: #2a0a0a; color: #ef4444; border: 1px solid #4a1a1a; display: block; }}
.status.info {{ background: #2e0a0a; color: #ff6b6b; border: 1px solid #4a1a1a; display: block; }}
.port-row {{ display: flex; gap: 8px; margin-bottom: 20px; }}
.port-input {{ flex: 1; background: #1a0f0f; border: 1px solid #333; border-radius: 10px;
               color: #e0e0e0; padding: 12px; font-size: 1rem; outline: none;
               -webkit-appearance: none; text-align: center; }}
.port-input:focus {{ border-color: #ff6b6b; }}
.port-label {{ font-size: 0.72rem; color: #555; margin-bottom: 4px; text-align: left; }}
</style>
</head>
<body>
<div class="card">
  <h1>📡 FileTransfer Wireless</h1>
  <div class="subtitle">
    On your phone open:<br>
    <strong style="color:#ccc">Wireless Debugging → Pair with code</strong><br>
    then enter the code below
  </div>

  <div id="autoSection">
    <div class="port-label">Pairing port (shown on phone)</div>
    <input id="pairPort" class="port-input" type="number" inputmode="numeric"
           placeholder="e.g. 39731" autocomplete="off" oninput="onPortInput()">
    <div style="margin-bottom:12px"></div>
    <div class="port-label">6-digit pairing code</div>
    <input id="pairCode" class="code-input" type="number" inputmode="numeric"
           placeholder="——————" maxlength="6" autocomplete="off" oninput="onCodeInput()">
    <div class="code-hint">Auto-submits when complete</div>
    <button class="btn" id="pairBtn" onclick="doPair()" disabled>Pair &amp; Connect</button>
  </div>

  <div id="status" class="status"></div>
</div>
<script>
const PHONE_IP = {json.dumps(phone_ip)};
const API      = {json.dumps(api_base)};
let pairPort   = null;

// Try to auto-discover the pairing port via mDNS
function discoverPort() {{
  fetch(API + '/api/mdns_discover?ip=' + encodeURIComponent(PHONE_IP))
    .then(r => r.json())
    .then(d => {{
      if (d.found && d.port) {{
        pairPort = d.port;
        document.getElementById('pairPort').value = d.port;
        document.getElementById('pairPort').style.borderColor = '#22c55e';
        checkReady();
      }}
    }})
    .catch(() => {{}});
}}
// Poll every 2s until port found
var discoverInterval = setInterval(function() {{
  if (!pairPort) discoverPort();
  else clearInterval(discoverInterval);
}}, 2000);
discoverPort();

function onPortInput() {{
  pairPort = document.getElementById('pairPort').value.trim() || null;
  checkReady();
}}

function onCodeInput() {{
  var code = document.getElementById('pairCode').value.replace(/\D/g,'').slice(0,6);
  document.getElementById('pairCode').value = code;
  checkReady();
  if (code.length === 6 && pairPort) doPair();
}}

function checkReady() {{
  var code = document.getElementById('pairCode').value.trim();
  document.getElementById('pairBtn').disabled = !(pairPort && code.length === 6);
}}

function showStatus(cls, msg) {{
  var el = document.getElementById('status');
  el.className = 'status ' + cls;
  el.textContent = msg;
}}

function doPair() {{
  var code = document.getElementById('pairCode').value.trim();
  if (!pairPort || code.length !== 6) {{ showStatus('err', 'Enter the port and 6-digit code.'); return; }}

  document.getElementById('pairBtn').disabled = true;
  showStatus('info', 'Pairing… keep the pairing screen open on your phone.');

  fetch(API + '/api/pair', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{ip: PHONE_IP, pair_port: pairPort, pair_code: code}})
  }})
  .then(r => r.json())
  .then(d => {{
    if (!d.success) {{
      showStatus('err', '✗ ' + d.message);
      document.getElementById('pairBtn').disabled = false;
      return;
    }}
    showStatus('ok', '✅ Connected! Redirecting to file browser…');
    setTimeout(() => {{ window.location.href = API + '/android'; }}, 1500);
  }})
  .catch(e => {{
    showStatus('err', 'Error: ' + e);
    document.getElementById('pairBtn').disabled = false;
  }});
}}

// Focus code field on load
window.addEventListener('load', () => document.getElementById('pairCode').focus());
</script>
</body>
</html>""".encode()
        self._send_html(page)

    # ── /api/connect (POST) ───────────────────────────────────

    def handle_connect_post(self):
        length = int(self.headers.get("Content-Length", 0))
        body   = json.loads(self.rfile.read(length) or b"{}")
        if body.get("disconnect"):
            adb_disconnect_all()
            self._json({"success": True, "message": "Disconnected"})
            return
        ip   = body.get("ip", "").strip()
        port = body.get("port", "5555").strip() or "5555"
        if not ip:
            self._json({"success": False, "message": "IP address required"})
            return
        success, msg = adb_connect_wireless(ip, port)
        self._json({"success": success, "message": msg})

    # ── /api/pair (POST) ──────────────────────────────────────

    def handle_pair_post(self):
        length = int(self.headers.get("Content-Length", 0))
        body   = json.loads(self.rfile.read(length) or b"{}")
        ip        = body.get("ip", "").strip()
        pair_port = body.get("pair_port", "").strip()
        pair_code = body.get("pair_code", "").strip()
        if not ip or not pair_port or not pair_code:
            self._json({"success": False, "message": "ip, pair_port and pair_code are required"})
            return
        try:
            success, msg = adb_pair_wireless(ip, pair_port, pair_code)
        except subprocess.TimeoutExpired:
            self._json({"success": False, "message": "Pairing timed out — check the code and try again"})
            return
        self._json({"success": success, "message": msg})

    # ── Upload ────────────────────────────────────────────────

    def _do_upload_post(self, raw):
        is_android = raw.startswith(ANDROID_PREFIX)
        ct         = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in ct:
            self.send_error(400, "Expected multipart/form-data")
            return
        boundary = None
        for part in ct.split(";"):
            part = part.strip()
            if part.startswith("boundary="):
                boundary = part[9:].strip('"').encode()
                break
        if not boundary:
            self.send_error(400, "No boundary")
            return
        length = int(self.headers.get("Content-Length", 0))
        chunks = []
        remaining = length
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 1 << 20))  # 1 MB at a time
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        saved  = 0
        for part in data.split(b"--" + boundary):
            if b"Content-Disposition" not in part:
                continue
            he = part.find(b"\r\n\r\n")
            if he == -1:
                continue
            headers_raw = part[:he].decode(errors="replace")
            body = part[he + 4:]
            if body.endswith(b"\r\n"):
                body = body[:-2]
            if body in (b"", b"--"):
                continue
            filename = None
            for hline in headers_raw.splitlines():
                if "filename=" in hline:
                    filename = os.path.basename(hline.split("filename=")[-1].strip().strip('"'))
                    break
            if not filename:
                continue
            if is_android:
                rel         = raw[len(ANDROID_PREFIX):]
                android_dir = "/sdcard" + (rel if rel else "/")
                tmp = tempfile.NamedTemporaryFile(delete=False, suffix=Path(filename).suffix)
                tmp.write(body); tmp.close()
                token = os.urandom(8).hex()
                with _jobs_lock:
                    _jobs[token] = {"status": "running", "progress": 0, "name": filename}
                threading.Thread(
                    target=adb_push_tracked,
                    args=(token, tmp.name, android_dir + "/" + filename, len(body)),
                    daemon=True
                ).start()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"token": token}).encode())
                return
            else:
                target = (MAC_ROOT / raw.lstrip("/")).resolve()
                dest   = target / filename
                stem, sfx = Path(filename).stem, Path(filename).suffix
                c = 1
                while dest.exists():
                    dest = target / f"{stem}_{c}{sfx}"; c += 1
                dest.write_bytes(body)
                print(f"  Saved: {dest}")
            saved += 1
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(f"Uploaded {saved} file(s)".encode())


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ip = get_local_ip()
    devices = adb_devices_list()
    phone_connected = any(d["state"] == "device" for d in devices)
    wifi_devices = [d for d in devices if d["type"] == "wifi" and d["state"] == "device"]
    free = free_bytes()

    port = PORT
    while True:
        try:
            server = ThreadedHTTPServer(("0.0.0.0", port), Handler)
            break
        except OSError as e:
            if e.errno == 48:  # Address already in use
                print(f"  Port {port} busy, trying {port + 1}…")
                port += 1
            else:
                raise

    if phone_connected and wifi_devices:
        android_status = f"✅ wireless ({wifi_devices[0]['serial']})"
    elif phone_connected:
        android_status = "✅ connected (USB)"
    else:
        android_status = "❌ not connected  →  open Android tab to connect over Wi-Fi"

    print(f"\n  📡 FileTransfer Wireless Server")
    print(f"  ─────────────────────────────────")
    print(f"  Local  : http://localhost:{port}")
    print(f"  Network: http://{ip}:{port}")
    print(f"  Android: {android_status}")
    print(f"  Free   : {format_size(free)} on Mac")
    print(f"\n  Downloads save to: {DOWNLOADS}")
    print(f"  Press Ctrl+C to stop.\n")

    threading.Timer(0.5, lambda: subprocess.Popen(
        ["open", f"http://localhost:{port}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.")


if __name__ == "__main__":
    main()
