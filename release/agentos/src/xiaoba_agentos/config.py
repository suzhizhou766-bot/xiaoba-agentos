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
    timeout_seconds: int
    app_config: "AppConfig"
    raw: Dict[str, Any]

    @property
    def driver(self) -> str:
        return str(self.raw.get("driver", ""))


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
        for item in self.raw.get("agents", []):
            if item.get("type") == "mock":
                continue
            if item.get("type") != "local_cli":
                raise ConfigError(f"{item.get('name', '<unnamed>')} 使用了未受控 CLI 类型")
            driver = str(item.get("driver", ""))
            if driver not in {"agy", "dsh"}:
                raise ConfigError(f"{item.get('name', '<unnamed>')} 的 local_cli 驱动必须是 agy 或 dsh")
            if str(item.get("gateway_route", "")).strip():
                raise ConfigError("local_cli 不得配置 gateway_route")
            if str(item.get("model", "")).strip():
                raise ConfigError("local_cli 模型由 cli_route.model_pool 决定")
            if not str(item.get("cli_route", "")).strip():
                raise ConfigError(f"{item.get('name', '<unnamed>')} 缺少 cli_route")

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
                if str(route.get("selection", "")) != "local_account":
                    raise ConfigError(f"cli_routes.{name}.selection 必须是 local_account")
                forbidden = sorted(
                    key for key in ("credential", "base_url", "endpoint") if key in route
                )
                if forbidden:
                    raise ConfigError(
                        f"cli_routes.{name} 不得配置 CC Switch 字段：" + ", ".join(forbidden)
                    )
                if not route.get("desktop_debug_port") or not Path(str(route["desktop_debug_port"])).is_absolute():
                    raise ConfigError("AGY 必须绑定 desktop_debug_port 的绝对路径")
                if any(key in route for key in ("model_pool", "provider", "model", "provider_id")):
                    raise ConfigError("AGY 模型由 desktop 选择；AgentOS 不得配置模型或模型池")
                if "desktop_auto_start" in route and not isinstance(route["desktop_auto_start"], bool):
                    raise ConfigError(f"cli_routes.{name}.desktop_auto_start 必须是布尔值")
                if route.get("desktop_auto_start") is True:
                    app_path = Path(str(route.get("desktop_app_path", "")))
                    if not app_path.is_absolute() or app_path.suffix != ".app":
                        raise ConfigError(f"cli_routes.{name}.desktop_app_path 必须是 .app 的绝对路径")
                    wait = route.get("desktop_start_wait_seconds", 30)
                    if isinstance(wait, bool) or not isinstance(wait, (int, float)) or not 1 <= wait <= 120:
                        raise ConfigError(f"cli_routes.{name}.desktop_start_wait_seconds 必须在 1 到 120 之间")
                continue
            if driver == "dsh":
                if str(route.get("selection", "")) != "local_harness":
                    raise ConfigError(f"cli_routes.{name}.selection 必须是 local_harness")
                if set(route) - {"driver", "selection", "desktop_config"}:
                    raise ConfigError("DSH 路由仅允许 driver、selection、desktop_config；不得配置上游、模型或其他字段")
                if not route.get("desktop_config") or not Path(str(route["desktop_config"])).is_absolute():
                    raise ConfigError("DSH 必须绑定 desktop_config 的绝对路径")
                continue
            raise ConfigError(f"cli_routes.{name}.driver 必须是 agy 或 dsh")

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
                )
            )
        return result
