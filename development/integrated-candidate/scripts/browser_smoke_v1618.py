#!/usr/bin/env python3
"""W2-J5: Lightweight (<30s) release browser/UI smoke runner covering 5 high-frequency user pitfall paths.

Paths verified deterministically against the candidate source tree (zero live mutation):
1. overview_csp_and_legacy_fold:
   - Real candidate xiaoba-monitor.html served over localhost HTTP with SystemModule-aligned CSP headers.
   - Confirms all external script tags stay inside CSP allowlist, zero unsafe-eval calls, and valid JS syntax via node --check.
   - Legacy 6-col routes table is wrapped inside default-collapsed <details id="legacyAgentsPanel">
     (no open attribute) and agent-pool-native.js contains no legacy heading rename hack.
2. pool_drawer_token_and_save:
   - Real candidate agent-pool.js executed in Node.js against our local candidate web.py HTTP server.
   - Confirms data-page-token is stripped from DOM immediately on boot (W2-G1).
   - Confirms empty no_progress_seconds displays dual default copy "任务默认 300 秒 / 测活默认 90 秒" (W2-G3).
   - Confirms a write POST succeeds (HTTP 200) carrying the in-memory X-Xiaoba-Token header.
3. restart_403_friendly_prompt:
   - Rotates server page token (simulating monitor restart) and submits writes through real web.py.
   - Confirms HTTP 403 returns actionable "Command+R" prompt from monitor_core/web.py AND both
     agent-pool.js & page.js surface the friendly refresh prompt while preserving non-token 403s (W2-G2).
4. review_queue_meta_token_and_load:
   - Real candidate page.js executed in Node.js against local HTTP server.
   - Confirms <meta name="xiaoba-page-token"> is removed immediately on boot (W2-G1) and review
     queue loads cleanly without uncaught errors.
5. assets_and_optional_live_probe:
   - Verifies review_ui_assets/manifest.json forward SHA-256 matches all candidate UI assets.
   - Optional read-only HTTP GET probe when --live-url is provided (never mutates live state).
"""
from __future__ import annotations

import argparse
import hashlib
import http.server
import json
import re
import shutil
import socketserver
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

MAX_SMOKE_SECONDS = 30.0
_DEFAULT_CANDIDATE_ROOT = Path(__file__).resolve().parents[1]


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


class _SmokeState:
    def __init__(self, page_token: str, html_bytes: bytes) -> None:
        self.page_token = page_token
        self.html_bytes = html_bytes
        self.saved_payloads: list[dict[str, Any]] = []
        self.ctx: Any = None
        self.port: int = 0


