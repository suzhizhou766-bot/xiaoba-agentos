"""冻结任务模块：列出被冻结的任务，并在用户逐个确认后恢复其中一个。

小八的任务在一侧后台失败时会被冻结（frozen_paused；旧版本留下的记录是 resume_ready），成功那一侧的输出与
执行回执原样保留。冻结的任务不会自己恢复，只有用户带着冻结哈希明确发起才会继续，而且只补跑失败的那一侧。

这里只做两件事，都只经过已安装并自带发布校验的小八入口，不读写数据库：
  - 列出：把 `xiaoba status --json` 里处于冻结状态的任务整理给页面；
  - 恢复：用户在页面上点了某一个任务并确认之后，对这一个任务调用
    `xiaoba task-resume 任务编号 --freeze-hash 冻结哈希`，把命令行的结果原样带回。
冻结哈希、成功侧回执、失败侧新鲜健康这些复核全部由命令行完成，这里不重做、不放宽。
监看中心不会自己发起恢复，不会重试，也不会批量恢复。入口使用的配置与 Worker 是同一份。
"""
import json
import re
import subprocess
import threading
from pathlib import Path

from .. import config as cfg
from ..web import Route
from . import Module

FROZEN_STATUSES = ("frozen_paused", "resume_ready")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
LIST_LIMITS = (50, 1000)   # status 只返回最近的若干条任务；冻结任务没列全时放宽一次

# 命令行拒绝恢复时给出的英文原因，换成给人看的说法；认不出的原样显示。顺序即匹配顺序。
REFUSALS = (
    ("no current healthy probe", "needs_probe",
     "失败的那一侧现在没有新鲜的合格探测结果。请先重新“上班”做一次真实探测，通过后再回来恢复"),
    ("explicit freeze hash is required", "hash_mismatch", "没有带上冻结哈希"),
    ("freeze hash mismatch", "hash_mismatch", "冻结哈希与当前记录不一致，任务可能已被重新冻结，请刷新后核对"),
    ("task is not frozen", "not_frozen", "这个任务已经不在冻结状态"),
    ("receipt", "evidence_gap", "证据缺口：成功侧的执行回执缺失，或与冻结时不一致"),
    ("snapshot", "evidence_gap", "证据缺口：执行快照在冻结期间发生了变化"),
    ("Agent run is missing", "evidence_gap", "证据缺口：成功侧的运行记录缺失"),
    ("output hash", "evidence_gap", "证据缺口：成功侧的输出与冻结时不一致"),
    ("Provider attempt event", "evidence_gap", "证据缺口：事件记录与冻结时不一致"),
    ("frozen task record", "evidence_gap", "证据缺口：冻结记录本身损坏或被改动"),
)


def short(value, limit=160):
    text = str(value or "").replace("\n", " ").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


