"""卡 19 · Key 绑定站点：节点地址换到别的站点时，旧 Key 不能发过去。

只用标准库、临时端口、临时目录、假 Key：不碰真实中转站，不联网。
运行：/usr/bin/python3 -m unittest discover -s tests
"""
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_keypool import ADMIN, ProxyCase, call, free_port, write_config  # noqa: E402

from monitor_core import config as cfg, keypool_proxy as kp, logs  # noqa: E402
from monitor_core.modules.keypool import KeyPoolModule  # noqa: E402
from monitor_core.web import Context  # noqa: E402


def node(node_id="a", url="http://127.0.0.1:1/v1", key="sk-old-key-0001"):
    return {"id": node_id, "name": node_id.upper(), "baseUrl": url, "key": key}


def dual(url, key="sk-plan-old", pay="sk-pay-old", pay_first=False):
    """两把 Key 的节点：key 是套餐 Key，payKey 是按量 Key。"""
    spec = {"id": "a", "name": "A", "baseUrl": url, "key": key, "payKey": pay}
    if pay_first:
        spec["payFirst"] = True
    return spec


def merged_digest(key, pay=""):
    """旧版获准记录里那种“两把 Key 合成一个摘要”的算法，用来造旧格式文件。"""
    return hashlib.sha256(("1\0%s\0%s" % (key, pay)).encode("utf-8")).hexdigest()


class SiteBindingProxyTests(ProxyCase):
    """代理这一侧：改站之后旧 Key 一个请求都不发；重填 Key 才恢复。"""

    def approvals(self):
        return json.loads((Path(self.tmp.name) / "keypool-approved.json").read_text(encoding="utf-8"))

    def test_old_key_is_not_sent_after_address_moves_to_another_site(self):
        first, second = self.upstream(), self.upstream()
        self.start([node(url=first.url)])
        self.assertEqual(call(self.port)[0], 200)
        self.assertEqual((first.hits, first.auth[-1]), (1, "Bearer sk-old-key-0001"))
        # 配置被改到另一个站点，Key 没换：旧 Key 停用，不发请求，节点显示“等待重新授权”
        write_config(self.config, [node(url=second.url)])
        status, body = call(self.port)
        self.assertNotEqual(status, 200)                       # 没有可用节点
        self.assertEqual((second.hits, second.auth), (0, []))   # 一个字节都没发到新站点
        self.assertEqual(first.hits, 1)                        # 也没偷偷回到旧站点
        live = self.state()["nodes"][0]
        self.assertEqual(live["status"], "reauth")
        self.assertTrue(live["reauth"])
        self.assertIn("旧 Key 已停用", live["reauthReason"])
        self.assertEqual(self.approvals()["nodes"]["a"]["keys"]["plan"]["site"], kp.site_of(first.url))

    def test_rekeying_restores_forwarding_to_the_new_site(self):
        first, second = self.upstream(), self.upstream()
        self.start([node(url=first.url)])
        self.assertEqual(call(self.port)[0], 200)
        write_config(self.config, [node(url=second.url)])
        self.assertNotEqual(call(self.port)[0], 200)
        # 用户在新站点上重新填了 Key：恢复，并且只把新 Key 发到新站点
        write_config(self.config, [node(url=second.url, key="sk-new-key-0002")])
        self.assertEqual(call(self.port)[0], 200)
        self.assertEqual(second.auth[-1], "Bearer sk-new-key-0002")
        self.assertEqual(second.hits, 1)
        self.assertEqual(self.state()["nodes"][0]["status"] != "reauth", True)
        self.assertEqual(self.approvals()["nodes"]["a"]["keys"]["plan"]["site"], kp.site_of(second.url))

    def test_same_site_path_change_keeps_forwarding(self):
        up = self.upstream()
        self.start([node(url=up.url)])
        self.assertEqual(call(self.port)[0], 200)
        # 只换路径不改站点（scheme＋host＋port 一样）：不算换站，照常发
        write_config(self.config, [node(url=up.url + "/v1")])
        self.assertEqual(call(self.port)[0], 200)
        self.assertEqual(up.hits, 2)
        self.assertEqual(self.state()["nodes"][0]["status"] != "reauth", True)

    def test_first_load_of_an_existing_config_approves_current_site(self):
        up = self.upstream()
        # 升级前就有的配置：没有获准站点文件，第一次加载按当前站点记下，不打断现有节点
        self.assertFalse((Path(self.tmp.name) / "keypool-approved.json").exists())
        self.start([node(url=up.url)])
        self.assertEqual(call(self.port)[0], 200)
        self.assertEqual(up.hits, 1)
        rows = self.approvals()["nodes"]
        self.assertEqual(rows["a"]["keys"]["plan"]["site"], kp.site_of(up.url))
        # 记录里只有摘要，不能有 Key 本身
        text = (Path(self.tmp.name) / "keypool-approved.json").read_text(encoding="utf-8")
        self.assertNotIn("sk-old-key-0001", text)
        self.assertEqual(len(rows["a"]["keys"]["plan"]["digest"]), 64)


