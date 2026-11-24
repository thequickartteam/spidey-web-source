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


@app.route('/ai')
def puter():
    return Response(PUTER_AI, mimetype='text/html')

PUTER_AI = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Pollinations Model Playground</title>
<style>
:root{color-scheme:dark;--bg:#0a1020;--panel:#121b30;--line:#2a3855;--text:#eaf0ff;--muted:#9eacc7;--accent:#83adff;--green:#8ce0b6;--red:#ff9b9b}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(1000px 550px at 75% -10%,#20355f,transparent 62%),var(--bg);color:var(--text);font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}
.wrap{max-width:1150px;margin:30px auto;padding:0 16px}
h1{font-size:clamp(24px,4vw,34px);margin:0}.sub,.muted,.hint{color:var(--muted)}
.card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:18px;margin:14px 0}
.row{display:flex;gap:12px;align-items:center;flex-wrap:wrap}
.tabs{display:flex;gap:8px;margin-bottom:14px}
.tab,button{border:0;border-radius:9px;padding:10px 14px;font:inherit;font-weight:700;cursor:pointer}
.tab{background:#202d47;color:var(--text)}.tab.active,button.primary{background:var(--accent);color:#071225}
button.secondary{background:#253450;color:var(--text)}button:disabled{opacity:.55;cursor:wait}
label{display:block;color:var(--muted);font-size:13px;font-weight:650;margin:0 0 6px}
textarea,input[type=text],input[type=number],input[type=search],select{font:inherit;color:var(--text);background:#091222;border:1px solid #34425e;border-radius:9px;padding:11px;width:100%}
textarea{min-height:120px;resize:vertical}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}@media(max-width:720px){.grid{grid-template-columns:1fr}}
.field{flex:1;min-width:200px}
.modeBox{display:flex;gap:18px;align-items:center;flex-wrap:wrap;margin-top:14px}
.modeBox label{margin:0;color:var(--text);font-size:14px}
.modelToolbar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:12px 0}
.modelToolbar input[type=search]{max-width:280px;padding:8px 11px}
.modelList{display:grid;grid-template-columns:repeat(auto-fill,minmax(215px,1fr));gap:8px;max-height:320px;overflow:auto}
.model{display:flex;gap:8px;align-items:center;background:#0b1425;border:1px solid #283651;border-radius:8px;padding:8px;font-size:13px;cursor:pointer;overflow-wrap:anywhere}
.model:hover{border-color:var(--accent)}
.model.sel{border-color:var(--accent);background:#10203c}
.model input{width:auto;accent-color:var(--accent)}
.status{font-size:13px;color:var(--muted)}.ok{color:var(--green)}.err{color:var(--red)}
.results{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,340px),1fr));gap:12px}
.result{background:#0b1425;border:1px solid var(--line);border-radius:11px;padding:13px;min-width:0}
.resultHead{display:flex;justify-content:space-between;gap:10px;align-items:center}
.result h3{font-size:14px;margin:0;overflow-wrap:anywhere}
.result pre{white-space:pre-wrap;overflow-wrap:anywhere;max-height:440px;overflow:auto;font:12.5px/1.55 ui-monospace,monospace;color:#d8e3fb;margin:10px 0 0}
.result img{max-width:100%;border-radius:8px;margin-top:10px}
.badge{font-size:11px;border:1px solid var(--line);border-radius:99px;padding:2px 8px;color:var(--muted);white-space:nowrap}
.badge.ok{color:var(--green);border-color:var(--green)}.badge.err{color:var(--red);border-color:var(--red)}
.hidden{display:none}.hint{font-size:12px;margin-top:8px}
a{color:#adc8ff}.linkBtn{background:none;border:0;color:#adc8ff;cursor:pointer;font-size:12px;padding:0;font-weight:700}
</style></head><body><main class="wrap">
<h1>Pollinations Model Playground</h1>
<p class="sub">Text &amp; image generation via Pollinations — run every model at once, or pick exactly one. All requests are POST, sent straight from your browser.</p>

<section class="card">
  <div class="tabs">
    <button class="tab active" data-category="text">📝 Text</button>
    <button class="tab" data-category="image">🎨 Image</button>
  </div>
  <div class="grid">
    <div><label for="prompt">Prompt</label><textarea id="prompt" placeholder="Write your prompt…">Explain briefly how a rainbow forms.</textarea></div>
    <div id="systemWrap"><label for="system">System message <span class="muted">(optional, text only)</span></label>
      <textarea id="system" placeholder="You are a helpful assistant."></textarea>
      <div class="hint">Requests go directly from your browser to Pollinations. Availability, limits and CORS may vary.</div></div>
    <div id="imageOptions" class="row hidden" style="align-items:flex-end">
      <div class="field" style="max-width:220px"><label for="imageSize">Image size</label>
        <select id="imageSize"><option value="1024x1024">1024 × 1024</option><option value="1024x768">1024 × 768</option><option value="768x1024">768 × 1024</option><option value="1280x720">1280 × 720</option></select></div>
      <div class="hint" style="flex:1">Image models may take longer. Results show the image plus a download link.</div>
    </div>
  </div>

  <div class="row" style="margin-top:12px">
    <div class="field" style="max-width:240px" id="maxTokensWrap"><label for="maxTokens">Max tokens</label>
      <input id="maxTokens" type="number" min="16" step="16" value="1024"></div>
  </div>

  <div class="modeBox">
    <strong>Call mode:</strong>
    <label><input type="radio" name="callMode" value="all" checked> Call all models</label>
    <label><input type="radio" name="callMode" value="single"> Call a single model</label>
    <span id="modeHint" class="hint" style="margin:0"></span>
  </div>

  <div id="modelBox">
    <div class="modelToolbar">
      <input id="modelSearch" type="search" placeholder="Search models…">
      <button id="selectAll" class="secondary" type="button">Select all</button>
      <button id="selectNone" class="secondary" type="button">Select none</button>
      <span id="modelCount" class="status"></span>
    </div>
    <div id="models" class="modelList"></div>
  </div>

  <div class="row" style="margin-top:15px">
    <button class="primary" id="run">Run models ▶</button>
    <button class="secondary" id="clear">Clear results</button>
    <button class="secondary" id="stop" hidden>Stop ✕</button>
    <span id="status" class="status">Loading Pollinations model catalogue…</span>
  </div>
</section>

<section class="card">
  <div class="row" style="justify-content:space-between">
    <h2 style="font-size:18px;margin:0">Results</h2><span id="resultSummary" class="status"></span>
  </div>
  <div id="results" class="results" style="margin-top:12px"></div>
</section>

<p class="hint">Text API: <code>text.pollinations.ai</code> · Image API: <code>image.pollinations.ai</code> · <a href="https://enter.pollinations.ai/models" target="_blank" rel="noreferrer">Model catalogue</a> · Never put a private API key in a public HTML file.</p>
</main>

<script>
(()=>{'use strict';
const $=id=>document.getElementById(id);
let category='text',catalog={text:[],image:[]},busy=false,stopFlag=false;
const fallback={text:["openai/gpt-5.4-nano","thinkingmachines/inkling-2-mini","google/gemini-4-neon","openai/gpt-6.2-astra","openai/gpt-6.2-sol","qwen/qwen4-max","tencent/hy4-flash","google/gemini-4-hydrogen","google/gemini-4-carbon","anthropic/claude-fable-5.2","meta/muse-ember","openai/gpt-6.1-terra","meta/muse-spark-1.3","minimax/minimax-m3.1-flash","moonshotai/kimi-k3.1-pro","moonshotai/kimi-k3.1-pro-highspeed","inception/mercury-3","deepseek/deepseek-v4.1-flash-1017","meta/muse-spark-open","nvidia/nemotron-3.5-super-vl","google/gemini-4-argon","meta/muse-wonder-1.0","anthropic/claude-opus-5.5:fast","stepfun/step-5-preview","anthropic/claude-haiku-5.5","mistralai/mistral-large-4","nex-agi/nex-n2.5-mini","nex-agi/nex-n2.5-pro","inclusionai/ling-3.1-flash","inferenceport-ai/lightning-text-v2","inferenceport-ai/lightning-text-v2.1","openai/gpt-6.1-sol","anthropic/claude-sonnet-5.5","anthropic/claude-opus-5.5","openai/gpt-5.3-codex","openai/gpt-6-luna","openai/gpt-6-sol","xiaomi/mimo-v2.6-flash","xiaomi/mimo-v2.6-pro","x-ai/grok-4.7","inclusionai/ling-3.0-flash-vl","z-ai/glm-5.3-flashx","tencent/hy3","openai/gpt-4o-mini","tencent/hy4-preview","deepseek/deepseek-v4.1-flash","qwen/qwen3.8-flash","openai/gpt-6-astra","qwen/qwen3.8-max-0902","deepseek/deepseek-v4-flash-vision-exp","google/gemini-3.8-flash","anthropic/claude-fable-5.1","thinkingmachines/inkling","z-ai/glm-5.3-flash","nvidia/nemotron-3.5-lightning","z-ai/glm-5.3","qwen/qwen3.8-27b","meta/muse-glimmer-30b","meta/muse-spark-1.2","qwen/qwen3.8-2.4t-a95b","qwen/qwen3.8-max","thinkingmachines/inkling-small","cohere/command-a-plus","qwen/qwen3.7-flash","nvidia/nemotron-3-ultra","meituan/longcat-2.0","poolside/laguna-s-2.1","google/gemma-4-31b-it","moonshotai/kimi-k3","openai/gpt-oss-20b","qwen/qwen3.7-max","x-ai/grok-4.6","xiaomi/mimo-v2.5","openai/gpt-5.6-luna","openai/gpt-5.6-sol","openai/gpt-5.6-terra","anthropic/claude-sonnet-5","inception/mercury-2","moonshotai/kimi-k2.7-code","qwen/qwen3.7-plus","anthropic/claude-fable-5","minimax/minimax-m3","anthropic/claude-opus-5","stepfun/step-3.5-flash","stepfun/step-3.7-flash","x-ai/grok-4.3","google/gemini-3.7-flash","mistralai/mistral-small-4","openai/gpt-5.4-mini","qwen/qwen3-vl-235b-a22b-thinking","google/gemma-4-26b-a4b-it","meta/llama-4-maverick","meta/llama-4-scout","openai/gpt-5.5","meta/llama-3.3-70b-instruct","deepseek/deepseek-v4-pro","anthropic/claude-opus-4.7","moonshotai/kimi-k2.6","mistralai/mistral-large-3","google/gemini-3.5-flash-lite","openai/gpt-audio-1.5","amazon/nova-2-lite-v1","pollinations/midijourney-large","qwen/qwen3-coder-next","qwen/qwen3-vl-30b-a3b-instruct","qwen/qwen3guard-gen-8b","minimax/minimax-m2.7","z-ai/glm-5.2","google/gemini-2.5-flash-lite","anthropic/claude-haiku-4.5","google/gemini-3.1-pro-preview","anthropic/claude-opus-4.6","x-ai/grok-4.20","anthropic/claude-sonnet-4.6","perplexity/sonar","deepseek/deepseek-v4-flash","google/gemini-2.5-flash-lite:search","amazon/nova-micro-v1","google/gemini-3-flash-preview","mistralai/mistral-small-3.2","openai/gpt-5-nano","openai/gpt-5.4","openai/gpt-audio-mini","pollinations/midijourney","qwen/qwen3-coder-30b-a3b-instruct","stealth/glyph-cluster","community/pollinations-ai/floret","community/pollinations-ai/catgpt","community/pollinations-ai/opposite-prompt","community/chigwell/gpt-6.1-terra","community/chigwell/gpt-6-astra","community/Catniti/hy4-flash","community/sharktide/azure-nemotron-3-ultra:data-zone","community/sharktide/azure-gpt-5.4-nano","community/scriptsnsenses-sys/gpt-6-luna:free","community/Brybod123/bryllm-qwen35-08b","community/chigwell/minimax-m3","community/sharktide/azure-mistral-large-4:azure:data-zone","community/Catniti/qwen3.8-27b","community/Catniti/gemini-4-carbon","community/Bakhshi7889/step-5-preview","community/rekty/towers-foundry-legal","community/rekty/towers-prism-media","community/rekty/towers-kite-fulfilment","community/rekty/towers-helios-grid","community/rekty/towers-orbit-education","community/rekty/towers-quill-co","community/rekty/towers-lumen-health","community/rekty/towers-atlas-freight","community/rekty/towers-verdant-analytics","community/rekty/towers-neon-games-studio","community/rekty/towers-sky-brew-labs","community/chigwell/claude-fable-5-2","community/chigwell/kimi-k3.1-pro","community/Catniti/north-mini-code","community/Catniti/command-a-plus","community/patbing/tower-reviewer","community/patbing/tower-writer","community/patbing/tower-researcher","community/Saauf/nemotron-3.5-bolt","community/Saauf/laguna-xs-2.1","community/Saauf/step-5-preview","community/Saauf/nemotron-3.5-lightning","community/Saauf/nemotron-3-ultra","community/Saauf/mistral-large-4","community/Lorodn4x/ps5-beta","community/mhmdrizki803-boop/towers-signal-reports","community/mhmdrizki803-boop/towers-echo-support","community/mhmdrizki803-boop/towers-ledger-owls","community/mhmdrizki803-boop/towers-pixel-bloom","community/mhmdrizki803-boop/towers-nova-outreach","community/mhmdrizki803-boop/towers-byte-foundry","community/mhmdrizki803-boop/towers-hexa-research","community/mhmdrizki803-boop/towers-aurora-copy","community/Marcus-Mok-GH/pt-leasing","community/Marcus-Mok-GH/pt-fieldnote","community/Marcus-Mok-GH/pt-orbit","community/Marcus-Mok-GH/pt-copperline","community/Marcus-Mok-GH/pt-harbor","community/Marcus-Mok-GH/pt-lantern","community/Marcus-Mok-GH/pt-tidepool","community/Marcus-Mok-GH/pt-quietloop","community/Marcus-Mok-GH/pt-emberforge","community/Marcus-Mok-GH/pt-lobby","community/Marcus-Mok-GH/pt-northwind","community/xiaotian1171/atrium-concierge","community/xiaotian1171/atrium-mise-en-place","community/xiaotian1171/atrium-fieldnotes","community/xiaotian1171/atrium-soundpost","community/xiaotian1171/atrium-tinyfactory","community/xiaotian1171/atrium-civic-atlas","community/xiaotian1171/atrium-shelflife","community/xiaotian1171/atrium-papertrail-bio","community/xiaotian1171/atrium-clearclause","community/xiaotian1171/atrium-loop-and-loom","community/xiaotian1171/atrium-northstar-climate","community/Saauf/minimax-m3.1-flash","community/tomdacatto/tower-kitchen-table","community/tomdacatto/tower-patchwork","community/tomdacatto/tower-stillwater","community/tomdacatto/tower-fieldnotes","community/tomdacatto/tower-lumen-repairs","community/tomdacatto/tower-noticeboard","community/tomdacatto/tower-seedbank","community/tomdacatto/tower-tidewatch","community/sharktide/inferenceport-ai-gpt-6-luna","community/YoannDev90/vercel-nemotron-3.5-lightning:free","community/Saauf/grok-4.20-fast","community/Catniti/glm-5.3-flash","community/scriptsnsenses-sys/deepseek-v4.1-flash","community/danhquach/prompt-enhancer","community/danhquach/bug-report-writer","community/chigwell/gemini-4-argon","community/Davizigjojo/blazerrouter","community/chigwell/claude-haiku-5-5","community/Saauf/glm-5.3-flash","community/morriszdweck/glm-5.3-flash-free","community/Saauf/llama3.1-8B:ultrafast","community/chigwell/claude-opus-5-5","community/Derlifed/retail-fastfood-interview-coach","community/samueltargino0567-ux/Gt3","community/sharktide/inferenceport-ai-lightning-text-v2.1","community/chigwell/gpt-6.1-sol","community/vendouple/gpt-6.2-sol:stable","community/Guest453/idea-judge","community/tomdacatto/Qwen3.8-27B-Uncensored","community/vendouple/claude-sonnet-5.5:stable","community/Creatneworld/pen","community/vendouple/claude-opus-5.5:stable","community/vendouple/gpt-6-luna:stable","community/vendouple/gpt-6.1-sol:stable","community/Guest453/show-your-work","community/Catniti/catniti-ai-agent","community/Catniti/agnes-3.0-flash","community/AkshayCoder48/prompt-to-art","community/AkshayCoder48/free-voice","community/AkshayCoder48/free-clips","community/AkshayCoder48/researcher","community/AkshayCoder48/code-pair","community/fadyabohamza-netizen/frugal","community/iotserver24/supercharge","community/aikhusus2025-ctrl/sirius-scorekeeper","community/tomdacatto/gotcha-scout","community/tomdacatto/tiered-health-router","community/afanasevmylife/gazette","community/Creatneworld/lamplighter","community/xiaotian1171/triage-router","community/voodoohop/catgpt-comic","community/afanasevmylife/polyrouter","community/sharktide/3D-agent","community/sharktide/inferenceport-ai-kimi-k2.7-code-deep-logician","community/Creatneworld/sirius-elevator-descent","community/Creatneworld/catgpt-comic","community/scriptsnsenses-sys/glm-5.3-flash-free","community/aikhusus2025-ctrl/sirius-elevator","community/aikhusus2025-ctrl/ember-perch","community/pegalink/hy4-preview-coding","community/pollinations-ai/midijourney","community/Catniti/nemotron-3.5-lightning","community/tomdacatto/llama-3.1-8B","community/MarcosFRG/gemma-4-31b:paid","community/smplstuff/title-generator","community/tomdacatto/qwen-3.8-27B-fast","community/voodoohop/email-overview","community/YoannDev90/qwen3.7-flash","community/MarcosFRG/glm-5.3-flash","community/MarcosFRG/gemma-4-26b-a4b:paid","community/Takax62/minimax-m3-429b-vml","community/sharktide/inferenceport-ai-lightning-text-v2-expanded-knowledge","community/MarcosFRG/qwen3.8-27b:paid","community/MarcosFRG/metraxai","community/gggff123/Inkling","community/gggff123/step-3.7-flash","community/pollinations-ai/polli","community/chirag-gamer/gpt-oss-120b","community/sharktide/inferenceport-ai-gemini-2.5-flash","community/sharktide/inferenceport-ai-lightning-text-v2","community/sharktide/inferenceport-ai-qwen-3.8-27b","community/sharktide/inferenceport-ai-minimax-m3","community/sharktide/inferenceport-ai-kimi-k2.7-code","community/MarcosFRG/minimax-m3:paid","community/MarcosFRG/gemini-3-flash-preview:paid","community/sharktide/inferenceport-ai-mimo-v2.5"],
image:['flux','zimage','klein','gptimage','qwen-image','gpt-image-2','wan-image','gptimage-large','kontext','seedream','p-image','p-image-edit','grok-imagine','nanobanana','nova-canvas','seedream5','nanobanana-2','grok-imagine-pro','wan-image-pro','nanobanana-pro','seedream-pro']};

const setStatus=(s,cls='')=>{$('status').textContent=s;$('status').className='status '+cls};
const modelId=m=>typeof m==='string'?m:(m?.name||m?.id||m?.model||'');
const extractList=d=>Array.isArray(d)?d:(d&&typeof d==='object'?['models','data','text','image'].map(k=>d[k]).find(Array.isArray)||[]:[]);
const getMode=()=>document.querySelector('input[name="callMode"]:checked').value;

/* ---------- catalogue ---------- */
async function loadCatalog(){
  try{
    const r=await fetch('https://text.pollinations.ai/models',{headers:{accept:'application/json'}});
    if(!r.ok)throw Error('HTTP '+r.status);
    const data=await r.json();
    let txt=[],img=[];
    for(const m of extractList(data)){
      const id=modelId(m);if(!id)continue;
      const type=(typeof m==='object'?(m.type||m.modality||m.category||''):'').toString().toLowerCase();
      if(type.includes('image'))img.push(id);else txt.push(id);
    }
    if(data&&typeof data==='object'){
      if(Array.isArray(data.image))img.push(...data.image.map(modelId));
      if(Array.isArray(data.text))txt.push(...data.text.map(modelId));
    }
    catalog.text=[...new Set(txt.filter(Boolean))];
    catalog.image=[...new Set(img.filter(Boolean))];
    // Live catalogues are currently restricted to a few anonymous-tier models;
    // merge in the full known list so the playground still offers everything.
    catalog.text=[...new Set([...fallback.text,...catalog.text])];
    catalog.image=[...new Set([...catalog.image,...fallback.image])];
    setStatus(`Catalogue loaded — ${catalog.text.length} text · ${catalog.image.length} image models (live + known).`,'ok');
  }catch(e){
    catalog={...fallback};
    setStatus(`Live catalogue unavailable (${e.message}) — using built-in list.`,'err');
  }
  renderModels();
}

/* ---------- model list ---------- */
function currentModels(){return catalog[category]||[]}
function renderModels(){
  const q=$('modelSearch').value.trim().toLowerCase();
  const arr=currentModels().filter(m=>!q||m.toLowerCase().includes(q));
  const single=getMode()==='single';
  const sel=selectedSet();
  const box=$('models');box.replaceChildren();
  arr.forEach(name=>{
    const lab=document.createElement('label');lab.className='model';
    const cb=document.createElement('input');cb.type='checkbox';cb.value=name;
    cb.checked=single?(sel.has(name)||arr.length===1):sel.has(name);
/* selection handled by delegated listeners below */
    const span=document.createElement('span');span.textContent=name;
    lab.append(cb,span);box.append(lab);
  });
  $('modelCount').textContent=`${arr.length} shown · ${selected().length} selected`;
  update();
}
function selectedSet(){return new Set([...document.querySelectorAll('#models input:checked')].map(x=>x.value))}
function selected(){return [...selectedSet()]}
function update(){
  const single=getMode()==='single';
  const n=selected().length;
  $('modeHint').textContent=single?(n===1?'✓ 1 model selected':'Select exactly one model'):`${currentModels().length||0} models in catalogue — checked ones will run`;
  $('modelCount').textContent=`${document.querySelectorAll('#models input').length} shown · ${n} selected`;
}
function enforceSelection(t){
  if(getMode()!=='single'){update();return}
  const boxes=[...document.querySelectorAll('#models input')];
  if(t&&t.type==='checkbox'&&t.checked)boxes.forEach(o=>{if(o!==t)o.checked=false});
  const on=boxes.filter(b=>b.checked);
  if(!on.length&&boxes.length)boxes[0].checked=true;
  else if(on.length>1)boxes.forEach(b=>b.checked=(b===on[0]));
  update();
}
$('models').addEventListener('click',e=>{const cb=e.target.closest('input[type=checkbox]');if(cb)setTimeout(()=>enforceSelection(cb),0)});
$('models').addEventListener('change',e=>{if(e.target.type==='checkbox')enforceSelection(e.target)});
$('modelSearch').addEventListener('input',renderModels);
$('selectAll').addEventListener('click',()=>{document.querySelectorAll('#models input').forEach(c=>c.checked=true);update()});
$('selectNone').addEventListener('click',()=>{document.querySelectorAll('#models input').forEach(c=>c.checked=false);update()});
document.querySelectorAll('input[name="callMode"]').forEach(r=>r.addEventListener('change',()=>{
  if(getMode()==='single'){
    // In single mode keep at most one checked; if multiple are checked keep only the first.
    const boxes=[...document.querySelectorAll('#models input')];const checked=boxes.filter(b=>b.checked);
    if(checked.length>1)boxes.forEach(b=>b.checked=(b===checked[0]));
    if(!checked.length&&boxes.length)boxes[0].checked=true;
  }
  update();
}));

/* ---------- tabs ---------- */
function syncCategory(){
  const isImg=category==='image';
  $('systemWrap').classList.toggle('hidden',isImg);
  $('imageOptions').classList.toggle('hidden',!isImg);
  $('maxTokensWrap').classList.toggle('hidden',isImg);
  $('prompt').placeholder=isImg?'Describe the image you want to create…':'Write your prompt…';
  document.querySelectorAll('.tab').forEach(b=>b.classList.toggle('active',b.dataset.category===category));
  renderModels();
}
document.querySelectorAll('.tab').forEach(b=>b.addEventListener('click',()=>{if(busy)return;category=b.dataset.category;syncCategory()}));

/* ---------- API calls (POST) ---------- */
function parseBody(raw){
  try{const j=JSON.parse(raw);
    if(typeof j==='string')return j;                       // some models return JSON-encoded text
    if(j?.choices?.[0]?.message?.content)return j.choices[0].message.content;
    if(typeof j?.content==='string')return j.content;
    return JSON.stringify(j,null,2);
  }catch{return raw}
}
async function textCall(model,prompt,system){
  const messages=[];if(system)messages.push({role:'system',content:system});
  messages.push({role:'user',content:prompt});
  const res=await fetch('https://text.pollinations.ai/',{method:'POST',
    headers:{'Content-Type':'application/json','Accept':'application/json'},
    body:JSON.stringify({messages,model,stream:false,max_tokens:Math.max(1,Number($('maxTokens').value)||1024)})});
  const raw=await res.text();
  if(!res.ok)throw Error(`HTTP ${res.status} ${res.statusText}\n${parseBody(raw)}`);
  return parseBody(raw)||'(empty response)';
}
async function imageCall(model,prompt){
  const [w,h]=$('imageSize').value.split('x').map(Number);
  const res=await fetch('https://image.pollinations.ai/',{method:'POST',
    headers:{'Content-Type':'application/json','Accept':'image/*'},
    body:JSON.stringify({prompt,model,width:w,height:h,n:1})});
  if(!res.ok){const t=await res.text();throw Error(`HTTP ${res.status} ${res.statusText}\n${t.slice(0,400)}`)}
  const blob=await res.blob();
  if(!blob.type.startsWith('image/'))throw Error('Expected an image response, got '+blob.type);
  return blob;
}

/* ---------- results ---------- */
function addCard(model){
  const card=document.createElement('article');card.className='result';
  const head=document.createElement('div');head.className='resultHead';
  const h=document.createElement('h3');h.textContent=model;
  const badge=document.createElement('span');badge.className='badge';badge.textContent='Queued';
  head.append(h,badge);
  const body=document.createElement('pre');body.textContent='Waiting…';
  card.append(head,body);$('results').prepend(card);
  return{card,badge,body};
}
async function runOne(model,prompt,system){
  const ui=addCard(model);ui.badge.textContent='Running';
  try{
    if(category==='text'){
      ui.body.textContent=await textCall(model,prompt,system);
    }else{
      const blob=await imageCall(model,prompt);
      const img=document.createElement('img');img.alt='Generated by '+model;img.src=URL.createObjectURL(blob);
      ui.body.replaceWith(img);
      const p=document.createElement('p');
      const a=document.createElement('a');a.href=img.src;a.download=model.replace(/[^a-z0-9_-]/gi,'_')+'.png';a.textContent='⬇ Download image';
      p.append(a);ui.card.append(p);
    }
    ui.badge.textContent='Complete';ui.badge.className='badge ok';
  }catch(e){
    ui.body.textContent=`Request failed: ${e.message}\n\n(Check the model name, service limits, or browser CORS.)`;
    ui.badge.textContent='Failed';ui.badge.className='badge err';
  }
}

/* ---------- run ---------- */
function setBusy(b){busy=b;$('run').disabled=b;$('clear').disabled=b;$('stop').hidden=!b;$('modelSearch').disabled=b;document.querySelectorAll('input[name="callMode"]').forEach(r=>r.disabled=b)}
$('stop').addEventListener('click',()=>{stopFlag=true;setStatus('Stopping after in-flight requests…')});
async function run(){
  if(busy)return;
  const prompt=$('prompt').value.trim();
  if(!prompt){setStatus('Enter a prompt first.','err');return}
  let models=selected();
  if(getMode()==='single'){
    if(models.length>1){const keep=models[0];document.querySelectorAll('#models input').forEach(c=>c.checked=(c.value===keep));models=[keep]}
    if(models.length!==1){setStatus('Single mode: pick exactly one model.','err');return}
  }else if(!models.length){models=[...currentModels()]}
  if(!models.length){setStatus('No models available.','err');return}
  stopFlag=false;setBusy(true);
  $('results').replaceChildren();$('resultSummary').textContent=`0 / ${models.length} finished`;
  setStatus(`Calling ${models.length} ${category} model${models.length===1?'':'s'}…`);
  let next=0,done=0;
  const workers=Array.from({length:Math.min(5,models.length)},async()=>{
    while(next<models.length&&!stopFlag){
      const model=models[next++];
      await runOne(model,prompt,$('system').value.trim());
      $('resultSummary').textContent=`${++done} / ${models.length} finished`;
    }
  });
  await Promise.all(workers);
  setBusy(false);update();
  setStatus(stopFlag?`Stopped — ${done}/${models.length} completed.`:`Finished ${models.length} request${models.length===1?'':'s'}.`,stopFlag?'err':'ok');
}
$('run').addEventListener('click',run);
$('clear').addEventListener('click',()=>{$('results').replaceChildren();$('resultSummary').textContent='';setStatus('Ready')});

syncCategory();loadCatalog();
})();
</script></body></html>"""


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
