#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数字层的开发用静态服务器，零依赖。

    python twin/tools/serve.py           # http://127.0.0.1:8000/
    python twin/tools/serve.py --port 8100

只绑 127.0.0.1：不弹 Windows 防火墙授权框，也不对外暴露。
将来 Python 后端起来之后，把 web/ 交给后端的静态文件中间件即可，这个脚本降级为纯开发工具。
"""

from __future__ import annotations

import argparse
import http.server
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.normpath(os.path.join(HERE, "..", "web"))


class Handler(http.server.SimpleHTTPRequestHandler):
    # 默认是 HTTP/1.0，每个响应完就关连接。浏览器并发拉 13 个 STL 时会不停重建连接，
    # 而且已关掉的 socket 上排队的那几个会被记成 net::ERR_ABORTED，白添噪声。
    protocol_version = "HTTP/1.1"

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=WEB, **kw)

    def end_headers(self):
        # 开发期不缓存：改完刷新即见，免得对着旧模块调试
        self.send_header("Cache-Control", "no-store, max-age=0")
        super().end_headers()

    def log_message(self, fmt, *a):
        # 静音成功日志，只留错误
        code = a[1] if len(a) > 1 else ""
        if not str(code).startswith("2"):
            super().log_message(fmt, *a)


# 必须显式覆盖：<script type="module"> 受严格 MIME 检查，.js 一旦不是 JS MIME
# 浏览器直接拒绝执行、整页白屏。不同机器的注册表可能把 .js 映射成 text/plain。
Handler.extensions_map = {
    **http.server.SimpleHTTPRequestHandler.extensions_map,
    ".js": "text/javascript",
    ".mjs": "text/javascript",
    ".stl": "model/stl",
    ".urdf": "application/xml",
    ".json": "application/json",
    ".wasm": "application/wasm",
    ".css": "text/css",
    ".html": "text/html",
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()

    if not os.path.isdir(WEB):
        print(f"找不到 {WEB}", file=sys.stderr)
        return 1

    http.server.ThreadingHTTPServer.allow_reuse_address = True
    with http.server.ThreadingHTTPServer((args.host, args.port), Handler) as srv:
        print(f"数字层：http://{args.host}:{args.port}/")
        print(f"静态根：{WEB}")
        print("Ctrl+C 结束")
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            print("\n已停止")
    return 0


if __name__ == "__main__":
    sys.exit(main())
