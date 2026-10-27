#!/usr/bin/env python3
"""Exercise the installed CLI and stdio MCP against owned, non-executed fixtures."""
import hashlib
import json
import queue
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "evidence/verification"
REA = str(ROOT / "bin/rea")


def digest_tree(path):
    return {str(p.relative_to(path)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in path.rglob("*") if p.is_file()}


def cli(name, args, expect=0):
    r = subprocess.run([REA, *args], capture_output=True, text=True, timeout=180)
    (OUT / (name + ".json")).write_text(r.stdout)
    (OUT / (name + ".stderr.txt")).write_text(r.stderr)
    assert r.returncode == expect, (name, r.returncode, r.stderr[:500])
    return json.loads(r.stdout)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    before = digest_tree(ROOT / "tests/fixtures")
    version = subprocess.check_output([REA, "--version"], text=True).strip()
    assert version == "6.5.0", version
    mcp_doctor = cli("mcp-doctor", ["mcp", "doctor", "--json"])
    assert mcp_doctor["healthy"]
    js = cli("javascript", ["analyze-javascript-application", str(ROOT / "tests/fixtures/javascript"),
                            "--max-heap-mb", "256", "--analysis-timeout-ms", "60000", "--json"])
    stats = js["normalized_result"]["statistics"]
    assert stats["parsed_javascript_files"] == 2 and stats["parse_failures"] == 0, stats
    macho = cli("macho", ["inspect-macho", str(ROOT / "tests/fixtures/REA-Smoke.app"), "--json"])
    assert macho["normalized_result"]["format"] == "mach-o"
    artifact = cli("macos-artifact", ["inspect-artifact", str(ROOT / "tests/fixtures/REA-Smoke.app"), "--json"])
    assert artifact["normalized_result"]["substeps"][0]["status"] == "completed"
    missing = cli("missing-target", ["inspect-artifact", str(ROOT / "tests/fixtures/ABSENT"), "--json"], 1)
    assert missing.get("code") and "error" in missing

    messages = queue.Queue()
    with (OUT / "mcp.stderr.txt").open("w") as stderr:
        proc = subprocess.Popen([REA, "mcp"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=stderr, text=True, bufsize=1)

        def reader():
            for line in proc.stdout:
                messages.put(json.loads(line))

        threading.Thread(target=reader, daemon=True).start()

        def send(msg):
            proc.stdin.write(json.dumps(msg) + "\n")
            proc.stdin.flush()

        def rpc(ident, method, params):
            send({"jsonrpc": "2.0", "id": ident, "method": method, "params": params})
            while True:
                reply = messages.get(timeout=180)
                if reply.get("id") == ident:
                    assert "error" not in reply, reply
                    return reply["result"]

        try:
            init = rpc(1, "initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                        "clientInfo": {"name": "xiaoba-rea-local-smoke", "version": "1.0.0"}})
            send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            inventory = rpc(2, "tools/list", {})
            tools = inventory["tools"]
            tool = next(t for t in tools if t["name"] == "analyze_javascript_application")
            (OUT / "mcp-javascript-input-schema.json").write_text(json.dumps(tool["inputSchema"], indent=2) + "\n")
            result = rpc(3, "tools/call", {"name": tool["name"], "arguments": {
                "input_path": str(ROOT / "tests/fixtures/javascript"), "max_heap_mb": 256,
                "analysis_timeout_ms": 60000}})
            (OUT / "mcp-javascript.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
            assert not result.get("isError"), str(result)[:500]
            assert "JavaScriptApplicationGraph" in json.dumps(result)
        finally:
            proc.stdin.close()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.terminate()
                proc.wait(timeout=10)
    assert digest_tree(ROOT / "tests/fixtures") == before, "Analysis changed a fixture"
    summary = {"checked_at": datetime.now(timezone.utc).isoformat(), "status": "PASS",
               "version": version, "cli_javascript": stats, "macos_artifact": "PASS",
               "macho": "PASS", "mcp_server": init["serverInfo"], "mcp_tool_count": len(tools),
               "mcp_actual_analysis": "PASS", "missing_target_rejected": True,
               "fixtures_unchanged": True, "target_executed": False,
               "native_decompilation": "NOT_TESTED_MISSING_PROVIDER",
               "host_agent_mcp_registration": "NOT_CHANGED"}
    (OUT / "smoke-summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
