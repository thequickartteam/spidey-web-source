#!/usr/bin/env python3
"""
Website Source Explorer — Render-ready.

Local dev:
    pip install -r requirements.txt
    python app.py
    open http://localhost:8080

Render:
    Build:  pip install -r requirements.txt
    Start:  gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 120
"""
import json
import os
import re
import shutil
import tempfile
import threading
import time
import urllib.parse
import uuid
import zipfile
from html.parser import HTMLParser

import requests
from flask import Flask, Response, jsonify, request

# ─────────────── config ───────────────
MAX_DEPTH         = 5
MAX_FILES         = 2000
MAX_FILE_BYTES    = 20 * 1024 * 1024   # skip individual files over 20 MB
MAX_TOTAL_BYTES   = 200 * 1024 * 1024  # hard cap on total crawl size
MAX_PREVIEW_BYTES = 5 * 1024 * 1024    # don't stream previews over 5 MB
SESSION_TTL       = 60 * 60            # 1 hour
USER_AGENT        = 'Mozilla/5.0 (compatible; SourceExplorer/1.0)'

CACHE_DIR = os.path.join(tempfile.gettempdir(), 'source_explorer_cache')
os.makedirs(CACHE_DIR, exist_ok=True)

app = Flask(__name__)

# In-memory job registry (single worker — see start command)
jobs = {}
jobs_lock = threading.Lock()
cancel_flags = {}

session = requests.Session()
session.headers.update({'User-Agent': USER_AGENT, 'Accept': '*/*'})

# ─────────────── helpers ───────────────
def safe_path(p: str) -> str:
    """Turn a URL path into something safe to join onto a base dir."""
    parts = []
    for part in p.split('/'):
        if not part or part in ('.', '..'):
            continue
        part = ''.join(c for c in part if ord(c) >= 32 and c not in '<>:"|?*\\')
        if part:
            parts.append(part)
    return '/'.join(parts) if parts else 'index.html'


def session_dir(token: str) -> str:
    return os.path.join(CACHE_DIR, token)


def files_dir(token: str) -> str:
    return os.path.join(session_dir(token), 'files')


def save_job(token: str, data: dict) -> None:
    path = session_dir(token)
    os.makedirs(path, exist_ok=True)
    tmp = os.path.join(path, 'job.json.tmp')
    with open(tmp, 'w') as f:
        json.dump(data, f)
    os.replace(tmp, os.path.join(path, 'job.json'))


def load_job(token: str):
    path = os.path.join(session_dir(token), 'job.json')
    if not os.path.isfile(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def cleanup_old_sessions() -> None:
    now = time.time()
    try:
        for entry in os.listdir(CACHE_DIR):
            path = os.path.join(CACHE_DIR, entry)
            if not os.path.isdir(path):
                continue
            try:
                if now - os.path.getmtime(path) > SESSION_TTL:
                    shutil.rmtree(path, ignore_errors=True)
                    with jobs_lock:
                        jobs.pop(entry, None)
                        cancel_flags.pop(entry, None)
            except Exception:
                pass
    except Exception:
        pass


# ─────────────── HTML / CSS link extraction ───────────────
class _LinkParser(HTMLParser):
    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=True)
        self.base = base_url
        self.links = set()

    def _add(self, raw):
        if not raw:
            return
        raw = raw.strip()
        if not raw or raw.startswith(('#', 'javascript:', 'mailto:', 'tel:', 'data:')):
            return
        try:
            self.links.add(urllib.parse.urljoin(self.base, raw))
        except Exception:
            pass

    def handle_starttag(self, tag, attrs):
        d = {k.lower(): v for k, v in attrs}
        for a in ('href', 'src', 'data', 'action'):
            if a in d:
                self._add(d[a])
        if 'srcset' in d and d['srcset']:
            for part in d['srcset'].split(','):
                bits = part.strip().split()
                if bits:
                    self._add(bits[0])
        if 'style' in d and d['style']:
            for m in re.finditer(r"url\(['\"]?([^'\")]+)['\"]?\)", d['style']):
                self._add(m.group(1))


