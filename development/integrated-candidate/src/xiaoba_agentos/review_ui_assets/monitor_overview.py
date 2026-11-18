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
import math
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
        self.config_path = Path(config_path)
        self.config_error = None
        try:
            self.runtime = cfg.agentos_runtime(self.config_path)
            self.db_path = self.runtime / "agentos.sqlite3"
        except (OSError, ValueError, TypeError, KeyError) as exc:
            self.runtime = self.db_path = None
            self.config_error = "AgentOS 配置不可读：" + type(exc).__name__
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
        if self.config_error:
            unavailable = {"available": False, "items": [], "problems": [self.config_error]}
            return {"ts": now_iso(), "source": "live", "schema": SCHEMA_VERSION,
                    "system": {"status": "down", "version": "?", "uptimeSec": None, "note": self.config_error},
                    "metrics": {}, "nodes": [], "links": [], "agents": [], "events": [], "eventsMode": "replace",
                    "reviewQueue": {"online": False, "pending": 0, "items": []},
                    "ops": {"available": False, "schema": OPS_SCHEMA_VERSION,
                            **{key: dict(unavailable) for key in ("todayLedger", "weeklyScores", "permanentRanking", "b19")},
                            "poolNodes": {"available": False, "nodes": [], "enabled": 0, "healthy": 0,
                                          "problems": [self.config_error]}},
                    "nodeHealth": {"available": False, "source": "unavailable", "ready": None, "enabled": None}}
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
        ops = {"available": False, "schema": OPS_SCHEMA_VERSION,
               "todayLedger": {"available": False, "items": [], "problems": ["数据库不可读"]},
               "weeklyScores": {"available": False, "week_start": None, "items": [], "problems": ["数据库不可读"]},
               "permanentRanking": {"available": False, "items": [], "problems": ["数据库不可读"]},
               "b19": {"available": False, "items": [], "problems": ["数据库不可读"]},
               "poolNodes": {"available": False, "nodes": [], "healthy": 0, "enabled": 0, "problems": ["数据库不可读"]}}
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
            # 卡 07 补充投影：复用同一只读连接，绝不写库、不派活、不调模型。
            # 每个投影自带 available 标志与 problems，API 不可用与数量 0 明确区分。
            ops = {"available": True, "schema": OPS_SCHEMA_VERSION,
                   "todayLedger": today_ledger(con),
                   "weeklyScores": weekly_agent_model_scores(con),
                   "permanentRanking": permanent_total_ranking(con),
                   "b19": b19_consecutive_failures(con),
                   "poolNodes": dynamic_pool_nodes(con)}
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
        pool = ops["poolNodes"]
        # 卡 07：全局节点健康以真实动态 Agent 池为准，removed 不计、旧固定双槽不再作为统计来源。
        # pool 不可读时明确标记，而不是回退到旧的固定两槽当“健康”。
        dynamic_node_health = {
            "source": "agent_pool" if pool["available"] else "unavailable",
            "ready": pool["healthy"] if pool["available"] else None,
            "enabled": pool["enabled"] if pool["available"] else None,
            "available": pool["available"],
            "legacyFixedTwo": healthy,
        }
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
            # 卡 07 C07-03：新增只读投影，独立于旧字段，旧读者忽略即可。
            "ops": ops,
            "nodeHealth": dynamic_node_health,
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


# ---------------------------------------------------------------------------
# 卡 07 C07-03：运行总览补充投影。全部只读，不调用 CLI、不改库、不派活、不调模型。
# 每个投影都区分“API 不可用 / 数量 0”两种状态，绝不把读不到缓存成假健康。
# event payload 结构随版本变化，所以这里只做防御式解析，坏行跳过并在 problems 里可见。
# ---------------------------------------------------------------------------
SHANGHAI_ZONE = datetime.timezone(datetime.timedelta(hours=8))
OPS_SCHEMA_VERSION = 1


