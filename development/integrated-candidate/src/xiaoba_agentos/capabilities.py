from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

DEFAULT_CAPABILITY_PATH = Path(__file__).resolve().parents[2] / "CAPABILITY.json"

CAPABILITY_TOKENS = (
    "reasoning",
    "coding",
    "shell",
    "filesystem",
    "vision",
    "native_web_search",
    "tool_calling",
    "long_context",
    "shell_read",
    "shell_write_workspace",
    "structured_editor_write",
    "filesystem_read",
    "filesystem_write_workspace",
    "network_access",

)


class CapabilityError(RuntimeError):
    """Raised when CAPABILITY.json cannot be loaded or is malformed."""


class CapabilityRegistry:
    """Loads CAPABILITY.json and resolves abstract capabilities for a provider/model.

    Capabilities are abstract tokens (reasoning, coding, shell, filesystem, vision,
    native_web_search, tool_calling, long_context). An undeclared provider/model
    resolves to the declared defaults (normally "unknown"). "unknown" is never
    treated as true by the router gate.
    """

    def __init__(self, path: Path = DEFAULT_CAPABILITY_PATH):
        self.path = Path(path)
        self._data = self._load()

    def _load(self) -> Dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise CapabilityError(f"CAPABILITY.json not found: {self.path}") from exc
        except json.JSONDecodeError as exc:
            raise CapabilityError(f"CAPABILITY.json is not valid JSON: {exc}") from exc
        if not isinstance(raw, dict) or not isinstance(raw.get("providers"), dict):
            raise CapabilityError("CAPABILITY.json must contain a providers object")
        if any(not isinstance(models, dict) for models in raw["providers"].values()):
            raise CapabilityError("provider entries must be model objects")
        declared_tokens = raw.get("capability_tokens", list(CAPABILITY_TOKENS))
        if not isinstance(declared_tokens, list) or any(not isinstance(t, str) for t in declared_tokens):
            raise CapabilityError("capability_tokens must be a list of strings")
        for values in [raw.get("defaults", {})] + [caps for models in raw["providers"].values() for caps in models.values()]:
            if not isinstance(values, dict) or any(v is not True and v is not False and v != "unknown" for v in values.values()):
                raise CapabilityError("capabilities must be true, false or unknown")
        tokens = tuple(raw.get("capability_tokens") or CAPABILITY_TOKENS)
        defaults = raw.get("defaults") or {}
        providers = raw.get("providers") or {}
        return {"tokens": tokens, "defaults": defaults, "providers": providers}

    def tokens(self) -> Tuple[str, ...]:
        return self._data["tokens"]

    def defaults(self) -> Dict[str, Any]:
        return self._data["defaults"]

    def providers(self) -> Dict[str, Dict[str, Dict[str, Any]]]:
        return self._data["providers"]

    def capabilities_for(self, provider: str, model: str) -> Dict[str, Any]:
        models = self._data["providers"].get(provider, {})
        entry = models.get(model)
        if entry is None:
            # Match a backend's display name to the same declared identity.
            # This is a capability lookup, never a model selection/fallback.
            normalize = lambda value: re.sub(r"[^a-z0-9]", "", value.lower())
            matches = [caps for name, caps in models.items() if normalize(name) == normalize(model)]
            if len(matches) == 1:
                entry = matches[0]
        defaults = self._data["defaults"]
        if entry is None:
            return {t: defaults.get(t, "unknown") for t in self._data["tokens"]}
        return {t: entry.get(t, defaults.get(t, "unknown")) for t in self._data["tokens"]}

    def has_capability(self, provider: str, model: str, token: str) -> bool:
        return self.capabilities_for(provider, model).get(token) is True

    def has_native_web_search(self, provider: str, model: str) -> bool:
        return self.has_capability(provider, model, "native_web_search")


def agent_provider_model(agent_config) -> Tuple[str, str]:
    raw = agent_config.raw
    if agent_config.type == "local_cli":
        from .gateway import local_cli_route
        selection = local_cli_route(agent_config.app_config, str(raw.get("cli_route", "")), agent_config.name)
        return selection["provider"], selection["model"]
    return str(raw.get("provider", "")), str(raw.get("model", ""))


def _agent_options(agent):
    return [agent_provider_model(agent)]


def web_capable_agents(
    agents: Sequence[Any], registry: CapabilityRegistry, needs_web: bool
) -> Tuple[List[Any], str]:
    if not needs_web:
        return list(agents), ""
    capable: List[Any] = []
    for agent in agents:
        if any(registry.has_native_web_search(provider, model) for provider, model in _agent_options(agent)):
            capable.append(agent)
    if not capable:
        return capable, "no enabled agent supports native_web_search for this task"
    return capable, ""


def capable_agents(
    agents: Sequence[Any],
    registry: CapabilityRegistry,
    required_tokens: Sequence[str],
) -> Tuple[List[Any], List[Tuple[str, List[str]]]]:
    ok: List[Any] = []
    missing: List[Tuple[str, List[str]]] = []
    for agent in agents:
        options = [registry.capabilities_for(provider, model) for provider, model in _agent_options(agent)]
        lacked = [] if any(all(caps.get(t) is True for t in required_tokens) for caps in options) else list(required_tokens)
        if lacked:
            missing.append((agent.name, lacked))
        else:
            ok.append(agent)
    return ok, missing
