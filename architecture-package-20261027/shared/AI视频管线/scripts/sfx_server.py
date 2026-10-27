#!/usr/bin/env python3
"""SFX 匹配引擎常驻服务 - v3 指纹库，默认端口 9878，带使用日志。"""
import argparse
import json
import pathlib
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from sfx_match import LIBRARY, load, query
from sfx_usage_log import log_feedback, log_match

DB_ROWS = len(load())
MAX_REQUEST_BYTES = 64 * 1024


class Handler(BaseHTTPRequestHandler):
    def _cors(self):
        origin = self.headers.get("Origin", "")
        if origin == "null" or origin.startswith("http://127.0.0.1:") or origin.startswith("http://localhost:"):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _send(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        url = urlparse(self.path)
        if url.path in ("/readyz", "/health"):
            self._send(200, {
                "service": "sfx-match-v3",
                "ready": True,
                "db_rows": DB_ROWS,
            })
            return
        if url.path == "/match":
            q = parse_qs(url.query)
            profile = q.get("profile", ["whoosh"])[0]
            dur_raw = q.get("dur", [""])[0]
            try:
                dur = float(dur_raw) if dur_raw else None
            except ValueError:
                dur = None
            try:
                top = max(1, min(int(q.get("top", ["15"])[0]), 100))
            except ValueError:
                top = 15
            project = q.get("project", [None])[0]
            matches = query(profile, dur, top, project=project)
            result = [{
                "score": s,
                "base_score": r.get("base_score"),
                "preference_bonus": r.get("preference_bonus"),
                "preference_tier": r.get("preference_tier"),
                "usage_count": r.get("usage_count"),
                "average_rating": r.get("average_rating"),
                "reject_count": r.get("reject_count"),
                "same_project": r.get("same_project"),
                "name": r.get("name"),
                "path": r.get("path"),
                "category": r.get("category"),
                "duration": float(r.get("duration", 0)),
                "keywords": r.get("keywords", ""),
            } for s, r in matches]
            request_id = log_match(profile, dur, top, result, project=project)
            self._send(200, {
                "profile": profile,
                "dur": dur,
                "project": project,
                "request_id": request_id,
                "matches": result,
            })
            return
        self._send(404, {"error": "not found"})

    def do_POST(self):
        url = urlparse(self.path)
        if url.path != "/feedback":
            self._send(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length <= 0 or length > MAX_REQUEST_BYTES:
                self._send(413, {"error": "请求体超过 64KB 限制"})
                return
            payload = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            self._send(400, {"error": "invalid json"})
            return
        rating = payload.get("rating")
        try:
            rating = float(rating)
        except (TypeError, ValueError):
            self._send(400, {"error": "rating must be between 1 and 5"})
            return
        if not 1 <= rating <= 5:
            self._send(400, {"error": "rating must be between 1 and 5"})
            return
        selected_path = payload.get("selected_path")
        if not selected_path:
            self._send(400, {"error": "selected_path is required"})
            return
        selected = pathlib.Path(str(selected_path)).expanduser().resolve()
        try:
            selected.relative_to(LIBRARY)
        except ValueError:
            self._send(400, {"error": "selected_path must be inside the SFX library"})
            return
        if not selected.is_file():
            self._send(400, {"error": "selected_path does not exist"})
            return
        log_feedback(
            request_id=payload.get("request_id"),
            selected_path=str(selected),
            rating=rating,
            project=payload.get("project"),
            note=payload.get("note"),
        )
        self._send(200, {"ok": True})


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9878)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.serve_forever()
