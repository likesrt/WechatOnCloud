#!/usr/bin/env python3
"""本地代理转发器 — HTTP/SOCKS5 上游，注入认证。

Chromium 连本地 127.0.0.1:18080 作为 HTTP 代理（无认证），
转发器根据 WOC_PROXY_PROTO 选择上游协议并完成认证/握手。

环境变量:
  WOC_PROXY_PROTO     http 或 socks5
  WOC_PROXY_HOST      上游地址
  WOC_PROXY_PORT      上游端口
  WOC_PROXY_AUTH      base64(user:pass)，无则上游无需认证
  WOC_PROXY_LISTEN    本地监听端口（默认 18080）
"""

import os
import select
import socket
import struct
import socketserver
import sys
from base64 import b64decode

# ---- 配置 ----
PROTO = os.environ["WOC_PROXY_PROTO"]  # "http" | "socks5"
HOST = os.environ["WOC_PROXY_HOST"]
PORT = int(os.environ["WOC_PROXY_PORT"])
AUTH_B64 = os.environ.get("WOC_PROXY_AUTH", "")
LISTEN = int(os.environ.get("WOC_PROXY_LISTEN", "18080"))

# 解析凭据
AUTH_USER, AUTH_PASS = "", ""
if AUTH_B64:
    try:
        raw = b64decode(AUTH_B64).decode("utf-8", errors="replace")
        AUTH_USER, _, AUTH_PASS = raw.partition(":")
    except Exception:
        pass

# HTTP 上游认证头
HTTP_AUTH_HEADER = (
    f"Proxy-Authorization: Basic {AUTH_B64}\r\n".encode() if AUTH_B64 else b""
)


# ---- 工具 ----
def _connect_upstream() -> socket.socket:
    """连接上游代理。"""
    return socket.create_connection((HOST, PORT), 10)


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    """读满 n 字节。"""
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise OSError("连接关闭")
        buf += chunk
    return buf


def _read_http_req(sock: socket.socket) -> bytes:
    """读到 \r\n\r\n，有 Content-Length 则继续读 body。"""
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
    end = data.find(b"\r\n\r\n")
    if end < 0:
        return data
    end += 4
    for line in data[:end].split(b"\r\n"):
        if line.lower().startswith(b"content-length:"):
            try:
                cl = int(line.split(b":", 1)[1].strip())
            except (ValueError, IndexError):
                cl = 0
            body = data[end:]
            while len(body) < cl:
                chunk = sock.recv(min(4096, cl - len(body)))
                if not chunk:
                    break
                body += chunk
            return data[:end] + body
    return data


def _relay(a: socket.socket, b: socket.socket) -> None:
    """双向中继，一端关闭即结束。"""
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


def _parse_host_port(req_line: bytes) -> tuple[str, int]:
    """从 CONNECT host:port 或 GET http://host:port/... 解析目标。"""
    line = req_line.decode("ascii", errors="replace")
    parts = line.split()
    if len(parts) < 2:
        raise OSError("请求格式错误")
    target = parts[1]
    # 去掉 http:// 前缀（GET http://example.com/...）
    if target.startswith("http://"):
        target = target.split("/", 3)[2]  # example.com:80
    host, _, port_str = target.rpartition(":")
    if not host:
        host = port_str  # 没有端口，整个是 host
        port_str = "80"
    return host, int(port_str or "80")


# ---- SOCKS5 ----
SOCKS_VER = 0x05
SOCKS_CMD_CONNECT = 0x01
SOCKS_ATYP_DOMAIN = 0x03
SOCKS_REP_SUCCEEDED = 0x00


