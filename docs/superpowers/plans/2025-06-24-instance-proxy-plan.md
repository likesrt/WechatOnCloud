# 实例代理配置 — 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为实例（容器）配置 HTTP/SOCKS5 代理服务器，每实例独立配置，让 Chromium/Telegram 等应用走代理翻墙。

**Architecture:** 环境变量注入（HTTP_PROXY/HTTPS_PROXY/ALL_PROXY/NO_PROXY）+ Chromium `--proxy-server` 旗标。数据持久化在 accounts.json 的 Instance 上，API 新建/修改/脱敏返回。

**Tech Stack:** TypeScript (Express/Fastify 后端), React (前端), Bash (autostart), Docker Compose

## Global Constraints

- 每实例独立代理配置，互不影响
- 支持 HTTP/HTTPS 和 SOCKS5 两种代理类型
- 代理配置修改后需重启实例生效
- NO_PROXY 排除 localhost/127.0.0.1/::1/*.local，保证面板↔实例内部通信不受影响
- 密码在 API 返回时脱敏为 `***`；修改时若不传密码则保持原值
- 老实例无 proxy 字段时行为不变（不设代理）

---

### Task 1: store.ts — 数据模型与校验

**Files:**
- Modify: `panel/server/src/store.ts:42-60`（Instance 接口），`panel/server/src/store.ts:237-248`（publicInstance），`panel/server/src/store.ts:303-343`（createInstance）

**Interfaces:**
- Consumes: 无（第一个任务）
- Produces: `ProxyConfig` 接口、`validateProxy()` 函数、`setInstanceProxy()` 函数、`publicInstance()` 含 proxy 脱敏

- [ ] **Step 1: 在 Instance 接口上方定义 ProxyConfig 类型并扩展 Instance**

在 `store.ts` 第 28 行（`APP_TYPES` 上方）新增：

```typescript
/** 实例代理配置。缺省 = 不设代理（直连）。 */
export interface ProxyConfig {
  type: 'http' | 'socks5';
  host: string;
  port: number;
  username?: string;
  password?: string;
}
```

在 `Instance` 接口（约第 59 行 `memHardLimitMB` 之后）新增字段：

```typescript
  /** 代理配置。缺省 = 不设代理（直连）；修改后重启实例生效。 */
  proxy?: ProxyConfig;
```

- [ ] **Step 2: 编写 validateProxy 校验函数**

在 `store.ts` 中，`setInstanceMemLimits` 函数之前（约第 251 行前）新增：

```typescript
/** 校验代理配置。合法则返回 ProxyConfig，非法则 throw。传 null/undefined 返回 null（清除代理）。 */
export function validateProxy(input: any): ProxyConfig | null {
  if (input === null || input === undefined) return null;
  if (typeof input !== 'object') throw new Error('代理配置格式不正确');
  const { type, host, port, username, password } = input;
  if (type !== 'http' && type !== 'socks5') throw new Error('代理类型仅支持 http 或 socks5');
  if (typeof host !== 'string' || host.length === 0 || host.length > 253 || host.includes('://'))
    throw new Error('代理地址不合法（不含协议头，如 192.168.1.1）');
  const p = Number(port);
  if (!Number.isInteger(p) || p < 1 || p > 65535) throw new Error('端口需为 1-65535');
  const cfg: ProxyConfig = { type, host, port };
  if (username !== undefined && username !== null) {
    if (typeof username !== 'string' || username.length > 128) throw new Error('代理用户名不合法');
    cfg.username = username;
  }
  if (password !== undefined && password !== null) {
    if (typeof password !== 'string' || password.length > 128) throw new Error('代理密码不合法');
    cfg.password = password;
  }
  return cfg;
}
```

- [ ] **Step 3: 修改 publicInstance 脱敏返回 proxy**

在 `publicInstance` 函数（约第 237 行）中，return 对象增加 proxy 字段（脱敏密码）：

```typescript
export function publicInstance(i: Instance) {
  return {
    id: i.id,
    name: i.name,
    appType: instanceAppType(i),
    icon: i.icon,
    createdAt: i.createdAt,
    createdBy: i.createdBy,
    memSoftLimitMB: i.memSoftLimitMB,
    memHardLimitMB: i.memHardLimitMB,
    // 代理配置脱敏返回：密码替换为 ***
    proxy: i.proxy ? { ...i.proxy, password: i.proxy.password ? '***' : undefined } : undefined,
  };
}
```

- [ ] **Step 4: 编写 setInstanceProxy 函数**

在 `store.ts` 中，`setInstanceMemLimits` 函数之后（约第 273 行后）新增：

```typescript
/** 设置/清除实例代理配置。proxy 为 null 表示清除。修改后需重启实例生效。 */
export function setInstanceProxy(id: string, proxy: ProxyConfig | null) {
  const inst = findInstance(id);
  if (!inst) throw new Error('实例不存在');
  if (proxy === null) {
    delete inst.proxy;
  } else {
    // 未传密码时保留原密码（修改场景：用户可能只想改地址不改密码）
    if (!proxy.password && inst.proxy?.password) {
      proxy.password = inst.proxy.password;
    }
    inst.proxy = proxy;
  }
  persist();
  return publicInstance(inst);
}
```

- [ ] **Step 5: 修改 createInstance 接收 proxy 参数**

`createInstance` 函数签名增加 `proxy` 参数：

```typescript
export function createInstance(
  name: string,
  createdBy: string,
  allowedUserIds: string[] = [],
  reuseVolumeName?: string,
  appType: AppType = 'wechat',
  proxy?: ProxyConfig,
) {
```

在 `inst` 对象构建中（约第 330 行 `createdBy` 之后）加入：

```typescript
    createdBy,
    ...(proxy ? { proxy } : {}),
  };
