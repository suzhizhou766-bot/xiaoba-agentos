from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class AgentConfig:
    name: str
    type: str
    enabled: bool
    role: str
    timeout_seconds: int  # Explicit health-probe deadline; real CLI tasks wait to completion.
    app_config: "AppConfig"
    raw: Dict[str, Any]
    standby: bool = False

    @property
    def driver(self) -> str:
        return str(self.raw.get("driver", ""))



def agy_settings_default() -> Path:
    """Where the agy CLI keeps the model the user last chose (read at call time)."""
    return Path.home() / ".gemini" / "antigravity-cli" / "settings.json"


def qoder_settings_default() -> Path:
    """Where standard qodercli keeps the model the user last chose."""
    return Path.home() / ".qoder" / "settings.json"


def _require_agy_settings_path(value: Any) -> None:
    # Exactly the user's own agy settings file: a same-shaped path elsewhere
    # would let whoever writes the config pick the model (pre-review R9).
    if Path(str(value)).expanduser() != agy_settings_default():
        raise ConfigError("AGY 必须绑定本用户的 agy CLI 设置文件（~/.gemini/antigravity-cli/settings.json）")


class AppConfig:
    def __init__(self, path: Path, raw: Dict[str, Any]):
        self.path = path
        self.raw = raw

    @classmethod
    def load(cls, path: Path) -> "AppConfig":
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ConfigError(f"配置文件不存在：{path}") from exc
        except json.JSONDecodeError as exc:
            raise ConfigError(f"配置文件不是有效 JSON：{exc}") from exc
        if raw.get("schema_version") != 2:
            if raw.get("schema_version") == 1 and raw.get("agents") and all(item.get("type") == "mock" for item in raw["agents"]):
                return cls(path.resolve(), raw)
            raise ConfigError("只支持 schema_version=2")
        config = cls(path.resolve(), raw)
        config._validate_release_policy()
        config._validate_worker_policy()
        config._validate_recovery_policy()
        config._validate_cli_agents()
        config._validate_cli_routes()
        config._validate_standby_policy()
        config._validate_skill_policy()
        config._validate_knowledge_policy()
        config._validate_memory_policy()
        return config

    def _validate_release_policy(self) -> None:
        release = self.raw.get("release")
        if not isinstance(release, dict):
            raise ConfigError("非 mock 运行必须配置 release 绑定")
        index_value = str(release.get("index_path", "")).strip()
        expected_version = str(release.get("expected_version", "")).strip()
        if not index_value or not Path(index_value).expanduser().is_absolute():
            raise ConfigError("release.index_path 必须是绝对路径")
        if not expected_version or "/" in expected_version or "\\" in expected_version:
            raise ConfigError("release.expected_version 必须是安全的版本号")

    def _validate_worker_policy(self) -> None:
        worker = self.raw.get("worker", {})
        positive = ("poll_seconds", "lease_seconds", "max_attempts", "parallelism", "minimum_successful_agents")
        for key in positive:
            if int(worker.get(key, 0)) <= 0:
                raise ConfigError(f"worker.{key} 必须大于 0")
        budget = int(worker.get("max_agent_invocations_per_task", worker["max_attempts"] * worker["parallelism"]))
        if budget < int(worker["minimum_successful_agents"]):
            raise ConfigError("worker.max_agent_invocations_per_task 不能小于 minimum_successful_agents")
        if int(worker.get("log_heartbeat_polls", 1)) <= 0:
            raise ConfigError("worker.log_heartbeat_polls 必须大于 0")
        names = [str(item.get("name", "")) for item in self.raw.get("agents", [])]
        if any(not name for name in names) or len(names) != len(set(names)):
            raise ConfigError("Agent 名称不能为空或重复")

    def _validate_recovery_policy(self) -> None:
        recovery = self.raw.get("recovery", {})
        if not isinstance(recovery, dict):
            raise ConfigError("recovery 必须是对象")
        for key in ("repair_max_attempts", "repair_retry_seconds"):
            if int(recovery.get(key, 1)) <= 0:
                raise ConfigError(f"recovery.{key} 必须大于 0")
        if recovery.get("resume_requires_user", True) is not True:
            raise ConfigError("v1.4 recovery.resume_requires_user 必须为 true")
        if "automatic_actions" in recovery:
            actions = recovery["automatic_actions"]
            if not isinstance(actions, list):
                raise ConfigError("recovery.automatic_actions 必须是列表")
            if actions:
                raise ConfigError("v1.5.2 同伴自动修复已退役，recovery.automatic_actions 必须为空列表或缺省")

    def _validate_cli_agents(self) -> None:
        required_integrations = {"node_command"}
        required_integrations.update(
            f"{item.get('driver')}_command"
            for item in self.raw.get("agents", [])
            if item.get("type") != "mock" and item.get("driver")
        )
        missing_integrations = sorted(required_integrations - set(self.raw.get("integrations", {})))
        if missing_integrations:
            raise ConfigError("缺少受控 CLI 集成：" + ", ".join(missing_integrations))
        integrations = self.raw.get("integrations", {})
        for item in self.raw.get("agents", []):
            if item.get("type") == "mock":
                continue
            if item.get("type") != "local_cli":
                raise ConfigError(f"{item.get('name', '<unnamed>')} 使用了未受控 CLI 类型")
            driver = str(item.get("driver", ""))
            if driver not in {"agy", "dsh", "qoder"}:
                raise ConfigError(f"{item.get('name', '<unnamed>')} 的 local_cli 驱动必须是 agy、dsh 或 qoder")
            if str(item.get("gateway_route", "")).strip():
                raise ConfigError("local_cli 不得配置 gateway_route")
            if str(item.get("model", "")).strip():
                raise ConfigError("local_cli 模型由 cli_route.model_pool 决定")
            if not str(item.get("cli_route", "")).strip():
                raise ConfigError(f"{item.get('name', '<unnamed>')} 缺少 cli_route")
            if driver == "qoder" and not str(integrations.get("qoder_command", "")).strip():
                raise ConfigError("qoder 后台必须在 integrations.qoder_command 登记 qodercli 路径")

    def _validate_cli_routes(self) -> None:
        routed_agents = [
            item
            for item in self.raw.get("agents", [])
            if item.get("type") == "local_cli"
        ]
        routes = self.raw.get("cli_routes", {})
        if routed_agents and not isinstance(routes, dict):
            raise ConfigError("CLI Agent 需要 cli_routes 配置")
        for item in routed_agents:
            route_name = str(item.get("cli_route", "")).strip()
            route = routes.get(route_name) if isinstance(routes, dict) else None
            if not isinstance(route, dict):
                raise ConfigError(f"{item.get('name', '<unnamed>')} 的 cli_route 未登记")
            if str(route.get("driver", "")) != str(item.get("driver", "")):
                raise ConfigError(f"{item.get('name', '<unnamed>')} 的 CLI 驱动与路由不一致")

        for name, route in routes.items() if isinstance(routes, dict) else []:
            if not isinstance(route, dict):
                raise ConfigError(f"cli_routes.{name} 必须是对象")
            driver = str(route.get("driver", ""))
            if driver == "agy":
                if str(route.get("selection", "")) == "agy_cli_settings":
                    if set(route) - {"driver", "selection", "settings_path"}:
                        raise ConfigError("AGY 的 agy_cli_settings 路由仅允许 driver、selection、settings_path；不得配置上游、模型或其他字段")
                    _require_agy_settings_path(route.get("settings_path", ""))
                    continue
                if str(route.get("selection", "")) != "local_account":
                    raise ConfigError(f"cli_routes.{name}.selection 必须是 agy_cli_settings 或 local_account")
                # Legacy form from before v1.6.5. It now reads the agy CLI
                # settings too; its desktop fields are accepted but ignored. A
                # model or upstream may still never be configured here.
                forbidden = sorted(
                    key for key in ("credential", "base_url", "endpoint", "model_pool", "provider", "model", "provider_id")
                    if key in route
                )
                if forbidden:
                    raise ConfigError(f"cli_routes.{name} 不得配置上游、模型或凭据字段：" + ", ".join(forbidden))
                # The gateway reads settings_path from a legacy route too, so it
                # gets the same check; otherwise any file could pick the model.
                if "settings_path" in route:
                    _require_agy_settings_path(route["settings_path"])
                continue
            if driver == "dsh":
                if str(route.get("selection", "")) != "local_harness":
                    raise ConfigError(f"cli_routes.{name}.selection 必须是 local_harness")
                if set(route) - {"driver", "selection", "desktop_config"}:
                    raise ConfigError("DSH 路由仅允许 driver、selection、desktop_config；不得配置上游、模型或其他字段")
                if not route.get("desktop_config") or not Path(str(route["desktop_config"])).is_absolute():
                    raise ConfigError("DSH 必须绑定 desktop_config 的绝对路径")
                continue
            if driver == "qoder":
                if str(route.get("selection", "")) != "qodercli_settings":
                    raise ConfigError(f"cli_routes.{name}.selection 必须是 qodercli_settings")
                if set(route) - {"driver", "selection", "settings_path"}:
                    raise ConfigError("Qoder 路由仅允许 driver、selection、settings_path；不得配置上游、模型或其他字段")
                # Same rule as AGY: without settings_path the gateway reads the
                # user's own ~/.qoder/settings.json; when written, it must be
                # exactly that file (Qoder CN lives elsewhere and is refused).
                if "settings_path" in route and (
                    Path(str(route["settings_path"])).expanduser() != qoder_settings_default()
                ):
                    raise ConfigError("Qoder 必须绑定本用户的标准版 qodercli 设置文件（~/.qoder/settings.json）；不得使用 Qoder CN")
                continue
            raise ConfigError(f"cli_routes.{name}.driver 必须是 agy、dsh 或 qoder")

    def _validate_standby_policy(self) -> None:
        """Validate the single standby backend without touching legacy configs.

        A standby never counts as one of the two required primary backends:
        `minimum_successful_agents` is compared against enabled primary agents
        only.  A config with no `standby` key on any agent loads exactly as
        before; where the key is present it must be the boolean true or false.
        """
        agents = self.raw.get("agents", [])
        routes = self.cli_routes if isinstance(self.cli_routes, dict) else {}
        # `standby` must be a real boolean. A truthy 1/"yes" used to be read as a
        # standby by AgentConfig (bool()) while this check only counted `is True`,
        # so it slipped past every rule below (found in the v1.6.5 pre-review).
        for item in agents:
            if "standby" in item and not isinstance(item["standby"], bool):
                raise ConfigError(f"{item.get('name', '<unnamed>')} 的 standby 必须是布尔值 true 或 false")
        standby = [item for item in agents if item.get("standby") is True]
        if len(standby) > 1:
            raise ConfigError("备用后台最多只能有一个")
        for item in standby:
            name = item.get("name", "<unnamed>")
            if item.get("type") != "local_cli" or str(item.get("driver", "")) != "qoder":
                raise ConfigError(f"{name} 只能作为 qoder 备用后台，备用不支持其他驱动")
            route = routes.get(str(item.get("cli_route", "")))
            # Defensive: _validate_cli_routes already refuses an unregistered
            # route or a driver mismatch before this runs, so this line is not
            # normally reached; it stays so the rule holds on its own.
            if not isinstance(route, dict) or str(route.get("driver", "")) != "qoder":
                raise ConfigError(f"{name} 的 cli_route 必须指向 driver 为 qoder 的线路")
        # Too few enabled primaries is deliberately NOT a load error: a user
        # who disables a primary during an outage must still be able to run
        # every command.  The runtime gates (health.execution_blockers and the
        # session probe) refuse work when primaries plus a usable standby
        # cannot make two distinct reviewers.

    def _validate_skill_policy(self) -> None:
        skills = self.raw.get("skills")
        if not isinstance(skills, dict):
            raise ConfigError("缺少 skills 配置")
        root_value = str(skills.get("root_dir", "")).strip()
        index_value = str(skills.get("index_path", "")).strip()
        if not root_value or not index_value:
            raise ConfigError("skills.root_dir 与 skills.index_path 不能为空")
        root = Path(root_value).expanduser()
        index = Path(index_value).expanduser()
        if not root.is_absolute():
            root = Path(self.raw["project_dir"]).expanduser() / root
        if not index.is_absolute():
            index = Path(self.raw["project_dir"]).expanduser() / index
        try:
            index.resolve().relative_to(root.resolve())
        except ValueError as exc:
            raise ConfigError("skills.index_path 必须位于 skills.root_dir 内") from exc
        maximum = int(skills.get("max_selected", 0))
        if maximum < 1 or maximum > 5:
            raise ConfigError("skills.max_selected 必须在 1 到 5 之间")

    def _validate_knowledge_policy(self) -> None:
        knowledge = self.raw.get("knowledge")
        if not isinstance(knowledge, dict):
            raise ConfigError("缺少 knowledge 配置")
        if not str(knowledge.get("index_path", "")).strip():
            raise ConfigError("knowledge.index_path 不能为空")
        roots = knowledge.get("allowed_roots")
        if not isinstance(roots, list) or not roots:
            raise ConfigError("knowledge.allowed_roots 不能为空")
        for value in roots:
            if not Path(str(value)).expanduser().is_absolute():
                raise ConfigError("knowledge.allowed_roots 必须全部为绝对路径")

    def _validate_memory_policy(self) -> None:
        memory = self.raw.get("memory")
        if not isinstance(memory, dict):
            raise ConfigError("缺少 memory 配置")
        vault_value = str(memory.get("obsidian_vault", "")).strip()
        if not vault_value or not Path(vault_value).expanduser().is_absolute():
            raise ConfigError("memory.obsidian_vault 必须是绝对路径")
        for key, default in (("obsidian_subdir", "AgentOS"), ("catalog_note", "70_复利规则/经验索引.md")):
            value = Path(str(memory.get(key, default)).strip())
            if not str(value) or value.is_absolute() or ".." in value.parts:
                raise ConfigError(f"memory.{key} 必须是 Vault 内的安全相对路径")
        if Path(str(memory.get("catalog_note", "70_复利规则/经验索引.md"))).suffix.lower() != ".md":
            raise ConfigError("memory.catalog_note 必须是 Markdown 文件")
        if int(memory.get("retire_after_failed_reuses", 2)) <= 0:
            raise ConfigError("memory.retire_after_failed_reuses 必须大于 0")
        graph_value = str(memory.get("knowlp_graph_dir", "")).strip()
        if memory.get("knowlp_enabled") and (not graph_value or not Path(graph_value).expanduser().is_absolute()):
            raise ConfigError("memory.knowlp_graph_dir 必须是绝对路径")

    @property
    def runtime_dir(self) -> Path:
        return Path(self.raw["runtime_dir"]).expanduser().resolve()

    @property
    def release(self) -> Dict[str, Any]:
        return self.raw["release"]

    @property
    def release_index(self) -> Path:
        return Path(str(self.release["index_path"])).expanduser().resolve()

    @property
    def expected_release_version(self) -> str:
        return str(self.release["expected_version"])

    @property
    def project_dir(self) -> Path:
        return Path(self.raw["project_dir"]).expanduser().resolve()

    @property
    def database_path(self) -> Path:
        return self.runtime_dir / "agentos.sqlite3"

    @property
    def worker(self) -> Dict[str, Any]:
        return self.raw["worker"]

    @property
    def memory(self) -> Dict[str, Any]:
        return self.raw["memory"]

    @property
    def integrations(self) -> Dict[str, Any]:
        return self.raw["integrations"]

    @property
    def cli_routes(self) -> Dict[str, Any]:
        return self.raw.get("cli_routes", {})

    @property
    def evolution(self) -> Dict[str, Any]:
        return self.raw["evolution"]

    @property
    def recovery(self) -> Dict[str, Any]:
        return self.raw.get("recovery", {})

    @property
    def skills(self) -> Dict[str, Any]:
        return self.raw["skills"]

    @property
    def skills_root(self) -> Path:
        value = Path(str(self.skills["root_dir"])).expanduser()
        return value.resolve() if value.is_absolute() else (self.project_dir / value).resolve()

    @property
    def skills_index(self) -> Path:
        value = Path(str(self.skills["index_path"])).expanduser()
        return value.resolve() if value.is_absolute() else (self.project_dir / value).resolve()

    @property
    def knowledge(self) -> Dict[str, Any]:
        return self.raw["knowledge"]

    @property
    def knowledge_index(self) -> Path:
        value = Path(str(self.knowledge["index_path"])).expanduser()
        return value.resolve() if value.is_absolute() else (self.project_dir / value).resolve()

    @property
    def knowledge_allowed_roots(self) -> List[Path]:
        return [Path(str(value)).expanduser().resolve() for value in self.knowledge["allowed_roots"]]

    @property
    def agents(self) -> List[AgentConfig]:
        result = []
        for item in self.raw.get("agents", []):
            result.append(
                AgentConfig(
                    name=item["name"],
                    type=item["type"],
                    enabled=bool(item.get("enabled", True)),
                    role=item.get("role", ""),
                    timeout_seconds=int(item.get("timeout_seconds", 600)),
                    app_config=self,
                    raw=item,
                    standby=bool(item.get("standby", False)),
                )
            )
        return result

    @property
    def primary_agents(self) -> List[AgentConfig]:
        """Enabled backends that count toward the two required reviews."""
        return [agent for agent in self.agents if agent.enabled and not agent.standby]

    @property
    def standby_agents(self) -> List[AgentConfig]:
        """Enabled standby backends; they substitute, never count as a second source."""
        return [agent for agent in self.agents if agent.enabled and agent.standby]
