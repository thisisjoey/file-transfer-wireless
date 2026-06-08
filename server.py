#!/usr/bin/env python3
"""
File transfer server — serves both Mac filesystem and Android phone via adb.
Run: python3 server.py [port]
"""

import os
import sys
import html
import json
import shutil
import subprocess
import mimetypes
import socket
import tempfile
import threading
import time
import urllib.parse
import zipfile
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
MAC_ROOT = Path.home()
DOWNLOADS = Path.home() / "Downloads"
ANDROID_PREFIX = "/android"
DOWNLOAD_API = "/api/download"
SPACE_API    = "/api/space"
SEARCH_API   = "/api/search"

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
    result = subprocess.run(["adb", "shell"] + cmd, capture_output=True, text=True)
    return result.stdout, result.stderr


def adb_connected():
    """Return True if at least one adb device is online."""
    result = subprocess.run(["adb", "devices"], capture_output=True, text=True)
    lines = [l for l in result.stdout.splitlines() if l and "List of" not in l]
    return any("device" in l for l in lines)


def adb_ls(path):
    out, _ = adb_shell(["ls", "-la", f'"{path}"'])
    entries = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        perms = parts[0]
        is_dir = perms.startswith("d")
        is_link = perms.startswith("l")
        name = parts[-1] if not is_link else parts[-3]
        if name in (".", ".."):
            continue
        try:
            size = int(parts[3]) if not is_dir else 0
        except (ValueError, IndexError):
            size = 0
        entries.append((name, is_dir or is_link, size))
    return sorted(entries, key=lambda x: (not x[1], x[0].lower()))


def adb_file_size(remote_path):
    out, _ = adb_shell(["stat", "-c", "%s", f'"{remote_path}"'])
    try:
        return int(out.strip())
    except ValueError:
        return 0