```

- [ ] **Step 6: 编译验证**

```bash
cd panel/server && npx tsc --noEmit
```
预期：无类型错误。

- [ ] **Step 7: 提交**

```bash
git add panel/server/src/store.ts
git commit -m "feat(proxy): store 层代理数据模型、校验、脱敏与持久化"
```

---

### Task 2: docker.ts — 环境变量注入

**Files:**
- Modify: `panel/server/src/docker.ts:106-128`（envList 函数）

**Interfaces:**
- Consumes: `Instance.proxy`（Task 1 定义）
- Produces: 代理环境变量追加到容器 env

- [ ] **Step 1: 编写 proxyEnv 辅助函数**

在 `docker.ts` 中，`envList` 函数上方新增：

```typescript
/** 将实例代理配置转为容器环境变量列表。无代理时返回空数组。 */
function proxyEnv(inst: Instance): string[] {
  const p = inst.proxy;
  if (!p) return [];
  const proto = p.type === 'socks5' ? 'socks5' : 'http';
  // 有认证时嵌入 URL
  const auth = p.username
    ? `${encodeURIComponent(p.username)}:${encodeURIComponent(p.password || '')}@`
    : '';
  const addr = `${proto}://${auth}${p.host}:${p.port}`;
  return [
    `HTTP_PROXY=${addr}`,
    `http_proxy=${addr}`,
    `HTTPS_PROXY=${addr}`,
    `https_proxy=${addr}`,
    `ALL_PROXY=${addr}`,
    `all_proxy=${addr}`,
    // 排除内部通信：面板↔实例、localhost。防止 KasmVNC 反代走代理导致连不上。
    `NO_PROXY=localhost,127.0.0.1,::1,*.local`,
    `no_proxy=localhost,127.0.0.1,::1,*.local`,
  ];
}
```

- [ ] **Step 2: envList 中追加 proxyEnv**

在 `envList` 函数的 return 之前（`return env` 之前），追加：

```typescript
  // 代理配置：注入 HTTP_PROXY/HTTPS_PROXY/ALL_PROXY/NO_PROXY 环境变量
  env.push(...proxyEnv(inst));
```

- [ ] **Step 3: 编译验证**

```bash
cd panel/server && npx tsc --noEmit
```
预期：无类型错误。

- [ ] **Step 4: 提交**

```bash
git add panel/server/src/docker.ts
git commit -m "feat(proxy): docker 层代理环境变量注入"
```

---

### Task 3: autostart — Chromium 代理旗标

**Files:**
- Modify: `docker/autostart:26-29`（深色模式代码段之后）

**Interfaces:**
- Consumes: 容器环境变量 `HTTP_PROXY` / `ALL_PROXY`（Task 2 注入）
- Produces: Chromium 启动时追加 `--proxy-server` 旗标

- [ ] **Step 1: 在 autostart 深色模式代码后追加代理旗标逻辑**

在 `docker/autostart` 第 29 行（`fi` 之后，深色段结束）和第 31 行（防最小化注释）之间，新增：

```bash
# 代理：Chromium 额外加 --proxy-server 旗标（比环境变量更可靠）。
# Telegram 桌面版吃系统 HTTP_PROXY 环境变量，无需额外处理。
if [ "$APP_TYPE" = "chromium" ] && [ -n "${HTTP_PROXY:-}" ]; then
    APP_LAUNCH="${APP_LAUNCH} --proxy-server=${HTTP_PROXY}"
    echo "[autostart] 代理已启用：--proxy-server=${HTTP_PROXY}"
