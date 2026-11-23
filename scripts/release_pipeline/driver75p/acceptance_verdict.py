"""Call the actual pinned build_release predicate; no acceptance evidence rewriting."""
from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path


def passed_verdict(text: str, agentos_root: Path | str) -> bool:
    root = Path(agentos_root).resolve()
    path = root / 'scripts' / 'build_release.py'
    if not path.is_file():
        raise FileNotFoundError(f'build_release.py missing at {path}')

    name = '_card63_build_verdict_' + hashlib.sha256(str(path).encode('utf-8')).hexdigest()[:16]
    if name not in sys.modules:
        sys.path[:0] = [str(root / 'src'), str(root / 'scripts')]
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise ImportError(f'Failed to load spec for {path}')
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)

    predicate = getattr(sys.modules[name], '_passed_verdict', None)
    if predicate is None:
        raise AttributeError(f'_passed_verdict not found in {path}')
    return bool(predicate(str(text or '')))