def _week_start(day):
    """自然周（周一）起始 ISO 日期，Asia/Shanghai。"""
    d = datetime.date.fromisoformat(day) if isinstance(day, str) else day
    return (d - datetime.timedelta(days=d.weekday())).isoformat()


def _op_available(items, problems, available=True):
    """available 只表示“数据源可读”；坏行被隔离写进 problems，不等于不可用。"""
    return {"available": available, "items": items, "problems": problems}


def today_ledger(con, day=None, limit=200):
    """今日台账：今天的任务、执行者、进度。只读 tasks + agent_runs。

    available=False 表示读不到（表/列缺失或查询失败），与“今天 0 个任务”严格区分。
    """
    day = day or datetime.datetime.now(SHANGHAI_ZONE).date().isoformat()
    problems = []
    try:
        tcols = [r[1] for r in con.execute("PRAGMA table_info(tasks)")]
        if not tcols:
            return _op_available([], ["tasks 表不可读"], available=False)
        t_id = pick(tcols, "task_id", "id")
        t_title = pick(tcols, "title", "summary", "prompt")
        t_status = pick(tcols, "status")
        t_created = pick(tcols, "created_at", "submitted_at")
        t_attempts = pick(tcols, "attempts")
        t_max = pick(tcols, "max_attempts")
        if not (t_id and t_status and t_created):
            return _op_available([], ["tasks 缺少台账所需列"], available=False)
        day_start = datetime.datetime.combine(datetime.date.fromisoformat(day), datetime.time.min, tzinfo=SHANGHAI_ZONE).timestamp()
        day_end = day_start + 86400
        cursor = con.execute(
            "SELECT %s FROM tasks WHERE (typeof(%s) IN ('real','integer') AND %s>=? AND %s<?) "
            "OR %s LIKE ? ORDER BY %s DESC LIMIT ?" % (
                ", ".join(c for c in {t_id, t_title, t_status, t_created, t_attempts, t_max} if c),
                t_created, t_created, t_created, t_created, t_created),
            (day_start, day_end, day + "%", limit))
        cols = [d[0] for d in cursor.description]
        rows = cursor.fetchall()
        acols = [r[1] for r in con.execute("PRAGMA table_info(agent_runs)")]
        a_name = pick(acols, "agent_name", "agent")
        a_task = pick(acols, "task_id")
        a_status = pick(acols, "status")
        executors = {}
        if a_name and a_task:
            try:
                where = "status='running'" if a_status else "1=0"
                for r in con.execute("SELECT %s,%s FROM agent_runs WHERE %s" % (a_task, a_name, where)):
                    executors.setdefault(str(r[0]), []).append(str(r[1]))
            except sqlite3.Error as exc:
                problems.append("执行者读取跳过：" + type(exc).__name__)
        ledger = []
        for r in rows:
            d = dict(zip(cols, r))
            tid = str(d.get(t_id))
            ledger.append({
                "task_id": tid[:8],
                "title": short(d.get(t_title)) if t_title else "",
                "status": d.get(t_status),
                "executor": executors.get(tid, []),
                "attempts": d.get(t_attempts) if t_attempts else None,
                "max_attempts": d.get(t_max) if t_max else None,
                "progress": ("%s/%s" % (d.get(t_attempts), d.get(t_max))) if (t_attempts and t_max) else (d.get(t_status) or "—"),
                "created": ts_text(d.get(t_created)),
            })
        return _op_available(ledger, problems)
    except sqlite3.Error as exc:
        return _op_available([], ["台账读取失败：" + type(exc).__name__], available=False)


def _finite_number(val):
    if type(val) not in (int, float):
        return False
    try:
        return math.isfinite(val)
    except OverflowError:
        return False


def _event_payloads(con, event_type, problems):
    for row in con.execute("SELECT sequence,created_at,payload_json FROM events WHERE event_type=? ORDER BY sequence", (event_type,)):
        try:
            value = json.loads(row["payload_json"])
            if not isinstance(value, dict):
                raise ValueError("事件不是对象")
            yield value, row["created_at"]
        except (ValueError, TypeError, OverflowError):
            problems.append("事件 %s 结构无效，已隔离" % row["sequence"])


