#!/usr/bin/env python3
"""Release tooling entry: submit frozen acceptance context explicitly.

Usage: submit_release_acceptance.py --config CONFIG PROMPT [submit options]
The task is only submitted when this script is explicitly run.
"""
import argparse
from pathlib import Path
import sys


def arguments(argv):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',required=True)
    args,rest=parser.parse_known_args(argv)
    if '--no-review' in rest:
        parser.error('release acceptance requires review')
    return ['--config',args.config,'submit',*rest,'--release-acceptance']


def main(argv=None):
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
    from xiaoba_agentos.cli import main as cli_main
    return cli_main(arguments(sys.argv[1:] if argv is None else argv))


if __name__ == '__main__':
    raise SystemExit(main())