elif [ "$APP_TYPE" = "chromium" ] && [ -n "${ALL_PROXY:-}" ]; then
    APP_LAUNCH="${APP_LAUNCH} --proxy-server=${ALL_PROXY}"
    echo "[autostart] 代理已启用：--proxy-server=${ALL_PROXY}"
fi
```

- [ ] **Step 2: 提交**

```bash
git add docker/autostart
git commit -m "feat(proxy): autostart Chromium 代理旗标 --proxy-server"
```

---

### Task 4: index.ts — API 路由

**Files:**
- Modify: `panel/server/src/index.ts:361-396`（创建实例路由），新增路由

**Interfaces:**
- Consumes: `validateProxy`、`setInstanceProxy`（Task 1）、`ProxyConfig`（Task 1）
- Produces: `POST /api/admin/instances` 收 proxy、`PUT /api/admin/instances/:id/proxy`

- [ ] **Step 1: 修改 import，加入新导出**

在 `index.ts` 顶部的 import 块中（约第 38 行），在 `setInstanceMemLimits` 之后追加：

```typescript
  validateProxy,
  setInstanceProxy,
  type ProxyConfig,
```

- [ ] **Step 2: 修改创建实例路由接收 proxy**

在 `POST /api/admin/instances` 路由中（约第 361 行），从 body 解出 proxy：

```typescript
  const { name, reuseVolume, appType, proxy } = (req.body as any) ?? {};
```

在 `createInstance` 调用前新增校验：

```typescript
  let proxyCfg: ProxyConfig | undefined;
  if (proxy !== undefined && proxy !== null) {
    try {
      const cfg = validateProxy(proxy);
      if (cfg) proxyCfg = cfg;
    } catch (e: any) {
      return reply.code(400).send({ error: '代理配置：' + (e?.message || '格式不正确') });
    }
  }
```

`createInstance` 调用追加参数：

```typescript
  const inst = createInstance(String(name), admin.id, allowedUserIds, reuseVolumeName, type, proxyCfg);
```

- [ ] **Step 3: 新增 PUT 路由修改代理配置**

在 `PUT /api/admin/instances/:id/mem-limits` 路由之后（约第 507 行），新增：

```typescript
/** 修改实例代理配置（仅管理员）。修改后需重启实例生效。 */
app.put('/api/admin/instances/:id/proxy', async (req, reply) => {
  if (!requireAdmin(req, reply)) return;
  const id = (req.params as any).id;
  const inst = findInstance(id);
  if (!inst) return reply.code(404).send({ error: '实例不存在' });
  const { proxy } = (req.body as any) ?? {};
  try {
    const cfg = validateProxy(proxy);
    const pub = setInstanceProxy(id, cfg);
    const label = cfg
      ? `${cfg.type.toUpperCase()} ${cfg.host}:${cfg.port}`
      : '已清除';
    appendPanelLog('INFO', `实例「${inst.name}」(id=${id}) 代理配置：${label}（重启后生效）`);
    return { instance: pub, message: cfg ? '代理已保存，重启实例后生效' : '代理已清除，重启实例后生效' };
  } catch (e: any) {
    return reply.code(400).send({ error: e?.message || '代理配置不合法' });
  }
});
```

- [ ] **Step 4: 编译验证**

```bash
cd panel/server && npx tsc --noEmit
```
预期：无类型错误。

- [ ] **Step 5: 提交**

```bash
git add panel/server/src/index.ts
git commit -m "feat(proxy): API 路由 — 创建实例收 proxy + 修改代理 PUT"
```

---

### Task 5: api.ts — 前端 API 层

**Files:**
- Modify: `panel/web/src/api.ts:47-56`（PanelInstance 接口），`panel/web/src/api.ts:159-163`（createInstance）

**Interfaces:**
- Consumes: 后端 `ProxyConfig` 形状
- Produces: `PanelInstance.proxy` 类型、`api.setInstanceProxy()` 方法

- [ ] **Step 1: PanelInstance 接口增加 proxy 字段**

在 `api.ts` 中，`PanelInstance` 接口（约第 47 行）新增：

```typescript
export interface PanelInstance {
  id: string;
  name: string;
  appType?: AppType;
  icon?: string;
  createdAt: string;
  createdBy: string;
  memSoftLimitMB?: number;
  memHardLimitMB?: number;
  /** 代理配置（脱敏：password 为 ***）。缺省 = 不设代理。 */
  proxy?: { type: 'http' | 'socks5'; host: string; port: number; username?: string; password?: string };
}
```

- [ ] **Step 2: createInstance 方法增加 proxy 参数**

修改 `createInstance` 方法签名（约第 159 行）：

```typescript
  createInstance: (
    name: string,
    allowedUserIds: string[] = [],
    reuseVolume?: string,
    appType: AppType = 'wechat',
    proxy?: { type: 'http' | 'socks5'; host: string; port: number; username?: string; password?: string },
  ) =>
    req<{ instance: PanelInstance }>('/api/admin/instances', {
      method: 'POST',
      body: JSON.stringify({ name, allowedUserIds, reuseVolume: reuseVolume || undefined, appType, proxy }),
    }),