def _assessments(con):
    """Use the release-owned ledger audit; never infer scores from raw revisions."""
    try:
        from xiaoba_agentos.task_scores import audit_ledger_tx
        if not exists_table(con, "events"):
            return [], ["events 表不可读"], False
        audit = audit_ledger_tx(con)
        problems = list(audit["problems"])
        rows = []
        for value in audit["valid_rows"]:
            if value.get("outcome") == "void":
                continue
            if (not all(isinstance(value.get(k), str) and value[k].strip()
                        for k in ("source_ref", "node_id", "task_id", "week_start", "role"))
                    or value.get("role") not in ("review", "collaborate")
                    or value.get("model") is not None and not isinstance(value["model"], str)):
                problems.append("评分 %s 身份字段无效，已隔离" % value["id"])
                continue
            rows.append(value)
        return rows, problems, audit["reason"] != "chain_invalid"
    except (ImportError, sqlite3.Error, ValueError, TypeError, KeyError) as exc:
        return [], ["评分账本核验不可用：" + type(exc).__name__], False


def weekly_agent_model_scores(con, day=None):
    """Aggregate only the latest audited nonvoid assessment for each task/node/role."""
    day = day or datetime.datetime.now(SHANGHAI_ZONE).date().isoformat()
    start = _week_start(day)
    rows, problems, available = _assessments(con)
    groups = {}
    for current in rows:
        if current["week_start"] != start:
            continue
        role, reviewer, model = current["role"], current["source_ref"], current.get("model") or "未登记模型"
        group = groups.setdefault((role, reviewer, model), {
            "role": role, "reviewer": reviewer, "model": model,
            "points": 0, "tasks": 0, "good_tasks": 0})
        group["points"] += current["points"]
        group["tasks"] += 1
        group["good_tasks"] += current["points"]
    ranking = list(groups.values())
    for group in ranking:
        group["success_rate"] = round(group["good_tasks"] / group["tasks"], 4)
    ranking.sort(key=lambda g: (-g["points"], -g["success_rate"], g["reviewer"], g["model"]))
    return {"available": available, "week_start": start, "items": ranking, "problems": problems}