def _socks5_connect(target_host: str, target_port: int) -> socket.socket:
    """SOCKS5 完整握手（认证 + CONNECT），返回已建隧道的上游 socket。"""
    up = _connect_upstream()
    try:
        # 1) 方法协商
        if AUTH_USER and AUTH_PASS:
            up.sendall(b"\x05\x01\x02")  # ver=5, 1 method, 0x02
        else:
            up.sendall(b"\x05\x01\x00")
        ver, method = _recv_exact(up, 2)
        if method == 0xFF:
            raise OSError("SOCKS5 上游拒绝所有认证方法")

        # 2) 用户名密码认证（0x02）
        if method == 0x02:
            u = AUTH_USER.encode("utf-8")
            p = AUTH_PASS.encode("utf-8")
            up.sendall(b"\x01" + bytes([len(u)]) + u + bytes([len(p)]) + p)
            _ver, status = _recv_exact(up, 2)
            if status != 0x00:
                raise OSError("SOCKS5 上游认证失败")

        # 3) CONNECT
        addr_b = target_host.encode("ascii")
        req = struct.pack("!BBBB", SOCKS_VER, SOCKS_CMD_CONNECT, 0x00, SOCKS_ATYP_DOMAIN)
        req += bytes([len(addr_b)]) + addr_b + struct.pack("!H", target_port)
        up.sendall(req)
        hdr = _recv_exact(up, 4)
        if hdr[1] != SOCKS_REP_SUCCEEDED:
            codes = {1: "一般失败", 2: "规则禁止", 3: "网络不可达",
                     4: "主机不可达", 5: "连接拒绝", 6: "TTL过期",
                     7: "命令不支持", 8: "地址类型不支持"}
            raise OSError(f"SOCKS5 CONNECT 失败: {codes.get(hdr[1], str(hdr[1]))}")
        # 跳过绑定地址
        atyp = hdr[3]
        if atyp == 0x01:
            _recv_exact(up, 4)
        elif atyp == SOCKS_ATYP_DOMAIN:
            _recv_exact(up, 1 + _recv_exact(up, 1)[0])
        else:
            _recv_exact(up, 16)
        _recv_exact(up, 2)
        return up
    except Exception:
        try:
            up.close()
        except OSError:
            pass
        raise


# ---- 请求处理 ----
class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        data = _read_http_req(self.request)
        if not data:
            return
        try:
            if PROTO == "socks5":
                self._via_socks5(data)
            else:
                self._via_http(data)
        except OSError:
            pass
        finally:
            try:
                self.request.close()
            except OSError:
                pass

    def _via_socks5(self, data: bytes) -> None:
        """HTTP 代理请求 → SOCKS5 上游。"""
        req_line = data.split(b"\r\n")[0]
        is_connect = req_line.startswith(b"CONNECT ")
        host, port = _parse_host_port(req_line)

        up = _socks5_connect(host, port)
        try:
            if is_connect:
                # CONNECT: 回 200 给浏览器，隧道中继
                self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                _relay(self.request, up)
            else:
                # 普通 HTTP: 通过 SOCKS5 隧道发请求
                up.sendall(data)
                _relay(self.request, up)
        finally:
            try:
                up.close()
            except OSError:
                pass

    def _via_http(self, data: bytes) -> None:
        """HTTP 代理请求 → HTTP 上游（注入认证头）。"""
        if HTTP_AUTH_HEADER:
            idx = data.find(b"\r\n")
            if idx >= 0:
                data = data[: idx + 2] + HTTP_AUTH_HEADER + data[idx + 2 :]

        is_connect = data.startswith(b"CONNECT ")
        up = _connect_upstream()
        try:
            up.sendall(data)
            if is_connect:
                # 读上游 CONNECT 响应，回给浏览器，隧道中继
                resp = b""
                while b"\r\n\r\n" not in resp:
                    chunk = up.recv(4096)
                    if not chunk:
                        return
                    resp += chunk
                self.request.sendall(resp)
            _relay(self.request, up)
        finally:
            try:
                up.close()
            except OSError:
                pass


if __name__ == "__main__":
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", LISTEN), Handler)
    srv.allow_reuse_address = True
    tag = "SOCKS5" if PROTO == "socks5" else "HTTP"
    print(
        f"[woc-proxy] {tag} 代理已启动 → 127.0.0.1:{LISTEN} → {HOST}:{PORT}"
        + ("（认证已注入）" if AUTH_B64 else "（无认证）"),
        flush=True,
    )
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
