# Codex Hub — OpenAI API 中转站

基于账号池的 OpenAI API 代理网关，实现 **Token 自由**。

## 核心功能

| 功能 | 说明 |
|---|---|
| **账号池管理** | 导入多个 OpenAI 账号，自动轮询/负载均衡 |
| **自动故障切换** | 一个账号 401/429/封号，自动切到下一个 |
| **Token 自动刷新** | 后台定时刷新 access_token，不用手动维护 |
| **健康检查** | 每 5 分钟检查账号状态，自动禁用失效账号 |
| **用量统计** | 每个账号的请求数、token 消耗、成功率 |
| **流式支持** | SSE streaming 完全兼容 |
| **API 兼容** | 对外暴露标准 OpenAI API 格式 |

## 架构

```
客户端请求 → Codex Hub (负载均衡) → 账号A / 账号B / 账号C ...
                                    ↓
                              OpenAI API
```

## 快速开始

### 1. 部署

```bash
git clone https://github.com/Ancoren/codex-hub.git
cd codex-hub

# 配置环境变量
cp .env.example .env
# 编辑 .env

docker-compose up -d
```

### 2. 添加账号

```bash
curl -X POST http://localhost:8080/admin/login \
  -H "Content-Type: application/json" \
  -d '{"password": "admin"}'

# 用返回的 token 添加账号
curl -X POST http://localhost:8080/admin/accounts \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{
    "email": "your@email.com",
    "access_token": "eyJ...",
    "refresh_token": "def502..."
  }'
```

### 3. 使用

把原来调用 OpenAI API 的 `base_url` 改成你的 Hub 地址：

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://your-hub-ip:8080/v1",  # ← 改这里
    api_key="hub-api-key-or-anything",       # ← 如果设置了 HUB_API_KEY
)

response = client.chat.completions.create(
    model="gpt-5.2",
    messages=[{"role": "user", "content": "Hello"}],
)
```

Codex CLI 也一样：

```bash
codex --api-base http://your-hub-ip:8080/v1 --api-key hub-api-key
```

## 管理后台 API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/admin/login` | 登录获取 token |
| GET | `/admin/stats` | 全局统计 |
| GET | `/admin/accounts` | 账号列表 |
| POST | `/admin/accounts` | 添加账号 |
| DELETE | `/admin/accounts/{id}` | 删除账号 |
| POST | `/admin/accounts/{id}/refresh` | 手动刷新 token |
| GET | `/admin/logs` | 请求日志 |
| POST | `/admin/pool/refresh` | 刷新账号池 |

## 负载均衡策略

`HUB_STRATEGY` 环境变量：

| 策略 | 说明 |
|---|---|
| `least_used` | 选择请求数最少的账号（默认） |
| `round_robin` | 轮询 |
| `random` | 随机 |
| `priority` | 按优先级（预留） |

## 从 openai-cpa-optimized 导入账号

如果你有之前注册的账号数据库，可以直接用 SQLite 工具导出 `accounts` 表的 `email`、`token_data` 字段，然后解析 `token_data` JSON 批量导入 Hub。

```python
import json, sqlite3, requests

conn = sqlite3.connect("cpa/data/data.db")
cursor = conn.cursor()
cursor.execute("SELECT email, token_data FROM accounts WHERE token_data LIKE '%access_token%'")

for email, token_json in cursor.fetchall():
    data = json.loads(token_json)
    requests.post("http://localhost:8080/admin/accounts", json={
        "email": email,
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token", ""),
        "id_token": data.get("id_token", ""),
        "account_id": data.get("account_id", ""),
    }, headers={"Authorization": "Bearer YOUR_ADMIN_TOKEN"})
```

## 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `HUB_ADMIN_PASSWORD` | `admin` | 管理后台密码 |
| `HUB_API_KEY` | `` | 网关访问密钥（留空则不校验） |
| `HUB_STRATEGY` | `least_used` | 负载均衡策略 |
| `HUB_HEALTH_CHECK_INTERVAL` | `300` | 健康检查间隔（秒） |
| `HUB_AUTO_REFRESH_TOKEN` | `true` | 自动刷新 token |
| `HUB_PORT` | `8080` | 服务端口 |
| `HUB_LOG_LEVEL` | `INFO` | 日志级别 |

## 宝塔部署

1. 上传代码到 `/www/wwwroot/codex-hub`
2. 终端执行 `docker-compose up -d`
3. Nginx 反代 `127.0.0.1:8080`
4. 申请 SSL

## ⚠️ 免责声明

仅供技术学习和研究使用。请遵守 OpenAI 服务条款和相关法律法规。