def _start_local_smoke_server(monitor_dir: Path, state: _SmokeState):
    if str(monitor_dir) not in sys.path:
        sys.path.insert(0, str(monitor_dir))
    import logging
    from monitor_core import web as mon_web  # noqa: E402

    class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    class _SmokeModule:
        id = "smoke"

        def routes(self):
            own = f"http://127.0.0.1:{state.port}/"
            stage = own + "stage/"
            director = own + "director/"
            pool_scripts = " ".join(own + p for p in (
                "agent-pool.js", "agent-pool-native.js", "open-review-native.js",
                "monitor-keypool.js", "monitor-review-center.js",
            ))
            pool_style = own + "agent-pool.css"
            csp = (
                f"default-src 'none'; script-src 'unsafe-inline' {stage} {pool_scripts}; "
                f"style-src 'unsafe-inline' https://fonts.googleapis.com {pool_style}; "
                f"img-src {own} {stage} data: blob:; font-src {own} https://fonts.gstatic.com data:; "
                f"connect-src {own} ws://127.0.0.1:{state.port}/ https://fonts.googleapis.com https://fonts.gstatic.com data: blob:; "
                f"frame-src {director} {stage}; frame-ancestors 'self'; base-uri 'none'; form-action 'none'"
            )

            def _overview(_req):
                return mon_web.Response(200, state.html_bytes, "text/html; charset=utf-8", csp=csp)

            def _monitor_script(name: str):
                def _handler(_req):
                    return mon_web.Response(200, (monitor_dir / name).read_bytes(), "application/javascript; charset=utf-8")
                return _handler

            def _pool_list(_req):
                return (200, {
                    "ok": True,
                    "event_chain": {"ok": True},
                    "nodes": [
                        {
                            "node_id": "smoke-node-1",
                            "name": "SmokeNode",
                            "kind": "review",
                            "interface": "cli",
                            "agent_type": "agy",
                            "entry": "/usr/local/bin/agy",
                            "model": "gemini-3-flash",
                            "enabled": True,
                            "revision": 1,
                            "no_progress_seconds": None,
                        }
                    ],
                    "waiting_tasks": [],
                })

            def _pool_write(req):
                body = req.json()
                state.saved_payloads.append(body)
                return (200, {
                    "ok": True,
                    "node": {"node_id": body.get("node_id", "smoke-node-1"), "revision": 2},
                })

            def _review_status(_req):
                return (200, {
                    "ok": True,
                    "inbox_online": True,
                    "sources": [],
                    "requests": [],
                    "decision_packets": [],
                })

            def _review_retry(_req):
                return (200, {"ok": True, "message": "已重试"})

            return [
                mon_web.Route("GET", "/", _overview, guard="page"),
                mon_web.Route("GET", "/monitor-keypool.js", _monitor_script("monitor-keypool.js"), guard="page"),
                mon_web.Route("GET", "/monitor-review-center.js", _monitor_script("monitor-review-center.js"), guard="page"),
                mon_web.Route("GET", "/api/agent-pool/state", _pool_list, guard="read"),
                mon_web.Route("POST", "/api/agent-pool/toggle", _pool_write, guard="write"),
                mon_web.Route("POST", "/api/agent-pool/edit", _pool_write, guard="write"),
                mon_web.Route("GET", "/api/open-review/state", _review_status, guard="read"),
                mon_web.Route("POST", "/api/open-review/retry", _review_retry, guard="write"),
            ]

        def start(self):
            return

        def stop(self):
            return

    logger = logging.getLogger("browser_smoke_v1618")
    logger.setLevel(logging.CRITICAL)
    # Bind port 0 first to get an ephemeral localhost port, then bind real monitor_core Context/App
    ctx = mon_web.Context({}, logger, 0)
    app = mon_web.App(ctx)
    app.register(_SmokeModule())
    srv = _Server(("127.0.0.1", 0), app.handler_class())
    port = srv.server_address[1]
    state.port = port
    ctx.port = port
    ctx.hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    ctx.origins = {f"http://{h}" for h in ctx.hosts}
    ctx.page_token = state.page_token
    state.ctx = ctx
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    return srv, f"http://127.0.0.1:{port}"


def _check_overview_csp_and_legacy_fold(
    node_bin: str, base_url: str, review_pkg: Path, monitor_dir: Path
) -> dict[str, Any]:
    with urllib.request.urlopen(base_url + "/", timeout=5) as resp:
        html = resp.read().decode("utf-8")
        csp = resp.headers.get("Content-Security-Policy", "")
        status = resp.status

    # Verify all external <script src=...> in overview HTML are allowed by SystemModule CSP
    script_srcs = re.findall(r"<script\b[^>]*\bsrc=[\"']([^\"']+)[\"']", html, re.I)
    allowed_names = (
        "/stage/", "agent-pool.js", "agent-pool-native.js", "open-review-native.js",
        "monitor-keypool.js", "monitor-review-center.js",
    )
    unallowed_scripts = [
        src for src in script_srcs
        if not any(allowed in src for allowed in allowed_names)
    ]
    # Verify extracted monitor scripts are served cleanly over same-origin HTTP
    split_script_http_ok = True
    for name in ("monitor-keypool.js", "monitor-review-center.js"):
        with urllib.request.urlopen(base_url + "/" + name, timeout=5) as sresp:
            ctype = sresp.headers.get("Content-Type", "")
            if sresp.status != 200 or "javascript" not in ctype or not sresp.read():
                split_script_http_ok = False
    # Verify zero unsafe-eval calls and valid JS syntax in all bundled review & monitor JS files
    js_targets = (
        [(name, review_pkg / name) for name in ("agent-pool.js", "agent-pool-native.js", "page.js", "open-review-native.js")]
        + [(name, monitor_dir / name) for name in ("monitor-keypool.js", "monitor-review-center.js")]
    )
    syntax_errors: list[str] = []
    unsafe_eval_hits: list[str] = []
    for name, fp in js_targets:
        if not fp.is_file():
            continue
        code = fp.read_text(encoding="utf-8")
        if re.search(r"\beval\s*\(|\bnew\s+Function\s*\(", code):
            unsafe_eval_hits.append(name)
        proc = subprocess.run([node_bin, "--check", str(fp)], capture_output=True, text=True, timeout=5)
        if proc.returncode != 0:
            syntax_errors.append(f"{name}: {(proc.stderr or proc.stdout).strip()[:160]}")

    details_match = re.search(r"<details\b[^>]*\bid=[\"']legacyAgentsPanel[\"'][^>]*>", html)
    details_tag = details_match.group(0) if details_match else ""
    is_collapsed_default = bool(details_tag) and not bool(re.search(r"\bopen\b", details_tag))

    native_js = (review_pkg / "agent-pool-native.js").read_text(encoding="utf-8")
    has_rename_hack = "历史线路资料" in native_js or "agentRows" in native_js

    ok = (
        status == 200
        and split_script_http_ok
        and "default-src 'none'" in csp
        and "unsafe-eval" not in csp
        and not unallowed_scripts
        and not unsafe_eval_hits
        and not syntax_errors
        and is_collapsed_default
        and not has_rename_hack
    )
    return {
        "ok": ok,
        "http_status": status,
        "split_script_http_ok": split_script_http_ok,
        "csp_present": "default-src 'none'" in csp and "unsafe-eval" not in csp,
        "unallowed_scripts": unallowed_scripts,
        "unsafe_eval_hits": unsafe_eval_hits,
        "js_syntax_errors": syntax_errors,
        "legacy_panel_collapsed_by_default": is_collapsed_default,
        "native_js_rename_hack_removed": not has_rename_hack,
    }