def adb_pull_tracked(token, remote_path, dest_path):
    """Run adb pull in a thread, tracking progress by watching dest file size."""
    size = adb_file_size(remote_path)
    with _jobs_lock:
        _jobs[token]["size"] = size

    proc = subprocess.Popen(
        ["adb", "pull", remote_path, str(dest_path)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )

    # Watch dest file size grow in a background thread
    def watch_size():
        while proc.poll() is None:
            try:
                current = Path(dest_path).stat().st_size if Path(dest_path).exists() else 0
                if size > 0:
                    pct = min(99, int(current * 100 / size))
                    with _jobs_lock:
                        _jobs[token]["progress"] = pct
            except Exception:
                pass
            time.sleep(0.5)

    watcher = threading.Thread(target=watch_size, daemon=True)
    watcher.start()

    proc.wait()
    watcher.join(timeout=1)

    with _jobs_lock:
        if proc.returncode == 0:
            _jobs[token]["progress"] = 100
            _jobs[token]["status"] = "done"
            _jobs[token]["dest"] = str(dest_path)
        else:
            _jobs[token]["status"] = "error"
            _jobs[token]["error"] = "adb pull failed"


def adb_push(local_path, remote_path):
    result = subprocess.run(["adb", "push", local_path, remote_path], capture_output=True)
    return result.returncode == 0


def adb_folder_pull_tracked(token, remote_path, folder_name):
    """Pull entire folder from Android, zip it, save to ~/Downloads."""
    tmp_dir = tempfile.mkdtemp()
    dest_tmp = os.path.join(tmp_dir, folder_name)
    os.makedirs(dest_tmp, exist_ok=True)

    proc = subprocess.Popen(
        ["adb", "pull", remote_path, dest_tmp],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )
    for line in proc.stdout:
        line = line.strip()
        if line.startswith("[") and "%" in line:
            try:
                pct = int(line.split("%")[0].strip().lstrip("[").strip())
                # scale to 0-80 so zipping is 80-100
                with _jobs_lock:
                    _jobs[token]["progress"] = int(pct * 0.8)
            except ValueError:
                pass
    proc.wait()

    if proc.returncode != 0:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        with _jobs_lock:
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

    with zipfile.ZipFile(str(zip_dest), "w", zipfile.ZIP_DEFLATED) as zf:
        pulled = Path(dest_tmp)
        for f in pulled.rglob("*"):
            zf.write(f, f.relative_to(pulled.parent))

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
header { background: #1a1a2e; padding: 16px 20px; border-bottom: 1px solid #333;
         position: sticky; top: 0; z-index: 10; }
.header-top { display: flex; align-items: center; gap: 16px; margin-bottom: 10px; }
header h1 { font-size: 1.1rem; font-weight: 600; color: #7c9eff; white-space: nowrap; }
.tabs { display: flex; gap: 8px; }
.tab { padding: 6px 16px; border-radius: 20px; font-size: 0.85rem; font-weight: 600;
       text-decoration: none; border: 1px solid #333; color: #888; transition: all 0.2s; }
.tab.active { background: #7c9eff; color: #000; border-color: #7c9eff; }
.tab:hover:not(.active) { border-color: #555; color: #ccc; }
.breadcrumb { font-size: 0.8rem; color: #888; word-break: break-all; }
.breadcrumb a { color: #7c9eff; text-decoration: none; }
.breadcrumb a:hover { text-decoration: underline; }
.container { padding: 16px; max-width: 900px; margin: 0 auto; }

/* upload box */
.upload-box { background: #1a1a2e; border: 2px dashed #333; border-radius: 12px;
              padding: 20px; margin-bottom: 20px; text-align: center; }
.upload-box input[type=file] { display: none; }
.upload-box label { cursor: pointer; display: inline-block; background: #7c9eff;
                    color: #000; padding: 10px 20px; border-radius: 8px; font-weight: 600; font-size: 0.9rem; }
.upload-box .hint { margin-top: 8px; font-size: 0.8rem; color: #666; }
.upload-form button { margin-top: 10px; background: #22c55e; color: #000;
                       border: none; padding: 10px 24px; border-radius: 8px;
                       font-weight: 600; cursor: pointer; font-size: 0.9rem; display: none; }
.upload-form button.visible { display: inline-block; }
.upload-progress { margin-top: 12px; display: none; }
.upload-progress.visible { display: block; }

/* file list */
.file-list { background: #1a1a2e; border-radius: 12px; overflow: hidden; border: 1px solid #2a2a3e; }
.file-row { border-bottom: 1px solid #222; }
.file-row:last-child { border-bottom: none; }
.file-item-wrap { display: flex; align-items: center; transition: background 0.15s; }
.file-item-wrap:hover { background: #252540; }
.file-item { display: flex; align-items: center; gap: 12px; flex: 1;
             padding: 14px 16px; text-decoration: none; color: inherit; }
.file-item-wrap .dl-btn { margin-right: 12px; flex-shrink: 0; }
.file-icon { font-size: 1.3rem; flex-shrink: 0; width: 28px; text-align: center; }
.file-name { flex: 1; font-size: 0.95rem; word-break: break-all; }
.file-meta { font-size: 0.75rem; color: #666; flex-shrink: 0; margin-right: 8px; }
.dl-btn { flex-shrink: 0; background: #2a2a4a; border: 1px solid #444; color: #7c9eff;
           padding: 6px 12px; border-radius: 8px; font-size: 0.8rem; font-weight: 600;
           cursor: pointer; transition: all 0.2s; white-space: nowrap; }
.dl-btn:hover { background: #7c9eff; color: #000; border-color: #7c9eff; }
.dl-btn:disabled { background: #1a1a2e; color: #555; border-color: #333; cursor: default; }

/* per-file progress */
.file-progress { padding: 0 16px 12px 56px; display: none; }
.file-progress.visible { display: block; }
.prog-bar { height: 5px; background: #222; border-radius: 3px; overflow: hidden; margin-bottom: 5px; }
.prog-fill { height: 100%; background: #7c9eff; width: 0%; border-radius: 3px; transition: width 0.3s; }
.prog-fill.done { background: #22c55e; }
.prog-fill.error { background: #ef4444; }
.prog-text { font-size: 0.75rem; color: #888; }

/* shared progress bar */
.progress-bar { height: 6px; background: #333; border-radius: 3px; overflow: hidden; }
.progress-fill { height: 100%; background: #7c9eff; width: 0%; transition: width 0.2s; border-radius: 3px; }
.progress-text { font-size: 0.8rem; color: #888; margin-top: 6px; }

.empty { text-align: center; padding: 40px; color: #555; }
.badge { font-size: 0.7rem; background: #22c55e; color: #000; padding: 2px 7px;
         border-radius: 10px; font-weight: 700; margin-left: 4px; vertical-align: middle; }
.space-info { font-size: 0.75rem; color: #555; text-align: right; margin-bottom: 8px; }

/* search */
.search-wrap { position: relative; flex: 1; max-width: 320px; }
.search-wrap input { width: 100%; background: #0f0f1a; border: 1px solid #333; border-radius: 8px;
                     color: #e0e0e0; padding: 7px 32px 7px 12px; font-size: 0.85rem; outline: none; }
.search-wrap input:focus { border-color: #7c9eff; }
.search-wrap input::placeholder { color: #555; }
.search-clear { position: absolute; right: 8px; top: 50%; transform: translateY(-50%);
                background: none; border: none; color: #666; cursor: pointer; font-size: 1rem;
                display: none; line-height: 1; }
.search-clear.visible { display: block; }
.search-results { background: #1a1a2e; border-radius: 12px; border: 1px solid #2a2a3e;
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
.modal { background: #1a1a2e; border: 1px solid #333; border-radius: 16px;
         max-width: 640px; width: 100%; max-height: 80vh; display: flex; flex-direction: column;
         box-shadow: 0 24px 80px rgba(0,0,0,0.6); }
.modal-header { display: flex; align-items: center; justify-content: space-between;
                padding: 18px 20px; border-bottom: 1px solid #2a2a3e; flex-shrink: 0; }
.modal-header h2 { font-size: 1rem; font-weight: 700; color: #7c9eff; }
.modal-close { background: none; border: none; color: #888; font-size: 1.4rem;
               cursor: pointer; line-height: 1; padding: 0 4px; }
.modal-close:hover { color: #e0e0e0; }
.modal-body { overflow-y: auto; padding: 20px; font-size: 0.85rem; line-height: 1.7; color: #ccc; }
.modal-body h2 { font-size: 0.95rem; color: #7c9eff; margin: 18px 0 6px; }
.modal-body h2:first-child { margin-top: 0; }
.modal-body p { margin-bottom: 10px; }
.modal-body ul, .modal-body ol { padding-left: 20px; margin-bottom: 10px; }
.modal-body li { margin-bottom: 4px; }
.modal-body code { background: #0f0f1a; border: 1px solid #2a2a3e; border-radius: 4px;
                   padding: 1px 6px; font-family: monospace; font-size: 0.82rem; color: #a0c4ff; }
.modal-body pre { background: #0f0f1a; border: 1px solid #2a2a3e; border-radius: 8px;
                  padding: 12px; overflow-x: auto; margin-bottom: 12px; }
.modal-body pre code { background: none; border: none; padding: 0; }
.modal-body hr { border: none; border-top: 1px solid #2a2a3e; margin: 16px 0; }
.modal-footer { padding: 14px 20px; border-top: 1px solid #2a2a3e; text-align: right; flex-shrink: 0; }
.modal-footer button { background: #7c9eff; color: #000; border: none; padding: 9px 24px;
                        border-radius: 8px; font-weight: 700; cursor: pointer; font-size: 0.9rem; }
.modal-footer button:hover { background: #a0b8ff; }
.help-btn { background: none; border: 1px solid #333; color: #888; padding: 5px 12px;
             border-radius: 8px; font-size: 0.8rem; cursor: pointer; transition: all 0.2s; }
.help-btn:hover { border-color: #7c9eff; color: #7c9eff; }
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

    document.getElementById('uploadForm').addEventListener('submit', (e) => {
      e.preventDefault();
      const fd = new FormData();
      for (const f of input.files) fd.append('files', f);
      const xhr = new XMLHttpRequest();
      xhr.open('POST', window.location.href);
      xhr.upload.addEventListener('progress', (ev) => {
        if (ev.lengthComputable && fill) {
          const pct = Math.round((ev.loaded / ev.total) * 100);
          fill.style.width = pct + '%';
          ptext.textContent = `Uploading… ${pct}%`;
          prog.classList.add('visible');
        }
      });
      xhr.addEventListener('load', () => {
        if (xhr.status === 200) window.location.reload();
        else if (ptext) ptext.textContent = 'Upload failed: ' + xhr.statusText;
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
function closeModal() { document.getElementById('readmeModal').classList.add('hidden'); sessionStorage.setItem('ft_seen','1'); }

// Show once per session (resets on page refresh, not on folder navigation)
if (!sessionStorage.getItem('ft_seen')) { openModal(); sessionStorage.setItem('ft_seen','1'); }

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
      btn.textContent = isFolder ? 'Zipping…' : 'Downloading…';
      ptext.textContent = 'Starting…';

      const url = '/api/download?path=' + encodeURIComponent(androidPath)
                + '&name=' + encodeURIComponent(fileName)
                + (isFolder ? '&folder=1' : '');

      fetch(url).then(r => r.json()).then(job => {
        if (job.error) {
          fill.classList.add('error'); fill.style.width = '100%';
          ptext.textContent = 'Error: ' + job.error;
          btn.textContent = 'Failed';
          return;
        }
        const token = job.token;
        const iv = setInterval(() => {
          fetch('/api/download?token=' + token).then(r => r.json()).then(s => {
            const pct = s.progress || 0;
            fill.style.width = pct + '%';
            if (s.status === 'done') {
              clearInterval(iv);
              fill.classList.add('done'); fill.style.width = '100%';
              ptext.textContent = `Saved to ~/Downloads/${s.name || fileName}`;
              btn.textContent = 'Done ✓';
            } else if (s.status === 'error') {
              clearInterval(iv);
              fill.classList.add('error'); fill.style.width = '100%';
              ptext.textContent = 'Download failed';
              btn.textContent = 'Failed'; btn.disabled = false;
            } else {
              ptext.textContent = pct + '%' + (isFolder ? ' (pulling + zipping)' : '');
            }
          });
        }, 500);
      });
    })
    .catch(err => {
      ptext.textContent = 'Error: ' + err;
      btn.textContent = 'Error'; btn.disabled = false;
    });
}
"""


# ── Page renderer ─────────────────────────────────────────────────────────────

def render_page(breadcrumb_html, entries, current_url, mode, upload=True, android_ok=True):
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
    if android_ok:
        android_tab = f'<a class="tab{and_active}" href="{ANDROID_PREFIX}">📱 Android <span class="badge">USB</span></a>'
    else:
        android_tab = '<span class="tab" style="opacity:0.35;cursor:default;" title="No Android device connected">📱 Android</span>'

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
<div class="modal-backdrop" id="readmeModal">
  <div class="modal">
    <div class="modal-header">
      <h2>📡 FileTransfer — Quick Start</h2>
      <button class="modal-close" onclick="closeModal()">✕</button>
    </div>
    <div class="modal-body">
      <h2>Requirements</h2>
      <ul>
        <li>macOS + Python 3 (pre-installed)</li>
        <li>Android phone with USB Debugging enabled</li>
        <li>USB-C cable + <code>adb</code> installed via Homebrew</li>
      </ul>
      <h2>Enable USB Debugging (one time)</h2>
      <ol>
        <li><strong>Settings → About Phone → Version</strong> — tap <strong>Build Number</strong> 7 times</li>
        <li>Go back → <strong>Additional Settings → Developer Options → USB Debugging</strong> ON</li>
        <li>Plug in cable → tap <strong>Allow</strong> on phone → select <strong>File Transfer</strong> mode</li>
      </ol>
      <h2>Features</h2>
      <ul>
        <li><strong>💻 Mac tab</strong> — browse your Mac filesystem, upload files</li>
        <li><strong>📱 Android tab</strong> — browse phone storage over USB</li>
        <li><strong>⬇ Save</strong> — download any file or folder to <code>~/Downloads</code> with progress bar</li>
        <li><strong>Search</strong> — searches entire phone storage recursively (type 2+ chars)</li>
        <li><strong>Upload</strong> — send files from Mac → Android or vice versa</li>
        <li>Checks free disk space before every download</li>
      </ul>
      <h2>Start / Stop</h2>
      <pre><code>python3 server.py        # start
./start.sh ~/Desktop     # share specific folder
kill $(lsof -ti:8765)    # stop</code></pre>
      <h2>Why not deploy to cloud?</h2>
      <p>This tool is <strong>local-only by design</strong> — <code>adb</code> must run on the Mac physically connected to your phone via USB. Nothing leaves your machine.</p>
      <hr>
      <p style="color:#555;font-size:0.78rem;">Tip: use <code>ngrok http 8765</code> to access over the internet via a tunnel.</p>
    </div>
    <div class="modal-footer">
      <button onclick="closeModal()">Got it, let's go</button>
    </div>
  </div>
</div>

<header>
  <div class="header-top">
    <h1>📡 FileTransfer</h1>
    <div class="tabs">
      <a class="tab{mac_active}" href="/">💻 Mac</a>
      {android_tab}
    </div>
    <div class="search-wrap">
      <input id="searchInput" type="search" placeholder="Search files &amp; folders…" autocomplete="off">
      <button class="search-clear" id="searchClear" title="Clear">✕</button>
    </div>
    <button class="help-btn" onclick="openModal()">? Help</button>
  </div>
  <div class="breadcrumb">{breadcrumb_html}</div>
</header>
<div class="container">
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
        elif raw == DOWNLOAD_API:
            self.handle_download_api(qs)
        elif raw.startswith(ANDROID_PREFIX):
            self.handle_android_get(raw)
        else:
            self.handle_mac_get(raw)

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
            out, _ = adb_shell(["test", "-d", f'"{p}"', "&&", "echo", "DIR", "||", "echo", "FILE"])
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
                job = dict(_jobs.get(token, {"status": "unknown", "progress": 0}))
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

    # ── Android browse ────────────────────────────────────────

    def handle_android_get(self, url_path):
        if not adb_connected():
            self.send_error(503, "Android device not connected — plug in your phone and enable USB Debugging")
            return

        rel          = url_path[len(ANDROID_PREFIX):]
        android_path = "/sdcard" + (rel if rel else "/")

        out, _ = adb_shell(["test", "-d", f'"{android_path}"', "&&", "echo", "DIR", "||", "echo", "FILE"])
        is_dir = "DIR" in out

        if is_dir:
            entries = adb_ls(android_path)
            parts   = rel.strip("/").split("/") if rel.strip("/") else []
            crumbs  = [f'<a href="{ANDROID_PREFIX}">📱 Android</a>']
            for i, part in enumerate(parts):
                link = ANDROID_PREFIX + "/" + "/".join(parts[:i+1])
                crumbs.append(f'<a href="{html.escape(link)}">{html.escape(part)}</a>')
            body = render_page(" / ".join(crumbs), entries, url_path, "android", android_ok=True)
            self._send_html(body)
        else:
            # Direct inline view (tap on filename still works)
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

    # ── Upload ────────────────────────────────────────────────

    def do_POST(self):
        raw        = urllib.parse.unquote(self.path.split("?")[0])
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
        data   = self.rfile.read(length)
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
                adb_push(tmp.name, android_dir + "/" + filename)
                os.unlink(tmp.name)
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
    result = subprocess.run(["adb", "devices"], capture_output=True, text=True)
    lines  = [l for l in result.stdout.splitlines() if l and "List of" not in l]
    phone_connected = any("device" in l for l in lines)
    free = free_bytes()

    print(f"\n  📡 FileTransfer Server")
    print(f"  ─────────────────────────────────")
    print(f"  Local  : http://localhost:{PORT}")
    print(f"  Network: http://{ip}:{PORT}")
    print(f"  Android: {'✅ connected' if phone_connected else '❌ not detected'}")
    print(f"  Free   : {format_size(free)} on Mac")
    print(f"\n  Downloads save to: {DOWNLOADS}")
    print(f"  Press Ctrl+C to stop.\n")

    server = HTTPServer(("0.0.0.0", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.")


if __name__ == "__main__":
    main()