def extract_html_links(html_text: str, base_url: str):
    p = _LinkParser(base_url)
    try:
        p.feed(html_text)
    except Exception:
        pass
    for m in re.finditer(r"url\(['\"]?([^'\")]+)['\"]?\)", html_text):
        p._add(m.group(1))
    return p.links


def extract_css_links(css_text: str, base_url: str):
    out = set()
    for m in re.finditer(r"url\(['\"]?([^'\")]+)['\"]?\)", css_text):
        try:
            out.add(urllib.parse.urljoin(base_url, m.group(1)))
        except Exception:
            pass
    return out


# ─────────────── crawler ───────────────
def crawl(start_url: str, max_depth: int, token: str):
    """Return list of {path, url, size, contentType, isBinary, raw}."""
    start_url = (start_url or '').strip()
    if not re.match(r'^https?://', start_url, re.I):
        start_url = 'https://' + start_url

    parsed = urllib.parse.urlparse(start_url)
    if not parsed.netloc:
        raise ValueError('Invalid URL')
    base_origin = f'{parsed.scheme}://{parsed.netloc}'

    visited = set()
    queue = [(start_url, 0)]
    files = []
    paths_seen = set()
    total = 0

    while queue:
        if cancel_flags.get(token):
            break
        if len(files) >= MAX_FILES or total >= MAX_TOTAL_BYTES:
            break

        url, depth = queue.pop(0)
        try:
            p = urllib.parse.urlparse(url)._replace(fragment='')
            url = urllib.parse.urlunparse(p)
        except Exception:
            continue
        if url in visited:
            continue
        visited.add(url)

        try:
            r = session.get(url, timeout=(5, 20), allow_redirects=True)
            r.raise_for_status()
        except Exception:
            continue

        raw = r.content
        if len(raw) > MAX_FILE_BYTES:
            continue
        total += len(raw)

        ctype = (r.headers.get('content-type') or '').lower()
        url_path = urllib.parse.urlparse(url).path or '/'
        if url_path.endswith('/'):
            url_path += 'index.html'
        path = safe_path(url_path.lstrip('/') or 'index.html')

        if path in paths_seen:
            continue
        paths_seen.add(path)

        ext = path.rsplit('.', 1)[-1].lower() if '.' in path else ''
        is_html = 'text/html' in ctype or ext in ('html', 'htm')
        is_css  = 'text/css'  in ctype or ext == 'css'
        is_text = (
            is_html or is_css
            or 'text/' in ctype
            or 'javascript' in ctype
            or 'json' in ctype
            or 'xml' in ctype
            or 'image/svg' in ctype
            or ext in ('js', 'mjs', 'txt', 'md', 'csv', 'yaml', 'yml',
                       'svg', 'xml', 'json', 'jsx', 'ts', 'tsx', 'map')
        )

        text = None
        if is_text:
            for enc in ('utf-8', 'latin-1'):
                try:
                    text = raw.decode(enc)
                    break
                except Exception:
                    continue
            if text is None:
                is_text = False

        files.append({
            'path': path,
            'url': url,
            'size': len(raw),
            'contentType': ctype,
            'isBinary': not is_text,
            'raw': raw,
        })

        if depth < max_depth:
            found = set()
            if is_html and text:
                found |= extract_html_links(text, url)
            elif is_css and text:
                found |= extract_css_links(text, url)

            for link in found:
                try:
                    lp = urllib.parse.urlparse(link)
                    if f'{lp.scheme}://{lp.netloc}' != base_origin:
                        continue
                    lp = lp._replace(fragment='')
                    norm = urllib.parse.urlunparse(lp)
                    if norm not in visited:
                        queue.append((norm, depth + 1))
                except Exception:
                    pass

        # progress
        with jobs_lock:
            if token in jobs:
                jobs[token]['progress'] = len(files)

    return files


