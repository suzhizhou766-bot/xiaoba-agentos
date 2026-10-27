#!/usr/bin/env python3
"""Build a local experiment; do not copy or modify Jianying libraries."""
import hashlib
import json
import os
from pathlib import Path
import plistlib
import subprocess

ROOT = Path(__file__).resolve().parent
APP = Path('/Applications/VideoFusion-macOS.app')
FRAMEWORKS = APP / 'Contents/Frameworks'


def main():
    os.umask(0o077)
    for name in ('bin', 'logs', 'tmp', 'results'):
        (ROOT / name).mkdir(exist_ok=True)
    info = plistlib.loads((APP / 'Contents/Info.plist').read_bytes())
    if info.get('CFBundleIdentifier') != 'com.lemon.lvpro':
        raise SystemExit('Unexpected application identity')
    output = ROOT / 'bin/draft_codec'
    if output.exists():
        raise SystemExit('Build output already exists; choose a new artifact name')
    command = [
        '/usr/bin/xcrun', 'clang++', '-std=c++17', '-arch', 'arm64', '-O2',
        str(ROOT / 'src/draft_codec.cpp'), '-L' + str(FRAMEWORKS),
        '-lvideoeditor', '-Wl,-rpath,' + str(FRAMEWORKS), '-o', str(output),
    ]
    env = dict(os.environ, TMPDIR=str(ROOT / 'tmp'),
               CLANG_MODULE_CACHE_PATH=str(ROOT / 'tmp/module-cache'))
    result = subprocess.run(command, capture_output=True, text=True,
                            env=env, timeout=180)
    (ROOT / 'logs/build.log').write_text(result.stdout + result.stderr)
    library = FRAMEWORKS / 'libvideoeditor.dylib'
    report = {
        'app_version': info.get('CFBundleShortVersionString'),
        'app_build': info.get('CFBundleVersion'),
        'library': str(library),
        'library_sha256': hashlib.file_digest(library.open('rb'), 'sha256').hexdigest(),
        'command': command, 'returncode': result.returncode,
        'library_copied': False, 'app_modified': False,
    }
    (ROOT / 'results/build.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({'returncode': result.returncode, 'app_version': report['app_version'],
                      'binary_exists': output.is_file()}, ensure_ascii=False))
    raise SystemExit(result.returncode)


if __name__ == '__main__':
    main()
