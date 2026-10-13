import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from xiaoba_agentos.config import AppConfig, ConfigError
from xiaoba_agentos.gateway import GatewayUnavailable, _desktop_failure_reason, ensure_desktop_available, local_cli_route


class DesktopDiagnosisTest(unittest.TestCase):
    def _config(self, root: Path, **route):
        return SimpleNamespace(
            project_dir=root, integrations={"node_command": "/usr/bin/true"},
            cli_routes={"local_agy": {"driver": "agy", "selection": "local_account",
                                      "desktop_debug_port": str(root / "port"), **route}},
        )

    def _agents(self):
        return [SimpleNamespace(name="agy_review", type="local_cli", raw={"driver": "agy", "cli_route": "local_agy"})]

    @staticmethod
    def _failed(reason):
        return subprocess.CompletedProcess([], 1, "", "refusing fallback.\nreason=" + reason + "\n")

    def test_reason_line_is_parsed_and_unknown_values_are_not_trusted(self):
        self.assertEqual("ambiguous_window", _desktop_failure_reason("x\nreason=ambiguous_window\n"))
        self.assertEqual("unknown", _desktop_failure_reason("reason=made_up_code\n"))
        self.assertEqual("unknown", _desktop_failure_reason("no reason here"))
        self.assertEqual("unknown", _desktop_failure_reason(None))

    def test_route_error_names_the_cause_and_still_refuses_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self._config(Path(directory))
            with patch("xiaoba_agentos.gateway.subprocess.run", return_value=self._failed("ambiguous_window")):
                with self.assertRaises(GatewayUnavailable) as caught:
                    local_cli_route(config, "local_agy", "agy_review")
            message = str(caught.exception)
            self.assertIn("不使用静态备用模型", message)
            self.assertIn("多个窗口", message)
            self.assertEqual("ambiguous_window", caught.exception.desktop_reason)

    def test_desktop_is_never_started_unless_the_route_opts_in(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self._config(Path(directory))
            with patch("xiaoba_agentos.gateway.subprocess.run") as run:
                self.assertEqual([], ensure_desktop_available(config, self._agents()))
            run.assert_not_called()

    def test_opted_in_route_starts_a_stopped_desktop_and_waits_for_it(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self._config(Path(directory), desktop_auto_start=True,
                                  desktop_app_path="/Applications/Antigravity.app", desktop_start_wait_seconds=5)
            ready = subprocess.CompletedProcess([], 0, "{}", "")
            opened = subprocess.CompletedProcess([], 0, "", "")
            results = [self._failed("desktop_not_running"), opened, self._failed("desktop_not_running"), ready]
            with patch("xiaoba_agentos.gateway.subprocess.run", side_effect=results) as run, patch(
                "xiaoba_agentos.gateway.time.sleep"
            ):
                actions = ensure_desktop_available(config, self._agents())
            self.assertEqual([{"route": "local_agy", "started": True, "ready": True, "reason": "desktop_not_running"}], actions)
            self.assertEqual(["/usr/bin/open", "-g", "-a", "/Applications/Antigravity.app"], run.call_args_list[1].args[0])

    def test_other_failures_are_not_answered_by_launching_the_app(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self._config(Path(directory), desktop_auto_start=True,
                                  desktop_app_path="/Applications/Antigravity.app")
            with patch("xiaoba_agentos.gateway.subprocess.run", return_value=self._failed("ambiguous_window")) as run:
                actions = ensure_desktop_available(config, self._agents())
            self.assertEqual(1, run.call_count)
            self.assertFalse(actions[0]["started"])

    def test_auto_start_requires_a_valid_app_path(self):
        base = json.loads((Path(__file__).resolve().parents[1] / "config" / "agentos.json").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def load(**route):
                raw = json.loads(json.dumps(base))
                raw["cli_routes"]["local_agy"].update(route)
                config = AppConfig(root / "config.json", raw)
                config._validate_cli_routes()
                return config

            load()
            load(desktop_auto_start=True, desktop_app_path="/Applications/Antigravity.app")
            for bad in (
                {"desktop_auto_start": True},
                {"desktop_auto_start": True, "desktop_app_path": "Antigravity.app"},
                {"desktop_auto_start": True, "desktop_app_path": "/bin/sh"},
                {"desktop_auto_start": "yes"},
                {"desktop_auto_start": True, "desktop_app_path": "/Applications/Antigravity.app",
                 "desktop_start_wait_seconds": 9999},
            ):
                with self.subTest(bad=bad), self.assertRaises(ConfigError):
                    load(**bad)


if __name__ == "__main__":
    unittest.main()
