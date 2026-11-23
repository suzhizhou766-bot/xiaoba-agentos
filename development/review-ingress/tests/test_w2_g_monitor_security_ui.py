"""W2-G5 regression tests for frontend token security and monitor UI fixes.

测试 ← 踩坑日期映射（防复发十条）：
- W2-G1（DOM 活令牌清除）← 2026-11-22 监看页面审查（agent-pool.js / page.js 令牌读完留在 DOM）
- W2-G2（重启后 403 友好刷新提示）← 2026-11-22 监看重启后修改节点误报“请从本机监看页面确认提交”
- W2-G3（节点详情抽屉掉线时限双口径对齐）← 2026-11-22 表单/详情/cli_identity 口径分叉
- W2-G4（旧 6 列历史线路表默认折叠与移除改名补丁）← 2026-11-22 首页历史线路表误导问题
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import types
import unittest
from pathlib import Path

INGRESS_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = INGRESS_DIR.parents[1]
REVIEW_PKG = INGRESS_DIR / "xiaoba_review"
MONITOR_DIR = REPO_ROOT / "monitor"
UI_ASSETS = REPO_ROOT / "development/integrated-candidate/src/xiaoba_agentos/review_ui_assets"
if str(REPO_ROOT / "development/integrated-candidate/src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "development/integrated-candidate/src"))
from xiaoba_agentos.review_ui_install import resolve_asset_path  # noqa: E402


class W2GMonitorSecurityUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")

    def _run_node(self, script: str) -> dict:
        if not self.node:
            self.skipTest("Node.js unavailable")
        proc = subprocess.run([self.node, "-e", script], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)
        return json.loads(proc.stdout)

    def test_g1_agent_pool_removes_dom_token_after_boot_and_still_sends_header(self):
        """W2-G1 (← 2026-11-22): agent-pool.js must strip data-page-token immediately."""
        src = (REVIEW_PKG / "agent-pool.js").read_text(encoding="utf-8")
        out = self._run_node(
            f"""
const src = {json.dumps(src)};
const attrs = {{ 'data-page-token': 'SECRET-LIVE-TOKEN-123' }};
const dataset = {{ pageToken: 'SECRET-LIVE-TOKEN-123' }};
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
global.setTimeout = () => 1;
global.clearTimeout = () => {{}};
global.AbortController = class {{ abort(){{}} signal = {{}}; }};
const seenPosts = [];
global.fetch = async (url, opts) => {{
  if (opts && opts.method === 'POST') {{
    seenPosts.push({{ url, headers: opts.headers }});
    return {{ ok: false, status: 403, json: async () => ({{ ok: false, error: '请从本机监看页面确认提交' }}) }};
  }}
  return {{ ok: true, status: 200, json: async () => ({{ ok: true, event_chain: {{ ok: true }}, nodes: [], waiting_tasks: [] }}) }};
}};
eval(src);
(async () => {{
  let err403 = '';
  try {{
    await window.XiaobaAgentPool.toggle({{ node_id: 'n1', revision: 1, enabled: false }});
  }} catch (e) {{
    err403 = e.message;
  }}
  process.stdout.write(JSON.stringify({{
    domHasDataset: 'pageToken' in dataset,
    domHasAttr: 'data-page-token' in attrs,
    sentToken: seenPosts[0]?.headers?.['X-Xiaoba-Token'] || '',
    err403,
  }}));
}})();
"""
        )
        self.assertFalse(out["domHasDataset"], "data-page-token remained in dataset")
        self.assertFalse(out["domHasAttr"], "data-page-token attribute remained in DOM")
        self.assertEqual(out["sentToken"], "SECRET-LIVE-TOKEN-123")
        self.assertIn("Command+R", out["err403"])
        self.assertIn("重启", out["err403"])

    def test_g1_page_js_removes_meta_token_and_rewrites_403(self):
        """W2-G1 & W2-G2 (← 2026-11-22): page.js removes meta token and shows friendly 403."""
        src = (REVIEW_PKG / "page.js").read_text(encoding="utf-8")
        out = self._run_node(
            f"""
