"""3D 导演台模块离线测试：用临时目录里的假构建产物，不依赖真实前端工程。"""
import hashlib
import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from monitor_core import config as cfg, logs  # noqa: E402
from monitor_core.modules.stage import StageModule  # noqa: E402
from monitor_core.web import App, Context  # noqa: E402


class StageTests(unittest.TestCase):
    def serve(self, build=True):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        if build:
            (root / "dist" / "assets").mkdir(parents=True)
            (root / "dist" / "models").mkdir()
            (root / "dist" / "index.html").write_text("<!doctype html><div id=root></div>", encoding="utf-8")
            (root / "dist" / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
            (root / "dist" / "models" / "box.glb").write_bytes(b"glTF\x02\x00")
            (root / "dist" / ".hidden").write_text("no", encoding="utf-8")
            (root / "secret.txt").write_text("outside", encoding="utf-8")
        config = cfg.load(root / "none.json", {"stage": {"dir": str(root / "dist"), "library_dir": str(root / "library")}})
        config["root"] = str(root)
        self.ctx = Context(config, logs.setup(None), 0)
        self.app = App(self.ctx)
        self.module = StageModule(self.ctx)
        self.app.register(self.module)
        self.httpd = self.app.server()
        self.port = self.httpd.server_address[1]
        self.ctx.hosts = {"127.0.0.1:%d" % self.port}
        self.ctx.origins = {"http://127.0.0.1:%d" % self.port}
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def get(self, path, host=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", path, headers={"Host": host or "127.0.0.1:%d" % self.port})
        response = conn.getresponse()
        body, headers = response.read(), dict(response.getheaders())
        conn.close()
        return response.status, headers, body

    def test_serves_only_built_files_with_types_and_isolating_policy(self):
        self.serve()
        for path in ("/stage", "/stage/", "/stage/?t=1"):
            code, headers, body = self.get(path)
            self.assertEqual((code, headers["Content-Type"]), (200, "text/html; charset=utf-8"), path)
            self.assertIn(b"id=root", body)
        policy = headers["Content-Security-Policy"]
        self.assertIn("frame-ancestors 'self'", policy)
        self.assertIn("default-src 'none'", policy)
        self.assertIn("connect-src http://127.0.0.1:0/stage/ data: blob:", policy)
        self.assertEqual(self.get("/stage/assets/app.js")[1]["Content-Type"], "text/javascript; charset=utf-8")
        code, headers, body = self.get("/stage/models/box.glb")
        self.assertEqual((code, headers["Content-Type"], body), (200, "model/gltf-binary", b"glTF\x02\x00"))
        for path in ("/stage/.hidden", "/stage/../secret.txt", "/stage/secret.txt", "/stage/assets/", "/stage/assets/nope.js"):
            self.assertEqual(self.get(path)[0], 404, path)
        self.assertEqual(self.get("/stage/", host="evil.example")[0], 403)
        self.assertEqual(self.get("/stage/assets/app.js", host="evil.example")[0], 403)
        self.assertEqual(self.module.health()["status"], "ok")
        for path in ("/stage/%2e%2e/secret.txt", "/stage/assets/../../secret.txt", "/stage//assets/app.js", "/stagex/index.html"):
            self.assertEqual(self.get(path)[0], 404, path)

    def test_rebuilt_files_are_served_without_restart(self):
        self.serve()
        root = self.module.dir
        (root / "assets" / "app.js").unlink()
        (root / "assets" / "app-NEWHASH.js").write_text("console.log(2)", encoding="utf-8")
        (root / "index.html").write_text("<!doctype html><script src=/stage/assets/app-NEWHASH.js></script>", encoding="utf-8")
        self.assertIn(b"NEWHASH", self.get("/stage/")[2])
        self.assertEqual(self.get("/stage/assets/app-NEWHASH.js")[2], b"console.log(2)")
        self.assertEqual(self.get("/stage/assets/app.js")[0], 404)

    def post(self, path, body, origin=True, raw=False):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Host": "127.0.0.1:%d" % self.port, "Content-Type": "application/octet-stream" if raw else "application/json"}
        if origin:
            headers["Origin"] = "http://127.0.0.1:%d" % self.port
        conn.request("POST", path, body=body if raw else json.dumps(body).encode("utf-8"), headers=headers)
        response = conn.getresponse()
        data = json.loads(response.read() or b"{}")
        conn.close()
        return response.status, data

    def get_json(self, path):
        code, _, body = self.get(path)
        return code, json.loads(body)

    def test_library_stores_drafts_as_files_and_assets_once_by_hash(self):
        self.serve()
        glb = b"glTF" + bytes(range(200)) * 50
        digest = hashlib.sha256(glb).hexdigest()
        project = {"version": 1, "name": "夜戏", "shots": [{"id": "s1"}, {"id": "s2"}],
                   "assets": [{"id": "a", "name": "chair.glb", "format": "glb", "data": "xbasset:sha256:" + digest}]}
        self.assertEqual(self.get_json("/stage/api/library")[1], {"ok": True, "drafts": []})
        # 引用了还没上传的素材：拒绝保存
        code, data = self.post("/stage/api/draft", {"id": "draft-1", "name": "夜戏", "project": project})
        self.assertEqual((code, data["missing"]), (409, [digest]))
        self.assertEqual(self.post("/stage/api/assets/missing", {"hashes": [digest]})[1]["missing"], [digest])
        self.assertEqual(self.post("/stage/api/asset?h=%s&type=model/gltf-binary" % digest, glb[:-1], raw=True)[0], 400)
        self.assertEqual(self.post("/stage/api/asset?h=%s&type=text/html" % digest, glb, raw=True)[0], 400)
        self.assertEqual(self.post("/stage/api/asset?h=%s&type=model/gltf-binary" % digest, glb, raw=True)[0], 200)
        self.assertEqual(self.post("/stage/api/asset?h=%s&type=model/gltf-binary" % digest, glb, raw=True)[0], 200)
        self.assertEqual(self.post("/stage/api/assets/missing", {"hashes": [digest]})[1]["missing"], [])
        for draft in ("draft-1", "draft-2"):
            code, data = self.post("/stage/api/draft", {"id": draft, "name": "夜戏", "project": project, "sessionOwner": "tab-1"})
            self.assertEqual(code, 200, data)
        library = self.module.library
        self.assertEqual(len(list((library / "assets").iterdir())), 1)                 # 两个工程共用一份素材
        self.assertLess((library / "drafts" / "draft-1.json").stat().st_size, 600)     # 工程文件里只有引用
        listed = self.get_json("/stage/api/library")[1]["drafts"]
        self.assertEqual({(d["id"], d["name"], d["shotCount"]) for d in listed}, {("draft-1", "夜戏", 2), ("draft-2", "夜戏", 2)})
        record = self.get_json("/stage/api/draft?id=draft-1")[1]["record"]
        self.assertEqual((record["project"], record["sessionOwner"]), (project, "tab-1"))
        code, headers, body = self.get("/stage/api/asset?h=" + digest)
        self.assertEqual((code, headers["Content-Type"], body), (200, "model/gltf-binary", glb))
        self.assertEqual(self.get_json("/stage/api/draft?id=nope")[1], {"ok": True, "record": None})

    def test_library_rejects_bad_ids_cross_origin_writes_and_cleans_unused_assets(self):
        self.serve()
        project = {"version": 1, "name": "x", "shots": [], "assets": []}
        for bad in ("../evil", "a/b", "", "x" * 81, 5):
            self.assertEqual(self.post("/stage/api/draft", {"id": bad, "name": "x", "project": project})[0], 400, bad)
        self.assertEqual(self.post("/stage/api/draft", {"id": "ok", "name": "x", "project": "nope"})[0], 400)
        self.assertEqual(self.post("/stage/api/draft", {"id": "ok", "name": "x", "project": project}, origin=False)[0], 403)
        self.assertEqual(self.get("/stage/api/asset?h=../../secret")[0], 400)
        self.assertEqual(self.get("/stage/api/asset?h=" + "0" * 64)[0], 404)
        self.assertFalse((self.module.library / "drafts").exists())
        png = b"\x89PNG" + b"z" * 100
        digest = hashlib.sha256(png).hexdigest()
        self.post("/stage/api/asset?h=%s&type=image/png" % digest, png, raw=True)
        using = dict(project, sceneImages=[{"id": "i", "data": "xbasset:sha256:" + digest}])
        self.post("/stage/api/draft", {"id": "keep", "name": "x", "project": using})
        self.post("/stage/api/draft", {"id": "gone", "name": "x", "project": project})
        asset = next((self.module.library / "assets").iterdir())
        os.utime(str(asset), (1, 1))                                                   # 过了宽限期
        self.assertEqual(self.post("/stage/api/draft/delete", {"id": "gone"})[1]["assetsRemoved"], 0)   # 还有草稿在用
        self.assertEqual(self.post("/stage/api/draft/delete", {"id": "keep"})[1]["assetsRemoved"], 1)
        self.assertEqual(self.post("/stage/api/draft/delete", {"id": "keep"})[0], 200)
        self.assertEqual(self.get_json("/stage/api/library")[1]["drafts"], [])

    def test_missing_build_is_reported_instead_of_crashing(self):
        self.serve(build=False)
        self.assertEqual(self.get("/stage/")[0], 404)
        health = self.module.health()
        self.assertEqual(health["status"], "failed")
        self.assertIn("npm run build", health["detail"])


if __name__ == "__main__":
    unittest.main()


class ComposeTests(StageTests):
    """用 Key 池模型写最终提示词：假的 Key 池，不发任何真实请求。"""

    def pool(self, nodes, answer=None, status=200):
        import http.server
        seen = self.seen = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                seen.append((self.path, self.headers.get("Authorization"), body))
                data = json.dumps(answer if answer is not None else
                                  {"model": "real-model", "choices": [{"message": {"content": "  最终提示词  "}}]}).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        fake = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=fake.serve_forever, daemon=True).start()
        self.addCleanup(fake.server_close)
        self.addCleanup(fake.shutdown)
        self.module.keypool_url = "http://127.0.0.1:%d" % fake.server_address[1]
        self.module.keypool_file.write_text(json.dumps({"masterKey": "sk-master-secret", "nodes": nodes}), encoding="utf-8")

    MESSAGES = [{"role": "system", "content": "写提示词"}, {"role": "user", "content": "镜头数据"}]

    def test_info_lists_model_names_only(self):
        self.serve()
        self.assertFalse(self.get_json("/stage/api/compose")[1]["available"])       # 没有 Key 池配置
        self.pool([{"id": "a", "baseUrl": "https://x/v1", "key": "k-node-secret", "model": "m-b"},
                   {"id": "b", "baseUrl": "https://x/v1", "payKey": "k2", "model": "m-a"},
                   {"id": "c", "baseUrl": "https://x/v1", "key": "k3", "enabled": False}])
        code, _, raw = self.get("/stage/api/compose")
        self.assertEqual(json.loads(raw), {"available": True, "models": ["m-a", "m-b"], "followsClient": False})
        self.assertNotIn(b"secret", raw)

    def test_compose_calls_the_pool_with_the_master_key_and_returns_text(self):
        self.serve()
        self.pool([{"id": "a", "baseUrl": "https://x/v1", "key": "k"}])
        code, data = self.post("/stage/api/compose", {"model": "tierflow_pro", "messages": self.MESSAGES})
        self.assertEqual((code, data["ok"], data["text"], data["model"]), (200, True, "最终提示词", "real-model"))
        path, auth, sent = self.seen[0]
        self.assertEqual((path, auth, sent["model"], sent["stream"]), ("/v1/chat/completions", "Bearer sk-master-secret",
                                                                       "tierflow_pro", False))
        self.assertEqual(sent["messages"], self.MESSAGES)
        self.assertNotIn("sk-master-secret", json.dumps(data))

    def test_compose_needs_same_origin_and_valid_input(self):
        self.serve()
        self.pool([{"id": "a", "baseUrl": "https://x/v1", "key": "k"}])
        good = {"model": "m", "messages": self.MESSAGES}
        self.assertEqual(self.post("/stage/api/compose", good, origin=False)[0], 403)
        for bad in ({"messages": self.MESSAGES}, {"model": "m", "messages": []},
                    {"model": "m", "messages": [{"role": "tool", "content": "x"}]},
                    {"model": "m", "messages": [{"role": "user", "content": ""}]}):
            self.assertEqual(self.post("/stage/api/compose", bad)[0], 400, bad)
        self.assertEqual(self.seen, [])

    def test_pool_errors_and_empty_answers_are_reported_plainly(self):
        self.serve()
        self.pool([{"id": "a", "baseUrl": "https://x/v1", "key": "k"}], answer={"error": {"message": "额度不足"}}, status=403)
        data = self.post("/stage/api/compose", {"model": "m", "messages": self.MESSAGES})[1]
        self.assertEqual(data["ok"], False)
        self.assertIn("HTTP 403", data["error"])
        self.pool([{"id": "a", "baseUrl": "https://x/v1", "key": "k"}], answer={"choices": []})
        data = self.post("/stage/api/compose", {"model": "m", "messages": self.MESSAGES})[1]
        self.assertEqual((data["ok"], data["error"]), (False, "模型没有返回内容"))
