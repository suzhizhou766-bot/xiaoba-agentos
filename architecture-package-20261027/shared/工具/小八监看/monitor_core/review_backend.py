#!/usr/bin/env python3
"""裁决后端的启动器：把小八AgentOS 自带的裁决中心服务作为监看中心的内部后端运行。

它直接加载 AgentOS 项目里的 integrations/review-center/server.py，原样使用其中的
Center（全部审核逻辑）、Handler（接口与它自己的来源、csrf 校验）和 Server，不改写任何逻辑。
与那个文件自带的 main() 相比只有两处不同：
  1. 写进第二大脑待审队列的链接指向监看中心（--public-url），而不是后端自己的端口；
  2. 不再提供旧的独立网页（/ 与 /app.js 返回 404），界面只有监看中心一处。
监听端口由监看中心每次启动时临时分配，只在本机回环地址上。
"""
import argparse
import importlib.util
import json
import os
import signal
import threading
import time
from urllib.parse import urlparse


def load_server_module(script):
    spec = importlib.util.spec_from_file_location("xiaoba_review_center_server", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def headless_handler(module):
    class HeadlessHandler(module.Handler):
        def do_GET(self):
            if urlparse(self.path).path in ("/", "/app.js"):
                if not self.safe_host():
                    return self.respond(403, {"error": "仅允许本机同源访问"})
                return self.respond(404, {"error": "界面已并入小八监看中心"})
            return super().do_GET()

    return HeadlessHandler


def main():
    parser = argparse.ArgumentParser(description="小八监看中心 · 裁决后端")
    parser.add_argument("--server-script", required=True, help="AgentOS 的 integrations/review-center/server.py")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--public-url", required=True, help="监看中心地址，用于第二大脑里的链接")
    args = parser.parse_args()
    public = urlparse(args.public_url)
    if public.scheme != "http" or public.hostname != "127.0.0.1" or not public.port or public.path not in ("", "/"):
        parser.error("--public-url 必须是 http://127.0.0.1:<端口>")

    module = load_server_module(args.server_script)
    os.umask(0o077)  # 与原服务一致：它写出的文件只有本人可读
    center = module.Center(port=args.port, config_path=args.config)
    center.url = args.public_url.rstrip("/")
    center.verify()
    center.sync(force=True)
    server = module.Server(("127.0.0.1", args.port), headless_handler(module))
    server.center = center
    threading.Thread(target=center.watch, daemon=True).start()
    # 记下“现在是哪个进程、用哪份配置在跑裁决后端”，供 AgentOS 的 doctor 做配置一致性比对；退出时清掉
    state = center.runtime / "backend-state.json"
    state.write_text(json.dumps({"pid": os.getpid(), "port": args.port, "config_path": str(center.config_path),
                                 "public_url": center.url, "managed_by": "xiaoba-monitor", "started_at": time.time()},
                                ensure_ascii=False), encoding="utf-8")
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown, daemon=True).start())
    print("Review backend listening on 127.0.0.1:%d, links -> %s" % (args.port, center.url), flush=True)
    try:
        server.serve_forever()
    finally:
        try:
            if json.loads(state.read_text(encoding="utf-8")).get("pid") == os.getpid():
                state.unlink()
        except (OSError, ValueError):
            pass


if __name__ == "__main__":
    main()
