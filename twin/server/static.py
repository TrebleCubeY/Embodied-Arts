"""静态文件服务。tools/serve.py 与 server/main.py 共用这一份。

必须显式覆盖 MIME：`<script type="module">` 受严格 MIME 检查，
.js 一旦不是 JS MIME，浏览器直接拒绝执行、整页白屏，
而不同机器的注册表可能把 .js 映射成 text/plain。
"""

import http.server
from functools import partial


class TwinStaticHandler(http.server.SimpleHTTPRequestHandler):
    # 默认是 HTTP/1.0，每个响应完就关连接。浏览器要并发拉 13 个 STL，
    # 反复重建连接之外，已关掉的 socket 上排队的那几个还会被记成 net::ERR_ABORTED，白添噪声。
    protocol_version = "HTTP/1.1"

    def end_headers(self):
        # 开发期不缓存：改完刷新即见，免得对着旧模块调试
        self.send_header("Cache-Control", "no-store, max-age=0")
        super().end_headers()

    def log_message(self, fmt, *a):
        # 静音成功日志，只留错误
        code = a[1] if len(a) > 1 else ""
        if not str(code).startswith("2"):
            super().log_message(fmt, *a)


TwinStaticHandler.extensions_map = {
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


class TwinStaticServer(http.server.ThreadingHTTPServer):
    """浏览器关标签、刷新页面时会提前断开连接，那是常事，
    别让 socketserver 把 traceback 打到日志里，把真正的错误淹掉。"""

    def handle_error(self, request, client_address):
        import sys

        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            return
        super().handle_error(request, client_address)


def make_server(host: str, port: int, root) -> TwinStaticServer:
    TwinStaticServer.allow_reuse_address = True
    return TwinStaticServer(
        (host, port), partial(TwinStaticHandler, directory=str(root))
    )