const src = {json.dumps(src)};
let metaRemoved = false;
const metaEl = {{ content: 'META-LIVE-TOKEN-999', remove(){{ metaRemoved = true; }} }};
const listeners = new Map();
const makeEl = (id) => ({{
  id, textContent: '', value: '', disabled: false, className: '',
  addEventListener(ev, fn){{ listeners.set(id + ':' + ev, fn); }},
  replaceChildren(){{}}, append(){{}}, querySelector(){{ return null; }}, options: [],
}});
const els = new Map();
const getEl = (id) => {{ if (!els.has(id)) els.set(id, makeEl(id)); return els.get(id); }};
global.document = {{
  activeElement: null,
  getElementById: getEl,
  createElement: (tag) => makeEl(tag),
  querySelector: (sel) => sel === 'meta[name="xiaoba-page-token"]' ? (metaRemoved ? null : metaEl) : null,
}};
global.location = {{ search: '' }};
global.setInterval = () => 1;
const seen = [];
global.fetch = async (url, opts) => {{
  if (opts && opts.method === 'POST') {{
    seen.push(opts.headers['X-Xiaoba-Token']);
    return {{ ok: false, status: 403, json: async () => ({{ ok: false, error: '请从本机监看页面确认提交' }}) }};
  }}
  return {{ ok: true, status: 200, json: async () => ({{ ok: true, inbox_online: true, sources: [], requests: [], decision_packets: [] }}) }};
}};
eval(src);
(async () => {{
  await listeners.get('retry:click')();
  process.stdout.write(JSON.stringify({{
    metaRemoved,
    sentToken: seen[0] || '',
    message: getEl('message').textContent,
  }}));
}})();
"""
        )
        self.assertTrue(out["metaRemoved"])
        self.assertEqual(out["sentToken"], "META-LIVE-TOKEN-999")
        self.assertIn("Command+R", out["message"])

    def test_g2_monitor_web_403_message_is_actionable(self):
        """W2-G2 (← 2026-11-22): monitor_core/web.py 403 token error tells user to Command+R."""
        if str(MONITOR_DIR) not in sys.path:
            sys.path.insert(0, str(MONITOR_DIR))
        from monitor_core import web as mon_web

        ctx = mon_web.Context({}, None, 8799)
        app = mon_web.App(ctx)
        route = mon_web.Route("POST", "/api/test", lambda req: (200, {"ok": True}), guard="write")
        raw = types.SimpleNamespace(
            headers={
                "Host": "127.0.0.1:8799",
                "Origin": "http://127.0.0.1:8799",
                "X-Xiaoba-Token": "stale-token",
                "Content-Type": "application/json",
            }
        )
        msg = app.check(route, raw)
        self.assertIsNotNone(msg)
        self.assertIn("Command+R", msg)
        self.assertIn("刷新", msg)
        # Non-token cross-site access still stays blocked with host/origin wording
        raw_cross = types.SimpleNamespace(
            headers={"Host": "evil.example:8799", "Origin": "http://evil.example"}
        )
        self.assertEqual(app.check(route, raw_cross), "仅允许本机直接访问")

    def test_g3_drawer_timeout_copy_matches_cli_identity_defaults(self):
        """W2-G3 (← 2026-11-22): empty timeout shows both 300s task / 90s probe defaults."""
        src = (REVIEW_PKG / "agent-pool.js").read_text(encoding="utf-8")
        self.assertIn("任务默认 300 秒 / 测活默认 90 秒", src)
        self.assertNotIn("(node.no_progress_seconds||300)+' 秒'", src)

    def test_g4_legacy_table_collapsed_and_rename_patch_removed_and_csp_clean(self):
        """W2-G4 (← 2026-11-22): legacy 6-col table collapsed by default, no rename hack, CSP clean."""
        native_js = (REVIEW_PKG / "agent-pool-native.js").read_text(encoding="utf-8")
        self.assertNotIn("历史线路资料", native_js)
        self.assertNotIn("agentRows", native_js)

        for path in (
            MONITOR_DIR / "xiaoba-monitor.html",
            resolve_asset_path(UI_ASSETS, "xiaoba-monitor.html"),
        ):
            html = path.read_text(encoding="utf-8")
            m = re.search(r'<details[^>]*id="legacyAgentsPanel"[^>]*>', html)
            self.assertIsNotNone(m, f"missing collapsed legacyAgentsPanel in {path}")
            self.assertNotIn(" open", m.group(0), "legacyAgentsPanel must be collapsed by default")
            self.assertIn('id="agentRows"', html)
            self.assertNotRegex(html, r'id="legacyAgentsPanel"[^>]*\bon\w+=')

        # W3-M1 single-source verification: resolve_asset_path resolves directly to canonical monitor/
        self.assertEqual(
            (MONITOR_DIR / "xiaoba-monitor.html").resolve(),
            resolve_asset_path(UI_ASSETS, "xiaoba-monitor.html").resolve(),
        )
        self.assertEqual(
            (MONITOR_DIR / "monitor_core/web.py").resolve(),
            resolve_asset_path(UI_ASSETS, "monitor_web.py").resolve(),
        )


if __name__ == "__main__":
    unittest.main()
