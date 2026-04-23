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
| POST | `/admin/accounts` | 添加单个账号 |
| POST | `/admin/import` | 批量导入（JSON） |
| POST | `/admin/import/sqlite` | 从 CPA SQLite 导入 |
| DELETE | `/admin/accounts/{id}` | 删除账号 |
| POST | `/admin/accounts/{id}/refresh` | 手动刷新 token |
| GET | `/admin/logs` | 请求日志 |
| POST | `/admin/pool/refresh` | 刷新账号池 |

## 批量导入账号

### 从 openai-cpa-optimized 自动导入

如果你把 CPA 和 Hub 部署在同一台机器（或共享 volume）：

```bash
# 先登录获取 token
TOKEN=$(curl -s -X POST http://localhost:8080/admin/login \
  -H "Content-Type: application/json" \
  -d '{"password": "admin"}' | jq -r .token)

# 一键导入所有历史账号（自动跳过"仅注册成功"和无 token 的）
curl -X POST http://localhost:8080/admin/import/sqlite \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "db_path": "/path/to/openai-cpa-optimized/data/data.db",
    "skip_reg_only": true
  }'
```

返回示例：
```json
{
  "status": "success",
  "total": 150,
  "success": 120,
  "skipped": 25,
  "failed": 5,
  "errors": ["xxx: invalid token_data JSON"]
}
```

### 从 JSON 批量导入

```bash
curl -X POST http://localhost:8080/admin/import \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "accounts": [
      {"email": "a@x.com", "access_token": "eyJ...", "refresh_token": "..."},
      {"email": "b@x.com", "access_token": "eyJ...", "refresh_token": "..."}
    ],
    "skip_existing": true
  }'
```

### 实时自动推送（推荐）

在 [openai-cpa-optimized](https://github.com/Ancoren/openai-cpa-optimized) 中开启 `hub` 配置，注册成功后会**自动实时推送**到 Hub，无需任何手动操作：

```yaml
hub:
  enable: true
  url: "http://your-hub:8080"
  admin_password: "your-hub-admin-password"
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