def persist_files(token: str, files: list) -> list:
    """Save raw bytes to disk, return metadata-only list."""
    base = files_dir(token)
    os.makedirs(base, exist_ok=True)
    meta = []
    for f in files:
        target = os.path.join(base, f['path'])
        os.makedirs(os.path.dirname(target), exist_ok=True) if os.path.dirname(target) else None
        try:
            with open(target, 'wb') as out:
                out.write(f['raw'])
        except Exception:
            continue
        meta.append({
            'path': f['path'],
            'url': f['url'],
            'size': f['size'],
            'contentType': f['contentType'],
            'isBinary': f['isBinary'],
        })
    with open(os.path.join(session_dir(token), 'meta.json'), 'w') as mf:
        json.dump(meta, mf)
    return meta


# ─────────────── API ───────────────
@app.route('/api/crawl', methods=['POST'])
def api_crawl():
    cleanup_old_sessions()

    data = request.get_json(silent=True) or {}
    url = (data.get('url') or '').strip()
    try:
        depth = min(int(data.get('depth', 2)), MAX_DEPTH)
    except Exception:
        depth = 2

    if not url:
        return jsonify({'error': 'URL required'}), 400

    token = uuid.uuid4().hex
    with jobs_lock:
        jobs[token] = {'state': 'crawling', 'files': [], 'error': None, 'progress': 0}
    save_job(token, {'state': 'crawling', 'progress': 0})

    def run():
        try:
            files = crawl(url, depth, token)
            meta = persist_files(token, files)
            with jobs_lock:
                jobs[token].update(state='done', files=meta)
            save_job(token, {'state': 'done', 'files': meta})
        except Exception as e:
            with jobs_lock:
                jobs[token].update(state='error', error=str(e))
            save_job(token, {'state': 'error', 'error': str(e)})
        finally:
            cancel_flags.pop(token, None)

    threading.Thread(target=run, daemon=True).start()
    return jsonify({'token': token})


@app.route('/api/status/<token>')
def api_status(token):
    with jobs_lock:
        job = jobs.get(token)
    if job is None:
        job = load_job(token)
    if job is None:
        return jsonify({'error': 'unknown token'}), 404
    return jsonify(job)


@app.route('/api/cancel/<token>', methods=['POST'])
def api_cancel(token):
    cancel_flags[token] = True
    return jsonify({'ok': True})


@app.route('/api/file/<token>/<path:file_path>')
def api_file(token, file_path):
    if not re.fullmatch(r'[a-f0-9]{32}', token or ''):
        return jsonify({'error': 'bad token'}), 400
    file_path = safe_path(file_path)
    target = os.path.join(files_dir(token), file_path)
    if not os.path.isfile(target):
        return jsonify({'error': 'not found'}), 404

    # Look up metadata
    meta_path = os.path.join(session_dir(token), 'meta.json')
    is_binary = True
    ctype = 'application/octet-stream'
    if os.path.isfile(meta_path):
        try:
            with open(meta_path) as mf:
                for m in json.load(mf):
                    if m['path'] == file_path:
                        is_binary = m['isBinary']
                        ctype = m['contentType'] or ctype
                        break
        except Exception:
            pass

    with open(target, 'rb') as f:
        raw = f.read()

    if is_binary:
        import base64
        return jsonify({
            'isBinary': True,
            'contentType': ctype,
            'content': base64.b64encode(raw).decode('ascii'),
        })
    else:
        try:
            text = raw.decode('utf-8')
        except UnicodeDecodeError:
            text = raw.decode('latin-1', errors='replace')
        return jsonify({'isBinary': False, 'contentType': ctype, 'content': text})


