#!/usr/bin/env python3
"""本地 SOCKS5 → 上游 HTTP 代理转发器。

Chromium 设 ALL_PROXY=socks5://127.0.0.1:1080 走本地，
本地完成 SOCKS5 握手后，向上游 HTTP 代理发 CONNECT 建隧道。
"""

import os
import select
import socket
import struct
import socketserver
from base64 import b64decode

HOST = os.environ["WOC_PROXY_HOST"]
PORT = int(os.environ["WOC_PROXY_PORT"])
AUTH_B64 = os.environ.get("WOC_PROXY_AUTH", "")
LISTEN = int(os.environ.get("WOC_PROXY_LISTEN", "1080"))

# HTTP 上游认证头
PROXY_AUTH = ""
if AUTH_B64:
    try:
        raw = b64decode(AUTH_B64).decode()
        PROXY_AUTH = f"Proxy-Authorization: Basic {AUTH_B64}\r\n"
    except Exception:
        pass

SOCKS_VER = 0x05
SOCKS_ATYP_IPV4 = 0x01
SOCKS_ATYP_DOMAIN = 0x03


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        c = sock.recv(n - len(buf))
        if not c:
            raise OSError("连接关闭")
        buf += c
    return buf


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


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            self._do_socks5()
        except OSError:
            pass

    def _do_socks5(self):
        c = self.request

        # 1. 方法协商（无认证）
        ver, nm = struct.unpack("!BB", _recv_exact(c, 2))
        if ver != SOCKS_VER:
            c.sendall(b"\x05\xFF")
            return
        _recv_exact(c, nm)
        c.sendall(b"\x05\x00")  # 无认证

        # 2. CONNECT 请求
        ver, cmd, _, atyp = struct.unpack("!BBBB", _recv_exact(c, 4))
        if cmd != 1:  # 只支持 CONNECT
            c.sendall(struct.pack("!BBBB", SOCKS_VER, 7, 0, SOCKS_ATYP_IPV4)
                      + socket.inet_aton("0.0.0.0") + struct.pack("!H", 0))
            return

        if atyp == SOCKS_ATYP_IPV4:
            host = socket.inet_ntoa(_recv_exact(c, 4))
        elif atyp == SOCKS_ATYP_DOMAIN:
            host = _recv_exact(c, _recv_exact(c, 1)[0]).decode()
        else:
            host = socket.inet_ntop(socket.AF_INET6, _recv_exact(c, 16))
        port = struct.unpack("!H", _recv_exact(c, 2))[0]

        # 3. HTTP CONNECT 上游
        up = socket.create_connection((HOST, PORT), 10)
        req = f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n{PROXY_AUTH}\r\n"
        up.sendall(req.encode())

        # 读上游响应
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = up.recv(4096)
            if not chunk:
                up.close()
                c.sendall(struct.pack("!BBBB", SOCKS_VER, 1, 0, SOCKS_ATYP_IPV4)
                          + socket.inet_aton("0.0.0.0") + struct.pack("!H", 0))
                return
            resp += chunk

        code = resp.split(b" ")[1] if len(resp.split(b" ")) > 1 else b""
        if code != b"200":
            up.close()
            c.sendall(struct.pack("!BBBB", SOCKS_VER, 5, 0, SOCKS_ATYP_IPV4)
                      + socket.inet_aton("0.0.0.0") + struct.pack("!H", 0))
            return

        # 4. 告诉客户端隧道已建立
        c.sendall(struct.pack("!BBBB", SOCKS_VER, 0, 0, SOCKS_ATYP_IPV4)
                  + socket.inet_aton("0.0.0.0") + struct.pack("!H", 0))

        # 5. 双向中继
        _relay(c, up)
        try:
            up.close()
        except OSError:
            pass


if __name__ == "__main__":
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", LISTEN), Handler)
    srv.allow_reuse_address = True
    print(
        f"[woc-proxy] SOCKS5 → HTTP 代理已启动 "
        f"127.0.0.1:{LISTEN} → {HOST}:{PORT}"
        + ("（认证）" if PROXY_AUTH else ""),
        flush=True,
    )
    srv.serve_forever()