class FrozenModule(Module):
    id, title, kind = "frozen", "冻结任务", "service"

    def __init__(self, ctx):
        super().__init__(ctx)
        settings = ctx.config["review"]
        # 已安装并自带发布校验的小八入口；与裁决模块用的是同一个
        self.cli = Path(settings.get("cli") or (Path.home() / ".agents/skills/xiaoba-agentos/scripts/xiaoba"))
        self.config_path = cfg.agentos_config_path(ctx.config)   # 默认跟随 Worker，见 config.agentos_config_path
        self._resume_lock = threading.Lock()

    def routes(self):
        return [
            Route("GET", "/api/review/frozen", self.listing, "read"),
            Route("POST", "/api/review/frozen/resume", self.resume, "write"),
        ]

    # ---- 调用小八入口 ----
    def _cli(self, args, timeout=90):
        """用与 Worker 相同的配置调用已安装的小八入口，返回 (退出码, 解析后的 JSON 或 None, 出错说明)。"""
        if not self.cli.is_file():
            return None, None, "找不到已安装的小八入口：%s" % self.cli
        try:
            done = subprocess.run([str(self.cli), "--config", str(self.config_path)] + list(args),
                                  capture_output=True, text=True, timeout=timeout, cwd=str(Path.home()))
        except subprocess.TimeoutExpired:
            return None, None, "小八入口在 %d 秒内没有返回" % timeout
        except OSError as exc:
            return None, None, "无法运行小八入口：%s" % short(exc, 120)
        try:
            data = json.loads(done.stdout)
        except ValueError:
            return done.returncode, None, short(done.stderr or done.stdout or "命令没有返回内容", 300)
        return done.returncode, (data if isinstance(data, dict) else None), ""

    # ---- 列出 ----
    def _frozen_raw(self):
        """返回 (冻结任务的原始行, 是否列全, 新鲜的后台健康, 出错说明)。读不到时第一项为 None。"""
        rows, complete, health = [], True, {}
        for limit in LIST_LIMITS:
            code, data, error = self._cli(["status", "--json", "--limit", str(limit)])
            if data is None or not isinstance(data.get("tasks"), list):
                if isinstance(data, dict) and isinstance(data.get("database_schema"), dict):
                    error = "小八的数据库需要先迁移，暂时读不到任务"
                return None, False, {}, error or "小八入口没有返回任务清单"
            rows = [t for t in data["tasks"] if isinstance(t, dict) and t.get("status") in FROZEN_STATUSES and t.get("id")]
            counts = (data.get("summary") or {}).get("tasks") if isinstance(data.get("summary"), dict) else None
            counts = counts if isinstance(counts, dict) else {}
            expected = sum(int(counts.get(status) or 0) for status in FROZEN_STATUSES)
            health = {str(row.get("agent_name")): str(row.get("status")) for row in (data.get("agent_health") or [])
                      if isinstance(row, dict)}
            complete = len(rows) >= expected
            if complete:
                break
        return rows, complete, health, ""

    @staticmethod
    def _item(task, health):
        try:
            frozen = json.loads(task.get("freeze_json") or "{}")
        except (ValueError, TypeError):
            frozen = None
        readable = isinstance(frozen, dict) and bool(frozen)
        frozen = frozen if readable else {}
        failures = []
        for entry in frozen.get("failures") or []:
            if not isinstance(entry, dict):
                continue
            agent = str(entry.get("agent") or "")
            failures.append({
                "agent": agent or "未知", "failureClass": str(entry.get("failure_class") or ""),
                "error": short(entry.get("error"), 300),
                "outputBudget": entry.get("output_limit_exhausted") is True,
                # 这一侧现在有没有新鲜的合格探测；没有的话命令行会拒绝恢复
                "probe": health.get(agent) or "none",
            })
        completed = [str(run.get("agent")) for run in (frozen.get("completed_runs") or [])
                     if isinstance(run, dict) and run.get("agent")]
        receipts = [r for r in (frozen.get("execution_receipts") or []) if isinstance(r, dict)]
        task_id = str(task["id"])
        return {
            "id": task_id, "shortId": task_id[:8], "title": short(task.get("prompt"), 80) or "（没有标题）",
            "status": task.get("status"), "legacy": task.get("status") == "resume_ready",
            "frozenAt": frozen.get("frozen_at") or task.get("updated_at"),
            "failureClass": str(task.get("failure_class") or frozen.get("failure_class") or ""),
            "failures": failures, "succeeded": completed,
            "hasReceipt": bool(receipts), "receipts": len(receipts),
            "freezeHash": str(task.get("freeze_hash") or ""),
            "resumeGeneration": int(task.get("resume_generation") or 0),
            "problem": "" if readable else "冻结记录读不出来，命令行多半会拒绝恢复",
        }

    def listing(self, req):
        rows, complete, health, error = self._frozen_raw()
        if rows is None:
            return 502, {"ok": False, "error": error}
        items = [self._item(row, health) for row in rows]
        return 200, {"ok": True, "items": items, "count": len(items), "complete": complete}

    # ---- 恢复：一次一个，必须带页面上显示的那个冻结哈希 ----
    @staticmethod
    def _refusal(result, error):
        raw = str(result.get("error") or "") if isinstance(result, dict) else ""
        for key, kind, label in REFUSALS:
            if key in raw:
                return kind, label
        if isinstance(result, dict) and raw and raw.strip("'\"") == str(result.get("task_id") or ""):
            return "not_found", "小八里找不到这个任务"
        return "blocked", short(raw or error or "命令行拒绝了这次恢复", 300)

    def resume(self, req):
        body = req.json()
        task_id, freeze_hash = body.get("taskId"), body.get("freezeHash")
        if any(key in body for key in ("items", "tasks", "taskIds")) or isinstance(task_id, (list, dict)):
            return 400, {"ok": False, "error": "一次只能恢复一个任务"}
        if not isinstance(task_id, str) or not task_id:
            return 400, {"ok": False, "error": "请求无效：需要指明一个任务"}
        if not isinstance(freeze_hash, str) or not HASH_RE.match(freeze_hash):
            return 400, {"ok": False, "error": "请求无效：需要带上页面上显示的冻结哈希"}
        if not self._resume_lock.acquire(blocking=False):   # 前一个没结束不接下一个
            return 409, {"ok": False, "kind": "busy", "error": "上一个恢复还没有结束，请等它返回后再操作"}
        try:
            rows, _, _, error = self._frozen_raw()
            if rows is None:
                return 502, {"ok": False, "error": error}
            # 只接受清单里此刻确实冻结着的那一个，哈希也要与此刻的记录一致；页面传来的编号不直接交给命令行
            target = next((row for row in rows if str(row["id"]) == task_id), None)
            if target is None:
                return 409, {"ok": False, "kind": "not_frozen", "error": "这个任务已不在冻结清单里，请刷新后再看"}
            if str(target.get("freeze_hash") or "") != freeze_hash:
                return 409, {"ok": False, "kind": "hash_mismatch",
                             "error": "冻结哈希与当前记录不一致，任务可能已被重新冻结，请刷新后核对"}
            code, result, error = self._cli(["task-resume", str(target["id"]), "--freeze-hash", freeze_hash], timeout=120)
            ok = code == 0 and isinstance(result, dict) and result.get("status") == "retry"
            self.ctx.log(self.id, "frozen task resumed by user" if ok else "frozen task resume refused",
                         "info" if ok else "warning", task=task_id[:8])
            if ok:
                return 200, {"ok": True, "status": "retry", "taskId": task_id,
                             "resumeGeneration": result.get("resume_generation"),
                             "resumedFrom": result.get("resumed_from"),
                             "outputBudgetExhausted": result.get("output_budget_exhausted") is True,
                             "warning": result.get("warning") or "", "result": result}
            kind, label = self._refusal(result, error)
            return 409, {"ok": False, "kind": kind, "error": "没有恢复：%s" % label, "result": result,
                         "detail": "" if result else short(error, 300)}
        finally:
            self._resume_lock.release()
