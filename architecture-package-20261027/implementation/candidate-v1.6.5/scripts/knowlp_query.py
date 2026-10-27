#!/usr/bin/env python3
"""Read-only bridge into the existing KnowLP module; never records feedback."""

import argparse
import importlib.util
import json
import os
import sys
import warnings
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--module", required=True)
    parser.add_argument("--vault", required=True)
    parser.add_argument("--graph-dir", required=True)
    parser.add_argument("--query", required=True)
    parser.add_argument("--limit", type=int, default=6)
    args = parser.parse_args()
    os.environ["KNOWLP_VAULT"] = args.vault
    os.environ["KNOWLP_GRAPH_DIR"] = args.graph_dir
    os.environ.setdefault("KNOWLP_FORCE_NGRAM", "1")
    warnings.filterwarnings(
        "ignore",
        message=r"Field 'lifespan' has an incomplete definition:.*unresolved forward reference.*",
    )
    module_path = Path(args.module).resolve()
    sys.path.insert(0, str(module_path.parent))
    spec = importlib.util.spec_from_file_location("xiaoba_knowlp_runtime", str(module_path))
    if not spec or not spec.loader:
        raise RuntimeError("无法加载 KnowLP module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = module.knowlp_search(args.query, limit=args.limit, engines=["knowlp", "ripgrep"])
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
