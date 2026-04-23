#!/usr/bin/env python3
"""
OpenAI Account Auto-Registration Script
Standalone CLI tool — no web UI, no database, no framework.

Requirements:
    pip install curl_cffi requests

Place the compiled auth_core extension next to this script:
    auth_core.cpython-311-x86_64-linux-gnu.so  (or your platform variant)

Usage:
    python register.py --proxy socks5://user:pass@host:port \
                       --count 5 \
                       --hub-url http://localhost:8080 \
                       --hub-password admin

Outputs accounts to accounts.jsonl (append mode).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import random
import re
import secrets
import string
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests
from curl_cffi import requests as curl_requests

# ---------------------------------------------------------------------------
# Auth core loader (tries script dir first, then PYTHONPATH)
# ---------------------------------------------------------------------------
_SCRIPT_DIR = Path(__file__).parent.resolve()
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

try:
    from auth_core import generate_payload, init_auth
except ImportError:
    try:
        from utils.auth_core import generate_payload, init_auth
    except ImportError:
        print("[FATAL] auth_core extension not found.")
        print("        Place auth_core.cpython-xxx.so next to this script or in PYTHONPATH.")
        sys.exit(1)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
AUTH_URL = "https://auth.openai.com/authorize"
TOKEN_URL = "https://auth.openai.com/oauth/token"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
DEFAULT_REDIRECT_URI = "http://localhost:1455/auth/callback"
DEFAULT_SCOPE = "openid email profile offline_access"

FIRST_NAMES = [
    "James", "John", "Robert", "Michael", "William", "David", "Richard",
    "Joseph", "Thomas", "Charles", "Emma", "Olivia", "Ava", "Isabella",
    "Sophia", "Mia", "Charlotte", "Amelia", "Harper", "Evelyn",
]
LAST_NAMES = [
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller",
    "Davis", "Rodriguez", "Martinez", "Hernandez", "Lopez", "Gonzalez",
    "Wilson", "Anderson", "Thomas", "Taylor", "Moore", "Jackson", "Martin",
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ssl_verify() -> bool:
    flag = os.getenv("OPENAI_SSL_VERIFY", "1").strip().lower()
    return flag not in {"0", "false", "no", "off"}


def _b64url_no_pad(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _sha256_b64url_no_pad(s: str) -> str:
    return _b64url_no_pad(hashlib.sha256(s.encode("ascii")).digest())


def _random_state(nbytes: int = 16) -> str:
    return secrets.token_urlsafe(nbytes)


def _pkce_verifier() -> str:
    return secrets.token_urlsafe(64)


def _parse_callback_url(callback_url: str) -> Dict[str, Any]:
    candidate = callback_url.strip()
    if not candidate:
        return {"code": "", "state": "", "error": "", "error_description": ""}
    if "://" not in candidate:
        if candidate.startswith("?"):
            candidate = f"http://localhost{candidate}"
        elif any(ch in candidate for ch in "/?#") or ":" in candidate:
            candidate = f"http://{candidate}"
        elif "=" in candidate:
            candidate = f"http://localhost/?{candidate}"
    parsed = urllib.parse.urlparse(candidate)
    query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    fragment = urllib.parse.parse_qs(parsed.fragment, keep_blank_values=True)
    for key, values in fragment.items():
        if key not in query or not query[key] or not (query[key][0] or "").strip():
            query[key] = values

    def get1(k: str) -> str:
        v = query.get(k, [""])
        return (v[0] or "").strip()

    code = get1("code")
    state = get1("state")
    error = get1("error")
    error_description = get1("error_description")
    if code and not state and "#" in code:
        code, state = code.split("#", 1)
    if not error and error_description:
        error, error_description = error_description, ""
    return {"code": code, "state": state, "error": error,
            "error_description": error_description}


def _jwt_claims_no_verify(id_token: str) -> Dict[str, Any]:
    if not id_token or id_token.count(".") < 2:
        return {}
    payload_b64 = id_token.split(".")[1]
    pad = "=" * ((4 - (len(payload_b64) % 4)) % 4)
    try:
        return json.loads(
            base64.urlsafe_b64decode((payload_b64 + pad).encode("ascii")).decode("utf-8")
        )
    except Exception:
        return {}


def _decode_jwt_segment(seg: str) -> Dict[str, Any]:
    raw = (seg or "").strip()
    if not raw:
        return {}
    pad = "=" * ((4 - (len(raw) % 4)) % 4)
    try:
        return json.loads(
            base64.urlsafe_b64decode((raw + pad).encode("ascii")).decode("utf-8")
        )
    except Exception:
        return {}


def _to_int(v: Any) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _oai_headers(did: str, extra: dict = None) -> dict:
    h = {
        "accept": "application/json",
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/110.0.0.0 Safari/537.36"
        ),
        "sec-ch-ua": '"Google Chrome";v="110", "Chromium";v="110", "Not_A Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "oai-device-id": did,
    }
    if extra:
        h.update(extra)
    return h


def _generate_password(length: int = 20) -> str:
    upper = random.choices(string.ascii_uppercase, k=2)
    lower = random.choices(string.ascii_lowercase, k=2)
    digits = random.choices(string.digits, k=2)
    specials = random.choices("!@#$%&*", k=2)
    pool = string.ascii_letters + string.digits + "!@#$%&*"
    rest = random.choices(pool, k=length - 8)
    chars = upper + lower + digits + specials + rest
    random.shuffle(chars)
    return "".join(chars)


def _generate_random_user_info() -> dict:
    name = f"{random.choice(FIRST_NAMES)} {random.choice(LAST_NAMES)}"
    year = random.randint(datetime.now().year - 45, datetime.now().year - 18)
    month = random.randint(1, 12)
    day = random.randint(1, 28)
    return {"name": name, "birthdate": f"{year}-{month:02d}-{day:02d}"}


def _parse_workspace_from_auth_cookie(auth_cookie: str) -> list:
    if not auth_cookie or "." not in auth_cookie:
        return []
    parts = auth_cookie.split(".")
    if len(parts) >= 2:
        claims = _decode_jwt_segment(parts[1])
        workspaces = claims.get("workspaces") or []
        if workspaces:
            return workspaces
    claims = _decode_jwt_segment(parts[0])
    return claims.get("workspaces") or []


def _extract_otp_code(text: str) -> Optional[str]:
    """Extract 6-digit OTP from email body."""
    patterns = [
        r"enter this code:\s*(\d{6})",
        r"verification code to continue:\s*(\d{6})",
        r"Your (?:ChatGPT|OpenAI) code is (\d{6})",
    ]
    for p in patterns:
        m = re.search(p, text, re.I)
        if m:
            return m.group(1)
    generic = re.findall(r"\b(\d{6})\b", text)
    if generic:
        return generic[-1]
    return None


# ---------------------------------------------------------------------------
# OAuth
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class OAuthStart:
    auth_url: str
    state: str
    code_verifier: str
    redirect_uri: str


def generate_oauth_url(
    *,
    redirect_uri: str = DEFAULT_REDIRECT_URI,
    scope: str = DEFAULT_SCOPE,
) -> OAuthStart:
    state = _random_state()
    code_verifier = _pkce_verifier()
    code_challenge = _sha256_b64url_no_pad(code_verifier)
    params = {
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": scope,
        "state": state,
        "prompt": "login",
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
    }
    return OAuthStart(
        auth_url=f"{AUTH_URL}?{urllib.parse.urlencode(params)}",
        state=state,
        code_verifier=code_verifier,
        redirect_uri=redirect_uri,
    )


def submit_callback_url(
    *,
    callback_url: str,
    expected_state: str,
    code_verifier: str,
    redirect_uri: str = DEFAULT_REDIRECT_URI,
    proxies: Any = None,
) -> str:
    cb = _parse_callback_url(callback_url)
    if cb["error"]:
        raise RuntimeError(f"oauth error: {cb['error']}: {cb['error_description']}".strip())
    if not cb["code"]:
        raise ValueError("callback url missing ?code=")
    if not cb["state"]:
        raise ValueError("callback url missing ?state=")
    if cb["state"] != expected_state:
        raise ValueError("state mismatch")

    resp = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "client_id": CLIENT_ID,
            "code": cb["code"],
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
        },
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        proxies=proxies,
        verify=_ssl_verify(),
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"token exchange failed: {resp.status_code}: {resp.text}")

    token_resp = resp.json()
    access_token = (token_resp.get("access_token") or "").strip()
    refresh_token = (token_resp.get("refresh_token") or "").strip()
    id_token = (token_resp.get("id_token") or "").strip()
    expires_in = _to_int(token_resp.get("expires_in"))

    claims = _jwt_claims_no_verify(id_token)
    email = str(claims.get("email") or "").strip()
    auth_claims = claims.get("https://api.openai.com/auth") or {}
    account_id = str(auth_claims.get("chatgpt_account_id") or "").strip()

    now = int(time.time())
    now_rfc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    expired_rfc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + max(expires_in, 0)))

    config_obj = {
        "id_token": id_token,
        "client_id": CLIENT_ID,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "account_id": account_id,
        "last_refresh": now_rfc,
        "email": email,
        "type": "codex",
        "expired": expired_rfc,
    }
    return json.dumps(config_obj, ensure_ascii=False, separators=(",", ":"))


def _follow_redirect_chain(
    session: curl_requests.Session,
    start_url: str,
    proxies: Any = None,
    max_redirects: int = 12,
) -> Tuple[Any, str]:
    current_url = start_url
    response = None
    for _ in range(max_redirects):
        try:
            response = session.get(
                current_url,
                allow_redirects=False,
                proxies=proxies,
                verify=_ssl_verify(),
                timeout=15,
            )
            if response.status_code not in (301, 302, 303, 307, 308):
                return response, current_url
            loc = response.headers.get("Location", "")
            if not loc:
                return response, current_url
            current_url = urllib.parse.urljoin(current_url, loc)
            if "code=" in current_url and "state=" in current_url:
                return None, current_url
        except Exception:
            return None, current_url
    return response, current_url


# ---------------------------------------------------------------------------
# Email Providers (simplified)
# ---------------------------------------------------------------------------

class BaseEmailProvider:
    """Abstract email provider interface."""

    def create_email(self, proxies: Any = None) -> Tuple[Optional[str], Optional[str]]:
        """Return (email, token_or_id)."""
        raise NotImplementedError

    def get_otp(self, email: str, token: str, proxies: Any = None,
                processed_ids: Optional[set] = None) -> Optional[str]:
        """Return 6-digit OTP or None."""
        raise NotImplementedError


@dataclass
class CloudflareTempEmailConfig:
    api_token: str = ""
    account_id: str = ""
    zone_id: str = ""
    domain: str = ""


class CloudflareTempEmailProvider(BaseEmailProvider):
    """Cloudflare Temp Email (requires external service or worker)."""

    def __init__(self, config: CloudflareTempEmailConfig):
        self.cfg = config

    def create_email(self, proxies: Any = None) -> Tuple[Optional[str], Optional[str]]:
        # Simplified: this requires a Cloudflare Worker or API endpoint
        # Placeholder for user to fill in their actual API
        print("[WARN] Cloudflare temp email provider is a stub — implement your API here")
        return None, None

    def get_otp(self, email: str, token: str, proxies: Any = None,
                processed_ids: Optional[set] = None) -> Optional[str]:
        return None


@dataclass
class MailCurlConfig:
    api_base: str = ""
    api_key: str = ""


class MailCurlProvider(BaseEmailProvider):
    """Generic mail-curl compatible API."""

    def __init__(self, config: MailCurlConfig):
        self.cfg = config

    def create_email(self, proxies: Any = None) -> Tuple[Optional[str], Optional[str]]:
        try:
            url = f"{self.cfg.api_base}/api/remail?key={self.cfg.api_key}"
            res = requests.post(url, proxies=proxies, verify=_ssl_verify(), timeout=15)
            data = res.json()
            if data.get("email") and data.get("id"):
                return data["email"], data["id"]
        except Exception as e:
            print(f"[ERROR] mail-curl create email failed: {e}")
        return None, None

    def get_otp(self, email: str, token: str, proxies: Any = None,
                processed_ids: Optional[set] = None) -> Optional[str]:
        if processed_ids is None:
            processed_ids = set()
        for attempt in range(20):
            try:
                inbox_url = f"{self.cfg.api_base}/api/inbox?key={self.cfg.api_key}&mailbox_id={token}"
                res = requests.get(inbox_url, proxies=proxies, verify=_ssl_verify(), timeout=10)
                if res.status_code == 200:
                    for mail_item in (res.json() or []):
                        m_id = mail_item.get("mail_id")
                        s_name = mail_item.get("sender_name", "").lower()
                        if m_id and m_id not in processed_ids and "openai" in s_name:
                            detail_res = requests.get(
                                f"{self.cfg.api_base}/api/mail?key={self.cfg.api_key}&id={m_id}",
                                proxies=proxies, verify=_ssl_verify(), timeout=10,
                            )
                            if detail_res.status_code == 200:
                                d = detail_res.json()
                                body = f"{d.get('subject', '')}\n{d.get('content', '')}\n{d.get('html', '')}"
                                code = _extract_otp_code(body)
                                if code:
                                    processed_ids.add(m_id)
                                    print(f"[SUCCESS] mail-curl OTP: {code}")
                                    return code
            except Exception as e:
                print(f"[WARN] mail-curl poll error: {e}")
            time.sleep(3)
        return None


@dataclass
class DuckMailConfig:
    api_url: str = "https://api.duckmail.com"
    domain: str = ""
    api_token: str = ""
    cookie: str = ""


class DuckMailProvider(BaseEmailProvider):
    """DuckMail provider."""

    def __init__(self, config: DuckMailConfig):
        self.cfg = config
        self._mode = "custom_api" if config.api_token else "cookie"

    def create_email(self, proxies: Any = None) -> Tuple[Optional[str], Optional[str]]:
        try:
            if self._mode == "custom_api":
                res = requests.post(
                    f"{self.cfg.api_url}/api/v1/mailboxes",
                    headers={"Authorization": f"Bearer {self.cfg.api_token}"},
                    proxies=proxies, verify=_ssl_verify(), timeout=15,
                )
                data = res.json()
                email = data.get("email") or data.get("address")
                token = data.get("token") or data.get("id") or email
                if email:
                    return email, token
            else:
                # Cookie-based mode — requires actual implementation
                pass
        except Exception as e:
            print(f"[ERROR] DuckMail create failed: {e}")
        return None, None

    def get_otp(self, email: str, token: str, proxies: Any = None,
                processed_ids: Optional[set] = None) -> Optional[str]:
        if processed_ids is None:
            processed_ids = set()
        for attempt in range(20):
            try:
                if self._mode == "custom_api":
                    res = requests.get(
                        f"{self.cfg.api_url}/api/v1/mailboxes/{token}/messages",
                        headers={"Authorization": f"Bearer {self.cfg.api_token}"},
                        proxies=proxies, verify=_ssl_verify(), timeout=10,
                    )
                    for msg in (res.json() or []):
                        m_id = msg.get("id")
                        if m_id in processed_ids:
                            continue
                        sender = str(msg.get("from", "")).lower()
                        subject = str(msg.get("subject", ""))
                        if "openai" in sender or "openai" in subject.lower():
                            body = msg.get("body", "") or msg.get("text", "")
                            code = _extract_otp_code(body)
                            if code:
                                processed_ids.add(m_id)
                                print(f"[SUCCESS] DuckMail OTP: {code}")
                                return code
            except Exception as e:
                print(f"[WARN] DuckMail poll error: {e}")
            time.sleep(3)
        return None


def create_email_provider(config_dict: dict) -> BaseEmailProvider:
    mode = config_dict.get("mode", "mail_curl")
    if mode == "mail_curl":
        return MailCurlProvider(MailCurlConfig(
            api_base=config_dict.get("api_base", ""),
            api_key=config_dict.get("api_key", ""),
        ))
    elif mode == "duckmail":
        return DuckMailProvider(DuckMailConfig(
            api_url=config_dict.get("api_url", "https://api.duckmail.com"),
            domain=config_dict.get("domain", ""),
            api_token=config_dict.get("api_token", ""),
            cookie=config_dict.get("cookie", ""),
        ))
    elif mode == "cloudflare":
        return CloudflareTempEmailProvider(CloudflareTempEmailConfig(
            api_token=config_dict.get("api_token", ""),
            account_id=config_dict.get("account_id", ""),
            zone_id=config_dict.get("zone_id", ""),
            domain=config_dict.get("domain", ""),
        ))
    else:
        raise ValueError(f"Unknown email provider mode: {mode}")


# ---------------------------------------------------------------------------
# Hub Pusher
# ---------------------------------------------------------------------------

class HubPusher:
    def __init__(self, hub_url: str, admin_password: str, api_key: str = ""):
        self.hub_url = hub_url.rstrip("/")
        self.password = admin_password
        self.api_key = api_key
        self._token: Optional[str] = None
        self._token_acquired_at: float = 0.0

    def _login(self) -> Optional[str]:
        if self._token and (time.time() - self._token_acquired_at) < 3600:
            return self._token
        try:
            resp = requests.post(
                f"{self.hub_url}/admin/login",
                json={"password": self.password},
                timeout=10,
            )
            data = resp.json()
            if data.get("status") == "success":
                self._token = data.get("token")
                self._token_acquired_at = time.time()
                return self._token
        except Exception as e:
            print(f"[WARN] Hub login failed: {e}")
        return None

    def push(self, email: str, password: str, token_data: dict) -> bool:
        token = self._login()
        if not token:
            return False
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        payload = {
            "email": email,
            "password": password,
            "access_token": token_data.get("access_token", ""),
            "refresh_token": token_data.get("refresh_token", ""),
            "id_token": token_data.get("id_token", ""),
            "account_id": token_data.get("account_id", ""),
        }
        try:
            resp = requests.post(
                f"{self.hub_url}/admin/accounts",
                headers=headers,
                json=payload,
                timeout=30,
            )
            if resp.status_code == 200:
                print(f"[SUCCESS] Pushed {email} to Hub")
                return True
            if resp.status_code == 409:
                print(f"[INFO] {email} already in Hub")
                return True
            print(f"[WARN] Hub push failed: {resp.status_code} {resp.text[:200]}")
        except Exception as e:
            print(f"[WARN] Hub push error: {e}")
        return False


# ---------------------------------------------------------------------------
# Registration Engine
# ---------------------------------------------------------------------------

@dataclass
class RegStats:
    success: int = 0
    failed: int = 0
    pwd_blocked: int = 0
    phone_verify: int = 0


class RegistrationEngine:
    def __init__(
        self,
        email_provider: BaseEmailProvider,
        proxy: Optional[str] = None,
        hub_pusher: Optional[HubPusher] = None,
        output_file: str = "accounts.jsonl",
    ):
        self.email_provider = email_provider
        self.proxy = proxy
        self.proxies = self._build_proxies(proxy)
        self.hub = hub_pusher
        self.output_file = output_file
        self.stats = RegStats()
        self._processed_ids: set = set()
        self._sessions: list = []

    def _build_proxies(self, proxy: Optional[str]) -> Optional[dict]:
        if not proxy:
            return None
        if proxy.startswith("socks5://"):
            proxy = proxy.replace("socks5://", "socks5h://")
        return {"http": proxy, "https": proxy}

    def _create_session(self) -> curl_requests.Session:
        s = curl_requests.Session(proxies=self.proxies, impersonate="chrome110")
        s.headers.update({"Connection": "close"})
        s.timeout = 30
        self._sessions.append(s)
        return s

    def _close_sessions(self) -> None:
        for s in self._sessions:
            try:
                s.close()
            except Exception:
                pass
        self._sessions.clear()

    def _post(self, session: curl_requests.Session, url: str, headers: dict,
              json_body: Any = None, timeout: int = 30) -> curl_requests.Response:
        last_err: Optional[Exception] = None
        for attempt in range(3):
            try:
                return session.post(
                    url, headers=headers, json=json_body,
                    proxies=self.proxies, verify=_ssl_verify(),
                    timeout=timeout, allow_redirects=False,
                )
            except Exception as exc:
                last_err = exc
                if attempt >= 2:
                    break
                time.sleep(2 * (attempt + 1))
        raise last_err or RuntimeError("Request failed")

    def _save_account(self, email: str, password: str, token_data: str) -> None:
        line = json.dumps({
            "email": email,
            "password": password,
            "token_data": json.loads(token_data),
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }, ensure_ascii=False)
        with open(self.output_file, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        print(f"[INFO] Saved to {self.output_file}")

    def _push_to_hub(self, email: str, password: str, token_data: dict) -> None:
        if self.hub:
            self.hub.push(email, password, token_data)

    def register_one(self) -> bool:
        if os.getenv("SKIP_NET_CHECK", "0").lower() not in ("1", "true", "yes", "on"):
            try:
                r = requests.get(
                    "https://cloudflare.com/cdn-cgi/trace",
                    proxies=self.proxies, timeout=15,
                )
                loc_match = re.search(r"^loc=(.+)$", r.text, re.MULTILINE)
                loc = loc_match.group(1) if loc_match else "UNKNOWN"
                if loc in ("CN", "HK"):
                    print(f"[ERROR] Proxy blocked region: {loc}")
                    self.stats.failed += 1
                    return False
                print(f"[INFO] Proxy alive: {loc}")
            except Exception as e:
                print(f"[ERROR] Proxy check failed: {e}")
                self.stats.failed += 1
                return False

        # 1. Get email
        email, email_token = self.email_provider.create_email(self.proxies)
        if not email:
            print("[ERROR] Failed to obtain temporary email")
            self.stats.failed += 1
            return False

        password = _generate_password()
        print(f"[INFO] Registering {email} with password {password[:4]}****")

        self._processed_ids.clear()

        for attempt in range(2):
            try:
                result = self._attempt(email, email_token, password)
                if result:
                    self.stats.success += 1
                    return True
            except Exception as e:
                print(f"[ERROR] Attempt {attempt + 1} failed: {e}")
                if attempt < 1:
                    time.sleep(2)
        self.stats.failed += 1
        return False

    def _attempt(self, email: str, email_token: str, password: str) -> bool:
        s_reg = self._create_session()
        oauth_reg = generate_oauth_url()

        # Init auth
        did, current_ua = init_auth(
            session=s_reg,
            email=email,
            masked_email=email,
            proxies=self.proxies,
            verify=_ssl_verify(),
        )
        if not did or not current_ua:
            print("[WARN] Failed to get oai-did — node may be flagged")

        reg_ctx: dict = {}

        print("[INFO] Computing sentinel challenge...")
        sentinel_signup = generate_payload(
            did=did, flow="authorize_continue", proxy=self.proxy or "",
            user_agent=current_ua, impersonate="chrome110", ctx=reg_ctx
        )
        if sentinel_signup:
            print("[INFO] Sentinel challenge passed")

        signup_headers = _oai_headers(did, {
            "Referer": "https://auth.openai.com/create-account",
            "content-type": "application/json",
        })
        if sentinel_signup:
            signup_headers["openai-sentinel-token"] = sentinel_signup

        signup_resp = self._post(
            s_reg,
            "https://auth.openai.com/api/accounts/authorize/continue",
            signup_headers,
            json_body={"username": {"value": email, "kind": "email"}, "screen_hint": "signup"},
        )

        if signup_resp.status_code == 403:
            print("[WARN] Registration hit 403 — retrying with new session")
            return False
        if signup_resp.status_code != 200:
            print(f"[ERROR] Signup authorize failed: {signup_resp.status_code}")
            return False

        signup_json = signup_resp.json() or {}
        continue_url = signup_json.get("continue_url", "")

        # Takeover path (existing account)
        if "log-in" in continue_url:
            print(f"[WARN] Email exists — takeover not supported in standalone mode")
            return False

        # Password setup
        sentinel_pwd = generate_payload(
            did=did, flow="username_password_create", proxy=self.proxy or "",
            user_agent=current_ua, impersonate="chrome110", ctx=reg_ctx
        )
        pwd_headers = _oai_headers(did, {
            "Referer": "https://auth.openai.com/create-account/password",
            "content-type": "application/json",
        })
        if sentinel_pwd:
            pwd_headers["openai-sentinel-token"] = sentinel_pwd

        pwd_resp = self._post(
            s_reg,
            "https://auth.openai.com/api/accounts/user/register",
            pwd_headers,
            json_body={"password": password, "username": email},
        )

        if pwd_resp.status_code != 200:
            err = pwd_resp.json() or {}
            err_code = err.get("error", {}).get("code")
            err_msg = err.get("error", {}).get("message", "")
            if err_code is None and "Failed to create account" in err_msg:
                print("[ERROR] Shadow ban detected — IP/domain likely blacklisted")
                self.stats.pwd_blocked += 1
                return False
            print(f"[ERROR] Password setup blocked: {pwd_resp.status_code}")
            self.stats.pwd_blocked += 1
            return False

        # OTP check
        reg_json = pwd_resp.json() or {}
        need_otp = (
            "verify" in reg_json.get("continue_url", "")
            or "otp" in (reg_json.get("page") or {}).get("type", "")
        )

        if need_otp:
            print("[INFO] Waiting for OTP...")
            code = self.email_provider.get_otp(
                email, email_token, self.proxies, self._processed_ids
            )
            if not code:
                print("[ERROR] OTP not received")
                return False

            sentinel_v = generate_payload(
                did=did, flow="authorize_continue", proxy=self.proxy or "",
                user_agent=current_ua, impersonate="chrome110", ctx=reg_ctx
            )
            vh = _oai_headers(did, {
                "Referer": "https://auth.openai.com/create-account/password",
                "content-type": "application/json",
            })
            if sentinel_v:
                vh["openai-sentinel-token"] = sentinel_v

            val_resp = self._post(
                s_reg,
                "https://auth.openai.com/api/accounts/email-otp/validate",
                vh,
                json_body={"code": code},
            )
            if val_resp.status_code != 200:
                print(f"[ERROR] OTP validation failed: {val_resp.status_code}")
                return False
            val_json = val_resp.json() or {}
            code_account_url = val_json.get("continue_url", "")
            if "/add-phone" in code_account_url:
                print("[WARN] Phone verification required")
                self.stats.phone_verify += 1
                return False

        # Create profile
        user_info = _generate_random_user_info()
        print(f"[INFO] Creating profile: {user_info['name']}, {user_info['birthdate']}")

        sentinel_create = generate_payload(
            did=did, flow="create_account", proxy=self.proxy or "",
            user_agent=current_ua, impersonate="chrome110", ctx=reg_ctx
        )
        create_headers = _oai_headers(did, {
            "Referer": "https://auth.openai.com/about-you",
            "content-type": "application/json",
        })
        if sentinel_create:
            create_headers["openai-sentinel-token"] = sentinel_create

        create_account_resp = self._post(
            s_reg,
            "https://auth.openai.com/api/accounts/create_account",
            create_headers,
            json_body=user_info,
        )

        if create_account_resp.status_code != 200:
            err_json = create_account_resp.json() or {}
            err_code = str(err_json.get("error", {}).get("code", "")).strip()
            if err_code == "identity_provider_mismatch":
                print("[ERROR] Third-party login account blocked")
                return False
            print(f"[ERROR] Account creation failed: {create_account_resp.status_code}")
            return False

        create_json = create_account_resp.json() or {}
        target_continue_url = str(create_json.get("continue_url") or "").strip()

        wait_time = random.randint(20, 45)
        print(f"[INFO] Account approved, waiting {wait_time}s before token extraction...")
        time.sleep(wait_time)

        # Token extraction
        if target_continue_url:
            hint_url = target_continue_url if target_continue_url.startswith("http") else f"https://auth.openai.com{target_continue_url}"
            try:
                _, current_url = _follow_redirect_chain(s_reg, hint_url, self.proxies)
                if "code=" in current_url and "state=" in current_url:
                    token_json = submit_callback_url(
                        callback_url=current_url,
                        expected_state=oauth_reg.state,
                        code_verifier=oauth_reg.code_verifier,
                        proxies=self.proxies,
                    )
                    self._finalize(email, password, token_json)
                    return True
            except Exception as e:
                print(f"[WARN] Token extraction via redirect failed: {e}")
                current_url = hint_url
        else:
            current_url = "https://auth.openai.com/sign-in-with-chatgpt/codex/consent"

        # Workspace path
        auth_cookie = s_reg.cookies.get("oai-client-auth-session") or ""
        workspaces = _parse_workspace_from_auth_cookie(auth_cookie)
        if workspaces:
            print("[INFO] Workspace detected, extracting token...")
            workspace_id = str((workspaces[0] or {}).get("id") or "").strip()
            if workspace_id:
                select_resp = self._post(
                    s_reg,
                    "https://auth.openai.com/api/accounts/workspace/select",
                    _oai_headers(did, {"Referer": current_url, "content-type": "application/json"}),
                    json_body={"workspace_id": workspace_id},
                )
                if select_resp.status_code == 200:
                    select_data = select_resp.json() or {}
                    next_url = str(select_data.get("continue_url") or "").strip()
                    if next_url:
                        _, final_url = _follow_redirect_chain(s_reg, next_url, self.proxies)
                        if "code=" in final_url and "state=" in final_url:
                            print("[INFO] Token extracted via workspace!")
                            token_json = submit_callback_url(
                                callback_url=final_url,
                                expected_state=oauth_reg.state,
                                code_verifier=oauth_reg.code_verifier,
                                proxies=self.proxies,
                            )
                            self._finalize(email, password, token_json)
                            return True

        # Silent OAuth fallback
        print("[INFO] Attempting silent OAuth token extraction...")
        for oauth_attempt in range(2):
            if oauth_attempt == 1:
                print("[WARN] Retrying silent OAuth...")
            s_log = self._create_session()
            oauth_log = generate_oauth_url()

            _, current_url = _follow_redirect_chain(s_log, oauth_log.auth_url, self.proxies)
            if "code=" in current_url and "state=" in current_url:
                token_json = submit_callback_url(
                    callback_url=current_url,
                    code_verifier=oauth_log.code_verifier,
                    redirect_uri=oauth_log.redirect_uri,
                    expected_state=oauth_log.state,
                    proxies=self.proxies,
                )
                self._finalize(email, password, token_json)
                return True

            log_did = s_log.cookies.get("oai-did") or did
            log_ctx = reg_ctx.copy()
            log_ctx["session_id"] = os.urandom(16).hex()
            log_ctx["time_origin"] = float(int(time.time() * 1000) - random.randint(20000, 300000))

            sentinel_log = generate_payload(
                did=log_did, flow="authorize_continue", proxy=self.proxy or "",
                user_agent=current_ua, impersonate="chrome110", ctx=log_ctx
            )
            log_start_headers = _oai_headers(log_did, {
                "Referer": current_url,
                "content-type": "application/json",
            })
            if sentinel_log:
                log_start_headers["openai-sentinel-token"] = sentinel_log

            login_start_resp = self._post(
                s_log,
                "https://auth.openai.com/api/accounts/authorize/continue",
                log_start_headers,
                json_body={"username": {"value": email, "kind": "email"}},
            )
            if login_start_resp.status_code != 200:
                print(f"[ERROR] Silent login step 1 failed: {login_start_resp.status_code}")
                return False

            # Password verify
            login_json = login_start_resp.json() or {}
            login_continue = login_json.get("continue_url", "")
            if "password" not in login_continue:
                print(f"[WARN] Unexpected login flow: {login_continue}")
                return False

            sentinel_login = generate_payload(
                did=log_did, flow="username_password", proxy=self.proxy or "",
                user_agent=current_ua, impersonate="chrome110", ctx=log_ctx
            )
            login_pwd_headers = _oai_headers(log_did, {
                "Referer": "https://auth.openai.com/log-in/password",
                "content-type": "application/json",
            })
            if sentinel_login:
                login_pwd_headers["openai-sentinel-token"] = sentinel_login

            login_pwd_resp = self._post(
                s_log,
                "https://auth.openai.com/api/accounts/user/login",
                login_pwd_headers,
                json_body={"password": password, "username": email},
            )
            if login_pwd_resp.status_code != 200:
                print(f"[ERROR] Silent login password verify failed: {login_pwd_resp.status_code}")
                return False

            # Follow redirect to get code
            login_pwd_json = login_pwd_resp.json() or {}
            next_continue = login_pwd_json.get("continue_url", "")
            if next_continue:
                _, final_url = _follow_redirect_chain(s_log, next_continue, self.proxies)
                if "code=" in final_url and "state=" in final_url:
                    token_json = submit_callback_url(
                        callback_url=final_url,
                        expected_state=oauth_log.state,
                        code_verifier=oauth_log.code_verifier,
                        proxies=self.proxies,
                    )
                    self._finalize(email, password, token_json)
                    return True

        return False

    def _finalize(self, email: str, password: str, token_json: str) -> None:
        print(f"[SUCCESS] Token extracted for {email}")
        token_data = json.loads(token_json)
        self._save_account(email, password, token_json)
        self._push_to_hub(email, password, token_data)

    def run(self, count: int = 1, delay_min: int = 5, delay_max: int = 30) -> None:
        print("=" * 50)
        print(f"Starting registration: target={count}")
        print("=" * 50)
        for i in range(count):
            print(f"\n--- Registration {i + 1}/{count} ---")
            ok = self.register_one()
            if i < count - 1:
                sleep_time = random.randint(delay_min, delay_max)
                print(f"[INFO] Sleeping {sleep_time}s before next...")
                time.sleep(sleep_time)
        print("\n" + "=" * 50)
        print(f"Done. Success: {self.stats.success}, Failed: {self.stats.failed}, "
              f"PwdBlocked: {self.stats.pwd_blocked}, PhoneReq: {self.stats.phone_verify}")
        print("=" * 50)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="OpenAI Account Auto-Registration")
    parser.add_argument("--proxy", default="", help="Proxy URL (socks5/http)")
    parser.add_argument("--count", type=int, default=1, help="Number of accounts to register")
    parser.add_argument("--delay-min", type=int, default=5, help="Min delay between registrations")
    parser.add_argument("--delay-max", type=int, default=30, help="Max delay between registrations")
    parser.add_argument("--output", default="accounts.jsonl", help="Output file (JSONL)")
    parser.add_argument("--hub-url", default="", help="Codex Hub URL for auto-push")
    parser.add_argument("--hub-password", default="", help="Codex Hub admin password")
    parser.add_argument("--hub-api-key", default="", help="Codex Hub gateway API key")
    parser.add_argument("--email-mode", default="mail_curl", choices=["mail_curl", "duckmail", "cloudflare"])
    parser.add_argument("--email-api-base", default="", help="Email API base URL")
    parser.add_argument("--email-api-key", default="", help="Email API key")
    parser.add_argument("--email-api-token", default="", help="Email API token (for duckmail)")
    parser.add_argument("--config", default="", help="JSON config file for advanced settings")
    args = parser.parse_args()

    # Load config file if provided
    config = {}
    if args.config and Path(args.config).exists():
        with open(args.config, "r", encoding="utf-8") as f:
            config = json.load(f)

    email_cfg = {
        "mode": config.get("email_mode", args.email_mode),
        "api_base": config.get("email_api_base", args.email_api_base),
        "api_key": config.get("email_api_key", args.email_api_key),
        "api_token": config.get("email_api_token", args.email_api_token),
    }
    provider = create_email_provider(email_cfg)

    hub = None
    hub_url = config.get("hub_url", args.hub_url)
    hub_password = config.get("hub_password", args.hub_password)
    if hub_url and hub_password:
        hub = HubPusher(
            hub_url=hub_url,
            admin_password=hub_password,
            api_key=config.get("hub_api_key", args.hub_api_key),
        )

    engine = RegistrationEngine(
        email_provider=provider,
        proxy=config.get("proxy", args.proxy) or None,
        hub_pusher=hub,
        output_file=config.get("output", args.output),
    )

    engine.run(
        count=config.get("count", args.count),
        delay_min=config.get("delay_min", args.delay_min),
        delay_max=config.get("delay_max", args.delay_max),
    )


if __name__ == "__main__":
    main()
