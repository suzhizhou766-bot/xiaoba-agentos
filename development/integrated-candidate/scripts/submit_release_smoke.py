#!/usr/bin/env python3
"""Submit one post-switch smoke task with a frozen audit-only purpose.

Usage: submit_release_smoke.py --config CONFIG PROMPT [submit options]
The task is only submitted when this script is explicitly run.
"""
import argparse
from pathlib import Path
import sys


def _force_value(parser, argv, option, value):
    result = []
    index = 0
    while index < len(argv):
        item = argv[index]
        if item == option:
            if index + 1 >= len(argv):
                parser.error(f"{option} requires a value")
            supplied = argv[index + 1]
            if supplied != value:
                parser.error(f"{option} must be {value}")
            index += 2
            continue
        if item.startswith(option + "="):
            if item.split("=", 1)[1] != value:
                parser.error(f"{option} must be {value}")
            index += 1
            continue
        result.append(item)
        index += 1
    return result


def arguments(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    args, rest = parser.parse_known_args(argv)
    if "--no-review" in rest:
        parser.error("release smoke requires review")
    if "--release-acceptance" in rest:
        parser.error("release smoke cannot be marked as release acceptance")
    rest = _force_value(parser, rest, "--memory-kind", "audit")
    rest = _force_value(parser, rest, "--task-purpose", "release_smoke")
    return ["--config", args.config, "submit", *rest,
            "--memory-kind", "audit", "--task-purpose", "release_smoke"]


def main(argv=None):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from xiaoba_agentos.cli import main as cli_main
    return cli_main(arguments(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":
    raise SystemExit(main())
