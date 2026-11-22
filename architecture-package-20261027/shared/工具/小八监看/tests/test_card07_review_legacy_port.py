"""卡 07 C07-02：review.py 旧端口（默认 8797）分支的反例。

不启动/停止 8797、不碰线上服务：用纯判定函数 + 临时端口上的假后端验证：
  - 默认态：legacy_port 上没有服务 → 自己拉起临时端口，绝不把 8797 当默认后端；
  - 明确旧服务：legacy_port 上确有服务 → 使用它（external），避免两个后端同时写队列；
  - 探测不可用：不另起后端；已认领的后端优先；脚本缺失不拉起。

兼容探测是为防双写保留，未证实前不移除；这些反例把它固定成可执行契约。
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from monitor_core.modules.review import legacy_port_decision  # noqa: E402


class LegacyPortDecisionTest(unittest.TestCase):
    def test_default_no_legacy_service_spawns_on_managed_port(self):
        # 默认：没有上次后端、没有旧服务、探测可用、允许管理、脚本存在 → 自己拉起
        self.assertEqual(
            "spawn", legacy_port_decision(None, False, False, "", True, True))
        # 不允许管理（测试模式）→ 空闲，不误用 8797
        self.assertEqual("idle", legacy_port_decision(None, False, False, "", False, True))

    def test_explicit_old_service_is_reused_not_spawned(self):
        # 旧独立服务确实在 legacy_port 上跑：使用它，避免两个后端同时写待审队列
        self.assertEqual("use_legacy", legacy_port_decision(None, False, True, "", True, True))
        # 即使脚本存在，旧服务优先，不另起第二个后端
        self.assertNotEqual("spawn", legacy_port_decision(None, False, True, "", True, True))

    def test_probe_unavailable_never_starts_a_second_backend(self):
        self.assertEqual(
            "defer_probe", legacy_port_decision(None, False, False, "lsof unavailable", True, True))
        # 即便旧服务“看起来活着”但探测不可用，也不据此重复拉起
        self.assertEqual(
            "defer_probe", legacy_port_decision(None, False, False, "timeout", True, True))

    def test_uncertain_previous_backend_blocks_live_legacy_adoption(self):
        self.assertEqual("defer_probe", legacy_port_decision(None, False, True, "unavailable", True, True))

    def test_previous_record_absence_clears_old_probe_warning(self):
        import tempfile
        from monitor_core.modules.review import ReviewModule
        with tempfile.TemporaryDirectory() as folder:
            module = ReviewModule.__new__(ReviewModule)
            module.state_file = Path(folder)/"missing.json"
            module.probe_warning = "old lsof error"
            self.assertIsNone(module._previous_backend())
            self.assertEqual("", module.probe_warning)

    def test_adopted_previous_backend_has_priority_over_legacy(self):
        # 上次由监看中心拉起、仍在监听的后端优先于旧服务
        self.assertEqual("adopt", legacy_port_decision((4242, 99999), True, True, "", True, True))

    def test_missing_script_does_not_claim_spawn(self):
        self.assertEqual("missing_script", legacy_port_decision(None, False, False, "", True, False))

    def test_previous_record_dead_falls_through_to_default(self):
        # 上次记录的进程已不在 → 视为无 → 默认拉起
        self.assertEqual("spawn", legacy_port_decision((4242, 99999), False, False, "", True, True))


if __name__ == "__main__":
    unittest.main()