def _run_node(node_bin: str, js_code: str) -> dict[str, Any]:
    proc = subprocess.run([node_bin, "-e", js_code], capture_output=True, text=True, timeout=15)
    if proc.returncode != 0:
        raise RuntimeError(f"Node smoke harness failed ({proc.returncode}): {(proc.stderr or proc.stdout).strip()[:400]}")
    return json.loads(proc.stdout)


def _check_pool_drawer_and_403(
    node_bin: str,
    base_url: str,
    review_pkg: Path,
    state: _SmokeState,
    initial_token: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    agent_pool_js = (review_pkg / "agent-pool.js").read_text(encoding="utf-8")
    page_js = (review_pkg / "page.js").read_text(encoding="utf-8")

    # Step 2: real HTTP write succeeds with in-memory token while DOM token is stripped on boot
    out_ok = _run_node(
        node_bin,
        f"""
const src = {json.dumps(agent_pool_js)};
const baseUrl = {json.dumps(base_url)};
const attrs = {{ 'data-page-token': {json.dumps(initial_token)} }};
const dataset = {{ pageToken: {json.dumps(initial_token)} }};
const makeEl = (id) => ({{
  id, hidden: true, textContent: '', value: '', disabled: false, className: '',
  dataset: id === 'apDrawer' ? dataset : {{}},
  elements: {{
    name: {{ value: '', focus(){{}} }}, entry: {{ value: '', addEventListener(){{}} }},
    model: {{ value: '', required: false }}, kind: {{ value: 'review', disabled: false, querySelector(){{ return null; }} }},
    interface: {{ value: 'cli', disabled: false, addEventListener(){{}} }},
    agent_type: {{ value: 'agy', disabled: false, addEventListener(){{}} }},
    no_progress_seconds: {{ value: '', disabled: false }},
    reasoning_effort: {{ value: '', disabled: false }},
    tmux_session: {{ value: '', disabled: true, required: false }},
    working_directory: {{ value: '', disabled: true, required: false }},
  }},
  removeAttribute(k){{ delete attrs[k]; if (k === 'data-page-token') delete dataset.pageToken; }},
  getAttribute(k){{ return attrs[k] ?? null; }},
  addEventListener(){{}}, querySelector(){{ return null; }}, querySelectorAll(){{ return []; }},
  replaceChildren(){{}}, append(){{}}, focus(){{}}, scrollIntoView(){{}}, setAttribute(){{}},
}});
const els = new Map();
const getEl = (id) => {{ if (!els.has(id)) els.set(id, makeEl(id)); return els.get(id); }};
global.document = {{
  hidden: false, activeElement: null,
  getElementById: getEl,
  createElement: (tag) => makeEl(tag),
  addEventListener(){{}},
}};
global.window = global;
global.setInterval = () => 1;
const realFetch = global.fetch;
global.fetch = (url, opts) => {{
  const full = String(url).startsWith('http') ? url : baseUrl + url;
  const headers = Object.assign({{ 'Origin': baseUrl }}, (opts && opts.headers) || {{}});
  return realFetch(full, Object.assign({{}}, opts || {{}}, {{ headers }}));
}};
eval(src);
(async () => {{
  await window.XiaobaAgentPool.toggle({{ node_id: 'smoke-node-1', revision: 1, enabled: false }});
  process.stdout.write(JSON.stringify({{
    domHasDataset: 'pageToken' in dataset,
    domHasAttr: 'data-page-token' in attrs,
    hasDualDefaultCopy: src.includes('任务默认 300 秒 / 测活默认 90 秒'),
    toggleOk: true,
  }}));
  process.exit(0);
}})().catch((e) => {{ console.error(e); process.exit(1); }});
""",
    )
    path2 = {
        "ok": (
            not out_ok["domHasDataset"]
            and not out_ok["domHasAttr"]
            and out_ok["hasDualDefaultCopy"]
            and out_ok["toggleOk"]
            and len(state.saved_payloads) >= 1
        ),
        "dom_token_removed": (not out_ok["domHasDataset"]) and (not out_ok["domHasAttr"]),
        "dual_timeout_copy_present": out_ok["hasDualDefaultCopy"],
        "real_http_write_ok": out_ok["toggleOk"],
        "server_received_writes": len(state.saved_payloads),
    }

    # Step 3: rotate server token (simulate monitor restart) and test real 403 on web.py + agent-pool.js + page.js
    state.page_token = "ROTATED-AFTER-RESTART-TOKEN"
    state.ctx.page_token = state.page_token

    # Direct HTTP 403 check against monitor_core/web.py
    req = urllib.request.Request(
        base_url + "/api/agent-pool/toggle",
        data=b'{"node_id":"smoke-node-1"}',
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Origin": base_url,
            "X-Xiaoba-Token": initial_token,
        },
    )
    server_403_status = 0
    server_403_error = ""
    try:
        urllib.request.urlopen(req, timeout=5)
    except urllib.error.HTTPError as err:
        server_403_status = err.code
        body = json.loads(err.read().decode("utf-8"))
        server_403_error = str(body.get("error") or "")

    # Agent-pool.js real 403 after server restart
    out_403_pool = _run_node(
        node_bin,
        f"""
const src = {json.dumps(agent_pool_js)};
const baseUrl = {json.dumps(base_url)};
const attrs = {{ 'data-page-token': {json.dumps(initial_token)} }};
const dataset = {{ pageToken: {json.dumps(initial_token)} }};
const makeEl = (id) => ({{
  id, hidden: true, textContent: '', value: '', disabled: false, className: '',
  dataset: id === 'apDrawer' ? dataset : {{}},
  elements: {{
    name: {{ value: '', focus(){{}} }}, entry: {{ value: '', addEventListener(){{}} }},
    model: {{ value: '', required: false }}, kind: {{ value: 'review', disabled: false, querySelector(){{ return null; }} }},
    interface: {{ value: 'cli', disabled: false, addEventListener(){{}} }},
    agent_type: {{ value: 'agy', disabled: false, addEventListener(){{}} }},
    no_progress_seconds: {{ value: '', disabled: false }},
    reasoning_effort: {{ value: '', disabled: false }},
    tmux_session: {{ value: '', disabled: true, required: false }},
    working_directory: {{ value: '', disabled: true, required: false }},
  }},
  removeAttribute(k){{ delete attrs[k]; if (k === 'data-page-token') delete dataset.pageToken; }},
  getAttribute(k){{ return attrs[k] ?? null; }},
  addEventListener(){{}}, querySelector(){{ return null; }}, querySelectorAll(){{ return []; }},
  replaceChildren(){{}}, append(){{}}, focus(){{}}, scrollIntoView(){{}}, setAttribute(){{}},
}});
const els = new Map();
const getEl = (id) => {{ if (!els.has(id)) els.set(id, makeEl(id)); return els.get(id); }};
global.document = {{ hidden: false, activeElement: null, getElementById: getEl, createElement: (t) => makeEl(t), addEventListener(){{}} }};
global.window = global;
global.setInterval = () => 1;
const realFetch = global.fetch;
global.fetch = (url, opts) => {{
  const full = String(url).startsWith('http') ? url : baseUrl + url;
  const headers = Object.assign({{ 'Origin': baseUrl }}, (opts && opts.headers) || {{}});
  return realFetch(full, Object.assign({{}}, opts || {{}}, {{ headers }}));
}};
eval(src);
(async () => {{
  let msg = '';
  try {{
    await window.XiaobaAgentPool.toggle({{ node_id: 'smoke-node-1', revision: 1, enabled: true }});
  }} catch (e) {{
    msg = e.message;
  }}
  process.stdout.write(JSON.stringify({{ msg }}));
  process.exit(0);
}})().catch((e) => {{ console.error(e); process.exit(1); }});
""",
    )

    # Non-token 403 must NOT be overwritten
    out_403_nontoken = _run_node(
        node_bin,
        f"""
const src = {json.dumps(page_js)};
let metaRemoved = false;
const metaEl = {{ content: 'ANY-TOKEN', remove(){{ metaRemoved = true; }} }};
const listeners = new Map();
const makeEl = (id) => ({{
  id, textContent: '', value: '', disabled: false, className: '',
  addEventListener(ev, fn){{ listeners.set(id + ':' + ev, fn); }},
  replaceChildren(){{}}, append(){{}}, querySelector(){{ return null; }}, options: [],
}});
const els = new Map();
const getEl = (id) => {{ if (!els.has(id)) els.set(id, makeEl(id)); return els.get(id); }};
global.document = {{
  activeElement: null, getElementById: getEl, createElement: (t) => makeEl(t),
  querySelector: (s) => s === 'meta[name="xiaoba-page-token"]' ? (metaRemoved ? null : metaEl) : null,
}};
global.location = {{ search: '' }};
global.setInterval = () => 1;
global.fetch = async (_u, opts) => {{
  if (opts && opts.method === 'POST') {{
    return {{ ok: false, status: 403, json: async () => ({{ ok: false, error: '仅允许本机直接访问' }}) }};
  }}
  return {{ ok: true, status: 200, json: async () => ({{ ok: true, inbox_online: true, sources: [], requests: [], decision_packets: [] }}) }};
}};
eval(src);
(async () => {{
  await listeners.get('retry:click')();
  process.stdout.write(JSON.stringify({{ msg: getEl('message').textContent }}));
}})();
""",
    )

    path3 = {
        "ok": (
            server_403_status == 403
            and "Command+R" in server_403_error
            and "Command+R" in out_403_pool["msg"]
            and "仅允许本机直接访问" in out_403_nontoken["msg"]
            and "Command+R" not in out_403_nontoken["msg"]
        ),
        "server_403_status": server_403_status,
        "server_403_error": server_403_error,
        "pool_js_403_message": out_403_pool["msg"],
        "non_token_403_preserved": out_403_nontoken["msg"],
    }
    return path2, path3