```

- [ ] **Step 3: 新增 setInstanceProxy 方法**

在 `api` 对象中，`setInstanceMemLimits` 之后（约第 172 行）新增：

```typescript
  /** 设置/清除实例代理配置。proxy 为 null 表示清除。修改后需重启实例生效。 */
  setInstanceProxy: (
    id: string,
    proxy: { type: 'http' | 'socks5'; host: string; port: number; username?: string; password?: string } | null,
  ) =>
    req<{ instance: PanelInstance; message: string }>(`/api/admin/instances/${id}/proxy`, {
      method: 'PUT',
      body: JSON.stringify({ proxy }),
    }),
```

- [ ] **Step 4: 编译验证**

```bash
cd panel/web && npx tsc --noEmit
```
预期：无类型错误。

- [ ] **Step 5: 提交**

```bash
git add panel/web/src/api.ts
git commit -m "feat(proxy): 前端 API 层 — PanelInstance.proxy + setInstanceProxy"
```

---

### Task 6: Admin.tsx — 创建实例弹窗 + 管理菜单 + 代理弹窗

**Files:**
- Modify: `panel/web/src/pages/Admin.tsx:1694-1751`（CreateInstance），`panel/web/src/pages/Admin.tsx:1038-1208`（InstanceAdminCard），新增 ProxyEditor 组件

**Interfaces:**
- Consumes: `api.createInstance`（Task 5）、`api.setInstanceProxy`（Task 5）、`PanelInstance.proxy`（Task 5）
- Produces: 创建弹窗代理区域、管理菜单「代理」入口、代理编辑弹窗

- [ ] **Step 1: 在 CreateInstance 中新增代理配置区域**

在 `CreateInstance` 组件中（约第 1694 行），新增状态：

```typescript
  // 代理配置（默认折叠）
  const [proxyOpen, setProxyOpen] = useState(false);
  const [proxy, setProxy] = useState<{ type: 'http' | 'socks5'; host: string; port: string; username: string; password: string }>({
    type: 'http', host: '', port: '', username: '', password: '',
  });
```

修改 `submit` 函数，组装 proxy 参数：

```typescript
      const proxyCfg = proxy.host && proxy.port
        ? { type: proxy.type, host: proxy.host, port: Number(proxy.port), ...(proxy.username ? { username: proxy.username } : {}), ...(proxy.password ? { password: proxy.password } : {}) }
        : undefined;
      await api.createInstance(name.trim(), [...sel], reuse || undefined, appType, proxyCfg);
