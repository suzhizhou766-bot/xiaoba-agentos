"""Isolated acceptance-site construction shared by the 75S nodes/review stages.

Builds a schema_version=2 AppConfig rooted entirely inside an evidence directory,
then opens the REAL EventStore + ReviewStore. No production runtime path is used
and no authentication material is read. The candidate config template carries no
secrets; only paths are remapped into the isolated root.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def build_isolated_config(candidate: Path, isolated_root: Path, version: str) -> Path:
    """Remap the candidate config template into isolated_root and return its path."""
    candidate = Path(candidate)
    isolated_root = Path(isolated_root)
    for d in ('runtime', 'vault', 'graph', 'agent-workspaces', 'outbox', 'project', 'releases'):
        (isolated_root / d).mkdir(parents=True, exist_ok=True)

    raw = json.loads((candidate / 'config/agentos.json').read_text(encoding='utf-8'))
    raw['project_dir'] = str(isolated_root / 'project')
    raw['runtime_dir'] = str(isolated_root / 'runtime')

    index_file = isolated_root / 'releases' / 'index.json'
    index_file.write_text(
        json.dumps({'format_version': 2, 'current_release': version, 'releases': []},
                   ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    raw['release'].update(index_path=str(index_file), acceptance_mode=True, expected_version=version)

    for key in ('memory', 'paths'):
        if key in raw:
            raw[key].update(
                obsidian_vault=str(isolated_root / 'vault'),
                knowlp_vault=str(isolated_root / 'vault'),
                knowlp_graph_dir=str(isolated_root / 'graph'),
            )
    raw['memory'].update(publish_enabled=False, knowlp_enabled=False)
    raw['skills'].update(
        root_dir=str(candidate / 'tests/fixtures/content/skills'),
        index_path=str(candidate / 'tests/fixtures/content/skills/index.json'),
    )
    raw['knowledge']['index_path'] = str(candidate / 'tests/fixtures/content/knowledge/index.json')

    cfg_file = isolated_root / 'acceptance-config.json'
    cfg_file.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    cfg_file.chmod(0o600)
    return cfg_file


def open_isolated_stores(cfg_path: Path, *, workspace_root: Path, outbox_root: Path):
    """Load the isolated AppConfig and open real EventStore + ReviewStore.

    Product modules must already be importable (see nodes.load_product). Returns
    (cfg, native_event_store, review_store). ReviewStore binds the workspace and
    creates the ri_* tables on the real native database.
    """
    from xiaoba_agentos.config import AppConfig
    from xiaoba_agentos.store import EventStore
    from xiaoba_review.store import ReviewStore

    cfg = AppConfig.load(Path(cfg_path))
    native = EventStore(cfg.database_path)
    native.initialize()
    review = ReviewStore(native, Path(outbox_root), Path(workspace_root))
    return cfg, native, review