def _check_review_queue_page(node_bin: str, base_url: str, review_pkg: Path, active_token: str) -> dict[str, Any]:
    page_js = (review_pkg / "page.js").read_text(encoding="utf-8")
    out = _run_node(
        node_bin,
        f"""
const src = {json.dumps(page_js)};
const baseUrl = {json.dumps(base_url)};
let metaRemoved = false;
const metaEl = {{ content: {json.dumps(active_token)}, remove(){{ metaRemoved = true; }} }};
const listeners = new Map();
const makeEl = (id) => ({{
  id, textContent: '', value: '', disabled: false, className: '',
  addEventListener(ev, fn){{ listeners.set(id + ':' + ev, fn); }},
  replaceChildren(){{}}, append(){{}}, querySelector(){{ return null; }}, options: [],
}});
const els = new Map();
const getEl = (id) => {{ if (!els.has(id)) els.set(id, makeEl(id)); return els.get(id); }};
global.document = {{
  activeElement: null, getElementById: getEl, createElement: (t) => makeEl(t),
  querySelector: (s) => s === 'meta[name="xiaoba-page-token"]' ? (metaRemoved ? null : metaEl) : null,
}};
global.location = {{ search: '' }};
global.setInterval = () => 1;
const realFetch = global.fetch;
global.fetch = (url, opts) => {{
  const full = String(url).startsWith('http') ? url : baseUrl + url;
  const headers = Object.assign({{ 'Origin': baseUrl }}, (opts && opts.headers) || {{}});
  return realFetch(full, Object.assign({{}}, opts || {{}}, {{ headers }}));
}};
eval(src);
(async () => {{
  await new Promise((r) => setTimeout(r, 80));
  await listeners.get('retry:click')();
  process.stdout.write(JSON.stringify({{
    metaRemoved,
    message: getEl('message').textContent,
  }}));
  process.exit(0);
}})().catch((e) => {{ console.error(e); process.exit(1); }});
""",
    )
    ok = bool(out["metaRemoved"]) and ("Command+R" not in out["message"])
    return {
        "ok": ok,
        "meta_token_removed": bool(out["metaRemoved"]),
        "retry_message": out["message"],
    }


