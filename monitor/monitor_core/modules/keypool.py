"""Key 池模块：把多个中转站的 API Key 合并成一个，并在监看中心里实时显示与管理。

结构与裁决后端一致：真正转发请求的是独立子进程 monitor_core/keypool_proxy.py
（只监听 127.0.0.1，默认端口 8780），监看中心负责拉起、看护、随监看中心一起关闭。
这样代理流量出问题不会拖垮监看页面，监看页面卡住也不会影响正在进行的请求。

配置文件 keypool.json（默认在监看目录，不进版本历史，权限 600）只由本模块写入：
  {"masterKey": "...", "nodes": [{"id","name","baseUrl","key","weight","maxConcurrency","enabled","pos"}],
   "settings": {...}}
代理发现文件变了会自动重新加载，所以增删改节点不需要重启任何东西。
页面上看不到完整的中转站 Key；合并 Key 只在点“复制”时经带令牌的接口取一次。

节点可以另外填余额查询用的网页会话（balanceCookie、balanceUser，见 keypool_balance.py）。
它们同样只存在 keypool.json，页面只知道“填没填”；余额只在用户点“查询余额”时去查。
"""
import json
import os
import re
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .. import config as cfg
from .. import keypool_balance
from .. import keypool_stats
from ..web import Response
from ..keypool_proxy import CLIENT_UA, DEFAULT_SETTINGS, join_url, mask, model_names, station_of
from ..supervise import Backoff, Breaker, ManagedProcess
from ..web import Route
from . import Module

ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
MAX_NODES = 64
BALANCE_WATCH_INTERVAL = 60
BALANCE_INTERVAL = 3      # Key 池页面开着时，余额自动刷新的间隔（秒）
BALANCE_BACKOFF = 30      # 被限流或出错后，这个节点先停这么久再自动查
BALANCE_AUTH_BACKOFF = 300  # 会话失效不会自己好：自动刷新隔久一点再试，重新填写或手动查询会立刻再查
MODELS_INTERVAL = 6 * 3600  # 每一路的模型清单多久重新读一次
MODELS_BACKOFF = 300        # 读模型清单失败（多半是限流）后，这一路先停这么久再读
MODEL_FIELDS = (("key", "models"), ("payKey", "payModels"))  # Key → 它的模型清单存在节点的哪个字段
MAX_ALIASES = 32


def new_master_key():
    return "sk-xb-" + secrets.token_urlsafe(24)


def new_node_id():
    return "n_" + secrets.token_hex(4)


