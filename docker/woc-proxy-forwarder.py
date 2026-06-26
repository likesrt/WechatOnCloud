#!/usr/bin/env python3
"""本地 HTTP 代理 → 上游 HTTP 代理，注入认证。

Chromium 设 HTTP_PROXY=http://127.0.0.1:8080（无认证，不弹框），
本地代理对每个请求注入 Proxy-Authorization 后转发上游。
"""

import os
import select
import socket
import socketserver
from base64 import b64decode

HOST = os.environ["WOC_PROXY_HOST"]
PORT = int(os.environ["WOC_PROXY_PORT"])
AUTH_B64 = os.environ.get("WOC_PROXY_AUTH", "")
LISTEN = int(os.environ.get("WOC_PROXY_LISTEN", "8080"))

AUTH_HDR = ""
if AUTH_B64:
    try:
        b64decode(AUTH_B64)
        AUTH_HDR = f"Proxy-Authorization: Basic {AUTH_B64}\r\n"
    except Exception:
        pass


def _relay(a: socket.socket, b: socket.socket) -> None:
    socks = [a, b]
    while True:
        try:
            r, _, _ = select.select(socks, [], [], 30)
        except (select.error, ValueError):
            break
        if not r:
            break
        for s in r:
            try:
                chunk = s.recv(65536)
            except OSError:
                return
            if not chunk:
                return
            other = b if s is a else a
            try:
                other.sendall(chunk)
            except OSError:
                return


def _read_req(sock: socket.socket) -> bytes | None:
    """读到 \r\n\r\n，有 Content-Length 续读 body。"""
    data = b""
    while b"\r\n\r\n" not in data:
        c = sock.recv(4096)
        if not c:
            return None
        data += c
    end = data.find(b"\r\n\r\n") + 4
    for line in data[:end].split(b"\r\n"):
        if line.lower().startswith(b"content-length:"):
            try:
                cl = int(line.split(b":", 1)[1].strip())
            except (ValueError, IndexError):
                cl = 0
            body = data[end:]
            while len(body) < cl:
                c = sock.recv(min(4096, cl - len(body)))
                if not c:
                    break
                body += c
            return data[:end] + body
    return data


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            data = _read_req(self.request)
            if not data:
                return
        except OSError:
            return

        # 注入认证头
        if AUTH_HDR:
            idx = data.find(b"\r\n")
            if idx >= 0:
                data = data[: idx + 2] + AUTH_HDR.encode() + data[idx + 2 :]

        is_connect = data.startswith(b"CONNECT ")

        try:
            up = socket.create_connection((HOST, PORT), 10)
        except OSError:
            try:
                self.request.close()
            except OSError:
                pass
            return

        try:
            up.sendall(data)
            if is_connect:
                resp = b""
                while b"\r\n\r\n" not in resp:
                    c = up.recv(4096)
                    if not c:
                        return
                    resp += c
                self.request.sendall(resp)
            _relay(self.request, up)
        except OSError:
            pass
        finally:
            try:
                up.close()
            except OSError:
                pass
            try:
                self.request.close()
            except OSError:
                pass


if __name__ == "__main__":
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", LISTEN), Handler)
    srv.allow_reuse_address = True
    print(
        f"[woc-proxy] HTTP 代理已启动 127.0.0.1:{LISTEN} → {HOST}:{PORT}"
        + ("（认证）" if AUTH_HDR else ""),
        flush=True,
    )
    srv.serve_forever()