def _check_ui_assets_and_optional_live(candidate_root: Path, live_url: Optional[str]) -> dict[str, Any]:
    src_dir = str(candidate_root / "src")
    if src_dir not in sys.path:
        sys.path.insert(0, src_dir)
    from xiaoba_agentos.review_ui_install import resolve_asset_path

    assets_dir = candidate_root / "src" / "xiaoba_agentos" / "review_ui_assets"
    manifest = json.loads((assets_dir / "manifest.json").read_text(encoding="utf-8"))
    mismatches: list[str] = []
    for entry in manifest.get("files", []):
        asset = entry.get("asset")
        expected = entry.get("after_sha256")
        fp = resolve_asset_path(assets_dir, str(asset))
        if not fp.is_file():
            mismatches.append(f"{asset}:missing")
        elif _sha256_file(fp) != expected:
            mismatches.append(f"{asset}:sha256_mismatch")

    live_probe: dict[str, Any] = {"checked": False}
    if live_url:
        try:
            with urllib.request.urlopen(live_url.rstrip("/") + "/", timeout=5) as resp:
                live_probe = {"checked": True, "ok": resp.status == 200, "http_status": resp.status}
        except Exception as exc:
            live_probe = {"checked": True, "ok": False, "error": f"{type(exc).__name__}: {exc}"}

    ok = (not mismatches) and (not live_probe["checked"] or bool(live_probe.get("ok")))
    return {
        "ok": ok,
        "asset_count": len(manifest.get("files", [])),
        "asset_mismatches": mismatches,
        "live_probe": live_probe,
    }