```

在表单中「应用类型」选择器之后、名称输入框之前，新增可折叠代理区域。插入位置：`</div>`（app-picker 结束，约第 1750 行）之后，`<input className="input" placeholder="实例名称"...` 之前：

```tsx
        <button type="button" className="proxy-toggle" onClick={() => setProxyOpen((v) => !v)}>
          <span>代理设置</span>
          <span className={'proxy-toggle-arrow' + (proxyOpen ? ' open' : '')}>{CaretIcon}</span>
          {!proxyOpen && proxy.host && (
            <span className="proxy-toggle-summary">{proxy.type.toUpperCase()} {proxy.host}:{proxy.port || '?'}</span>
          )}
        </button>
        {proxyOpen && (
          <div className="proxy-fields">
            <div className="proxy-row">
              <select className="proxy-sel" value={proxy.type} onChange={(e) => setProxy((p) => ({ ...p, type: e.target.value as any }))}>
                <option value="http">HTTP</option>
                <option value="socks5">SOCKS5</option>
              </select>
              <input className="input proxy-addr" placeholder="代理地址" value={proxy.host} onChange={(e) => setProxy((p) => ({ ...p, host: e.target.value }))} />
              <input className="input proxy-port" placeholder="端口" type="number" value={proxy.port} onChange={(e) => setProxy((p) => ({ ...p, port: e.target.value }))} />
            </div>
            <div className="proxy-row">
              <input className="input proxy-auth" placeholder="用户名（可选）" autoCapitalize="off" value={proxy.username} onChange={(e) => setProxy((p) => ({ ...p, username: e.target.value }))} />
              <PasswordInput placeholder="密码（可选）" value={proxy.password} onChange={(v) => setProxy((p) => ({ ...p, password: v }))} />
            </div>
            <button type="button" className="btn-text" onClick={() => setProxy({ type: 'http', host: '', port: '', username: '', password: '' })}>
              清除代理
            </button>
          </div>
        )}
```

- [ ] **Step 2: 在 InstanceAdminCard 管理菜单中新增「代理」入口**

在 `InstanceAdminCard` 的「设置」菜单组中（约第 1173-1192 行），在「数据卷」按钮之前新增：

```tsx
                  <button className="btn-text" onClick={onProxy}>
                    代理
                  </button>
```

在 props 类型中新增 `onProxy`（约第 1053 行）：

```typescript
  onProxy: () => void;
```

- [ ] **Step 3: 在 Admin 组件中新增代理编辑弹窗状态与调用**

在 `Admin` 组件中（约第 266 行，`iconInst` 状态之后），新增：

```typescript
  const [proxyInst, setProxyInst] = useState<InstanceWithStatus | null>(null); // 代理编辑弹窗
```

在实例卡片的渲染中（约第 479 行 `onIcon` 之后），新增：

```typescript
                    onProxy={() => setProxyInst(inst)}
```

在 `Admin` 组件的 return 末尾（约 `{volumeInst && ...}` 弹窗渲染区域），新增代理弹窗渲染：

```tsx
        {proxyInst && (
          <ProxyEditor
            inst={proxyInst}
            onClose={() => setProxyInst(null)}
            onDone={() => { setProxyInst(null); load(); }}
          />
        )}
