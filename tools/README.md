# OpenAI Account Auto-Registration Tool

Standalone CLI script for registering OpenAI accounts. No web UI, no database, no framework dependencies.

## Requirements

```bash
pip install curl_cffi requests
```

You also need the compiled `auth_core` extension placed next to `register.py`:

```
tools/
├── register.py
├── auth_core.cpython-311-x86_64-linux-gnu.so   # or your platform variant
└── README.md
```

The `auth_core` extension handles OpenAI's Sentinel challenge. Without it, registration will fail.

## Quick Start

### 1. Basic usage (single account)

```bash
python register.py --proxy "socks5://user:pass@host:port"
```

### 2. Register multiple accounts with auto-push to Codex Hub

```bash
python register.py \
  --proxy "socks5://user:pass@host:port" \
  --count 10 \
  --delay-min 10 \
  --delay-max 30 \
  --hub-url "http://localhost:8080" \
  --hub-password "admin" \
  --email-mode mail_curl \
  --email-api-base "https://your-mail-api.com" \
  --email-api-key "your-key"
```

### 3. Use JSON config file

Create `config.json`:

```json
{
  "proxy": "socks5://user:pass@host:port",
  "count": 5,
  "delay_min": 10,
  "delay_max": 20,
  "output": "accounts.jsonl",
  "hub_url": "http://localhost:8080",
  "hub_password": "admin",
  "email_mode": "mail_curl",
  "email_api_base": "https://your-mail-api.com",
  "email_api_key": "your-key"
}
```

```bash
python register.py --config config.json
```

## Email Providers

### mail_curl (default)

Compatible with mail-curl style APIs:
- `POST /api/remail?key=xxx` → returns `{email, id}`
- `GET /api/inbox?key=xxx&mailbox_id=xxx` → inbox list
- `GET /api/mail?key=xxx&id=xxx` → message detail

### duckmail

```bash
python register.py \
  --email-mode duckmail \
  --email-api-token "your-duckmail-token"
```

### cloudflare

Stub implementation — requires you to implement your Cloudflare Worker API.

## Output

Accounts are appended to `accounts.jsonl` (one JSON object per line):

```json
{"email": "xxx@xxx.com", "password": "xxx", "token_data": {"access_token": "...", "refresh_token": "..."}, "created_at": "2026-04-23T12:00:00Z"}
```

## Hub Auto-Push

If `--hub-url` and `--hub-password` are provided, successfully registered accounts are automatically pushed to your Codex Hub instance via the `/admin/accounts` API.

## CLI Arguments

| Argument | Default | Description |
|---|---|---|
| `--proxy` | | Proxy URL (socks5/http) |
| `--count` | 1 | Number of accounts to register |
| `--delay-min` | 5 | Min seconds between registrations |
| `--delay-max` | 30 | Max seconds between registrations |
| `--output` | accounts.jsonl | Output file path |
| `--hub-url` | | Codex Hub URL |
| `--hub-password` | | Hub admin password |
| `--hub-api-key` | | Hub gateway API key |
| `--email-mode` | mail_curl | Email provider mode |
| `--email-api-base` | | Email API base URL |
| `--email-api-key` | | Email API key |
| `--email-api-token` | | Email API token (duckmail) |
| `--config` | | JSON config file path |
