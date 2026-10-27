"""运行总览模块：只读地汇总小八AgentOS 的运行状态。

只读取运行数据，不调用 xiaoba CLI，不写任何文件：
  - .runtime/agentos.sqlite3（以 mode=ro 打开）
  - .runtime/worker-state.json、.runtime/releases/index.json
  - .runtime/logs/worker.out.log 末尾（只取 status 等字段，不转发答案正文）
  - 配置文件里的 agents 启用状态
待审队列摘要来自裁决模块。
"""
import collections
import datetime
import json
import os
import socket
import sqlite3
import threading
import time
from pathlib import Path

from .. import config as cfg
from ..web import Route
from . import Module

SCHEMA_VERSION = 2  # /api/status 的结构版本；字段含义变化时递增

AGENT_NODE = {"agy_review": "agy", "dsh_review": "dsh"}   # v1.5.0 起后台只有这两个；历史记录里的旧后台不再画节点
AGENT_LABEL = {"agy_review": "AGY 审查", "dsh_review": "DSH 审查"}
DSH_PROFILE = Path.home() / ".dsh" / "profiles" / "desktop" / "cordis.patch.yml"


def dsh_desktop_model(path=DSH_PROFILE):
    """DeepSeek Harness 桌面端现在选的“提供方 · 模型”。只读配置里的这两项；读不出来返回 None。

    DSH 审查跟随桌面端的选择，探针表里记的只是上次探针那一刻的值。
    """
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return None

    def value(line, key):
        text = line.strip()
        if not text.startswith(key + ":"):
            return None
        return text[len(key) + 1:].split(" #")[0].strip().strip("\"'") or None

    provider = model = None
    inside = False
    for line in lines:
        if line.startswith("- "):
            inside = value(line[2:], "id") == "agent-default-model"
        elif inside:
            provider = provider or value(line, "provider")
            model = model or value(line, "model")
    if not provider or not model:
        return None
    shown, indent = provider, None
    for line in lines:
        if indent is None:
            if line.strip() == provider + ":":
                indent = len(line) - len(line.lstrip())
        elif line.strip() and len(line) - len(line.lstrip()) <= indent:
            break
        elif value(line, "displayName"):
            shown = value(line, "displayName")
            break
    return "%s · %s" % (shown, model)

EVENT_LABEL = {
    "route.decided": "路由已决定",
    "task.submitted": "任务已提交",
    "task.leased": "Worker 租用任务",
    "task.completed": "任务完成",
    "task.failed": "任务失败",
    "task.frozen": "任务已冻结，等待恢复",
    "task.resumed": "任务已恢复",
    "agent.started": "后台开始执行",
    "agent.finished": "后台返回结果",
    "agent.provider_attempt": "后台调用模型",
    "health.agent_probe": "后台真实探针",
    "health.provider_attempt": "健康检查调用模型",
    "judge.decided": "裁判已选出胜者",
    "memory.candidate_staged": "经验候选进入暂存",
}
# 事件类型前缀 → 拓扑节点
EVENT_SOURCE = [
    ("route.", "router"), ("task.submitted", "queue"), ("task.leased", "worker"), ("task.", "queue"),
    ("health.", "gate"), ("judge.", "judge"), ("memory.candidate", "outbox"), ("memory.review", "review"),
    ("memory.publish", "vault"), ("review.", "review"), ("repair.", "gate"), ("session.", "host"),
]
# 事件 → 点亮哪些链路
EVENT_LINKS = {
    "route.decided": [("host", "cli"), ("cli", "router")],
    "task.submitted": [("router", "queue"), ("knowlp", "queue")],
    "task.leased": [("queue", "worker"), ("worker", "gate")],
    "judge.decided": [("judge", "audit")],
    "memory.candidate_staged": [("judge", "outbox")],
}


def now_iso():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except PermissionError:
        return True
    except Exception:
        return False