@app.route('/api/zip/<token>')
def api_zip(token):
    if not re.fullmatch(r'[a-f0-9]{32}', token or ''):
        return jsonify({'error': 'bad token'}), 400
    base = files_dir(token)
    meta_path = os.path.join(session_dir(token), 'meta.json')
    if not os.path.isdir(base) or not os.path.isfile(meta_path):
        return jsonify({'error': 'session not found'}), 404

    try:
        with open(meta_path) as mf:
            meta = json.load(mf)
    except Exception:
        return jsonify({'error': 'bad metadata'}), 500

    domain = 'website'
    try:
        sample_url = next((m['url'] for m in meta if m.get('url')), '')
        if sample_url:
            domain = urllib.parse.urlparse(sample_url).netloc or 'website'
    except Exception:
        pass

    # Build zip once, then stream from disk
    tmp = tempfile.NamedTemporaryFile(suffix='.zip', delete=False)
    tmp_path = tmp.name
    tmp.close()
    try:
        with zipfile.ZipFile(tmp_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for m in meta:
                fp = os.path.join(base, m['path'])
                if os.path.isfile(fp):
                    try:
                        zf.write(fp, m['path'])
                    except Exception:
                        pass
    except Exception as e:
        try:
            os.remove(tmp_path)
        except Exception:
            pass
        return jsonify({'error': str(e)}), 500

    def generate():
        try:
            with open(tmp_path, 'rb') as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    yield chunk
        finally:
            try:
                os.remove(tmp_path)
            except Exception:
                pass

    return Response(
        generate(),
        mimetype='application/zip',
        headers={'Content-Disposition': f'attachment; filename="{domain}-source.zip"'},
    )


@app.route('/')
def index():
    return Response(INDEX_HTML, mimetype='text/html')


# ─────────────── UI ───────────────
INDEX_HTML = r'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Website Source Explorer</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/github-dark.min.css">
<script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/highlight.min.js"></script>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;
       background:#0d1117;color:#c9d1d9;height:100vh;display:flex;flex-direction:column;overflow:hidden}
  header{padding:12px 20px;background:#161b22;border-bottom:1px solid #30363d;
         display:flex;flex-wrap:wrap;align-items:center;gap:10px}
  header h1{font-size:1.2rem;font-weight:600;color:#f0f6fc;margin-right:20px;white-space:nowrap}
  header h1 span{color:#58a6ff}
  .controls{display:flex;flex-wrap:wrap;align-items:center;gap:8px;flex:1}
  .controls input{padding:8px 12px;border-radius:6px;border:1px solid #30363d;background:#0d1117;
                  color:#c9d1d9;font-size:.9rem;outline:none}
  .controls input:focus{border-color:#58a6ff}
  .controls input#urlInput{flex:2;min-width:220px}
  .controls input#depthInput{width:70px}
  .controls button{padding:8px 16px;border-radius:6px;border:none;font-size:.9rem;font-weight:500;
                   cursor:pointer;white-space:nowrap;transition:background .2s}
  .btn-primary{background:#238636;color:#fff}.btn-primary:hover{background:#2ea043}
  .btn-primary:disabled{background:#21262d;color:#484f58;cursor:not-allowed}
  .btn-danger{background:#da3633;color:#fff}.btn-danger:hover{background:#f85149}
  .btn-secondary{background:#21262d;color:#c9d1d9;border:1px solid #30363d}
  .btn-secondary:hover{background:#30363d}
  .status-bar{padding:6px 20px;background:#161b22;border-bottom:1px solid #30363d;font-size:.8rem;
              color:#8b949e;display:flex;justify-content:space-between;align-items:center;min-height:32px}
  .status-bar .status-text{display:flex;align-items:center;gap:8px}
  .spinner{display:inline-block;width:12px;height:12px;border:2px solid #30363d;
           border-top-color:#58a6ff;border-radius:50%;animation:spin .8s linear infinite}
  @keyframes spin{to{transform:rotate(360deg)}}
  .main-container{display:flex;flex:1;overflow:hidden}
  .sidebar{width:340px;min-width:200px;background:#161b22;border-right:1px solid #30363d;
           display:flex;flex-direction:column;overflow:hidden}
  .sidebar-header{padding:10px 14px;border-bottom:1px solid #30363d;display:flex;
                  justify-content:space-between;align-items:center;font-size:.8rem;font-weight:600;
                  color:#8b949e;text-transform:uppercase;letter-spacing:.05em}
  .file-tree{flex:1;overflow-y:auto;padding:8px 0;font-size:.85rem;user-select:none}
  .file-tree::-webkit-scrollbar{width:8px}
  .file-tree::-webkit-scrollbar-thumb{background:#30363d;border-radius:4px}
  .tree-item{display:flex;align-items:center;padding:3px 14px 3px 0;cursor:pointer;white-space:nowrap;
             overflow:hidden;text-overflow:ellipsis;color:#c9d1d9}
  .tree-item:hover{background:#1c2128}
  .tree-item.active{background:#1f6feb33;color:#58a6ff}
  .tree-item .icon{margin-right:6px;font-size:.9rem;flex-shrink:0}
  .tree-item .name{overflow:hidden;text-overflow:ellipsis}
  .tree-folder>.tree-item{font-weight:500}
  .tree-folder-children{padding-left:16px}
  .tree-folder-children.collapsed{display:none}
  .tree-empty{padding:20px;text-align:center;color:#484f58;font-size:.85rem}
  .viewer-panel{flex:1;display:flex;flex-direction:column;overflow:hidden;background:#0d1117}
  .viewer-header{padding:8px 16px;background:#161b22;border-bottom:1px solid #30363d;
                 display:flex;align-items:center;justify-content:space-between;font-size:.8rem;
                 color:#8b949e;min-height:36px}
  .viewer-header .file-path{font-family:'SFMono-Regular',Consolas,'Liberation Mono',Menlo,monospace;
                            color:#58a6ff;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .viewer-header .file-meta{white-space:nowrap;margin-left:12px}
  .viewer-content{flex:1;overflow:auto;padding:0}
  .viewer-content::-webkit-scrollbar{width:10px;height:10px}
  .viewer-content::-webkit-scrollbar-thumb{background:#30363d;border-radius:4px}
  .viewer-content pre{margin:0;padding:16px;font-size:.85rem;line-height:1.5;background:#0d1117!important}
  .viewer-content code{font-family:'SFMono-Regular',Consolas,'Liberation Mono',Menlo,monospace;font-size:.85rem}
  .binary-view{padding:40px;text-align:center;color:#8b949e}
  .binary-view .icon{font-size:3rem;margin-bottom:12px}
  .binary-view .filename{font-weight:600;color:#c9d1d9;margin-bottom:6px}
  .binary-view .meta{font-size:.85rem;margin-bottom:20px}
  .binary-view img{max-width:100%;max-height:400px;border-radius:6px;
                   border:1px solid #30363d;margin-top:16px}
  .empty-state{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;
               color:#484f58;gap:12px;padding:40px;text-align:center}
  .empty-state .big-icon{font-size:3rem;opacity:.4}
  .empty-state p{max-width:400px;line-height:1.6;font-size:.9rem}
  .legal-note{font-size:.7rem;color:#484f58;padding:0 20px 8px;text-align:center}
  @media (max-width:700px){.sidebar{width:240px;min-width:160px}header h1{font-size:1rem}}
</style>
</head>
<body>

<header>
  <h1>🌐 <span>Source</span> Explorer</h1>
  <div class="controls">
    <input type="url" id="urlInput" placeholder="https://example.com" value="https://example.com">
    <input type="number" id="depthInput" min="0" max="5" value="2" title="Max crawl depth">
    <button class="btn-primary" id="crawlBtn">Crawl</button>
    <button class="btn-danger" id="stopBtn" style="display:none;">Stop</button>
    <button class="btn-secondary" id="zipBtn" disabled>⬇ ZIP</button>
  </div>
</header>

<div class="status-bar">
  <div class="status-text" id="statusText"><span>Ready.</span></div>
  <div id="fileCount"></div>
</div>

<div class="legal-note">Only crawl websites you own or have explicit permission to access.</div>

<div class="main-container">
  <aside class="sidebar">
    <div class="sidebar-header"><span>Directory</span><span id="treeCount"></span></div>
    <div class="file-tree" id="fileTree"><div class="tree-empty">No files yet.</div></div>
  </aside>
  <section class="viewer-panel">
    <div class="viewer-header" id="viewerHeader" style="display:none;">
      <span class="file-path" id="filePath"></span>
      <span class="file-meta" id="fileMeta"></span>
    </div>
    <div class="viewer-content" id="viewerContent">
      <div class="empty-state">
        <div class="big-icon">📄</div>
        <p>Enter a URL and click <strong>Crawl</strong> to view the site's source code.</p>
      </div>
    </div>
  </section>
</div>

<script>
const $ = id => document.getElementById(id);
const urlInput = $('urlInput'), depthInput = $('depthInput');
const crawlBtn = $('crawlBtn'), stopBtn = $('stopBtn'), zipBtn = $('zipBtn');
const statusText = $('statusText'), fileCountEl = $('fileCount'), treeCountEl = $('treeCount');
const fileTreeEl = $('fileTree'), viewerHeader = $('viewerHeader');
const filePathEl = $('filePath'), fileMetaEl = $('fileMeta'), viewerContent = $('viewerContent');

let filesData = [];
let currentToken = null;
let currentPath = null;
let isCrawling = false;
let pollTimer = null;

function esc(s){return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
                               .replace(/"/g,'&quot;').replace(/'/g,'&#39;');}
function setStatus(m,err){statusText.innerHTML = err
  ? '<span style="color:#f85149">⚠ '+esc(m)+'</span>' : esc(m);}
function setStatusSpin(m){statusText.innerHTML = '<span class="spinner"></span> <span>'+esc(m)+'</span>';}
function fmtSize(b){if(!b)return '0 B';const u=['B','KB','MB','GB'];
  const i=Math.floor(Math.log(b)/Math.log(1024));return (b/Math.pow(1024,i)).toFixed(1)+' '+u[i];}
function guessLang(path, ct){
  const ext = path.split('.').pop().toLowerCase();
  const map = {html:'html',htm:'html',css:'css',js:'javascript',mjs:'javascript',json:'json',
               xml:'xml',svg:'xml',txt:'plaintext',md:'markdown',php:'php',py:'python',
               ts:'typescript',tsx:'typescript',jsx:'javascript',sh:'bash',yml:'yaml',yaml:'yaml'};
  if (map[ext]) return map[ext];
  if (ct && ct.indexOf('html') >= 0) return 'html';
  if (ct && ct.indexOf('css') >= 0) return 'css';
  if (ct && ct.indexOf('javascript') >= 0) return 'javascript';
  if (ct && ct.indexOf('json') >= 0) return 'json';
  return 'plaintext';
}

async function startCrawl(){
  if (isCrawling) return;
  const url = urlInput.value.trim();
  if (!url) { setStatus('Enter a URL first.', true); return; }
  const depth = parseInt(depthInput.value) || 2;

  filesData = []; currentToken = null; currentPath = null;
  fileTreeEl.innerHTML = '<div class="tree-empty">Crawling…</div>';
  fileCountEl.textContent = '0 files'; treeCountEl.textContent = '0 files';
  zipBtn.disabled = true;
  viewerHeader.style.display = 'none';
  viewerContent.innerHTML = '<div class="empty-state"><div class="big-icon">⏳</div><p>Crawling in progress…</p></div>';

  isCrawling = true; crawlBtn.disabled = true;
  stopBtn.style.display = 'inline-block';
  setStatusSpin('Starting crawl of ' + url + ' (depth ' + depth + ') …');

  try {
    const r = await fetch('/api/crawl', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({url: url, depth: depth})
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || ('HTTP ' + r.status));
    currentToken = data.token;
    pollStatus(currentToken);
  } catch (e) {
    isCrawling = false; crawlBtn.disabled = false; stopBtn.style.display = 'none';
    setStatus('Crawl failed: ' + e.message, true);
    viewerContent.innerHTML = '<div class="empty-state"><div class="big-icon">🚫</div><p>' +
      esc(e.message) + '</p></div>';
  }
}

async function pollStatus(token){
  if (!isCrawling) return;
  try {
    const r = await fetch('/api/status/' + token);
    if (!r.ok) throw new Error('status ' + r.status);
    const data = await r.json();

    if (data.state === 'done') {
      isCrawling = false; crawlBtn.disabled = false; stopBtn.style.display = 'none';
      filesData = data.files || [];
      fileCountEl.textContent = filesData.length + ' files';
      treeCountEl.textContent = filesData.length + ' files';
      zipBtn.disabled = filesData.length === 0;
      renderTree();
      if (filesData.length === 0) {
        setStatus('No files discovered.', true);
        viewerContent.innerHTML = '<div class="empty-state"><div class="big-icon">🚫</div>' +
          '<p>No files discovered. Check the URL and try again.</p></div>';
      } else {
        setStatus('✅ Done — ' + filesData.length + ' files discovered.');
        const idx = filesData.find(f => f.path === 'index.html') || filesData[0];
        if (idx) { markActive(idx.path); showFile(idx); }
      }
      return;
    }
    if (data.state === 'error') {
      isCrawling = false; crawlBtn.disabled = false; stopBtn.style.display = 'none';
      setStatus('Crawl error: ' + (data.error || 'unknown'), true);
      viewerContent.innerHTML = '<div class="empty-state"><div class="big-icon">🚫</div><p>' +
        esc(data.error || 'Crawl failed') + '</p></div>';
      return;
    }
    setStatusSpin('Crawling… ' + (data.progress || 0) + ' files found so far');
    pollTimer = setTimeout(() => pollStatus(token), 1000);
  } catch (e) {
    isCrawling = false; crawlBtn.disabled = false; stopBtn.style.display = 'none';
    setStatus('Polling failed: ' + e.message, true);
  }
}

function stopCrawl(){
  if (!currentToken) return;
  fetch('/api/cancel/' + currentToken, {method: 'POST'}).catch(() => {});
  if (pollTimer) clearTimeout(pollTimer);
  isCrawling = false; crawlBtn.disabled = false; stopBtn.style.display = 'none';
  setStatus('Crawl cancelled.');
}

function renderTree(){
  if (!filesData.length) { fileTreeEl.innerHTML = '<div class="tree-empty">No files.</div>'; return; }
  const root = {children:{}, files:[]};
  for (const f of filesData) {
    const parts = f.path.split('/');
    let n = root;
    for (let i = 0; i < parts.length - 1; i++) {
      const p = parts[i];
      if (!n.children[p]) n.children[p] = {children:{}, files:[]};
      n = n.children[p];
    }
    n.files.push({name: parts[parts.length - 1], file: f});
  }
  fileTreeEl.innerHTML = '';
  renderNode(root, fileTreeEl, 0);
}

function renderNode(node, parent, level){
  Object.keys(node.children).sort().forEach(function(name){
    const child = node.children[name];
    const folder = document.createElement('div');
    folder.className = 'tree-folder';
    const head = document.createElement('div');
    head.className = 'tree-item';
    head.style.paddingLeft = (8 + level * 14) + 'px';
    head.innerHTML = '<span class="icon">📁</span><span class="name">' + esc(name) + '</span>';
    const kids = document.createElement('div');
    kids.className = 'tree-folder-children';
    head.addEventListener('click', function(e){
      e.stopPropagation();
      kids.classList.toggle('collapsed');
      head.querySelector('.icon').textContent = kids.classList.contains('collapsed') ? '📁' : '📂';
    });
    folder.appendChild(head); folder.appendChild(kids); parent.appendChild(folder);
    renderNode(child, kids, level + 1);
  });
  node.files.sort(function(a,b){return a.name.localeCompare(b.name);}).forEach(function(item){
    const el = document.createElement('div');
    el.className = 'tree-item';
    el.dataset.path = item.file.path;
    if (item.file.path === currentPath) el.classList.add('active');
    el.style.paddingLeft = (8 + level * 14) + 'px';
    const icon = item.file.isBinary ? '🖼️' : '📄';
    el.innerHTML = '<span class="icon">' + icon + '</span><span class="name">' + esc(item.name) + '</span>';
    el.title = item.file.path;
    el.addEventListener('click', function(e){
      e.stopPropagation();
      document.querySelectorAll('.tree-item.active').forEach(function(x){x.classList.remove('active');});
      el.classList.add('active');
      showFile(item.file);
    });
    parent.appendChild(el);
  });
}

function markActive(path){
  document.querySelectorAll('.tree-item').forEach(function(x){
    if (x.dataset.path === path) x.classList.add('active');
    else x.classList.remove('active');
  });
}

async function showFile(f){
  currentPath = f.path;
  viewerHeader.style.display = 'flex';
  filePathEl.textContent = f.path;
  fileMetaEl.textContent = (f.contentType || 'unknown') + ' · ' + fmtSize(f.size);

  if (f.size > 5 * 1024 * 1024) {
    viewerContent.innerHTML = '<div class="binary-view"><div class="icon">📦</div>' +
      '<div class="filename">' + esc(f.path.split('/').pop()) + '</div>' +
      '<div class="meta">' + fmtSize(f.size) + ' — too large to preview</div>' +
      '<p style="font-size:.85rem;color:#484f58">Download the ZIP to view this file.</p></div>';
    return;
  }

  viewerContent.innerHTML = '<div class="empty-state"><div class="big-icon">⏳</div><p>Loading…</p></div>';

  const encodedPath = f.path.split('/').map(encodeURIComponent).join('/');
  try {
    const r = await fetch('/api/file/' + currentToken + '/' + encodedPath);
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const data = await r.json();

    if (data.isBinary) {
      const ext = f.path.split('.').pop().toLowerCase();
      const isImg = ['png','jpg','jpeg','gif','webp','svg','bmp','ico'].indexOf(ext) >= 0
                  || (data.contentType || '').indexOf('image/') === 0;
      if (isImg) {
        const mime = data.contentType || 'image/png';
        viewerContent.innerHTML =
          '<div class="binary-view"><div class="icon">🖼️</div>' +
          '<div class="filename">' + esc(f.path.split('/').pop()) + '</div>' +
          '<div class="meta">' + esc(mime) + ' · ' + fmtSize(f.size) + '</div>' +
          '<img src="data:' + mime + ';base64,' + data.content + '"></div>';
      } else {
        viewerContent.innerHTML =
          '<div class="binary-view"><div class="icon">📦</div>' +
          '<div class="filename">' + esc(f.path.split('/').pop()) + '</div>' +
          '<div class="meta">' + esc(data.contentType || 'binary') + ' · ' + fmtSize(f.size) + '</div>' +
          '<p style="font-size:.85rem;color:#484f58">Binary file — download the ZIP to get the original.</p></div>';
      }
      return;
    }

    const lang = guessLang(f.path, data.contentType);
    viewerContent.innerHTML = '<pre><code class="language-' + lang + '">' + esc(data.content || '') + '</code></pre>';
    const codeEl = viewerContent.querySelector('code');
    if (codeEl && window.hljs) { try { hljs.highlightElement(codeEl); } catch (e) {} }
  } catch (e) {
    viewerContent.innerHTML = '<div class="empty-state"><div class="big-icon">🚫</div><p>Failed to load file: ' +
      esc(e.message) + '</p></div>';
  }
}

function downloadZip(){
  if (!currentToken) return;
  window.location.href = '/api/zip/' + currentToken;
}

crawlBtn.addEventListener('click', startCrawl);
stopBtn.addEventListener('click', stopCrawl);
zipBtn.addEventListener('click', downloadZip);
urlInput.addEventListener('keydown', function(e){ if (e.key === 'Enter' && !isCrawling) startCrawl(); });
</script>
</body>
</html>
'''


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
