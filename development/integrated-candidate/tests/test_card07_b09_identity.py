"""卡 07 / B09：monitor.json 与裁决后端是否同一数据库/配置身份的反例测试。

只用临时配置、临时 monitor.json、临时 backend-state.json；不连端口、不碰生产库、
不调模型、不派活。三种失败必须明示，绝不假健康：
  - unavailable：monitor.json 不可读；
  - stale：后端记录进程已不在；
  - different_database：monitor/会话/后端三方库不一致。
"""
import json
import os
import plistlib
from unittest.mock import patch
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from xiaoba_agentos.health import monitor_database_identity  # noqa: E402
from xiaoba_agentos.repo_paths import resolve_monitor_root  # noqa: E402
from xiaoba_agentos.review_ui_install import ASSETS as UI_ASSETS, resolve_asset_path  # noqa: E402

from fixtures_review import _config  # noqa: E402


class B09IdentityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = _config(self.root / "project", "config/agentos.json")
        self.project = self.config.project_dir
        self.monitor_json = self.root / "monitor.json"
        shared = self.root / "monitor_core/agentos_config.py"
        shared.parent.mkdir()
        source = resolve_asset_path(UI_ASSETS, "agentos_config.py")
        shared.write_bytes(source.read_bytes())
        self.empty_env = {}
        self.no_plist = self.root / "no-worker.plist"

    def _write_monitor(self, config_path=None, project=None):
        data = {"agentos_project": str(project or self.project)}
        if config_path is not None:
            data["agentos_config"] = str(config_path)
        self.monitor_json.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def _call(self, **kw):
        kw.setdefault("env", self.empty_env)
        kw.setdefault("worker_plist", self.no_plist)
        return monitor_database_identity(self.config, self.monitor_json, **kw)

    # ---- unavailable 明示 ----
    def test_missing_monitor_json_is_unavailable_not_healthy(self):
        result = monitor_database_identity(self.config, self.root / "none.json",
                                           env=self.empty_env, worker_plist=self.no_plist)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "unavailable")
        self.assertTrue(result["warnings"])

    def test_monitor_json_same_config_is_ok(self):
        self._write_monitor(self.config.path)
        result = self._call()
        self.assertTrue(result["ok"], result["problems"])
        self.assertEqual(result["status"], "ok")

    # ---- different_database 明示 ----
    def test_different_database_fails_closed(self):
        other_root = self.root / "other-project"
        other = _config(other_root, "config/agentos.json")
        self._write_monitor(other.path, project=other_root)
        result = self._call()
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "different_database")
        self.assertTrue(any("库不同" in p for p in result["problems"]))

    def test_missing_installed_resolver_is_unavailable(self):
        self.monitor_json=self.root/'without-shared/monitor.json'
        self.monitor_json.parent.mkdir()
        self._write_monitor(self.config.path)
        result=self._call()
        self.assertEqual('unavailable',result['status']);self.assertFalse(result['ok'])
        self.assertTrue(any('公共配置解析器' in p for p in result['problems']))

    def test_installed_resolver_drift_blocks_configuration_consistency(self):
        from xiaoba_agentos.health import config_consistency
        self._write_monitor(self.config.path)
        (self.root/'monitor_core/agentos_config.py').write_text('# TEST drift')
        result=self._call()
        self.assertEqual('different_config',result['status'])
        report=config_consistency(self.config,home=self.root/'empty-home',monitor_json=self.monitor_json)
        self.assertFalse(report['ok'])

    def test_monitor_config_unreadable_is_unavailable(self):
        self._write_monitor(self.root / "does-not-exist.json")
        result = self._call()
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "unavailable")

    # ---- 裁决后端：一致 / 不同库 / stale ----
    def test_review_backend_same_config_is_ok(self):
        self._write_monitor(self.config.path)
        state = self.root / "backend-state.json"
        state.write_text(json.dumps({"pid": os.getpid(), "port": 8797,
                                     "config_path": str(self.config.path)}), encoding="utf-8")
        result = self._call(backend_state=state)
        self.assertTrue(result["ok"], result["problems"])
        self.assertIn("review_backend", result["identities"])

    def test_review_backend_different_database_fails_closed(self):
        self._write_monitor(self.config.path)
        other = _config(self.root / "other", "config/agentos.json")
        state = self.root / "backend-state.json"
        state.write_text(json.dumps({"pid": os.getpid(), "port": 8797,
                                     "config_path": str(other.path)}), encoding="utf-8")
        result = self._call(backend_state=state)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "different_database")

    def test_review_backend_stale_pid_is_explicit(self):
        self._write_monitor(self.config.path)
        state = self.root / "backend-state.json"
        # 用一个极可能不存在的 PID，不依赖具体进程表：997311 一般为空。
        state.write_text(json.dumps({"pid": 997311, "port": 8797,
                                     "config_path": str(self.config.path)}), encoding="utf-8")
        result = self._call(backend_state=state)
        self.assertEqual(result["status"], "stale")
        self.assertTrue(any("stale" in w for w in result["warnings"]))
        self.assertFalse(result["identities"]["review_backend"]["alive"])

    def test_shared_precedence_relative_paths_and_missing_plist_target(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "c07_shared_agentos_config", resolve_asset_path(UI_ASSETS, "agentos_config.py")
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        agentos_config_path = mod.agentos_config_path
        settings = {"root":str(self.root), "agentos_project":str(self.project)}
        env_config = self.config.path
        plist = self.root / "worker.plist"
        plist.write_bytes(plistlib.dumps({"EnvironmentVariables":{"XIAOBA_CONFIG":str(self.root/'missing')}}))
        self.assertEqual(self.project/'config/agentos.json', agentos_config_path(settings,env={},worker_plist=plist))
        self.assertEqual(env_config, agentos_config_path(settings,env={"XIAOBA_CONFIG":str(env_config)},worker_plist=plist))
        settings["agentos_config"] = "project/config/agentos.json"
        self.assertEqual(env_config,agentos_config_path(settings,env={"XIAOBA_CONFIG":"other"},worker_plist=plist))

    def test_same_database_different_config_is_a_mismatch(self):
        other = self.root/'changed.json'
        raw=json.loads(self.config.path.read_text());raw['worker']['poll_seconds']=23
        other.write_text(json.dumps(raw))
        self._write_monitor(other)
        result=self._call()
        self.assertEqual('different_config',result['status'])
        self.assertFalse(result['ok'])

    def test_stale_backend_never_masks_actual_database_mismatch(self):
        other=_config(self.root/'other','config/agentos.json')
        self._write_monitor(other.path)
        state=self.root/'stale.json'
        state.write_text(json.dumps({'pid':997311,'config_path':str(self.config.path)}))
        with patch('os.kill',side_effect=ProcessLookupError):
            self.assertEqual('different_database',self._call(backend_state=state)['status'])

    def test_production_config_consistency_includes_monitor(self):
        from xiaoba_agentos.health import config_consistency, doctor
        from xiaoba_agentos.store import EventStore
        self._write_monitor(_config(self.root/'other','config/agentos.json').path)
        with patch.dict(os.environ,{},clear=True):
            report=config_consistency(self.config,home=self.root/'empty-home',monitor_json=self.monitor_json)
        self.assertFalse(report['ok']);self.assertEqual('different_database',report['monitor_database_identity']['status'])
        # Doctor reaches the same production call; no model/port activity.
        monitor=self.project.parent/'unused-monitor.json'
        real = config_consistency
        self.config.raw['memory'].update(knowlp_python=sys.executable,knowlp_module=str(self.root/'missing-module'))
        self.config.raw['integrations']['node_command']=sys.executable
        self.config.raw['skills']={'root_dir':str(self.root/'skills'),'index_path':str(self.root/'skills/index.json')}
        self.config.raw['knowledge']={'index_path':str(self.root/'knowledge.json'),'allowed_roots':[str(self.root)]}
        self.config.path.write_text(json.dumps(self.config.raw))
        store=EventStore(self.config.database_path);store.initialize()
        with patch('xiaoba_agentos.health.config_consistency',side_effect=lambda cfg,binding: real(cfg,binding,home=self.root/'empty-home',monitor_json=self.monitor_json)) as called:
            result=doctor(self.config,store,entity_root=self.root/'entities')
        called.assert_called_once()
        self.assertFalse(result['config_consistency']['ok'])

    def test_absent_monitor_is_diagnostic_without_blocking_consistency(self):
        from xiaoba_agentos.health import config_consistency
        result=config_consistency(self.config,home=self.root/'empty-home',monitor_json=self.root/'absent.json')
        self.assertTrue(result['ok'])
        self.assertEqual('unavailable',result['monitor_database_identity']['status'])

    def test_monitor_installed_resolver_matches_asset_and_runtime(self):
        import importlib.util
        core=Path(__file__).resolve().parents[1]
        mon=resolve_monitor_root(core.parents[1])
        asset=resolve_asset_path(UI_ASSETS, 'agentos_config.py')
        self.assertEqual(asset.read_bytes(),(mon/'monitor_core/agentos_config.py').read_bytes())
        spec=importlib.util.spec_from_file_location('c07_installed_config',mon/'monitor_core/agentos_config.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        self.assertEqual(self.config.runtime_dir,module.agentos_runtime(self.config.path))
        sys.path.insert(0,str(mon))
        from monitor_core.modules.overview import Monitor
        instance=Monitor(self.project,self.config.path)
        self.assertEqual(self.config.database_path,instance.db_path)

    def test_monitor_invalid_config_is_unavailable_without_fallback_database(self):
        core=Path(__file__).resolve().parents[1]
        mon=resolve_monitor_root(core.parents[1])
        sys.path.insert(0,str(mon))
        from monitor_core.modules.overview import Monitor
        invalid=self.root/'invalid.json';invalid.write_text('{}')
        instance=Monitor(self.project,invalid)
        self.assertIsNone(instance.db_path)
        view=instance.snapshot()
        self.assertFalse(view['ops']['available'])
        self.assertEqual('unavailable',view['nodeHealth']['source'])

    def test_no_backend_record_only_checks_two_parties(self):
        self._write_monitor(self.config.path)
        result = self._call(backend_state=self.root / "missing-state.json")
        self.assertTrue(result["ok"])
        self.assertTrue(any("只核 monitor 与会话两方" in w for w in result["warnings"]))


if __name__ == "__main__":
    unittest.main()