```

- [ ] **Step 4: 编写 ProxyEditor 组件**

在 `Admin.tsx` 文件末尾（`CreateInstance` 函数之前，约第 1632 行之前）新增 `ProxyEditor` 组件：

```tsx
/** 实例代理配置弹窗。修改后需重启实例生效。 */
function ProxyEditor({ inst, onClose, onDone }: { inst: InstanceWithStatus; onClose: () => void; onDone: () => void }) {
  const { toast } = useUI();
  const hasProxy = !!inst.proxy;
  const [type, setType] = useState<'http' | 'socks5'>(inst.proxy?.type || 'http');
  const [host, setHost] = useState(inst.proxy?.host || '');
  const [port, setPort] = useState(inst.proxy?.port ? String(inst.proxy.port) : '');
  const [username, setUsername] = useState(inst.proxy?.username || '');
  const [password, setPassword] = useState(''); // 不回显原密码
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setErr('');
    setBusy(true);
    try {
      const cfg = host && port
        ? { type, host, port: Number(port), ...(username ? { username } : {}), ...(password ? { password } : {}) }
        : null;
      const r = await api.setInstanceProxy(inst.id, cfg);
      toast(r.message || '已保存', 'ok');
      onDone();
    } catch (e: any) {
      setErr(e.message || '保存失败');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="modal-mask" onClick={onClose}>
      <form className="card modal" onClick={(e) => e.stopPropagation()} onSubmit={submit}>
        <h2>代理设置 — {inst.name}</h2>
        <div className="proxy-fields">
          <div className="proxy-row">
            <select className="proxy-sel" value={type} onChange={(e) => setType(e.target.value as any)}>
              <option value="http">HTTP</option>
              <option value="socks5">SOCKS5</option>
            </select>
            <input className="input proxy-addr" placeholder="代理地址" value={host} onChange={(e) => setHost(e.target.value)} />
            <input className="input proxy-port" placeholder="端口" type="number" value={port} onChange={(e) => setPort(e.target.value)} />
          </div>
          <div className="proxy-row">
            <input className="input proxy-auth" placeholder="用户名（可选）" autoCapitalize="off" value={username} onChange={(e) => setUsername(e.target.value)} />
            <PasswordInput placeholder={hasProxy ? '新密码（留空不变）' : '密码（可选）'} value={password} onChange={setPassword} />
          </div>
        </div>
        <p className="s-foot" style={{ marginTop: 8 }}>修改后需<b>重启实例</b>生效。代理仅影响 Chromium / Telegram 等应用，微信不受影响。</p>
        {err && <div className="error">{err}</div>}
        <div className="modal-actions">
          <button type="button" className="btn" onClick={onClose}>取消</button>
          {hasProxy && (
            <button type="button" className="btn btn-danger" disabled={busy} onClick={async () => {
              setBusy(true);
              try {
                const r = await api.setInstanceProxy(inst.id, null);
                toast(r.message || '代理已清除', 'ok');
                onDone();
              } catch (e: any) {
                setErr(e.message || '清除失败');
              } finally {
                setBusy(false);
              }
            }}>清除代理</button>
          )}
          <button className="btn btn-primary" disabled={busy}>保存</button>
        </div>
      </form>
    </div>
  );
}
```

- [ ] **Step 5: 编译验证**

```bash
cd panel/web && npx tsc --noEmit
```
预期：无类型错误（可能需要检查 import 是否缺 `PasswordInput` — 已在文件顶部导入）。

- [ ] **Step 6: 提交**

```bash
git add panel/web/src/pages/Admin.tsx
git commit -m "feat(proxy): 前端 UI — 创建弹窗代理区域 + 管理菜单代理入口 + 编辑弹窗"
```

---

### Task 7: styles.css — 代理表单样式

**Files:**
- Modify: `panel/web/src/styles.css`（末尾追加）

**Interfaces:**
- Consumes: Task 6 中使用的 CSS class：`.proxy-toggle`、`.proxy-fields`、`.proxy-row`、`.proxy-sel`、`.proxy-addr`、`.proxy-port`、`.proxy-auth`、`.proxy-toggle-arrow`、`.proxy-toggle-summary`
- Produces: 代理表单的克制样式

- [ ] **Step 1: 在 styles.css 末尾追加代理样式**

```css
/* ---- 代理配置 ---- */
.proxy-toggle {
  display: flex;
  align-items: center;
  gap: 6px;
  background: none;
  border: none;
  color: var(--c-muted);
  font-size: 13px;
  cursor: pointer;
  padding: 6px 0;
  width: 100%;
}
.proxy-toggle:hover { color: var(--c-text); }
.proxy-toggle-arrow {
  display: inline-flex;
  transition: transform 0.15s;
}
.proxy-toggle-arrow.open { transform: rotate(180deg); }
.proxy-toggle-summary {
  font-size: 12px;
  color: var(--c-accent);
  margin-left: auto;
}

.proxy-fields { display: flex; flex-direction: column; gap: 6px; }
.proxy-row {
  display: flex;
  gap: 6px;
}
.proxy-sel {
  width: 90px;
  flex-shrink: 0;
  border: 1px solid var(--c-border);
  border-radius: 6px;
  background: var(--c-bg);
  color: var(--c-text);
  font-size: 13px;
  padding: 0 8px;
  height: 34px;
}
.proxy-addr { flex: 1; }
.proxy-port { width: 90px; flex-shrink: 0; }
.proxy-auth { flex: 1; }
```

- [ ] **Step 2: 提交**

```bash
git add panel/web/src/styles.css
git commit -m "feat(proxy): 代理表单样式"
```

---

### Task 8: 端到端验证

- [ ] **Step 1: 本地构建镜像验证**

```bash
cd /home/codeg/workspace/WechatOnCloud && ./scripts/build-local.sh
```
预期：面板镜像 + 实例镜像均构建成功。

- [ ] **Step 2: 启动验证**

```bash
docker compose up -d
```
预期：面板启动成功，访问 `http://localhost:36080` 可登录。

- [ ] **Step 3: 功能验证**

1. 创建实例时展开「代理设置」，填写 HTTP 代理地址端口，创建成功
2. 进入该实例的管理卡片，点击「管理 → 代理」，可看到已配置的代理信息
3. 修改代理配置并保存
4. 清除代理配置并保存
5. 进入 Chromium 实例，访问 `https://httpbin.org/ip`，确认 IP 为代理 IP

- [ ] **Step 4: 提交（如有改动）**

```bash
git add -A && git commit -m "chore(proxy): 端到端验证完成"
```