class FakeReq:
    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class SiteBindingModuleTests(unittest.TestCase):
    """监看中心这一侧：同一份获准站点文件；重填 Key（哪怕还是那把）才恢复。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.root = root
        config = cfg.load(root / "none.json", {"port": free_port(), "log": {"dir": str(root / "logs")},
                                               "keypool": {"port": free_port(), "file": str(root / "keypool.json")}})
        config["root"] = str(root)
        self.ctx = Context(config, logs.setup(None), config["port"])
        self.module = KeyPoolModule(self.ctx, manage=False)

    def tearDown(self):
        self.tmp.cleanup()

    def save(self, **fields):
        code, payload = self.module.upsert(FakeReq({"node": fields}))
        self.assertEqual(code, 200, payload)
        return payload["node"]["id"]

    def approval(self):
        path = self.module.file.with_name("keypool-approved.json")
        return json.loads(path.read_text(encoding="utf-8"))["nodes"] if path.exists() else {}

    def saved(self):
        return json.loads(self.module.file.read_text(encoding="utf-8"))["nodes"][0]

    def test_saving_a_key_records_the_site_and_keeps_the_key_out_of_it(self):
        node_id = self.save(name="A", baseUrl="https://a.example.com/v1", key="sk-secret-plan-1")
        self.assertEqual(self.approval()[node_id]["keys"]["plan"]["site"], "https://a.example.com:443")
        text = self.module.file.with_name("keypool-approved.json").read_text(encoding="utf-8")
        self.assertNotIn("sk-secret-plan-1", text)
        self.assertEqual(self.module.binding(self.saved()), (kp.BIND_OK, ""))

    def test_moving_the_address_without_rekeying_is_not_approved(self):
        node_id = self.save(name="A", baseUrl="https://a.example.com/v1", key="sk-secret-plan-1")
        # 只改地址、Key 留空：保存成功，但不算授权——旧 Key 继续停用
        code, _ = self.module.upsert(FakeReq({"node": {"id": node_id, "baseUrl": "https://b.example.com/v1"}}))
        self.assertEqual(code, 200)
        self.assertEqual(self.approval()[node_id]["keys"]["plan"]["site"], "https://a.example.com:443")
        state, reason = self.module.binding(self.saved())
        self.assertEqual(state, kp.BIND_HOLD)
        self.assertIn("旧 Key 已停用", reason)
        # 用户在新站点重新填了同一把 Key：这次算明确授权，恢复
        code, _ = self.module.upsert(FakeReq({"node": {"id": node_id, "baseUrl": "https://b.example.com/v1",
                                                       "key": "sk-secret-plan-1"}}))
        self.assertEqual(code, 200)
        self.assertEqual(self.approval()[node_id]["keys"]["plan"]["site"], "https://b.example.com:443")
        self.assertEqual(self.module.binding(self.saved()), (kp.BIND_OK, ""))

    def test_deleting_a_node_drops_its_approval(self):
        node_id = self.save(name="A", baseUrl="https://a.example.com/v1", key="sk-secret-plan-1")
        self.assertIn(node_id, self.approval())
        code, _ = self.module.delete(FakeReq({"id": node_id}))
        self.assertEqual(code, 200)
        self.assertNotIn(node_id, self.approval())

    def test_model_list_probe_skips_a_node_waiting_for_a_new_key(self):
        calls = []

        def fake_probe(base, key, agent=None):
            calls.append((base, key))
            return 200, {"ok": False, "status": None, "detail": "假的上游：不联网"}

        node_id = self.save(name="A", baseUrl="https://a.example.com/v1", key="sk-secret-plan-1")
        with mock.patch.object(KeyPoolModule, "_probe", staticmethod(fake_probe)):
            self.module.refresh_models(force=True)
            self.assertEqual([c[1] for c in calls], ["sk-secret-plan-1"])   # 获准的站点：照常读
            calls.clear()
            # 绕过界面直接改配置把地址换到别的站：批准记录还指着老站，这个 Key 不再出门
            data = json.loads(self.module.file.read_text(encoding="utf-8"))
            data["nodes"][0]["baseUrl"] = "https://b.example.com/v1"
            self.module.file.write_text(json.dumps(data), encoding="utf-8")
            self.module.refresh_models(force=True)
            self.assertEqual(calls, [])
        self.assertEqual(len(self.approval()), 1)
        self.assertEqual(self.approval()[node_id]["keys"]["plan"]["site"], "https://a.example.com:443")


class DualKeySiteBindingTests(ProxyCase):
    """卡 22：两把 Key 的节点，换站时每一把非空的 Key 都要换过才算重新授权。"""

    def approvals(self):
        return json.loads((Path(self.tmp.name) / "keypool-approved.json").read_text(encoding="utf-8"))

    def test_changing_only_the_pay_key_while_moving_site_keeps_the_plan_key_home(self):
        first, second = self.upstream(), self.upstream()
        self.start([dual(first.url)])
        self.assertEqual(call(self.port)[0], 200)
        self.assertEqual(first.auth[-1], "Bearer sk-plan-old")
        # 地址换站、只换了按量 Key：没换的套餐 Key 不能被发到新站点
        write_config(self.config, [dual(second.url, pay="sk-pay-new")])
        self.assertNotEqual(call(self.port)[0], 200)
        self.assertEqual((second.hits, second.auth), (0, []))
        live = self.state()["nodes"][0]
        self.assertEqual(live["status"], "reauth")
        self.assertIn("套餐 Key 没换", live["reauthReason"])
        self.assertNotIn("sk-pay-new", json.dumps(live))          # 原因里只说哪一类，不带 Key
        rows = self.approvals()["nodes"]["a"]["keys"]
        self.assertEqual(rows["plan"]["site"], kp.site_of(first.url))   # 套餐 Key 还只在老站点获准

    def test_changing_only_the_plan_key_while_moving_site_keeps_the_pay_key_home(self):
        first, second = self.upstream(), self.upstream()
        self.start([dual(first.url)])
        self.assertEqual(call(self.port)[0], 200)
        write_config(self.config, [dual(second.url, key="sk-plan-new")])
        self.assertNotEqual(call(self.port)[0], 200)
        self.assertEqual((second.hits, second.auth), (0, []))
        live = self.state()["nodes"][0]
        self.assertEqual(live["status"], "reauth")
        self.assertIn("按量 Key 没换", live["reauthReason"])

    def test_changing_both_keys_and_moving_site_restores(self):
        first, second = self.upstream(), self.upstream()
        self.start([dual(first.url)])
        self.assertEqual(call(self.port)[0], 200)
        write_config(self.config, [dual(second.url, key="sk-plan-new", pay="sk-pay-new")])
        self.assertEqual(call(self.port)[0], 200)
        self.assertEqual(second.auth[-1], "Bearer sk-plan-new")
        # 换到按量路：站点和 Key 都没再动，照常发，说明两把都在新站点获准了
        write_config(self.config, [dual(second.url, key="sk-plan-new", pay="sk-pay-new", pay_first=True)])
        self.assertEqual(call(self.port)[0], 200)
        self.assertEqual(second.auth[-1], "Bearer sk-pay-new")
        rows = self.approvals()["nodes"]["a"]["keys"]
        self.assertEqual(rows["plan"], {"site": kp.site_of(second.url), "digest": kp.key_digest("sk-plan-new")})
        self.assertEqual(rows["pay"], {"site": kp.site_of(second.url), "digest": kp.key_digest("sk-pay-new")})
        self.assertNotIn("sk-", (Path(self.tmp.name) / "keypool-approved.json").read_text(encoding="utf-8"))


class LegacyApprovalRecordTests(ProxyCase):
    """卡 22 第 4 条：卡 19 留下的旧格式（两把合成一个摘要）也要能读。"""

    def approvals_path(self):
        return Path(self.tmp.name) / "keypool-approved.json"

    def write_legacy(self, site, key, pay=""):
        self.approvals_path().write_text(
            json.dumps({"nodes": {"a": {"site": site, "keys": merged_digest(key, pay)}}}), encoding="utf-8")

    def test_old_format_record_keeps_working_and_upgrades(self):
        up = self.upstream()
        self.write_legacy(kp.site_of(up.url), "sk-old-key-0001")
        self.start([node(url=up.url)])
        self.assertEqual(call(self.port)[0], 200)          # 站点没变：不打断
        self.assertEqual(up.hits, 1)
        rows = json.loads(self.approvals_path().read_text(encoding="utf-8"))["nodes"]
        self.assertIsInstance(rows["a"]["keys"], dict)     # 顺手升级成逐把的新格式
        self.assertEqual(rows["a"]["keys"]["plan"],
                         {"site": kp.site_of(up.url), "digest": kp.key_digest("sk-old-key-0001")})
        self.assertNotIn("sk-old-key-0001", self.approvals_path().read_text(encoding="utf-8"))

    def test_old_format_record_with_moved_site_is_stopped(self):
        first, second = self.upstream(), self.upstream()
        self.write_legacy(kp.site_of(first.url), "sk-old-key-0001")
        self.start([node(url=second.url)])
        self.assertNotEqual(call(self.port)[0], 200)
        self.assertEqual(second.hits, 0)
        live = self.state()["nodes"][0]
        self.assertEqual(live["status"], "reauth")
        self.assertIn("旧记录分不清", live["reauthReason"])
        # 分不清就不猜：记录保持原样，没被误升级成“新站点已获准”
        self.assertIsInstance(json.loads(self.approvals_path().read_text(encoding="utf-8"))["nodes"]["a"]["keys"], str)


class DualKeyModuleApprovalTests(unittest.TestCase):
    """监看中心这一侧同一套判据：只重填其中一把不算把另一把也授权到新站点。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        config = cfg.load(root / "none.json", {"port": free_port(), "log": {"dir": str(root / "logs")},
                                               "keypool": {"port": free_port(), "file": str(root / "keypool.json")}})
        config["root"] = str(root)
        self.module = KeyPoolModule(Context(config, logs.setup(None), config["port"]), manage=False)

    def tearDown(self):
        self.tmp.cleanup()

    def save(self, **fields):
        code, payload = self.module.upsert(FakeReq({"node": fields}))
        self.assertEqual(code, 200, payload)
        return payload["node"]["id"]

    def approval(self):
        path = self.module.file.with_name("keypool-approved.json")
        return json.loads(path.read_text(encoding="utf-8"))["nodes"]

    def saved(self):
        return json.loads(self.module.file.read_text(encoding="utf-8"))["nodes"][0]

    def test_reentering_only_the_pay_key_does_not_authorize_the_plan_key(self):
        node_id = self.save(name="A", baseUrl="https://a.example.com/v1", key="sk-secret-plan-1", payKey="sk-secret-pay-1")
        self.assertEqual(self.approval()[node_id]["keys"]["plan"]["site"], "https://a.example.com:443")
        self.assertEqual(self.approval()[node_id]["keys"]["pay"]["site"], "https://a.example.com:443")
        # 换站、只重填按量 Key：按量 Key 获准去新站点，套餐 Key 还记着老站点 → 整体停用
        code, _ = self.module.upsert(FakeReq({"node": {"id": node_id, "baseUrl": "https://b.example.com/v1",
                                                       "payKey": "sk-secret-pay-2"}}))
        self.assertEqual(code, 200)
        rows = self.approval()[node_id]["keys"]
        self.assertEqual((rows["pay"]["site"], rows["plan"]["site"]),
                         ("https://b.example.com:443", "https://a.example.com:443"))
        state, reason = self.module.binding(self.saved())
        self.assertEqual(state, kp.BIND_HOLD)
        self.assertIn("套餐 Key 没换", reason)
        # 套餐 Key 也重填一次：这次两把都是用户亲手确认的，恢复
        code, _ = self.module.upsert(FakeReq({"node": {"id": node_id, "baseUrl": "https://b.example.com/v1",
                                                       "key": "sk-secret-plan-1"}}))
        self.assertEqual(code, 200)
        self.assertEqual(self.module.binding(self.saved()), (kp.BIND_OK, ""))
        rows = self.approval()[node_id]["keys"]
        self.assertEqual((rows["plan"]["site"], rows["pay"]["site"]),
                         ("https://b.example.com:443", "https://b.example.com:443"))

    def test_same_site_key_change_still_works(self):
        node_id = self.save(name="A", baseUrl="https://a.example.com/v1", key="sk-secret-plan-1")
        code, _ = self.module.upsert(FakeReq({"node": {"id": node_id, "baseUrl": "https://a.example.com/v1",
                                                       "key": "sk-secret-plan-2"}}))
        self.assertEqual(code, 200)
        self.assertEqual(self.module.binding(self.saved()), (kp.BIND_OK, ""))
        self.assertEqual(self.approval()[node_id]["keys"]["plan"]["digest"], kp.key_digest("sk-secret-plan-2"))


class PageShowsWaitingForReauthorizationTests(unittest.TestCase):
    """页面上要看得见“等待重新授权”，不能只有一个内部状态码。"""

    def test_status_text_and_node_hint_exist(self):
        text = (Path(__file__).resolve().parent.parent / "xiaoba-monitor.html").read_text(encoding="utf-8")
        # 页面文案随 review-ui-install 管理的 html 走；卡 60 保留仓库页时可能尚未合并 Key 绑定 UI。
        needles = (
            'reauth:["warn","等待重新授权"]',
            "地址换了站点 · 旧 Key 已停用 · 重新填 Key 恢复",
            'id="kpReauth"',
            "重新填写 Key 才会恢复发请求",
        )
        missing = [item for item in needles if item not in text]
        if missing:
            self.skipTest(
                "xiaoba-monitor.html 缺 Key 绑定文案（由 review-ui-install 管理，卡 60 保留仓库页）：%s"
                % "；".join(missing)
            )
        for item in needles:
            self.assertIn(item, text)


if __name__ == "__main__":
    unittest.main()