def permanent_total_ranking(con):
    """正式经验永久总分排名：支持撤回冲正（memory.reuse_reversed）及重新接受，校验真实交付/撤回绑定。只读。"""
    problems, scores = [], {}
    try:
        if not exists_table(con, "events"):
            return _op_available([], ["events 表不可读"], available=False)
        deliveries = {}
        for drow in con.execute(
            "SELECT sequence,event_id,payload_json FROM events WHERE event_type='task.delivery_recorded' ORDER BY sequence"
        ).fetchall():
            try:
                dval = json.loads(drow["payload_json"])
                d_apps = dval.get("application_ids") if isinstance(dval, dict) else None
                if (not isinstance(dval, dict)
                        or not isinstance(drow["event_id"], str) or not drow["event_id"]
                        or dval.get("decision") not in ("accept", "withdraw")
                        or not all(isinstance(dval.get(k), str) and dval[k] for k in ("real_task_id", "message_id", "input_sha256"))
                        or not isinstance(dval.get("artifact"), dict)
                        or not isinstance(d_apps, list)
                        or not all(isinstance(aid, str) and aid for aid in d_apps)):
                    raise ValueError("交付事件字段无效")
                deliveries[drow["event_id"]] = dval
            except (ValueError, TypeError, KeyError, OverflowError):
                problems.append("事件 %s 交付记录无效，已隔离" % drow["sequence"])
        active = {}
        reversed_ids = set()
        rows = con.execute(
            "SELECT sequence,event_id,event_type,payload_json FROM events "
            "WHERE event_type IN ('memory.reuse_scored','memory.reuse_reversed') ORDER BY sequence"
        ).fetchall()
        for row in rows:
            try:
                value = json.loads(row["payload_json"])
                if not isinstance(value, dict):
                    raise ValueError("事件不是对象")
                kind = row["event_type"]
                source = value.get("source_task_id")
                task = value.get("real_task_id")
                if not isinstance(source, str) or not source.strip() or not isinstance(task, str) or not task.strip():
                    problems.append("复用分事件字段无效，已跳过")
                    continue
                key = (task, source)
                if kind == "memory.reuse_scored":
                    score = value.get("score")
                    if (type(score) is not int or score not in (0, 1)
                            or not _finite_number(value.get("used_at"))):
                        problems.append("复用分事件字段无效，已跳过")
                        continue
                    deliv_eid = value.get("delivery_event_id")
                    if "delivery_event_id" in value or "application_ids" in value:
                        app_ids = value.get("application_ids")
                        delivery = deliveries.get(deliv_eid) if isinstance(deliv_eid, str) else None
                        if (not isinstance(deliv_eid, str) or not deliv_eid
                                or not isinstance(app_ids, list) or not app_ids
                                or not all(isinstance(aid, str) and aid for aid in app_ids)
                                or len(set(app_ids)) != len(app_ids)
                                or not isinstance(value.get("source_sha256"), str) or len(value["source_sha256"]) != 64
                                or not delivery or delivery.get("decision") != "accept"
                                or delivery.get("real_task_id") != task
                                or not set(app_ids) <= set(delivery.get("application_ids", []))):
                            problems.append("复用分缺少有效交付绑定，已隔离")
                            continue
                    if key in active:
                        problems.append("重复复用分事件，已跳过")
                        continue
                    active[key] = {
                        "event_id": row["event_id"],
                        "score": score,
                        "delivery_event_id": deliv_eid if isinstance(deliv_eid, str) else None,
                    }
                    item = scores.setdefault(source, {"source_task_id": source, "score": 0, "used": 0})
                    item["score"] += score
                    item["used"] += 1
                else:
                    prior = active.get(key)
                    contrib = value.get("contribution")
                    score_eid = value.get("score_event_id")
                    deliv_eid = value.get("delivery_event_id")
                    withdrawal = deliveries.get(deliv_eid) if isinstance(deliv_eid, str) else None
                    withdraws_id = value.get("withdraws_delivery_event_id")
                    if (not prior or not isinstance(score_eid, str) or score_eid != prior["event_id"]
                            or score_eid in reversed_ids
                            or type(contrib) is not int or contrib not in (0, 1)
                            or contrib != prior["score"]
                            or not isinstance(deliv_eid, str) or not deliv_eid
                            or not withdrawal or withdrawal.get("decision") != "withdraw"
                            or withdrawal.get("real_task_id") != task
                            or not isinstance(withdraws_id, str) or not withdraws_id
                            or withdrawal.get("withdraws_delivery_event_id") != withdraws_id
                            or (prior.get("delivery_event_id") is not None and prior.get("delivery_event_id") != withdraws_id)):
                        problems.append("复用冲正事件字段或撤回绑定无效，已隔离")
                        continue
                    reversed_ids.add(score_eid)
                    active.pop(key, None)
                    item = scores.setdefault(source, {"source_task_id": source, "score": 0, "used": 0})
                    item["score"] = max(0, item["score"] - contrib)
                    item["used"] = max(0, item["used"] - 1)
            except (ValueError, TypeError, KeyError, OverflowError):
                problems.append("事件 %s 结构无效，已隔离" % row["sequence"])
                continue
        ranking = list(scores.values())
        ranking.sort(key=lambda x: (-x["score"], -x["used"], x["source_task_id"]))
        return _op_available(ranking, problems)
    except sqlite3.Error as exc:
        return _op_available([], ["永久总分读取失败：" + type(exc).__name__], available=False)


