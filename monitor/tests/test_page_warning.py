"""卡 6 尾巴：端口监听检测不可用的警告必须写在页面正文里，不能只做标签上的悬浮提示。

运行：/usr/bin/python3 -m unittest discover -s tests
"""
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from monitor_core.modules.system import SystemModule  # noqa: E402

PAGE = Path(__file__).resolve().parent.parent / "xiaoba-monitor.html"
WARNING = "端口 8799 的监听检测不可用（找不到 lsof），无法确认它是否在运行"


class StubModule:
    def __init__(self, module_id, title, health):
        self.id, self.title, self.kind, self._health = module_id, title, "service", health

    def health(self):
        return dict(self._health)


class StubContext:
    def __init__(self, root, page, port, modules):
        self.config = {"page_file": str(page), "root": str(root)}
        self.page_token = "tok-for-tests-0000"
        self.port = port
        self.started_at = time.time()
        self.modules = modules


class WarningBannerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.page = self.root / "page.html"
        self.page.write_text('<div id="app"></div>', encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def build(self, warning=WARNING):
        module = StubModule("keypool", "Key 池", {"status": "ok", "detail": "2 个节点可用；" + warning, "warning": warning})
        ctx = StubContext(self.root, self.page, 8799, {"keypool": module})
        return ctx, SystemModule(ctx)

    def test_warning_is_written_into_the_page_body(self):
        ctx, system = self.build()
        code, health = system.api_health(None)
        self.assertEqual(code, 200)
        self.assertEqual(health["modules"][0]["warning"], WARNING)
        page = system.index(SimpleNamespace(headers={"Host": "127.0.0.1:8799"})).body.decode("utf-8")
        self.assertIn('id="pageWarn"', page)      # 正文里留好了位置
        self.assertIn("检测不可用", page)          # 而且这次真的写进去了
        self.assertIn("Key 池：", page)
        self.assertLess(page.index('id="pageWarn"'), page.index('<div id="app">'))  # 在页面内容之前，一眼能看到

    def test_banner_keeps_a_place_for_the_script_before_any_warning(self):
        ctx, system = self.build()
        page = system.index(SimpleNamespace(headers={"Host": "127.0.0.1:8799"})).body.decode("utf-8")
        self.assertIn('id="pageWarn"', page)
        self.assertIn("hidden", page)             # 还没读到警告时是空的，页面脚本随后会填

    def test_page_script_keeps_the_banner_up_to_date(self):
        # 卡 66：不再只对 HTML 源码做子串断言；先跑健康接口再渲染正文，核对横幅真的带上警告。
        ctx, system = self.build()
        code, health = system.api_health(None)
        self.assertEqual(code, 200)
        self.assertEqual(WARNING, health["modules"][0]["warning"])
        page = system.index(SimpleNamespace(headers={"Host": "127.0.0.1:8799"})).body.decode("utf-8")
        self.assertIn('id="pageWarn"', page)
        self.assertIn("检测不可用", page)
        self.assertIn("Key 池：", page)
        self.assertNotIn("hidden", page.split('id="pageWarn"', 1)[1].split(">", 1)[0])
        text = PAGE.read_text(encoding="utf-8")
        self.assertIn("m.warning", text)
        self.assertIn("box.hidden = !warn.length", text)


if __name__ == "__main__":
    unittest.main()