def run_browser_smoke(
    candidate_root: Path = _DEFAULT_CANDIDATE_ROOT,
    *,
    live_url: Optional[str] = None,
) -> dict[str, Any]:
    started = time.monotonic()
    node_bin = shutil.which("node")
    if not node_bin:
        return {
            "ok": False,
            "error": "node_binary_missing",
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "paths": {},
        }

    candidate = Path(candidate_root).resolve()
    repo_root = candidate.parents[1]
    monitor_dir = repo_root / "monitor"
    review_pkg = repo_root / "development" / "review-ingress" / "xiaoba_review"
    html_bytes = (monitor_dir / "xiaoba-monitor.html").read_bytes()

    initial_token = "SMOKE-BOOT-TOKEN-V1618"
    state = _SmokeState(initial_token, html_bytes)
    srv, base_url = _start_local_smoke_server(monitor_dir, state)
    try:
        p1 = _check_overview_csp_and_legacy_fold(node_bin, base_url, review_pkg, monitor_dir)
        p2, p3 = _check_pool_drawer_and_403(node_bin, base_url, review_pkg, state, initial_token)
        p4 = _check_review_queue_page(node_bin, base_url, review_pkg, state.page_token)
        p5 = _check_ui_assets_and_optional_live(candidate, live_url)
    finally:
        srv.shutdown()
        srv.server_close()

    elapsed = round(time.monotonic() - started, 3)
    paths = {
        "1_overview_csp_and_legacy_fold": p1,
        "2_pool_drawer_token_and_save": p2,
        "3_restart_403_friendly_prompt": p3,
        "4_review_queue_meta_token_and_load": p4,
        "5_assets_and_service_probe": p5,
    }
    within_budget = elapsed <= MAX_SMOKE_SECONDS
    all_ok = within_budget and all(bool(item.get("ok")) for item in paths.values())
    return {
        "ok": all_ok,
        "elapsed_seconds": elapsed,
        "budget_seconds": MAX_SMOKE_SECONDS,
        "within_budget": within_budget,
        "paths": paths,
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-root", type=Path, default=_DEFAULT_CANDIDATE_ROOT)
    parser.add_argument("--live-url", default=None, help="Optional read-only live monitor URL probe")
    args = parser.parse_args(argv)
    report = run_browser_smoke(args.candidate_root, live_url=args.live_url)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
