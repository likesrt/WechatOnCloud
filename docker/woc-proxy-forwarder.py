#!/usr/bin/env python3
"""本地代理转发器 — 向上游代理注入认证头。

Chromium 不支持 HTTP_PROXY/--proxy-server 中内嵌的 user:pass@ 认证，
导致代理返回 407 后弹框要求手动输入。本脚本在容器内监听本地端口，
对每个请求注入 Proxy-Authorization 头后转发到上游代理，
Chromium 连本地无需认证，彻底消除弹框。

环境变量：
  WOC_PROXY_HOST      上游代理地址（必填）
  WOC_PROXY_PORT      上游代理端口（必填）
  WOC_PROXY_AUTH      认证信息，base64(user:pass)（可选，无则不注入）
  WOC_PROXY_LISTEN    本地监听端口（默认 18080）
"""

import socket
import threading
import os
import select
import sys


def get_env_int(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


UPSTREAM_HOST = os.environ.get("WOC_PROXY_HOST", "")
UPSTREAM_PORT = get_env_int("WOC_PROXY_PORT", 0)
UPSTREAM_AUTH = os.environ.get("WOC_PROXY_AUTH", "")
LISTEN_PORT = get_env_int("WOC_PROXY_LISTEN", 18080)

if not UPSTREAM_HOST or not UPSTREAM_PORT:
    print("[woc-proxy] WOC_PROXY_HOST/WOC_PROXY_PORT 未设置，退出", file=sys.stderr)
    sys.exit(1)

AUTH_HEADER = (
    f"Proxy-Authorization: Basic {UPSTREAM_AUTH}\r\n".encode()
    if UPSTREAM_AUTH
    else b""
)


def recv_headers(sock: socket.socket) -> bytes:
    """读取直到 \r\n\r\n 标记 HTTP 头结束，有 Content-Length 再读 body。"""
    data = b""
    while b"\r\n\r\n" not in data:
        try:
            chunk = sock.recv(4096)
        except OSError:
            break
        if not chunk:
            break
        data += chunk
    if b"\r\n\r\n" not in data:
        return data  # 不完整的请求，原样转发

    headers_end = data.index(b"\r\n\r\n") + 4
    headers = data[:headers_end]

    # 检查 Content-Length 决定是否读 body
    cl = 0
    for line in headers.split(b"\r\n"):
        if line.lower().startswith(b"content-length:"):
            try:
                cl = int(line.split(b":", 1)[1].strip())
            except (ValueError, IndexError):
                pass
            break

    body = data[headers_end:]
    while len(body) < cl:
        try:
            chunk = sock.recv(min(4096, cl - len(body)))
        except OSError:
            break
        if not chunk:
            break
        body += chunk

    return headers + body


def relay(client: socket.socket) -> None:
    """读取客户端首请求，注入认证头后发往上游，然后双向中继。"""
    try:
        data = recv_headers(client)
    except OSError:
        client.close()
        return
    if not data:
        client.close()
        return

    upstream: socket.socket | None = None
    try:
        upstream = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        upstream.settimeout(10)
        upstream.connect((UPSTREAM_HOST, UPSTREAM_PORT))
        upstream.settimeout(None)
    except OSError as e:
        print(f"[woc-proxy] 连接上游 {UPSTREAM_HOST}:{UPSTREAM_PORT} 失败: {e}", file=sys.stderr)
        client.close()
        return

    # 首请求注入认证头（插在请求行之后、其余头部之前）
    if AUTH_HEADER:
        idx = data.find(b"\r\n")
        if idx >= 0:
            data = data[: idx + 2] + AUTH_HEADER + data[idx + 2 :]

    try:
        upstream.sendall(data)
    except OSError:
        client.close()
        upstream.close()
        return

    # 双向中继
    socks: list[socket.socket] = [client, upstream]
    try:
        while True:
            r, _, _ = select.select(socks, [], [], 30)
            if not r:
                break
            for s in r:
                try:
                    chunk = s.recv(65536)
                except OSError:
                    chunk = b""
                if not chunk:
                    for sock in socks:
                        try:
                            sock.close()
                        except OSError:
                            pass
                    return
                other = upstream if s is client else client
                try:
                    other.sendall(chunk)
                except OSError:
                    for sock in socks:
                        try:
                            sock.close()
                        except OSError:
                            pass
                    return
    finally:
        for sock in socks:
            try:
                sock.close()
            except OSError:
                pass


def main() -> None:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        server.bind(("127.0.0.1", LISTEN_PORT))
    except OSError as e:
        print(f"[woc-proxy] 绑定 127.0.0.1:{LISTEN_PORT} 失败: {e}", file=sys.stderr)
        sys.exit(1)
    server.listen(16)
    print(
        f"[woc-proxy] 代理转发器已启动 → 127.0.0.1:{LISTEN_PORT} "
        f"→ {UPSTREAM_HOST}:{UPSTREAM_PORT}"
        + ("（认证已注入）" if AUTH_HEADER else "（无认证）"),
        flush=True,
    )

    while True:
        try:
            client, addr = server.accept()
        except OSError:
            break
        t = threading.Thread(target=relay, args=(client,), daemon=True)
        t.start()


if __name__ == "__main__":
    main()
