"""进度事件与租约续期一次写多少条。

全部离线：临时库、假适配器、可注入的假时钟，不真的睡，也不启动进程。
无进展判定在 `adapters.run_cli` 里，本文件不碰它，只数事件链写了几条、什么时候写。
"""
import json
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from xiaoba_agentos.adapters import AgentResult, BaseAdapter, PermanentAdapterError
from xiaoba_agentos.orchestrator import (
    LEASE_RENEW_REMAINING_RATIO,
    Orchestrator,
    PROGRESS_EVENT_MAX_SILENCE_SECONDS,
    PROGRESS_EVENT_MIN_INTERVAL_SECONDS,
)
from xiaoba_agentos.receipts import ExecutionReceiptStore
import test_standby_substitution as fixtures


PROGRESS_FIELDS = {
    "agent", "run_id", "stage", "pid", "elapsed_seconds", "stdout_bytes",
    "activity_events", "no_progress_seconds", "idle_timeout_seconds",
}


class Clock:
    def __init__(self, origin):
        self.origin = origin
        self.now = origin

    def time(self):
        return self.now

    def monotonic(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@contextmanager
def fake_clock(origin):
    """把假时钟交出去，让 10 分钟的模拟在毫秒内跑完。

    `time.time` / `time.monotonic` 是共享模块上的属性，替换一次即覆盖 store、
    orchestrator 和 receipts 里所有的取时调用。
    """
    clock = Clock(origin)
    with patch("time.time", new=clock.time), patch("time.monotonic", new=clock.monotonic):
        yield clock


def update(stage, activity_events, no_progress_seconds=0.5):
    """一份与 `run_cli` 回调字段一致的更新（接口不许改，所以这里逐字段对齐）。"""
    return {"stage": stage, "pid": 4242, "stdout_bytes": 4096,
            "activity_events": activity_events, "no_progress_seconds": no_progress_seconds,
            "idle_timeout_seconds": 300}


def steps(count, stage="model_activity", activity=lambda index: index + 1, no_progress=0.5):
    return [(stage, activity(index), no_progress) for index in range(count)]


class PacedCli(BaseAdapter):
    """代替一次 CLI 运行：按 orchestrator 给的 progress_interval 推进假时钟并回调。

    回调抛出的错误原样往外传，和 `run_cli` 一样（真实实现里那会结束进程组）。
    """

    def __init__(self, agent, clock, plan, on_step=None):
        BaseAdapter.__init__(self, agent)
        self.clock = clock
        self.plan = plan
        self.on_step = on_step

    def run(self, prompt, context):
        for index, (stage, activity_events, no_progress) in enumerate(self.plan):
            if index:
                self.clock.advance(self.progress_interval)
            if self.on_step is not None:
                self.on_step(index)
            payload = update(stage, activity_events, no_progress)
            payload["elapsed_seconds"] = round(self.clock.now - self.clock.origin, 3)
            self.progress_callback(payload)
        return AgentResult(self.config.name, "review complete", 1000)


class ProgressEventRateTest(unittest.TestCase):
    def _begin(self, lease_seconds):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        helper = fixtures.StandbySubstitutionTest()
        config = helper._config(Path(directory.name), fixtures.PRIMARIES)
        config.raw["worker"]["lease_seconds"] = lease_seconds
        store = helper._store(config)
        task_id = store.submit("降频验收", 3)
        # 先取时基再领租约，剩下的租期只会略多于整数倍，边界步长不受调度抖动影响。
        start = time.time()
        task = store.lease_next(lease_seconds)
        return SimpleNamespace(config=config, store=store, task_id=task_id,
                               attempt=task["attempts"], lease_seconds=float(lease_seconds),
                               start=start)

    def _events(self, world, event_type):
        with world.store.read_only() as connection:
            return [json.loads(row["payload_json"] or "{}") for row in connection.execute(
                "SELECT payload_json FROM events WHERE task_id=? AND event_type=? ORDER BY sequence",
                (world.task_id, event_type))]

    def _drive(self, world, clock, plan, on_step=None):
        adapter = PacedCli(world.config.agents[0], clock, plan, on_step)
        with patch("xiaoba_agentos.orchestrator.build_adapter", return_value=adapter):
            return Orchestrator(world.config, world.store)._run_agent(
                world.task_id, world.config.agents[0], "review", "", world.attempt)

    def _receipt(self, world):
        run_id = world.store.task_runs(world.task_id)[0]["id"]
        path = world.config.runtime_dir / "receipts" / world.task_id / (str(run_id) + ".json")
        return ExecutionReceiptStore(path).get(str(run_id))

    # ------------------------------------------------------------ 写入频率

    def test_ten_minutes_of_steady_activity_writes_about_one_event_per_interval(self):
        world = self._begin(1800)
        plan = steps(121)  # 每 5 秒一次，共 600 秒，stage 不变、活动数一直涨
        with fake_clock(world.start) as clock:
            self._drive(world, clock, plan)
            written = self._events(world, "agent.progress")
            renewed = self._events(world, "task.lease_renewed")
            self.assertTrue(world.store.owns_lease(world.task_id, world.attempt))
        self.assertLessEqual(len(written), 22, "10 分钟不该再写两百多条进度事件")
        self.assertGreaterEqual(len(written), 15, "降频不能把进度事件降成没有")
        # 1800 s lease, 600 s run: renewal starts below 900 s left, so none yet.
        self.assertEqual(0, len(renewed))
        gaps = [second["elapsed_seconds"] - first["elapsed_seconds"]
                for first, second in zip(written, written[1:])]
        self.assertTrue(all(gap >= PROGRESS_EVENT_MIN_INTERVAL_SECONDS for gap in gaps), gaps)
        # Round 28: and never longer than the backstop plus one callback interval.
        self.assertTrue(all(gap <= PROGRESS_EVENT_MAX_SILENCE_SECONDS + 5.0 for gap in gaps), gaps)
        self.assertTrue(world.store.verify_event_chain()["ok"])

    def test_a_quiet_run_still_gets_one_liveness_event_per_backstop_interval(self):
        world = self._begin(1800)
        # 租期 1800 秒 => 回调每 5 秒一次；60 秒的兜底间隔就是每 12 步一条。
        stride = int(PROGRESS_EVENT_MAX_SILENCE_SECONDS / 5.0)
        plan = [("cli_ready", 0, round(index * 5.0, 3)) for index in range(121)]
        with fake_clock(world.start) as clock:
            self._drive(world, clock, plan)
            written = self._events(world, "agent.progress")
        self.assertEqual(len(plan[::stride]), len(written))
        self.assertEqual([step[2] for step in plan[::stride]],
                         [row["no_progress_seconds"] for row in written])
        self.assertTrue(world.store.verify_event_chain()["ok"])

    def test_a_stage_change_writes_immediately_instead_of_waiting(self):
        world = self._begin(1800)
        plan = [("cli_ready", 0, 0.5), ("model_activity", 1, 0.5)]
        with fake_clock(world.start) as clock:
            self._drive(world, clock, plan)
            written = self._events(world, "agent.progress")
        self.assertEqual(2, len(written))
        self.assertEqual(["cli_ready", "model_activity"], [row["stage"] for row in written])
        self.assertEqual(5.0, written[1]["elapsed_seconds"] - written[0]["elapsed_seconds"])

    def test_the_progress_event_payload_keeps_every_current_field(self):
        world = self._begin(1800)
        with fake_clock(world.start) as clock:
            self._drive(world, clock, steps(1))
            written = self._events(world, "agent.progress")
        self.assertEqual(PROGRESS_FIELDS, set(written[0]))
        self.assertEqual(world.config.agents[0].name, written[0]["agent"])

    # ------------------------------------------------------------ 续租时机

    def test_the_lease_renews_only_once_it_is_past_the_half_way_mark(self):
        # 租期 100 秒：45 秒时还剩 55 秒，不该续；55 秒时还剩 45 秒，续一次。
        for steps_taken, expected in ((10, 0), (12, 1)):
            world = self._begin(100)
            with self.subTest(steps=steps_taken), fake_clock(world.start) as clock:
                self._drive(world, clock, steps(steps_taken))
                renewed = self._events(world, "task.lease_renewed")
                left = world.store.current_lease_until(world.task_id, world.attempt) - clock.now
            self.assertEqual(expected, len(renewed))
            self.assertGreaterEqual(left, world.lease_seconds * 0.5)

    def test_renewal_failure_stops_the_run_before_any_progress_event(self):
        world = self._begin(100)
        with fake_clock(world.start) as clock, patch.object(
                world.store, "renew_lease", return_value=False):
            with self.assertRaisesRegex(PermanentAdapterError, "task lease lost"):
                self._drive(world, clock, steps(12))
            written = self._events(world, "agent.progress")
            renewed = self._events(world, "task.lease_renewed")
            receipt = self._receipt(world)
        self.assertEqual([], renewed)
        self.assertEqual(2, len(written))
        # 抛错发生在写回执之前：t=55 那一次既没续租也没落盘，停在 t=50。
        self.assertEqual(50.0, receipt["progress"]["elapsed_seconds"])
        self.assertEqual(world.start + 50.0, receipt["heartbeat_at"])

    def test_a_lost_lease_stops_the_next_callback_from_renewing_or_writing(self):
        world = self._begin(100)

        def take_over(index):
            if index != 3:
                return
            clock.now = world.start + 101
            world.store.recover_expired()
            world.store.lease_next(100)

        with fake_clock(world.start) as clock:
            with self.assertRaisesRegex(PermanentAdapterError, "task lease lost"):
                self._drive(world, clock, steps(8), on_step=take_over)
            written = self._events(world, "agent.progress")
            renewed = self._events(world, "task.lease_renewed")
            task = world.store.get_task(world.task_id)
            self.assertFalse(world.store.owns_lease(world.task_id, world.attempt))
            self.assertTrue(world.store.verify_event_chain()["ok"])
        self.assertEqual(2, task["attempts"])
        self.assertEqual([], renewed)
        self.assertEqual(1, len(written))

    # ------------------------------------------------------------ 回执照旧

    def test_every_callback_updates_the_receipt_even_when_no_event_is_written(self):
        world = self._begin(1800)
        with fake_clock(world.start) as clock:
            self._drive(world, clock, steps(4))
            written = self._events(world, "agent.progress")
            receipt = self._receipt(world)
        self.assertEqual(1, len(written))
        self.assertEqual(15.0, receipt["progress"]["elapsed_seconds"])
        self.assertEqual(4, receipt["progress"]["activity_events"])
        self.assertEqual(world.start + 15.0, receipt["heartbeat_at"])

    def test_the_three_thresholds_are_named_constants_not_config(self):
        self.assertEqual((0.5, 30.0, 60.0),
                         (LEASE_RENEW_REMAINING_RATIO,
                          PROGRESS_EVENT_MIN_INTERVAL_SECONDS,
                          PROGRESS_EVENT_MAX_SILENCE_SECONDS))
        # ...and not configuration: neither the shipped template nor the code's
        # config reader knows these knobs.
        template = (Path(__file__).resolve().parents[1] / "config/agentos.json").read_text(encoding="utf-8")
        config_source = (Path(__file__).resolve().parents[1] / "src/xiaoba_agentos/config.py").read_text(encoding="utf-8")
        for knob in ("LEASE_RENEW_REMAINING_RATIO", "PROGRESS_EVENT_MIN_INTERVAL", "PROGRESS_EVENT_MAX_SILENCE",
                     "lease_renew", "progress_event_min", "progress_event_max"):
            self.assertNotIn(knob.lower(), template.lower())
            self.assertNotIn(knob, config_source)


if __name__ == "__main__":
    unittest.main()