class KeyPoolModule(Module):
    id, title, kind = "keypool", "Key 池", "view"

    def __init__(self, ctx, manage=True):
        super().__init__(ctx)
        settings = ctx.config["keypool"]
        self.port = int(settings["port"])
        self.file = cfg.resolve(ctx.config, settings["file"])
        self.python = settings.get("python") or sys.executable
        self.script = Path(__file__).resolve().parent.parent / "keypool_proxy.py"
        self.state_file = cfg.resolve(ctx.config, "run") / "keypool.json"
        self.admin_token = secrets.token_urlsafe(32)
        self.manage = manage
        self.process = None
        self.blocked = ""          # 端口被别的程序占用时的说明
        self.lock = threading.Lock()  # 串行化对 keypool.json 的读改写
        self.balances = {}            # 节点编号 → 最近一次余额查询结果（只在内存里）
        self.balance_lock = threading.Lock()
        self.balance_get = keypool_balance.http_get
        self.balance_cache = {}       # 节点编号 → 换算单位与套餐的短时缓存
        self.balance_wait = {}        # 节点编号 → 自动刷新最早可以再查的时间
        self.balance_auto_at = 0.0
        self.closing = threading.Event()
        self.usage_dir = str(cfg.resolve(ctx.config, ctx.config["keypool"].get("usage_dir", "run/keypool-usage")))
        self.balance_logged = {}      # 节点编号 → 上一次记进余额流水的 (按量, 套餐)
        self.models_next = {}         # (节点编号, Key 字段) → 这一路的模型清单最早什么时候再读（只在内存里）
        self.models_due = threading.Event()   # 节点刚改过：模型清单马上重读，不等下一轮

    # ---- 配置文件 ----
    def load(self):
        try:
            with open(self.file, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if not isinstance(data, dict):
                raise ValueError("keypool.json 必须是 JSON 对象")
        except FileNotFoundError:
            data = {}
        data.setdefault("masterKey", "")
        if not isinstance(data.get("nodes"), list):
            data["nodes"] = []
        if not isinstance(data.get("settings"), dict):
            data["settings"] = {}
        return data

    def save(self, data):
        self.file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.file.with_name(self.file.name + ".tmp-%d" % os.getpid())
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
        os.chmod(str(tmp), 0o600)
        os.replace(str(tmp), str(self.file))  # 原子替换：代理不会读到写了一半的文件

    def ensure_file(self):
        with self.lock:
            data = self.load()
            changed = not self.file.exists()
            if not data["masterKey"]:
                data["masterKey"] = new_master_key()
                changed = True
            if changed:
                self.save(data)
            elif (self.file.stat().st_mode & 0o077) != 0:
                os.chmod(str(self.file), 0o600)

    # ---- 生命周期 ----
    def start(self):
        self.ensure_file()
        if not self.manage:
            return
        self._retire_previous()
        if self._port_busy():
            self.blocked = "端口 %d 被其他程序占用，Key 池代理没有启动" % self.port
            self.ctx.log(self.id, "port busy", "error", port=self.port)
            return
        watchdog = self.ctx.config["watchdog"]
        env = dict(os.environ)
        env["KEYPOOL_ADMIN_TOKEN"] = self.admin_token
        log_dir = cfg.resolve(self.ctx.config, self.ctx.config["log"]["dir"])
        log_dir.mkdir(parents=True, exist_ok=True)
        self.process = ManagedProcess(
            "keypool", [self.python, str(self.script), "--config", str(self.file), "--port", str(self.port),
                        "--usage-dir", self.usage_dir],
            self.ctx.logger, env=env, cwd=str(self.script.parent), umask=0o077,
            ready=lambda: self.admin("/_admin/state", timeout=2)[0] == 200, ready_timeout=20,
            backoff=Backoff(watchdog["backoff_seconds"]),
            breaker=Breaker(watchdog["max_fast_failures"], watchdog["fast_failure_seconds"]),
            output_path=str(log_dir / "keypool.log"))
        self.process.start()
        threading.Thread(target=self._record_pid, name="keypool-pid", daemon=True).start()
        self.closing.clear()
        threading.Thread(target=self._balance_watch, name="keypool-balance-watch", daemon=True).start()
        threading.Thread(target=self._models_watch, name="keypool-models-watch", daemon=True).start()
        self.ctx.log(self.id, "proxy managed by monitor", port=self.port)

    def stop(self):
        self.closing.set()
        self.models_due.set()
        if self.process:
            self.process.stop()
        try:
            self.state_file.unlink()
        except OSError:
            pass

    def reset(self):
        if self.process:
            self.process.reset()

    def _port_busy(self):
        with socket.socket() as sock:
            sock.settimeout(0.5)
            return sock.connect_ex(("127.0.0.1", self.port)) == 0

    def _listener_pid(self):
        try:
            out = subprocess.run(["/usr/sbin/lsof", "-nP", "-tiTCP:%d" % self.port, "-sTCP:LISTEN"],
                                 capture_output=True, text=True, timeout=5).stdout.split()
            return int(out[0]) if out else None
        except (OSError, ValueError, subprocess.SubprocessError):
            return None

    def _record_pid(self):
        for _ in range(40):
            status = self.process.status() if self.process else {}
            if status.get("state") == "running" and status.get("pid"):
                try:
                    self.state_file.parent.mkdir(parents=True, exist_ok=True)
                    self.state_file.write_text(json.dumps({"pid": status["pid"], "port": self.port}), encoding="utf-8")
                except OSError:
                    pass
                return
            time.sleep(0.5)

    def _retire_previous(self):
        """监看中心被强行结束时留下的旧代理：确认是它还占着端口，就结束它（它的管理令牌已经丢了，没法接管）。"""
        try:
            record = json.loads(self.state_file.read_text(encoding="utf-8"))
            pid = int(record["pid"])
        except (OSError, ValueError, KeyError, TypeError):
            return
        if pid and pid == self._listener_pid():
            try:
                os.kill(pid, signal.SIGTERM)
                self.ctx.log(self.id, "stopped proxy left by a previous run", pid=pid)
            except OSError:
                pass
            for _ in range(20):
                if not self._port_busy():
                    break
                time.sleep(0.25)
        try:
            self.state_file.unlink()
        except OSError:
            pass

    # ---- 与代理通信 ----
    def admin(self, path, timeout=3):
        request = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path),
                                         headers={"X-KeyPool-Admin": self.admin_token})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, {"ok": False, "error": "代理返回 HTTP %d" % exc.code}
        except Exception as exc:
            return 502, {"ok": False, "error": "代理没有响应：%s" % exc}

    def process_health(self):
        if self.blocked:
            return {"status": "failed", "detail": self.blocked}
        if not self.process:
            return {"status": "starting" if self.manage else "warn", "detail": "代理未启动"}
        status = self.process.status()
        state = status["state"]
        mapped = {"running": "ok", "starting": "starting", "backoff": "warn", "failed": "failed"}.get(state, "warn")
        detail = {"running": "代理运行中", "starting": "代理正在启动", "backoff": "代理已退出，等待自动重启",
                  "failed": status["error"] or "代理反复退出，已停止自动重启"}.get(state, state)
        return {"status": mapped, "detail": detail, "pid": status["pid"], "restarts": status["restarts"]}

    def health(self):
        base = self.process_health()
        if base["status"] != "ok":
            return base
        data = self.load()
        nodes = [n for n in data["nodes"] if n.get("enabled", True) is not False]
        if not nodes:
            return {"status": "warn", "detail": "还没有启用的节点", "pid": base.get("pid")}
        code, live = self.admin("/_admin/state?since=999999999", timeout=2)
        if code != 200:
            return {"status": "warn", "detail": live.get("error") or "读不到代理状态", "pid": base.get("pid")}
        usable = [n for n in live.get("nodes", []) if n["status"] in ("ok", "idle", "busy")]
        trouble = [n for n in live.get("nodes", []) if n["status"] in ("cooling", "down")]
        if not usable:
            return {"status": "failed" if trouble else "warn", "detail": "没有可用节点", "pid": base.get("pid")}
        detail = "%d 个节点可用" % len(usable) + ("，%d 个冷却或故障" % len(trouble) if trouble else "")
        return {"status": "warn" if trouble else "ok", "detail": detail, "pid": base.get("pid")}

    # ---- 路由 ----
    def routes(self):
        return [
            Route("GET", "/api/keypool/state", self.state, "read"),
            Route("POST", "/api/keypool/nodes", self.upsert, "write"),
            Route("POST", "/api/keypool/nodes/delete", self.delete, "write"),
            Route("POST", "/api/keypool/nodes/test", self.test, "write"),
            Route("POST", "/api/keypool/layout", self.layout, "write"),
            Route("POST", "/api/keypool/master-key", self.master_key, "write"),
            Route("POST", "/api/keypool/master-key/rotate", self.rotate, "write"),
            Route("POST", "/api/keypool/restart", self.restart, "write"),
            Route("POST", "/api/keypool/settings", self.set_settings, "write"),
            Route("POST", "/api/keypool/published-models", self.set_published, "write"),
            Route("POST", "/api/keypool/stations", self.set_station, "write"),
            Route("POST", "/api/keypool/models/refresh", self.models_refresh, "write"),
            Route("POST", "/api/keypool/release", self.release, "write"),
            Route("POST", "/api/keypool/balance", self.balance, "write"),
            Route("GET", "/api/keypool/stats", self.stats, "read"),
            Route("POST", "/api/keypool/stats/baseline", self.set_baseline, "write"),
            Route("GET", "/api/keypool/usage", self.usage, "read"),
            Route("GET", "/api/keypool/usage.csv", self.usage_csv, "read"),
        ]

    def public_node(self, node):
        return {"id": node.get("id"), "name": node.get("name") or node.get("id"), "baseUrl": node.get("baseUrl") or "",
                "keyMasked": mask(node.get("key")), "hasKey": bool(node.get("key")), "weight": node.get("weight", 1),
                "payKeyMasked": mask(node.get("payKey")), "hasPayKey": bool(node.get("payKey")),
                "payFirst": bool(node.get("payFirst")), "payFloor": node.get("payFloor") or 0,
                "maxConcurrency": node.get("maxConcurrency") or None, "enabled": node.get("enabled", True) is not False,
                "pos": node.get("pos") if isinstance(node.get("pos"), dict) else None,
                "slot": node.get("slot") if isinstance(node.get("slot"), dict) else None,
                "hasBalance": bool(node.get("balanceCookie") and node.get("balanceUser")),
                "model": node.get("model") or "", "payModel": node.get("payModel") or "",
                "userAgent": node.get("userAgent") or "",
                "models": model_names(node.get("models")), "payModels": model_names(node.get("payModels")),
                "aliases": node.get("aliases") if isinstance(node.get("aliases"), dict) else {}}

    def state(self, req):
        try:
            since = int(req.param("since", "0") or 0)
        except ValueError:
            since = 0
        data = self.load()
        if req.param("balance") == "1":
            self._balance_kick(data["nodes"])  # Key 池页面开着时才带这个参数
        code, live = self.admin("/_admin/state?since=%d" % since, timeout=3)
        live = live if code == 200 and isinstance(live, dict) else None
        stats = {n["id"]: n for n in (live or {}).get("nodes", [])}
        nodes = []
        for node in data["nodes"]:
            item = self.public_node(node)
            item["live"] = stats.get(node.get("id"))
            item["balance"] = self.balances.get(node.get("id")) if item["hasBalance"] else None
            nodes.append(item)
        return 200, {
            "ok": True, "health": self.process_health(), "online": live is not None, "slotLayout": True,
            "endpoint": "http://127.0.0.1:%d" % self.port, "masterKeyMasked": mask(data["masterKey"], 6, 4),
            "nodes": nodes, "settings": dict(DEFAULT_SETTINGS, **data["settings"]),
            "publishedModels": model_names(data.get("publishedModels")), "modelCatalog": self.catalog(data["nodes"]),
            "stations": self.stations(data, (live or {}).get("stations") or []),
            "totals": (live or {}).get("totals"), "events": (live or {}).get("events", []),
            "seq": (live or {}).get("seq", since), "configError": (live or {}).get("configError"),
            "proxyStartedAt": (live or {}).get("startedAt"),
            "guardEnabled": (live or {}).get("guardEnabled", True),
            "security": (live or {}).get("security", {"counts": {}, "events": []})}

    @staticmethod
    def _clean_url(value):
        value = str(value or "").strip().rstrip("/")
        parsed = urllib.parse.urlparse(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.query or parsed.fragment:
            raise ValueError("地址要以 http:// 或 https:// 开头，例如 https://api.example.com/v1")
        if parsed.username or parsed.password:
            raise ValueError("地址里不要带用户名或密码")
        return value

    @staticmethod
    def _number(value, kind, low, high, name):
        if value in (None, ""):
            return None
        try:
            number = kind(value)
        except (TypeError, ValueError):
            raise ValueError("%s 必须是数字" % name)
        if not low <= number <= high:
            raise ValueError("%s 要在 %s 到 %s 之间" % (name, low, high))
        return number

    def upsert(self, req):
        body = req.json().get("node")
        if not isinstance(body, dict):
            raise ValueError("缺少 node")
        with self.lock:
            data = self.load()
            node_id = body.get("id")
            current = None
            if node_id:
                if not ID_RE.match(str(node_id)):
                    raise ValueError("节点编号无效")
                current = next((n for n in data["nodes"] if n.get("id") == node_id), None)
                if current is None:
                    return 404, {"ok": False, "error": "找不到这个节点，可能已被删除"}
            elif len(data["nodes"]) >= MAX_NODES:
                return 400, {"ok": False, "error": "节点最多 %d 个" % MAX_NODES}
            node = dict(current or {"id": new_node_id(), "enabled": True, "weight": 1})
            if "name" in body or current is None:
                name = str(body.get("name") or "").strip()
                if not name or len(name) > 40:
                    raise ValueError("名称不能为空，最多 40 个字")
                node["name"] = name
            if "baseUrl" in body or current is None:
                node["baseUrl"] = self._clean_url(body.get("baseUrl"))
            # 双路：key 是套餐 Key（只有一个 Key 的节点也填它），payKey 是余额（按量）Key。留空不修改。
            for field, clear in (("key", "keyClear"), ("payKey", "payKeyClear")):
                key = str(body.get(field) or "").strip()
                if len(key) > 512 or any(ch.isspace() for ch in key):
                    raise ValueError("Key 格式不对")
                if key:
                    node[field] = key
                elif body.get(clear):
                    node.pop(field, None)
            if not (node.get("key") or node.get("payKey")):
                raise ValueError("节点至少要有一个 Key")
            if "payFirst" in body:
                node["payFirst"] = bool(body.get("payFirst"))
            if "payFloor" in body:
                node["payFloor"] = self._number(body.get("payFloor"), float, 0, 1000000, "余额底线") or 0
            # 余额查询用的网页会话：留空不修改；balanceClear 清除。
            cookie = str(body.get("balanceCookie") or "").strip()
            if cookie.lower().startswith("session="):
                cookie = cookie[len("session="):].strip()
            tf_user = str(body.get("balanceUser") or "").strip()
            for value, limit, name in ((cookie, 4096, "Session Cookie"), (tf_user, 512, "TF-User")):
                if len(value) > limit or any(ch.isspace() or ord(ch) < 33 or ch in ";," for ch in value):
                    raise ValueError("%s 格式不对：只填值本身，不要带空格、分号或换行" % name)
            if body.get("balanceClear"):
                node.pop("balanceCookie", None)
                node.pop("balanceUser", None)
            if cookie:
                node["balanceCookie"] = cookie
            if tf_user:
                node["balanceUser"] = tf_user
            if (cookie or tf_user) and not (node.get("balanceCookie") and node.get("balanceUser")):
                raise ValueError("余额查询需要同时填写 Session Cookie 和 TF-User")
            if cookie or tf_user or body.get("balanceClear") or "baseUrl" in body:
                for store in (self.balances, self.balance_cache, self.balance_wait):
                    store.pop(node["id"], None)
            # Key 或地址变了：这一路原来读到的模型清单不再作数，后台马上重读
            for key_field, list_field in MODEL_FIELDS:
                if current is None or node.get(key_field) != current.get(key_field) or node.get("baseUrl") != current.get("baseUrl"):
                    node.pop(list_field, None)
                    self.models_next.pop((node["id"], key_field), None)
            # 别名：客户端要的模型名 → 这个节点上的模型名
            if "aliases" in body:
                aliases = body.get("aliases") or {}
                if not isinstance(aliases, dict) or len(aliases) > MAX_ALIASES:
                    raise ValueError("别名格式不对")
                clean = {}
                for asked, sent in aliases.items():
                    pair = model_names([asked, sent])
                    if len(pair) != 2:
                        raise ValueError("别名要写成“客户端要的模型名 = 这个节点上的模型名”，两边不能相同")
                    clean[pair[0]] = pair[1]
                if clean:
                    node["aliases"] = clean
                else:
                    node.pop("aliases", None)
            # 池里没有任何一路有客户端要的模型时，这个节点改用的模型；留空表示原样发。payModel 是余额路的，留空表示和套餐路相同
            for field in ("model", "payModel"):
                if field not in body:
                    continue
                model = str(body.get(field) or "").strip()
                if len(model) > 160 or any(ord(ch) < 32 for ch in model):
                    raise ValueError("模型名称无效")
                if model:
                    node[field] = model
                else:
                    node.pop(field, None)
            # 这个节点专用的客户端标识：个别中转站前面的防护只认特定标识。留空表示客户端带什么就转发什么
            if "userAgent" in body:
                agent = str(body.get("userAgent") or "").strip()
                if len(agent) > 200 or any(ord(ch) < 32 or ord(ch) > 126 for ch in agent):
                    raise ValueError("客户端标识只能用英文字符，最长 200 个")
                if agent:
                    node["userAgent"] = agent
                else:
                    node.pop("userAgent", None)
            if "weight" in body:
                node["weight"] = self._number(body.get("weight"), float, 0.1, 100, "权重") or 1
            if "maxConcurrency" in body:
                node["maxConcurrency"] = self._number(body.get("maxConcurrency"), int, 0, 1000, "最大并发") or 0
            if "enabled" in body:
                node["enabled"] = bool(body.get("enabled"))
            if isinstance(body.get("pos"), dict):
                node["pos"] = self._pos(body["pos"])
            if current is None:
                data["nodes"].append(node)
            else:
                data["nodes"][data["nodes"].index(current)] = node
            self.save(data)
        self.models_due.set()
        self.ctx.log(self.id, "node saved", node=node["id"], name=node["name"], created=current is None)
        return 200, {"ok": True, "node": self.public_node(node)}

    @staticmethod
    def _pos(value):
        x = KeyPoolModule._number(value.get("x"), float, -5000, 5000, "位置")
        y = KeyPoolModule._number(value.get("y"), float, -5000, 5000, "位置")
        return {"x": round(x or 0, 1), "y": round(y or 0, 1)}

    @staticmethod
    def _slot(value):
        col = KeyPoolModule._number(value.get("col"), int, 0, 7, "格位")
        row = KeyPoolModule._number(value.get("row"), int, 0, 255, "格位")
        return {"col": col or 0, "row": row or 0}

    def delete(self, req):
        node_id = str(req.json().get("id") or "")
        with self.lock:
            data = self.load()
            kept = [n for n in data["nodes"] if n.get("id") != node_id]
            if len(kept) == len(data["nodes"]):
                return 404, {"ok": False, "error": "找不到这个节点"}
            data["nodes"] = kept
            self.save(data)
        for store in (self.balances, self.balance_cache, self.balance_wait):
            store.pop(node_id, None)
        self.ctx.log(self.id, "node deleted", node=node_id)
        return 200, {"ok": True}

    def layout(self, req):
        positions = req.json().get("positions")
        if not isinstance(positions, dict):
            raise ValueError("缺少 positions")
        with self.lock:
            data = self.load()
            for node in data["nodes"]:
                place = positions.get(node.get("id"))
                if not isinstance(place, dict):
                    continue
                if "col" in place or "row" in place:
                    # 格位（第几列、第几行）：不随画布宽度变，页面按它吸附排布。旧的像素位置不再需要
                    node["slot"] = self._slot(place)
                    node.pop("pos", None)
                else:
                    node["pos"] = self._pos(place)
            hub = positions.get("_hub")
            if isinstance(hub, dict):
                data.setdefault("layout", {})["hub"] = self._pos(hub)
            self.save(data)
        return 200, {"ok": True}

    def test(self, req):
        """测试连通：用节点的地址和 Key 请求一次 /v1/models（不产生费用）。"""
        body = req.json()
        field = "payKey" if body.get("which") == "pay" else "key"
        base, key = body.get("baseUrl"), str(body.get(field) or "").strip()
        agent = body.get("userAgent")
        if body.get("id"):
            node = next((n for n in self.load()["nodes"] if n.get("id") == body["id"]), None)
            if node is None:
                return 404, {"ok": False, "error": "找不到这个节点"}
            base = base or node.get("baseUrl")
            key = key or node.get(field) or ""
            agent = node.get("userAgent") if agent is None else agent
        base = self._clean_url(base)
        if not key:
            raise ValueError("缺少 Key")
        agent = str(agent or "").strip()
        if len(agent) > 200 or any(ord(ch) < 32 or ord(ch) > 126 for ch in agent):
            raise ValueError("客户端标识只能用英文字符，最长 200 个")
        code, result = self._probe(base, key, agent)
        if result.get("ok") and body.get("id") and result.get("modelList"):
            self._store_models(body["id"], field, base, key, result["modelList"])  # 测的就是已保存的这个 Key：顺手记下模型清单
        return code, result

    @staticmethod
    def _probe(base, key, agent=None):
        url = join_url(base, "/v1/models")
        request = urllib.request.Request(url, headers={"Authorization": "Bearer " + key, "x-api-key": key,
                                                       "anthropic-version": "2023-06-01",
                                                       "Accept-Encoding": "identity", "User-Agent": agent or CLIENT_UA})
        started = time.time()
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                payload = response.read(2 * 1024 * 1024)
                code = response.status
        except urllib.error.HTTPError as exc:
            ms = int((time.time() - started) * 1000)
            hint = {401: "Key 无效或已过期", 403: "没有权限或余额不足", 404: "地址不对（找不到 /v1/models）",
                    429: "被限流了，稍后再试"}.get(exc.code, "上游返回错误")
            try:
                text = exc.read(4096).decode("utf-8", "replace").lower()
            except Exception:
                text = ""
            if exc.code == 403 and ("error code: 10" in text or "cloudflare" in text or "<html" in text):
                hint = "被对方站点的防护拦截（不是 Key 的问题）"  # 请求没到中转站本身：Key 对不对都是这个结果
            return 200, {"ok": False, "status": exc.code, "ms": ms, "detail": hint}
        except Exception as exc:
            return 200, {"ok": False, "status": None, "ms": int((time.time() - started) * 1000),
                         "detail": "连不上：%s" % (getattr(exc, "reason", None) or exc)}
        ms = int((time.time() - started) * 1000)
        models, names = None, []
        try:
            parsed = json.loads(payload.decode("utf-8"))
            if isinstance(parsed.get("data"), list):
                models = len(parsed["data"])
                names = sorted({item["id"].strip() for item in parsed["data"] if isinstance(item, dict)
                                and isinstance(item.get("id"), str) and item["id"].strip() and len(item["id"]) <= 160
                                and not any(ord(ch) < 32 for ch in item["id"])})[:500]
        except (ValueError, AttributeError):
            pass
        detail = "连接成功" + ("，可用模型 %d 个" % models if models is not None else "")
        return 200, {"ok": True, "status": code, "ms": ms, "detail": detail, "models": models, "modelList": names}

    # ---- 每一路的模型清单 ----
    def _store_models(self, node_id, key_field, base, key, names):
        """把读到的模型清单写进节点配置；期间节点的地址或 Key 被改掉了就不写，清单没变也不写（免得代理白白重载）。"""
        list_field = dict(MODEL_FIELDS)[key_field]
        with self.lock:
            data = self.load()
            node = next((n for n in data["nodes"] if n.get("id") == node_id), None)
            if node is None or node.get(key_field) != key or str(node.get("baseUrl") or "").rstrip("/") != base:
                return
            self.models_next[(node_id, key_field)] = time.time() + MODELS_INTERVAL
            if model_names(node.get(list_field)) != names:
                node[list_field] = names
                self.save(data)

    def refresh_models(self, force=False):
        """给每个启用节点的每个 Key 读一次 /v1/models（不产生费用），只读到期的或还没读过的。返回 (读成功, 读失败)。"""
        now, good, bad = time.time(), 0, 0
        data = self.load()
        limits = data.get("stations") if isinstance(data.get("stations"), dict) else {}
        for node in data["nodes"]:
            if node.get("enabled", True) is False:
                continue
            # 读模型清单也算在对方的 IP 限流里：同一个站的几个 Key 按这个站的请求间隔隔开
            try:
                gap = float((limits.get(station_of(node.get("baseUrl"))) or {}).get("minIntervalSeconds") or 0)
            except (TypeError, ValueError, AttributeError):
                gap = 0
            for key_field, list_field in MODEL_FIELDS:
                key, slot = node.get(key_field), (node.get("id"), key_field)
                if not key or self.closing.is_set() or (not force and self.models_next.get(slot, 0) > now):
                    continue
                try:
                    base = self._clean_url(node.get("baseUrl"))
                    result = self._probe(base, key, node.get("userAgent"))[1]
                except ValueError:
                    continue
                if result.get("ok") and result.get("modelList"):
                    self._store_models(node["id"], key_field, base, key, result["modelList"])
                    good += 1
                else:
                    self.models_next[slot] = now + MODELS_BACKOFF   # 读不到就保留上一份清单
                    bad += 1
                self.closing.wait(max(0.3, gap))
        return good, bad

    def _models_watch(self):
        wait = 3
        while True:
            self.models_due.wait(wait)
            if self.closing.is_set():
                return
            self.models_due.clear()
            wait = 60
            try:
                good, bad = self.refresh_models()
                if good or bad:
                    self.ctx.log(self.id, "model lists refreshed", ok=good, failed=bad)
            except Exception as exc:
                self.ctx.log(self.id, "model list refresh failed", "error", error=str(exc))

    def models_refresh(self, req):
        """手动重读所有启用线路的模型清单。"""
        req.json()
        good, bad = self.refresh_models(force=True)
        return 200, {"ok": True, "refreshed": good, "failed": bad}

    @staticmethod
    def catalog(nodes):
        """所有启用线路上能选的模型（含别名），以及各有几条线路能接：给页面勾选“对外公布”用。"""
        count = {}
        for node in nodes:
            if node.get("enabled", True) is False:
                continue
            aliases = node.get("aliases") if isinstance(node.get("aliases"), dict) else {}
            for key_field, list_field in MODEL_FIELDS:
                if not node.get(key_field):
                    continue
                names = model_names(node.get(list_field))
                for name in set(names) | {k for k, v in aliases.items() if not names or v in names}:
                    count[name] = count.get(name, 0) + 1
        return [{"id": name, "routes": count[name]} for name in sorted(count)]

    @staticmethod
    def stations(data, live):
        """每个中转站（同一个地址下的节点）的限速设置和实时情况。对方按 IP 限流，所以限速是整个站一起算的。"""
        limits = data.get("stations") if isinstance(data.get("stations"), dict) else {}
        live = {s.get("name"): s for s in live if isinstance(s, dict)}
        out = {}
        for node in data["nodes"]:
            name = station_of(node.get("baseUrl"))
            if not name:
                continue
            limit = limits.get(name) if isinstance(limits.get(name), dict) else {}
            item = out.setdefault(name, {"name": name, "nodes": 0, "maxConcurrency": limit.get("maxConcurrency") or 0,
                                         "minIntervalSeconds": limit.get("minIntervalSeconds") or 0,
                                         "inflight": (live.get(name) or {}).get("inflight", 0),
                                         "holdLeft": (live.get(name) or {}).get("holdLeft", 0)})
            item["nodes"] += node.get("enabled", True) is not False
        return [out[name] for name in sorted(out)]

    def set_station(self, req):
        """整个站的限速：同时最多几个请求、两次请求之间至少隔几秒。都填 0 就是不限。"""
        body = req.json()
        name = str(body.get("station") or "").strip().lower()
        with self.lock:
            data = self.load()
            if not name or name not in {station_of(n.get("baseUrl")) for n in data["nodes"]}:
                return 404, {"ok": False, "error": "没有节点属于这个站"}
            cap = self._number(body.get("maxConcurrency"), int, 0, 1000, "同时最多几个请求") or 0
            gap = self._number(body.get("minIntervalSeconds"), float, 0, 600, "请求间隔") or 0
            limits = data["stations"] if isinstance(data.get("stations"), dict) else {}
            keep = {k: v for k, v in (limits.get(name) or {}).items() if k == "maxOutputTokens"}  # 输出上限不归这个表单管
            if cap or gap or keep:
                limits[name] = dict(keep, maxConcurrency=cap, minIntervalSeconds=round(gap, 2))
            else:
                limits.pop(name, None)
            if limits:
                data["stations"] = limits
            else:
                data.pop("stations", None)
            self.save(data)
        self.ctx.log(self.id, "station limit changed", station=name, maxConcurrency=cap, minIntervalSeconds=gap)
        return 200, {"ok": True, "stations": self.stations(data, [])}

    def set_published(self, req):
        """对外公布的模型：客户端向 Key 池读模型列表时看到的清单。空清单表示公布全部。"""
        value = req.json().get("models")
        if not isinstance(value, list) or len(value) > 200:
            raise ValueError("缺少 models")
        names = model_names(value)
        with self.lock:
            data = self.load()
            if names:
                data["publishedModels"] = names
            else:
                data.pop("publishedModels", None)
            self.save(data)
        self.ctx.log(self.id, "published models changed", count=len(names))
        return 200, {"ok": True, "publishedModels": names}

    def _balance_one(self, node, auto):
        """查一个节点并记下结果。失败时保留上一次成功的数字（last），页面照常显示旧值并标出原因。"""
        node_id, now = node["id"], time.time()
        cache = self.balance_cache.setdefault(node_id, {})
        if not auto:
            cache.clear()  # 手动查询要最新的
        try:
            result = keypool_balance.query(node.get("baseUrl"), node["balanceCookie"], node["balanceUser"], get=self.balance_get,
                                           pause=1.5, retry=not auto, cache=cache)
            result["ok"] = True
            self.balance_wait.pop(node_id, None)
            # 余额变了就记一笔流水：统计里的“花费”按余额下降估算
            reading = (result["payAsYouGo"], None if result.get("degraded") else result["plan"])
            if self.balance_logged.get(node_id) != reading:
                try:
                    keypool_stats.append_balance(self.usage_dir, node_id, reading[0], reading[1], now)
                    self.balance_logged[node_id] = reading
                except OSError:
                    pass
            # 把读数送给代理：双路节点据此在撞错之前就换路，余额底线也靠它判断
            plan = "" if result.get("degraded") else "&plan=%s" % result["plan"]
            self.admin("/_admin/balance?node=%s%s&pay=%s" % (urllib.parse.quote(node_id), plan, result["payAsYouGo"]),
                       timeout=2)
        except keypool_balance.BalanceError as exc:
            previous = self.balances.get(node_id) or {}
            result = {"ok": False, "error": str(exc), "last": previous if previous.get("ok") else previous.get("last")}
            self.balance_wait[node_id] = now + (BALANCE_AUTH_BACKOFF if "会话" in str(exc) else BALANCE_BACKOFF)
        result["at"] = int(now)
        self.balances[node_id] = result
        return result

    def _balance_kick(self, nodes):
        """页面每次读状态时调用：到了间隔就在后台把填了会话的节点各查一次，不阻塞页面。"""
        now = time.time()
        if now - self.balance_auto_at < BALANCE_INTERVAL:
            return
        due = [n for n in nodes if n.get("balanceCookie") and n.get("balanceUser") and self.balance_wait.get(n.get("id"), 0) <= now]
        if not due or not self.balance_lock.acquire(False):
            return
        self.balance_auto_at = now

        def run():
            try:
                workers = [threading.Thread(target=self._balance_one, args=(node, True), daemon=True) for node in due]
                for worker in workers:
                    worker.start()
                for worker in workers:
                    worker.join(20)
            finally:
                self.balance_lock.release()

        threading.Thread(target=run, name="keypool-balance", daemon=True).start()

    def _balance_watch(self):
        """页面没开着时，双路或设了底线的节点仍然每分钟查一次余额，代理才知道该走哪一路。"""
        wait = 8
        while not self.closing.wait(wait):
            wait = BALANCE_WATCH_INTERVAL
            try:
                nodes = [n for n in self.load()["nodes"] if n.get("payKey") and n.get("enabled", True) is not False
                         and (n.get("key") or n.get("payFloor"))]
                if nodes:
                    self._balance_kick(nodes)
            except Exception as exc:
                self.ctx.log(self.id, "balance watch failed", "error", error=str(exc))

    def balance(self, req):
        """手动查询余额：可指定一个节点，否则查所有填了会话的节点。不用缓存，被限流时重试一次。"""
        wanted = req.json().get("id")
        nodes = [n for n in self.load()["nodes"] if n.get("balanceCookie") and n.get("balanceUser")
                 and (not wanted or n.get("id") == wanted)]
        if not nodes:
            return 400, {"ok": False, "error": "这个节点还没有填写余额查询用的 Session Cookie 和 TF-User"
                         if wanted else "还没有节点填写余额查询用的 Session Cookie 和 TF-User"}
        if not self.balance_lock.acquire(timeout=25):
            return 409, {"ok": False, "error": "正在查询余额，请稍等"}
        try:
            results = {}
            for index, node in enumerate(nodes):
                if index:
                    time.sleep(0.4)  # 接口有限流，节点之间稍微隔开
                results[node["id"]] = self._balance_one(node, False)
            good = [r for r in results.values() if r["ok"]]
            self.ctx.log(self.id, "balance queried", nodes=len(results), failed=len(results) - len(good))
            return 200, {"ok": True, "balances": results, "total": round(sum(r["total"] for r in good), 4),
                         "failed": len(results) - len(good)}
        finally:
            self.balance_lock.release()

    # ---- 用量统计 ----
    RANGES = {"1h": 3600, "24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400, "90d": 90 * 86400}

    # ---- 统计起点：「从现在起重新统计」。只是不再把更早的记录算进来，记录本身一条不删 ----
    def _baseline_file(self):
        return os.path.join(self.usage_dir, "baseline.json")

    def _baseline(self):
        try:
            with open(self._baseline_file(), "r", encoding="utf-8") as handle:
                since = float(json.load(handle).get("since") or 0)
        except (OSError, ValueError, TypeError, AttributeError):
            return 0.0
        return since if 0 < since <= time.time() else 0.0

    def set_baseline(self, req):
        action = req.json().get("action")
        if action == "set":
            os.makedirs(self.usage_dir, mode=0o700, exist_ok=True)
            tmp = self._baseline_file() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump({"since": time.time()}, handle)
            os.replace(tmp, self._baseline_file())
        elif action == "clear":
            try:
                os.unlink(self._baseline_file())
            except FileNotFoundError:
                pass
        else:
            raise ValueError("action 只能是 set 或 clear")
        self.ctx.log(self.id, "stats baseline " + action)
        return 200, {"ok": True, "baseline": self._baseline() or None}

    def _usage_rows(self, req):
        span = self.RANGES.get(req.param("range", "24h"), 86400)
        until = time.time()
        since = max(until - span, self._baseline())
        filters = {k: req.param(k, "")[:200] for k in keypool_stats.FILTERS + ("result",)}
        rows = [r for r in keypool_stats.load(self.usage_dir, since, until) if keypool_stats.matches(r, filters)]
        return rows, since, until

    def stats(self, req):
        rows, since, until = self._usage_rows(req)
        bucket = {"hour": 3600, "day": 86400}.get(req.param("bucket", ""), 3600 if until - since <= 2 * 86400 else 86400)
        data = keypool_stats.summarize(rows, since, until, bucket)
        money = keypool_stats.spend(self.usage_dir, since, until)
        names = {n.get("id"): n.get("name") or n.get("id") for n in self.load()["nodes"]}
        money["nodes"] = [{"key": k, "label": names.get(k, k), "amount": v}
                          for k, v in sorted(money["nodes"].items(), key=lambda kv: -kv[1])]
        return 200, dict(data, ok=True, since=since, until=until, bucket=bucket, spend=money, baseline=self._baseline() or None)

    def usage(self, req):
        rows, since, until = self._usage_rows(req)
        try:
            limit = max(1, min(200, int(req.param("limit", "50") or 50)))
            offset = max(0, int(req.param("offset", "0") or 0))
        except ValueError:
            limit, offset = 50, 0
        rows.reverse()                    # 新的在前
        return 200, {"ok": True, "total": len(rows), "offset": offset, "rows": rows[offset:offset + limit]}

    def usage_csv(self, req):
        rows, since, until = self._usage_rows(req)
        name = "keypool-usage-%s.csv" % time.strftime("%Y%m%d-%H%M%S", time.localtime(until))
        return Response(200, keypool_stats.to_csv(rows).encode("utf-8"), "text/csv; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="%s"' % name})

    def master_key(self, req):
        req.json()
        return 200, {"ok": True, "key": self.load()["masterKey"], "endpoint": "http://127.0.0.1:%d" % self.port}

    def rotate(self, req):
        req.json()
        with self.lock:
            data = self.load()
            data["masterKey"] = new_master_key()
            self.save(data)
        self.ctx.log(self.id, "master key rotated")
        return 200, {"ok": True, "key": data["masterKey"]}

    def set_settings(self, req):
        body = req.json()
        with self.lock:
            data = self.load()
            s = data.setdefault("settings", {})
            for key in ("guardEnabled", "maskSecrets"):
                if key in body:
                    s[key] = bool(body[key])
            self.save(data)
        self.ctx.log(self.id, "guard settings changed", **{k: data["settings"].get(k) for k in ("guardEnabled", "maskSecrets")})
        return 200, {"ok": True, "settings": data["settings"]}

    def release(self, req):
        node_id = str(req.json().get("id") or "")
        code, data = self.admin("/_admin/release?node=" + urllib.parse.quote(node_id))
        if code != 200 or not data.get("ok"):
            return 404, {"ok": False, "error": "放行失败，节点可能已不存在"}
        self.ctx.log(self.id, "node released from quarantine", node=node_id)
        return 200, {"ok": True}

    def restart(self, req):
        req.json()
        if self.blocked and not self._port_busy():
            self.blocked = ""
            self.start()
        elif self.process:
            self.process.reset()
            self.process.kill_child()
        return 200, {"ok": True}
