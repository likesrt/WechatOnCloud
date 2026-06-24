# 实例代理配置 — 设计文档

> 日期: 2025-06-24
> 状态: 待审核

## 1. 需求概述

为实例（容器）配置代理服务器，让实例内所有出站流量走代理。主要场景：NAS 在国内、通过代理翻墙使用 Telegram / X 等。

- 每实例独立配置（实例 A 走直连、实例 B 走代理）
- 支持 HTTP/HTTPS 代理和 SOCKS5 代理
- 用户已有代理服务器，只需填写地址端口
- 修改后重启实例生效

## 2. 技术方案

### 2.1 选择：环境变量 + Chromium 旗标

不采用 iptables/redsocks 透明代理，原因：
- 复杂度高，需装额外软件、维护 iptables 规则
- 需排除面板↔实例 KasmVNC 内部通信，易出错
- 翻墙场景主要使用者（Chromium、Telegram）都能在应用层处理代理

**实现方式**：

| 应用 | 代理方式 |
|------|----------|
| Chromium | `--proxy-server` 命令行旗标（最可靠） |
| Telegram 桌面版 | 系统 `HTTP_PROXY`/`HTTPS_PROXY` 环境变量 |
| 微信 | 不设代理（微信不需要翻墙） |
| 系统级工具（curl/wget） | 环境变量 |

环境变量在容器创建时注入，覆盖所有进程。Chromium 额外加旗标是因为它对环境变量的支持不如旗标稳定。

### 2.2 数据流

```
面板 UI（创建/管理页面）
  → POST/PUT /api/admin/instances (含 proxy 字段)
    → store.ts: Instance.proxy 持久化到 accounts.json
      → docker.ts: envList() 读取 inst.proxy，追加代理环境变量
        → 容器启动时注入环境变量
          → autostart: Chromium 拼接 --proxy-server 旗标
```

## 3. 数据模型

### 3.1 store.ts — Instance 接口扩展

```typescript
export interface ProxyConfig {
  type: 'http' | 'socks5';   // 代理类型
  host: string;               // 代理服务器地址
  port: number;               // 端口（1-65535）
  username?: string;          // 认证用户名（可选）
  password?: string;          // 认证密码（可选）
}

export interface Instance {
  // ... 现有字段不变
  proxy?: ProxyConfig;        // 代理配置；缺省 = 不设代理
}
```

### 3.2 校验规则

- `host`: 非空，最长 253 字符，不含协议头（不含 `://`）
- `port`: 整数，1-65535
- `username`/`password`: 可选，最长 128 字符
- 传入空 proxy 或 `proxy=null` = 清除代理配置（恢复直连）

## 4. 容器创建

### 4.1 envList() — 追加代理环境变量

```typescript
function proxyEnv(inst: Instance): string[] {
  const p = inst.proxy;
  if (!p) return [];
  const proto = p.type === 'socks5' ? 'socks5' : 'http';
  // 有认证时嵌入 URL
  const auth = p.username ? `${encodeURIComponent(p.username)}:${encodeURIComponent(p.password || '')}@` : '';
  const addr = `${proto}://${auth}${p.host}:${p.port}`;
  return [
    `HTTP_PROXY=${addr}`,
    `http_proxy=${addr}`,
    `HTTPS_PROXY=${addr}`,
    `https_proxy=${addr}`,
    `ALL_PROXY=${addr}`,
    `all_proxy=${addr}`,
    // 排除内部通信：面板↔实例、localhost
    `NO_PROXY=localhost,127.0.0.1,::1,*.local`,
    `no_proxy=localhost,127.0.0.1,::1,*.local`,
  ];
}
```

大小写双写是为了兼容不同工具（部分工具读大写、部分读小写）。

### 4.2 NO_PROXY 说明

`NO_PROXY` 排除 localhost 和 `*.local`，确保：
- 面板反向代理到实例 KasmVNC 不受影响
- 实例内服务间通信不经过代理

## 5. Chromium 启动旗标

### 5.1 autostart 修改

在 `autostart` 中，检测代理环境变量，为 Chromium 拼接 `--proxy-server`：

```bash
# 代理：Chromium 额外加 --proxy-server 旗标（比环境变量更可靠）
if [ "$APP_TYPE" = "chromium" ] && [ -n "${HTTP_PROXY:-}" ]; then
    APP_LAUNCH="${APP_LAUNCH} --proxy-server=${HTTP_PROXY}"