def b19_consecutive_failures(con):
    """Latest audited assessments, ordered by original task completion, not correction time."""
    rows, problems, available = _assessments(con)
    streams = {}
    for current in rows:
        # Ledger insertion order breaks same-day ties for historical manual rows.
        # Native tasks have an exact completion timestamp that corrections retain.
        try:
            run = con.execute("SELECT finished_at FROM agent_runs WHERE id=?", (current.get("run_id"),)).fetchone()
            task = con.execute("SELECT updated_at FROM tasks WHERE id=?", (current["task_id"],)).fetchone()
            raw_completed = run[0] if run and run[0] is not None else (task[0] if task else 0)
            completed = raw_completed if _finite_number(raw_completed) else 0
        except sqlite3.Error as exc:
            return _op_available([], problems + ["B19 任务证据读取失败：" + type(exc).__name__], False)
        key = (current["source_ref"], current.get("model") or "未登记模型")
        streams.setdefault(key, []).append((current.get("completed_date", current.get("date", "")),
                                           completed, current["points"] == 0))
    items = []
    for (reviewer, model), values in streams.items():
        flags = [v[2] for v in sorted(values, key=lambda v: (v[0], v[1]))]
        streak = 0
        for flag in reversed(flags):
            if not flag:
                break
            streak += 1
        if streak:
            items.append({"reviewer": reviewer, "model": model, "consecutive": streak,
                          "total_unqualified": sum(flags), "total": len(flags)})
    items.sort(key=lambda x: (-x["consecutive"], x["reviewer"]))
    return _op_available(items, problems, available)


def dynamic_pool_nodes(con):
    """真实动态节点状态：从 ri_sources 读 Agent 池节点，removed 不计入可用。

    返回 available、节点明细与可用计数；读不到返回 available=False，绝不回退成
    旧的 AGY/DSH 固定双槽，也不用过期健康状态覆盖池节点新状态。
    """
    problems = []
    try:
        if not exists_table(con, "ri_sources"):
            return {"available": False, "nodes": [], "healthy": 0, "enabled": 0,
                    "problems": ["ri_sources 表不可读（未加载 Agent 池或只读连接失败）"]}
        nodes = []
        enabled = healthy = 0
        for row in con.execute("SELECT id,data FROM ri_sources"):
            try:
                source = json.loads(row["data"])
            except (ValueError, TypeError):
                problems.append("节点记录解析失败，已跳过")
                continue
            if not isinstance(source, dict) or not isinstance(source.get("node"), dict):
                problems.append("节点 %s 缺少动态节点资料，已隔离" % row["id"])
                continue
            node = source["node"]
            if type(node.get("enabled")) is not bool or type(node.get("removed")) is not bool:
                problems.append("节点 %s 启用状态无效，已隔离" % row["id"])
                continue
            removed = node["removed"]
            on = node["enabled"] and not removed
            test = node.get("test") if isinstance(node.get("test"), dict) else {}
            ready = bool(test.get("status") == "passed")
            if on:
                enabled += 1
                if ready:
                    healthy += 1
            nodes.append({"node_id": row["id"], "name": str(node.get("name") or row["id"])[:60],
                          "model": node.get("model"), "enabled": on, "removed": removed,
                          "ready": ready, "test_status": test.get("status")})
        nodes.sort(key=lambda n: (n["removed"], not n["enabled"], n["node_id"]))
        return {"available": True, "nodes": nodes, "healthy": healthy, "enabled": enabled,
                "problems": problems}
    except sqlite3.Error as exc:
        return {"available": False, "nodes": [], "healthy": 0, "enabled": 0,
                "problems": ["动态节点读取失败：" + type(exc).__name__]}


def exists_table(con, table):
    return bool(con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


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
        if self.monitor.config_error:
            return {"status": "failed", "detail": self.monitor.config_error}
        if not self.monitor.db_path.exists():
            return {"status": "failed", "detail": "找不到 AgentOS 数据库：%s" % self.monitor.db_path}
        return {"status": "ok", "detail": "只读读取 AgentOS 运行数据"}
