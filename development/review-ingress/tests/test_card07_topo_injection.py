"""卡 07 C07-01：topo 锚点保护 + agent-pool-native.js 注入不重复/不重叠。

只用静态断言与最小 DOM mock 执行页面脚本，不启动监看服务、不碰 8799、不派活、不调模型。
J11c 人工依据确认交互在这里只核“存在且未被引擎覆盖”，真实点击验证属于卡12。
"""
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
import unittest

REPO = Path(__file__).resolve().parents[3]
MONITOR = REPO / 'monitor'
sys.path[:0] = [str(REPO / 'development/integrated-candidate/src'),
                str(MONITOR), str(Path(__file__).resolve().parents[1])]
sys.dont_write_bytecode = True

from monitor_core.web import Response  # noqa: E402
from xiaoba_agentos.review_ui_install import ASSETS as UI_ASSETS, resolve_asset_path  # noqa: E402
from xiaoba_review.monitor import OpenReviewModule  # noqa: E402

NATIVE_JS = Path(__file__).resolve().parents[1] / 'xiaoba_review' / 'agent-pool-native.js'
AUTHORITATIVE_HTML = MONITOR / 'xiaoba-monitor.html'
INGRESS_HTML = resolve_asset_path(UI_ASSETS, 'xiaoba-monitor.html')


class TopoInjectionTests(unittest.TestCase):
    def test_authoritative_and_ingress_html_are_byte_identical(self):
        # 同源同步：裁决前端资源与权威监看源必须是同一份字节。
        self.assertEqual(AUTHORITATIVE_HTML.read_bytes(), INGRESS_HTML.read_bytes())

    def test_authoritative_anchor_is_single_and_unmodified(self):
        html = AUTHORITATIVE_HTML.read_text(encoding='utf-8')
        self.assertEqual(html.count('id="topo"'), 1)
        self.assertIn('<svg id="topo"', html)
        self.assertNotIn('agent-pool-native.js', html)  # 由 mount_pool 注入，避免重复

    def test_mount_pool_injects_once_and_preserves_anchor(self):
        body = b'<html><body><svg id="topo"></svg><aside id="detail"></aside></body></html>'
        first = OpenReviewModule.mount_pool(Response(200, body, 'text/html'), 'PAGE_TOKEN')
        self.assertEqual(first.body.count(b'id="topo"'), 1)
        self.assertEqual(first.body.count(b'<svg id="topo"'), 1)
        self.assertEqual(first.body.count(b'/agent-pool-native.js'), 1)
        self.assertEqual(first.body.count(b'/agent-pool.js'), 1)
        self.assertIn(b'data-page-token="PAGE_TOKEN"', first.body)
        self.assertNotIn(b'__TOKEN__', first.body)
        self.assertNotIn(b'<iframe', first.body)

    def test_mount_pool_skips_response_without_anchor(self):
        body = b'<html><body><p>no topology here</p></body></html>'
        result = OpenReviewModule.mount_pool(Response(200, body, 'text/html'), 'T')
        self.assertEqual(result.body, body)  # 锚点不存在时不注入，不制造第二段脚本

    def test_mount_pool_non_200_is_untouched(self):
        body = b'<html><body><svg id="topo"></svg></body></html>'
        result = OpenReviewModule.mount_pool(Response(503, body, 'text/html'), 'T')
        self.assertEqual(result.body, body)

    def test_native_js_creates_single_group_and_single_foreign_object(self):
        js = NATIVE_JS.read_text(encoding='utf-8')
        self.assertEqual(js.count("group.id='agentNodeTopology'"), 1)
        self.assertEqual(js.count("createElementNS(ns,'g')"), 2)  # 主组 + 路由子组
        self.assertEqual(js.count("createElementNS(ns,'foreignObject')"), 1)
        self.assertEqual(js.count("topo.append(group)"), 1)

    def test_native_js_appends_into_existing_anchor_not_new_topology(self):
        js = NATIVE_JS.read_text(encoding='utf-8')
        self.assertIn("document.getElementById('topo')", js)
        self.assertNotIn("createElementNS(ns,'svg')", js)  # 不新建第二张 SVG 造成重叠
        self.assertIn("topo.append(group)", js)

    def test_j11c_evidence_confirmation_interaction_is_present(self):
        html = AUTHORITATIVE_HTML.read_text(encoding='utf-8')
        for marker in ('rc-basis-confirm', 'validation_basis_confirmation',
                       'syncBasisConfirmationState', 'renderBasisConfirmations'):
            self.assertIn(marker, html, 'J11c 人工依据确认交互不得被覆盖：' + marker)


if __name__ == '__main__':
    unittest.main()