elif [ "$APP_TYPE" = "chromium" ] && [ -n "${ALL_PROXY:-}" ]; then
    APP_LAUNCH="${APP_LAUNCH} --proxy-server=${ALL_PROXY}"
fi
```

Telegram 不需要额外处理——它吃系统 `HTTP_PROXY` 环境变量。

## 6. API

### 6.1 创建实例时传入代理

`POST /api/admin/instances` body 新增可选字段 `proxy`：

```json
{
  "name": "TG 实例",
  "appType": "telegram",
  "allowedUserIds": [],
  "proxy": {
    "type": "socks5",
    "host": "192.168.1.100",
    "port": 1080
  }
}
```

### 6.2 修改实例代理配置

新增 `PUT /api/admin/instances/:id/proxy`：

```json
// 设置代理
{ "proxy": { "type": "http", "host": "proxy.example.com", "port": 8080 } }

// 清除代理
{ "proxy": null }
```

校验 proxy 字段后更新 store 并持久化。返回更新后的 `publicInstance`。

### 6.3 查询时脱敏

`GET /api/instances` 和 `GET /api/admin/instances/:id/proxy` 返回的 proxy 中，`password` 脱敏为 `***`（前端显示时不可见原文）。修改时若不传 password 则保持原值。

## 7. UI

### 7.1 创建实例弹窗

在 Admin.tsx 的「新建实例」表单中，新增可折叠区域「代理设置」（默认折叠）：

- 折叠时显示「代理: 未配置」（灰色小字）
- 展开后：类型下拉（HTTP / SOCKS5）、地址输入框、端口输入框、用户名（可选）、密码（可选，type=password）
- 清除按钮：一键清空代理配置

### 7.2 实例管理卡片

在实例卡片的操作菜单中，新增「代理」入口：

- 点击弹出代理配置弹窗（同创建时的表单）
- 底部提示「修改后需重启实例生效」
- 保存按钮调用 `PUT /api/admin/instances/:id/proxy`

### 7.3 实例详情/桌面页

可在桌面页顶栏或侧栏显示当前代理状态（小图标，hover 显示详情），仅提示、不可在此修改。

## 8. 边界情况

| 场景 | 处理 |
|------|------|
| 代理地址不可达 | 应用内表现为网络不通，用户自行排查代理服务 |
| 代理认证失败 | 同不可达；日志中可能出现 407/认证错误 |
| 修改代理后未重启 | 提示「重启后生效」，不自动重启（避免打断当前会话） |
| 老实例（无 proxy 字段） | `instanceAppType` 回退逻辑；`inst.proxy` 为 undefined → 不设代理 |
| 容器内 KasmVNC 通信 | `NO_PROXY` 排除 localhost，内部反代不受影响 |
| 同时配 HTTP_PROXY 和 ALL_PROXY | 不允许——proxy.type 决定一种协议 |

## 9. 涉及文件

| 文件 | 改动 |
|------|------|
| `panel/server/src/store.ts` | Instance 接口加 proxy 字段；校验与持久化 |
| `panel/server/src/docker.ts` | envList() 追加 proxyEnv() |
| `panel/server/src/index.ts` | 新增 PUT /api/admin/instances/:id/proxy；创建接口收 proxy |
| `docker/autostart` | Chromium 加 --proxy-server 旗标 |
| `panel/web/src/api.ts` | 新增 setInstanceProxy() 方法 |
| `panel/web/src/pages/Admin.tsx` | 创建/管理弹窗加代理表单 |
| `panel/web/src/styles.css` | 代理表单样式 |
