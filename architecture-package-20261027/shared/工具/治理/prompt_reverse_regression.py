#!/usr/bin/env python3
"""Technical regression checks for the local Zhipu prompt reverse service."""
from __future__ import annotations

import argparse
import base64
import json
import os
import socket
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(os.environ.get("XIAOBA_ASSISTANT_ROOT", Path.home() / "Documents/私人助理"))
SAMPLE = ROOT / "AI视频管线/03_素材/小男孩首尾帧_20260808/首帧_站立_躺椅旁.png"

def request(url: str, payload: bytes, headers: dict[str, str] | None = None, method: str = "POST"):
    req = urllib.request.Request(url, data=payload, headers=headers or {"Content-Type": "application/json"}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=75) as response:
            return response.status, response.read(4_000_000).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(4_000).decode("utf-8", "replace")

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8890")
    parser.add_argument("--sample", type=Path, default=SAMPLE)
    parser.add_argument("--output", type=Path, default=ROOT / ".runtime/work/smoke/prompt-reverse-regression.json")
    args = parser.parse_args()
    base = args.url.rstrip("/")
    checks = []
    status, body = request(base + "/healthz", b"", method="GET")
    checks.append({"name": "healthz", "expected": [200], "actual": status, "pass": status == 200})
    status, body = request(base + "/reverse", json.dumps({}).encode())
    checks.append({"name": "missing_image", "expected": [400], "actual": status, "pass": status == 400})
    status, body = request(base + "/reverse", b"not-json")
    checks.append({"name": "invalid_json", "expected": [400], "actual": status, "pass": status == 400})
    # Send a real over-limit body. Faking Content-Length without sending the
    # bytes makes the HTTP server wait for a body that will never arrive.
    # Only send the headers: the server must reject on Content-Length before
    # waiting for a body, which keeps this check fast and deterministic.
    port = int(base.rsplit(":", 1)[-1])
    with socket.create_connection(("127.0.0.1", port), timeout=10) as conn:
        conn.sendall((f"POST /reverse HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nContent-Type: application/octet-stream\r\nContent-Length: {18 * 1024 * 1024 + 1}\r\nConnection: close\r\n\r\n").encode())
        raw = conn.recv(128)
    try: status = int(raw.split(b" ", 2)[1])
    except (IndexError, ValueError): status = 0
    checks.append({"name": "oversized_request", "expected": [413], "actual": status, "pass": status == 413})
    if args.sample.is_file():
        image = base64.b64encode(args.sample.read_bytes()).decode()
        parsed = None
        status, body = 0, ""
        valid = False
        for attempt in range(1, 4):
            status, body = request(base + "/reverse", json.dumps({"image_b64": image, "filename": args.sample.name}).encode())
            try:
                parsed = json.loads(body)
            except json.JSONDecodeError:
                parsed = None
            required = {"scene", "scene_desc_en", "confidence", "palette", "photography"}
            valid = status == 200 and isinstance(parsed, dict) and parsed.get("ok") is True and required <= set((parsed.get("result") or {}))
            if valid or attempt == 3:
                break
            time.sleep(2 * attempt)
        required = {"scene", "scene_desc_en", "confidence", "palette", "photography"}
        check = {"name": "real_zhipu_sample", "expected": [200], "actual": status, "pass": valid, "fields_valid": valid, "attempts": attempt}
        if not valid:
            check["error"] = (body or "")[:500]
        checks.append(check)
    else:
        checks.append({"name": "real_zhipu_sample", "pass": False, "error": f"sample missing: {args.sample}"})
    payload = {"schema_version": 1, "checked_at": datetime.now(timezone.utc).isoformat(), "service": base, "checks": checks, "status": "PASS" if all(c.get("pass") for c in checks) else "FAIL"}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if payload["status"] == "PASS" else 1

if __name__ == "__main__":
    raise SystemExit(main())