def port_open(port, host="127.0.0.1"):
    try:
        with socket.create_connection((host, port), timeout=0.3):
            return True
    except Exception:
        return False


def tail_lines(path, max_bytes=65536):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            data = f.read().decode("utf-8", "replace")
        lines = data.splitlines()
        if size > max_bytes and lines:
            lines = lines[1:]
        return lines, size
    except Exception:
        return [], 0


def pick(cols, *names):
    for n in names:
        if n in cols:
            return n
    for n in names:
        for c in cols:
            if n in c:
                return c
    return None


def ts_text(v):
    """数据库里的时间是 Unix 秒（REAL）；转成本地时间文本，其他类型原样返回。"""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        try:
            return datetime.datetime.fromtimestamp(v).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return v
    return v


def short(s, n=40):
    s = str(s or "").replace("\n", " ").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


class Monitor:
    def __init__(self, project, config_path, review=None):
        self.project = Path(project)
        self.runtime = self.project / ".runtime"
        self.db_path = self.runtime / "agentos.sqlite3"
        self.config_path = Path(config_path)
        self.review = review
        self.last_seq = None
        self.log_size = None
        self.arrivals = collections.deque()  # (time, event_type, agent)
        self.lock = threading.Lock()

    # ---------- 数据读取 ----------
    def connect(self):
        uri = self.db_path.as_uri() + "?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=2)
        con.row_factory = sqlite3.Row
        return con

    def columns(self, con, table):
        try:
            return [r[1] for r in con.execute("PRAGMA table_info(%s)" % table)]
        except Exception:
            return []

    def agents_config(self):
        cfg = read_json(self.config_path) or {}
        raw = cfg.get("agents", {})
        out = {}
        items = raw.items() if isinstance(raw, dict) else [(a.get("name") or a.get("id"), a) for a in raw if isinstance(a, dict)]
        for name, a in items:
            if not name:
                continue
            out[name] = {"enabled": bool(a.get("enabled", True)) if isinstance(a, dict) else True}
        for name in AGENT_NODE:
            out.setdefault(name, {"enabled": name in ("agy_review", "dsh_review")})
        return out

    def snapshot(self):
        with self.lock:
            return self._snapshot()

    def _snapshot(self):
        t = time.time()
        nodes = {}
        facts = collections.defaultdict(dict)
        warnings = []

        # 发布与 Worker
        rel = read_json(self.runtime / "releases" / "index.json") or {}
        version = str(rel.get("current_release") or "?").lstrip("v")
        rel_info = (rel.get("releases") or {})
        if isinstance(rel_info, list):
            rel_info = {r.get("version") or r.get("name"): r for r in rel_info if isinstance(r, dict)}
        rel_status = (rel_info.get(version) or rel_info.get("v" + version) or {}).get("status") if isinstance(rel_info, dict) else None
        ws = read_json(self.runtime / "worker-state.json") or {}
        worker_pid = ws.get("pid")
        worker_up = bool(worker_pid) and pid_alive(worker_pid)
        started = ws.get("started_at")
        uptime = int(t - started) if (worker_up and started) else None

        lines, size = tail_lines(self.runtime / "logs" / "worker.out.log")
        recent_status = []
        last_completed = None
        for ln in lines[-200:]:
            try:
                j = json.loads(ln)
            except Exception:
                continue
            st = j.get("status")
            if st:
                recent_status.append(st)
            if st == "completed":
                last_completed = {"task_id": j.get("task_id"), "winner": j.get("winner"), "scores": j.get("scores")}
        last_status = recent_status[-1] if recent_status else None
        log_grew = self.log_size is not None and size > self.log_size
        self.log_size = size

        # 数据库
        tasks_by_status, events_total, pending_review, today_tasks = {}, 0, 0, 0
        agent_rows, running_tasks, recent_tasks, events_out, new_events = [], [], [], [], []
        health = {}
        runs_by_agent = collections.Counter()
        running_by_agent = {}
        route_counts = collections.Counter()
        try:
            con = self.connect()
            tcols = self.columns(con, "tasks")
            t_id, t_st = pick(tcols, "task_id", "id"), pick(tcols, "status")
            t_cr = pick(tcols, "created_at", "submitted_at")
            t_up = pick(tcols, "updated_at", "completed_at", "finished_at")
            t_title = pick(tcols, "title", "summary", "task_text", "message", "prompt", "task")
            if t_st:
                for r in con.execute("SELECT %s s, COUNT(*) c FROM tasks GROUP BY %s" % (t_st, t_st)):
                    tasks_by_status[r["s"]] = r["c"]
            today = datetime.date.today().isoformat()
            if t_cr:
                day_start = datetime.datetime.combine(datetime.date.today(), datetime.time.min).timestamp()
                today_tasks = con.execute(
                    "SELECT COUNT(*) FROM tasks WHERE (typeof(%s) IN ('real','integer') AND %s >= ?) OR %s LIKE ?" % (t_cr, t_cr, t_cr),
                    (day_start, today + "%")).fetchone()[0]
            if t_id and t_st:
                order = t_up or t_cr or "rowid"
                sel = ", ".join(c for c in {t_id, t_st, t_cr, t_up, t_title} if c)
                for r in con.execute("SELECT %s FROM tasks ORDER BY %s DESC LIMIT 20" % (sel, order)):
                    d = dict(r)
                    recent_tasks.append({"id": str(d.get(t_id))[:8], "status": d.get(t_st), "title": short(d.get(t_title)) if t_title else "",
                                         "at": ts_text(d.get(t_up) or d.get(t_cr))})
            running_tasks = [x for x in recent_tasks if x["status"] not in ("completed", "failed", "cancelled", "retired")]

            rcols = self.columns(con, "agent_runs")
            r_ag, r_st = pick(rcols, "agent_name", "agent"), pick(rcols, "status")
            r_end, r_task = pick(rcols, "finished_at", "ended_at", "completed_at"), pick(rcols, "task_id")
            r_start = pick(rcols, "started_at", "created_at")
            if r_ag:
                for r in con.execute("SELECT %s a, COUNT(*) c FROM agent_runs GROUP BY %s" % (r_ag, r_ag)):
                    runs_by_agent[r["a"]] = r["c"]
                where = []
                if r_end:
                    where.append("%s IS NULL" % r_end)
                if r_st:
                    where.append(("%s NOT IN ('completed','failed','cancelled','error','timeout','permanent_error','transient_error')" if r_end else "%s IN ('running','started')") % r_st)
                if where:
                    q = "SELECT %s a%s%s FROM agent_runs WHERE %s ORDER BY rowid DESC LIMIT 10" % (
                        r_ag, (", %s t" % r_task) if r_task else "", (", %s s" % r_start) if r_start else "", " AND ".join(where))
                    live_ids = set(x["id"] for x in running_tasks)
                    for r in con.execute(q):
                        d = dict(r)
                        tid = str(d.get("t") or "")[:8]
                        if r_task and tid not in live_ids:
                            continue  # 任务已结束的残留记录不算在跑
                        running_by_agent.setdefault(d["a"], {"task": tid, "since": ts_text(d.get("s"))})

            hcols = self.columns(con, "agent_health")
            h_ag = pick(hcols, "agent_name", "agent")
            h_st = pick(hcols, "status", "state")
            h_pr, h_mo = pick(hcols, "provider"), pick(hcols, "model")
            h_at = pick(hcols, "checked_at", "updated_at", "probed_at", "created_at")
            h_er = pick(hcols, "error", "last_error", "reason")
            if h_ag:
                for r in con.execute("SELECT * FROM agent_health"):
                    d = dict(r)
                    health[d.get(h_ag)] = {"status": d.get(h_st), "provider": d.get(h_pr), "model": d.get(h_mo),
                                           "at": ts_text(d.get(h_at)), "error": short(d.get(h_er), 80) if h_er and d.get(h_er) else None}

            mcols = self.columns(con, "memory_candidates")
            m_st = pick(mcols, "status", "stage")
            if m_st:
                pending_review = con.execute(
                    "SELECT COUNT(*) FROM memory_candidates WHERE %s LIKE 'pending%%'" % m_st).fetchone()[0]

            dcols = self.columns(con, "route_decisions")
            d_rt = pick(dcols, "route", "decision")
            if d_rt:
                for r in con.execute("SELECT %s r, COUNT(*) c FROM route_decisions GROUP BY %s" % (d_rt, d_rt)):
                    route_counts[r["r"]] = r["c"]

            events_total = con.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            max_seq = con.execute("SELECT MAX(sequence) FROM events").fetchone()[0] or 0
            if self.last_seq is not None and max_seq > self.last_seq:
                for r in con.execute("SELECT sequence, event_type, payload_json FROM events WHERE sequence > ? ORDER BY sequence LIMIT 500", (self.last_seq,)):
                    new_events.append((r["event_type"], self.payload_agent(r["payload_json"])))
            self.last_seq = max_seq
            for r in con.execute("SELECT sequence, task_id, event_type, payload_json, created_at FROM events ORDER BY sequence DESC LIMIT 60"):
                events_out.append(self.format_event(r))
            con.close()
        except Exception as e:  # 数据库不可读时仍返回其余状态
            warnings.append("数据库读取失败：%s" % e)

        for et, ag in new_events:
            self.arrivals.append((t, et, ag))
        if log_grew:
            self.arrivals.append((t, "worker.poll", None))
        while self.arrivals and t - self.arrivals[0][0] > 120:
            self.arrivals.popleft()

        # ---------- 节点 ----------
        agents_cfg = self.agents_config()
        queue_depth = sum(c for s, c in tasks_by_status.items() if s not in ("completed", "failed", "cancelled", "retired"))
        done = tasks_by_status.get("completed", 0)
        failed = tasks_by_status.get("failed", 0)
        win = [x for x in recent_tasks if x["status"] in ("completed", "failed")]
        success = (sum(1 for x in win if x["status"] == "completed") / len(win)) if win else None
        activity = collections.Counter()
        for (_, et, ag) in self.arrivals:
            activity[self.event_source(et, ag)] += 1

        gate_status = {"idle": "ok", "completed": "ok", "worker_started": "ok", "candidate_required": "warn",
                       "degraded_health": "warn", "blocked_configuration": "down"}.get(last_status, "idle")
        status_text = {"idle": "空闲轮询", "completed": "刚完成任务", "worker_started": "刚启动", "degraded_health": "后台健康降级",
                       "blocked_configuration": "配置受阻", "candidate_required": "等待候选"}.get(last_status, last_status or "无记录")

        def node(nid, status, summary, **f):
            nodes[nid] = {"id": nid, "status": status, "summary": summary, "activity": activity.get(nid, 0)}
            facts[nid].update({k: v for k, v in f.items() if v is not None})

        node("host", "ok", "Codex / 桌面会话", 路由决定累计=sum(route_counts.values()) or None,
             路由分布="、".join("%s %d" % (k, v) for k, v in route_counts.most_common()) or None)
        node("cli", "ok" if rel_status in (None, "verified_immutable") else "warn", "v%s · %s" % (version, "已校验" if rel_status == "verified_immutable" else (rel_status or "未知")),
             当前发布="v" + version, 发布状态=rel_status)
        node("router", "ok", "规则路由 · 不调模型", direct=route_counts.get("direct"), agentos_multi=route_counts.get("agentos_multi"),
             review=route_counts.get("review"), ask_user=route_counts.get("ask_user"))
        node("knowlp", "ok" if Path("/Users/suzhizhou/Documents/Codex/KnowLP/runtime/knowlp_mcp.py").exists() else "warn", "提交时冻结检索结果",
             图目录=".runtime/knowlp")
        node("queue", "busy" if queue_depth else "ok", "待处理 %d · 完成 %d · 失败 %d" % (queue_depth, done, failed),
             **{"状态分布": "、".join("%s %d" % (k, v) for k, v in sorted(tasks_by_status.items())) or "空"})
        node("worker", ("busy" if running_tasks else "ok") if worker_up else "down",
             ("pid %s · 运行 %s" % (worker_pid, fmt_dur(uptime))) if worker_up else "进程未运行",
             进程=worker_pid, 最近轮询=status_text, 数据库版本=ws.get("database_schema_version"))
        node("gate", gate_status if worker_up else "down", status_text,
             近200行=("、".join("%s %d" % kv for kv in collections.Counter(recent_status).most_common())) or None)
        agents_out = []
        for name, nid in AGENT_NODE.items():
            enabled = agents_cfg.get(name, {}).get("enabled", False)
            h = health.get(name) or {}
            run = running_by_agent.get(name)
            if not enabled:
                st = "disabled"
            elif run:
                st = "busy"
            elif (h.get("status") or "") in ("healthy", "ok"):
                st = "ok"
            elif h.get("status"):
                st = "warn"
            else:
                st = "idle"
            model = " · ".join(x for x in (h.get("provider"), h.get("model")) if x)   # 上次探针那一刻的值
            now_model = dsh_desktop_model() if name == "dsh_review" else None        # 桌面端现在选的；AGY 的读不到
            node(nid, st, ("已禁用" if not enabled else (now_model or model or "未探测")), 探针状态=h.get("status"),
                 最近探针=h.get("at"), 探针时模型=model or None,
                 运行次数=runs_by_agent.get(name), 当前任务=(run or {}).get("task"), 错误=h.get("error"))
            agents_out.append({"id": nid, "name": AGENT_LABEL[name], "key": name, "status": st, "model": model or "—",
                               "modelNow": now_model,
                               "task": (run or {}).get("task") or ("—" if enabled else "已禁用"), "runs": runs_by_agent.get(name, 0),
                               "probeAt": h.get("at")})
        node("judge", "ok", ("上次胜者 %s" % last_completed["winner"]) if last_completed else "等待两份合格结果",
             上次任务=str((last_completed or {}).get("task_id") or "")[:8] or None,
             上次得分=("、".join("%s %.3f" % kv for kv in (last_completed or {}).get("scores", {}).items()) or None) if last_completed else None)
        node("audit", "ok", "累计 %d 条 · 哈希链" % events_total, 事件总数=events_total)
        node("outbox", "warn" if pending_review else "ok", "待审核 %d" % pending_review, 待审核候选=pending_review)
        rq = self.review_queue()
        where = {"managed": "由监看中心运行", "external": "沿用已在运行的后端"}.get(rq.get("mode"), "未连接")
        node("review", ("warn" if rq["pending"] else "ok") if rq["online"] else ("idle" if rq.get("starting") else "down"),
             "监看中心内 · %s" % (("待审 %d" % rq["pending"]) if rq["online"] else ("启动中" if rq.get("starting") else "不可用")),
             后端=where, 待审=rq["pending"] if rq["online"] else None, 第二大脑同步=rq.get("syncState"),
             同步时间=rq.get("syncAt"), 错误=rq.get("error") or None)
        node("vault", "ok" if Path("/Users/suzhizhou/Documents/私人助理/第二大脑").exists() else "warn",
             "Obsidian · 经验库", 目录="第二大脑")
        for nid in nodes:
            nodes[nid]["facts"] = facts[nid]

        # ---------- 链路 ----------
        rates = collections.Counter()
        for (_, et, ag) in self.arrivals:
            for pair in EVENT_LINKS.get(et, []):
                rates[pair] += 1
            an = AGENT_NODE.get(ag or "")
            if an and et.startswith(("agent.", "health.")):
                rates[("gate", an)] += 1
                if et == "agent.finished":
                    rates[(an, "judge")] += 1
            if et == "worker.poll":
                rates[("queue", "worker")] += 0.3
        links = [{"from": a, "to": b, "rate": min(4.0, 0.4 + c / 3.0)} for (a, b), c in rates.items()]
        for name, run in running_by_agent.items():
            an = AGENT_NODE.get(name)
            if an:
                links.append({"from": "gate", "to": an, "rate": 1.2})

        sys_status = "down" if not worker_up else ("warn" if gate_status in ("warn", "down") else "ok")
        healthy = sum(1 for n in ("agy_review", "dsh_review") if (health.get(n) or {}).get("status") in ("healthy", "ok"))
        return {
            "ts": now_iso(),
            "source": "live",
            "schema": SCHEMA_VERSION,
            "system": {"status": sys_status, "version": version, "uptimeSec": uptime, "note": "；".join(warnings) or None},
            "metrics": {"todayTasks": today_tasks, "successRate": success, "queueDepth": queue_depth,
                        "pendingReview": pending_review, "healthyAgents": healthy, "eventsTotal": events_total},
            "nodes": list(nodes.values()),
            "links": links,
            "agents": agents_out,
            "reviewQueue": rq,
            "events": events_out,
            "eventsMode": "replace",
        }

    def review_queue(self):
        review = self.review
        if review is None:
            return {"online": False, "items": [], "pending": 0, "byStage": {}, "recentDecisions": [], "error": "裁决模块未启用"}
        return review.queue_summary(ts_text)

    def payload_agent(self, payload):
        try:
            p = json.loads(payload or "{}")
            for k in ("agent", "agent_name", "agent_id", "name"):
                if isinstance(p.get(k), str):
                    return p[k]
        except Exception:
            pass
        return None

    def event_source(self, et, agent):
        if agent in AGENT_NODE and et.startswith(("agent.", "health.")):
            return AGENT_NODE[agent]
        if et == "worker.poll":
            return "worker"
        for prefix, nid in EVENT_SOURCE:
            if et.startswith(prefix):
                return nid
        return "audit"

    def format_event(self, r):
        et = r["event_type"] or ""
        agent = self.payload_agent(r["payload_json"])
        low = et.lower()
        level = "error" if any(k in low for k in ("fail", "error", "frozen")) else (
            "warn" if any(k in low for k in ("degrad", "block", "retry", "timeout", "reject")) else "info")
        msg = EVENT_LABEL.get(et, et)
        if agent:
            msg += " · " + AGENT_LABEL.get(agent, agent)
        if r["task_id"]:
            msg += " · 任务 " + str(r["task_id"])[:8]
        return {"ts": ts_text(r["created_at"]), "level": level, "source": self.event_source(et, agent), "msg": msg, "type": et}


def fmt_dur(s):
    if s is None:
        return "—"
    d, h, m = s // 86400, s % 86400 // 3600, s % 3600 // 60
    return "%d天%d时" % (d, h) if d else ("%d时%d分" % (h, m) if h else "%d分" % m)


class OverviewModule(Module):
    id, title, kind = "overview", "运行总览", "view"

    def __init__(self, ctx):
        super().__init__(ctx)
        self.monitor = Monitor(ctx.config["agentos_project"], cfg.agentos_config_path(ctx.config))

    def start(self):
        self.monitor.review = self.ctx.modules.get("review")

    def routes(self):
        return [Route("GET", "/api/status", self.status, "read")]

    def status(self, req):
        return 200, self.monitor.snapshot()

    def health(self):
        if not self.monitor.db_path.exists():
            return {"status": "failed", "detail": "找不到 AgentOS 数据库：%s" % self.monitor.db_path}
        return {"status": "ok", "detail": "只读读取 AgentOS 运行数据"}
