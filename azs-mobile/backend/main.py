import ipaddress
import json
import hashlib
import html
import logging
import os
import re
import secrets
import smtplib
import sqlite3
import time
import urllib.error
import urllib.request
import uuid
from calendar import monthrange
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from functools import lru_cache
from pathlib import Path
from typing import Optional

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - optional local convenience
    load_dotenv = None

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from backend.fuel_stock import (
    FuelStockImportError,
    FuelStockImportPayload,
    FuelStockImportResponse,
    FuelStockStationResponse,
    fuel_stock_health,
    get_station_fuel_stock,
    init_fuel_stock_db,
    replace_fuel_stock_snapshot,
)

APP_DIR = Path(__file__).resolve().parent
PROJECT_DIR = APP_DIR.parent
DATA_DIR = PROJECT_DIR / "data"

if load_dotenv:
    load_dotenv(PROJECT_DIR / ".env")
    load_dotenv(PROJECT_DIR / ".env.local", override=True)

AUTH_DB_PATH = DATA_DIR / "auth.sqlite3"
KPI_DB_PATH = DATA_DIR / "kpi_metrics.sqlite3"
AUTH_ALLOWLIST_PATH = DATA_DIR / "auth_allowlist.json"
PRIVATE_STATIONS_PATH = DATA_DIR / "stations.json"
STATIONS_PATH = PROJECT_DIR / "public" / "stations.json"
STATIONS_SAMPLE_PATH = PROJECT_DIR / "public" / "stations.sample.json"
STAFF_RECOMMENDATIONS_PATH = PROJECT_DIR / "public" / "staff_recommendations.json"
PRIVATE_STAFF_RECOMMENDATIONS_PATH = PROJECT_DIR / "data" / "staff_recommendations.json"
SQL_TEMPLATE = APP_DIR / "sql" / "station_kpis.sql"
STAFF_SQL_TEMPLATE = APP_DIR / "sql" / "station_staff.sql"
ANALYTICS_SQL_TEMPLATE = APP_DIR / "sql" / "analytics_overview.sql"
SIMILAR_SQL_TEMPLATE = APP_DIR / "sql" / "station_similar.sql"
COMPARE_SQL_TEMPLATE = APP_DIR / "sql" / "analytics_compare.sql"
DEFAULT_CORS_ORIGINS = "http://localhost:5173,http://localhost:5174,http://127.0.0.1:5173,http://127.0.0.1:5174"
DEFAULT_ALLOWED_EMAIL_DOMAINS = "lukoil.com,lukoil.ru,licard.com,spb.lukoil.com,ynp.lukoil.com"
SESSION_COOKIE_NAME = "azs_session"
SESSION_TTL_SECONDS = int(os.getenv("AUTH_SESSION_TTL_SECONDS", str(60 * 60 * 12)))
PASSWORD_ITERATIONS = int(os.getenv("AUTH_PASSWORD_ITERATIONS", "260000"))
EMAIL_CODE_TTL_SECONDS = int(os.getenv("AUTH_EMAIL_CODE_TTL_SECONDS", str(10 * 60)))
EMAIL_CODE_LENGTH = int(os.getenv("AUTH_EMAIL_CODE_LENGTH", "6"))
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
AUTH_RATE_LIMIT: dict[str, list[float]] = {}
# How many trusted proxy hops sit in front of this server.
# 0  = no proxy; ignore X-Forwarded-For entirely and use the TCP peer IP.
# N>0 = trust the N rightmost entries in X-Forwarded-For (added by trusted proxies);
#       use the entry just to the left of those as the real client IP.
# Incorrect values allow IP-spoofing attacks on rate-limit counters.
TRUSTED_PROXY_DEPTH = int(os.getenv("TRUSTED_PROXY_DEPTH", "0"))
_RATE_LIMIT_CLEANUP_COUNTER: int = 0

logger = logging.getLogger("azs-api")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")


class KpiMetric(BaseModel):
    id: str
    label: str
    value: float
    unit: str
    momPct: Optional[float] = None
    yoyPct: Optional[float] = None


class StationKpiResponse(BaseModel):
    ksss: str
    period: str = Field(pattern=r"^\d{4}-\d{2}$")
    source: str
    updatedAt: str
    metrics: list[KpiMetric]


class StaffDay(BaseModel):
    date: str
    label: str
    day: float
    night: float


class StationStaffResponse(BaseModel):
    ksss: str
    period: str = Field(pattern=r"^\d{4}-\d{2}$")
    source: str
    updatedAt: str
    staffTotal: float
    today: StaffDay
    days: list[StaffDay]


class AnalyticsOverviewRow(BaseModel):
    id: str
    label: str
    count: int
    metrics: list[KpiMetric]


class AnalyticsOverviewResponse(BaseModel):
    period: str = Field(pattern=r"^\d{4}-\d{2}$")
    groupBy: str
    source: str
    updatedAt: str
    rows: list[AnalyticsOverviewRow]


class SimilarStation(BaseModel):
    ksss: str
    stationNumber: str
    name: str
    subject: str
    score: int
    reasons: list[str]
    metrics: list[KpiMetric]


class SimilarStationsResponse(BaseModel):
    ksss: str
    period: str = Field(pattern=r"^\d{4}-\d{2}$")
    source: str
    updatedAt: str
    items: list[SimilarStation]


class CompareStation(BaseModel):
    ksss: str
    stationNumber: str
    name: str
    subject: str
    regionalManager: str
    territoryManager: str
    format: str
    location: str
    trkCount: Optional[float] = None
    postsCount: Optional[float] = None
    staffTotal: float
    metrics: list[KpiMetric]


class CompareResponse(BaseModel):
    period: str = Field(pattern=r"^\d{4}-\d{2}$")
    source: str
    updatedAt: str
    items: list[CompareStation]


class AuthCredentials(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=8, max_length=256)
    name: str = Field(default="", max_length=120)


class EmailVerificationRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    code: str = Field(min_length=4, max_length=12)


class EmailResendRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)


class PasswordResetRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)


class PasswordResetConfirmRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    code: str = Field(min_length=4, max_length=12)
    password: str = Field(min_length=8, max_length=256)


class AuthUser(BaseModel):
    id: int
    email: str
    name: str = ""


class AuthResponse(BaseModel):
    user: AuthUser


class AuthFlowResponse(BaseModel):
    user: Optional[AuthUser] = None
    verificationRequired: bool = False
    email: str = ""
    message: str = ""
    devCode: str = ""


class AuthPolicyResponse(BaseModel):
    allowedDomains: list[str]
    allowlistEnabled: bool
    emailVerificationRequired: bool = True


class KpiPeriodsResponse(BaseModel):
    source: str
    updatedAt: str
    periods: list[str]


class KpiImportRecord(BaseModel):
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    ksss: str = Field(min_length=1, max_length=32)
    revenue: float
    revenueNtu: Optional[float] = None
    revenue_ntu: Optional[float] = None
    fuelVolume: Optional[float] = None
    fuel_volume: Optional[float] = None
    checks: float
    checksNtu: Optional[float] = None
    checks_ntu: Optional[float] = None
    avgCheck: Optional[float] = None
    avg_check: Optional[float] = None
    updatedAt: Optional[str] = Field(default=None, max_length=80)


class KpiImportPayload(BaseModel):
    source: str = Field(default="dwh-sync", max_length=120)
    period: Optional[str] = Field(default=None, pattern=r"^\d{4}-\d{2}$")
    replacePeriod: bool = False
    records: list[KpiImportRecord] = Field(min_length=1)


class KpiImportResponse(BaseModel):
    ok: bool
    imported: int
    period: str
    periods: list[str]
    updatedAt: str


app = FastAPI(title="AZS KPI API", version="0.4.0")
app.add_middleware(GZipMiddleware, minimum_size=1000)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in os.getenv("CORS_ORIGINS", DEFAULT_CORS_ORIGINS).split(",") if origin.strip()],
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


@app.on_event("startup")
def startup():
    init_auth_db()
    init_kpi_db()
    init_fuel_stock_db()
    _sec = logging.getLogger("azs.security")
    if not auth_enabled():
        _sec.critical(
            "AUTH_DISABLED is set — ALL authentication checks are bypassed. "
            "This MUST NEVER be used in production."
        )
    if email_dev_mode():
        _sec.warning(
            "AUTH_EMAIL_DEV_MODE is active — OTP codes are returned in API responses. "
            "This MUST NEVER be used in production."
        )


@app.middleware("http")
async def add_security_headers(request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Permissions-Policy", "geolocation=(self), camera=(), microphone=()")
    # API endpoints return JSON only — forbid all executable content at the CSP level.
    # The frontend HTML/JS is served by the static server (Vite / nginx) and must set
    # its own, more permissive CSP that allows Yandex Maps scripts.
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'none'; frame-ancestors 'none'",
    )
    # Use request_is_https() instead of request.url.scheme so the HSTS header is
    # also sent when the app sits behind a TLS-terminating reverse proxy.
    if request_is_https(request):
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response


@app.middleware("http")
async def log_requests(request: Request, call_next):
    request_id = str(uuid.uuid4())[:8]
    start = time.perf_counter()
    response = await call_next(request)
    duration_ms = round((time.perf_counter() - start) * 1000)
    logger.info(
        "[%s] %s %s -> %s (%dms)",
        request_id,
        request.method,
        request.url.path,
        response.status_code,
        duration_ms,
    )
    response.headers["X-Request-ID"] = request_id
    return response


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error("Unhandled exception: %s: %s", type(exc).__name__, exc, exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
    )


def current_period() -> str:
    return datetime.now().strftime("%Y-%m")


def data_mode() -> str:
    return os.getenv("APP_DATA_MODE") or os.getenv("KPI_DATA_MODE", "mock")


def auth_enabled() -> bool:
    return os.getenv("AUTH_DISABLED", "").lower() not in {"1", "true", "yes"}


def split_config_values(value: str) -> list[str]:
    return [item.strip().lower() for item in value.split(",") if item.strip()]


def normalize_allowed_domain(domain: str) -> str:
    return domain.strip().lower().lstrip("@. ")


def read_allowlist_file() -> dict[str, list[str]]:
    if not AUTH_ALLOWLIST_PATH.exists():
        return {"domains": [], "emails": []}
    try:
        payload = json.loads(AUTH_ALLOWLIST_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail="Invalid auth allowlist JSON") from exc
    return {
        "domains": [normalize_allowed_domain(str(item)) for item in payload.get("domains", []) if str(item).strip()],
        "emails": [str(item).strip().lower() for item in payload.get("emails", []) if str(item).strip()],
    }


def allowed_email_domains() -> set[str]:
    configured = [normalize_allowed_domain(item) for item in split_config_values(os.getenv("AUTH_ALLOWED_EMAIL_DOMAINS", DEFAULT_ALLOWED_EMAIL_DOMAINS))]
    return set(configured + read_allowlist_file()["domains"])


def env_allowed_emails() -> set[str]:
    return set(split_config_values(os.getenv("AUTH_ALLOWED_EMAILS", "")) + read_allowlist_file()["emails"])


def email_domain(email: str) -> str:
    return email.rsplit("@", 1)[-1].lower() if "@" in email else ""


def normalize_email(email: str) -> str:
    normalized = email.strip().lower()
    if not EMAIL_PATTERN.match(normalized):
        raise HTTPException(status_code=422, detail="Введите корректный email")
    return normalized


def db_allowed_emails(conn: sqlite3.Connection) -> set[str]:
    try:
        rows = conn.execute("SELECT email FROM email_allowlist").fetchall()
    except sqlite3.OperationalError:
        return set()
    return {str(row["email"]).strip().lower() for row in rows if str(row["email"]).strip()}


def email_is_allowed(email: str, conn: Optional[sqlite3.Connection] = None) -> bool:
    normalized = normalize_email(email)
    domain = email_domain(normalized)
    domains = {item for item in allowed_email_domains() if item}
    if normalized in env_allowed_emails():
        return True
    if conn and normalized in db_allowed_emails(conn):
        return True
    return any(domain == allowed or domain.endswith(f".{allowed}") for allowed in domains)


def corporate_access_error() -> HTTPException:
    domains = sorted(allowed_email_domains())
    suffix = f" Разрешенные домены: {', '.join(domains)}." if domains else ""
    return HTTPException(status_code=403, detail=f"Доступ разрешен только сотрудникам компании с корпоративным email.{suffix}")


def validate_password(password: str) -> str:
    if len(password) < 8:
        raise HTTPException(status_code=422, detail="Пароль должен быть не короче 8 символов")
    return password


def auth_connection():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(AUTH_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_auth_db():
    with auth_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL UNIQUE CHECK (instr(email, '@') > 1),
                name TEXT NOT NULL DEFAULT '',
                password_hash TEXT NOT NULL,
                created_at INTEGER NOT NULL
            )
            """
        )
        user_columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
        if "email_verified_at" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN email_verified_at INTEGER NOT NULL DEFAULT 0")
            conn.execute("UPDATE users SET email_verified_at = created_at WHERE email_verified_at = 0")
        if "last_login_at" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN last_login_at INTEGER NOT NULL DEFAULT 0")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_users_email ON users(email)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_users_email_verified ON users(email_verified_at)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS email_allowlist (
                email TEXT NOT NULL PRIMARY KEY CHECK (instr(email, '@') > 1),
                note TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS email_verification_codes (
                email TEXT PRIMARY KEY CHECK (instr(email, '@') > 1),
                code_hash TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                last_sent_at INTEGER NOT NULL,
                FOREIGN KEY(email) REFERENCES users(email) ON DELETE CASCADE
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_email_codes_expires ON email_verification_codes(expires_at)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS password_reset_codes (
                email TEXT PRIMARY KEY CHECK (instr(email, '@') > 1),
                code_hash TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                last_sent_at INTEGER NOT NULL,
                FOREIGN KEY(email) REFERENCES users(email) ON DELETE CASCADE
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_password_reset_codes_expires ON password_reset_codes(expires_at)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS auth_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL CHECK (instr(email, '@') > 1),
                event TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                ip TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_auth_events_email ON auth_events(email)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_auth_events_created ON auth_events(created_at)")


def kpi_connection():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(KPI_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_kpi_db():
    with kpi_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS station_kpi_daily (
                metric_date TEXT NOT NULL CHECK (length(metric_date) = 10),
                period TEXT NOT NULL CHECK (length(period) = 7),
                ksss TEXT NOT NULL,
                revenue REAL NOT NULL DEFAULT 0,
                revenue_ntu REAL,
                fuel_volume REAL NOT NULL DEFAULT 0,
                checks REAL NOT NULL DEFAULT 0,
                checks_ntu REAL,
                avg_check REAL,
                updated_at TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (metric_date, ksss)
            )
            """
        )
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(station_kpi_daily)").fetchall()}
        for column in ("revenue_ntu", "checks_ntu", "avg_check"):
            if column not in columns:
                conn.execute(f"ALTER TABLE station_kpi_daily ADD COLUMN {column} REAL")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_station_kpi_daily_period ON station_kpi_daily(period)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_station_kpi_daily_ksss_period ON station_kpi_daily(ksss, period)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_station_kpi_daily_updated ON station_kpi_daily(updated_at)")


def request_ip(request: Optional[Request] = None) -> str:
    """Return the real client IP, respecting TRUSTED_PROXY_DEPTH.

    With TRUSTED_PROXY_DEPTH=0 (default) X-Forwarded-For is ignored entirely
    and the direct TCP peer address is returned — the only spoofing-safe choice
    when there is no trusted proxy in front of this server.
    """
    if not request:
        return ""
    if TRUSTED_PROXY_DEPTH > 0:
        forwarded = request.headers.get("x-forwarded-for", "")
        ips = [ip.strip() for ip in forwarded.split(",") if ip.strip()]
        if ips:
            # Peel off the rightmost TRUSTED_PROXY_DEPTH entries (added by trusted
            # proxies) and take the first entry to the left of them.
            index = max(len(ips) - TRUSTED_PROXY_DEPTH, 0)
            candidate = ips[index]
            try:
                ipaddress.ip_address(candidate)
                return candidate
            except ValueError:
                pass
    return request.client.host if request.client else ""


def request_is_https(request: Request) -> bool:
    forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    if forwarded_proto:
        return forwarded_proto == "https"
    cf_visitor = request.headers.get("cf-visitor", "").lower()
    return request.url.scheme == "https" or '"scheme":"https"' in cf_visitor


def insert_auth_event(conn: sqlite3.Connection, email: str, event: str, reason: str = "", ip: str = ""):
    conn.execute(
        "INSERT INTO auth_events (email, event, reason, ip, created_at) VALUES (?, ?, ?, ?, ?)",
        (email[:254].lower(), event[:80], reason[:240], ip[:80], int(time.time())),
    )


def audit_auth_event(email: str, event: str, reason: str = "", request: Optional[Request] = None):
    ip = ""
    if request:
        ip = request_ip(request)
    with auth_connection() as conn:
        insert_auth_event(conn, email, event, reason, ip)


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def smtp_configured() -> bool:
    return bool(os.getenv("SMTP_HOST") and os.getenv("SMTP_FROM_EMAIL"))


def resend_configured() -> bool:
    return bool(os.getenv("RESEND_API_KEY") and os.getenv("RESEND_FROM_EMAIL"))


def email_configured() -> bool:
    return smtp_configured() or resend_configured()


def email_dev_mode() -> bool:
    return env_bool("AUTH_EMAIL_DEV_MODE", default=not email_configured())


def _send_via_resend(to: str, subject: str, html_content: str, text_content: str) -> None:
    from_name = os.getenv("SMTP_FROM_NAME", "Классификатор АЗС").strip()
    from_email = os.getenv("RESEND_FROM_EMAIL", "").strip()
    api_key = os.getenv("RESEND_API_KEY", "")

    if api_key.startswith("xkeysib-"):
        # Brevo API
        payload = json.dumps({
            "sender": {"name": from_name, "email": from_email},
            "to": [{"email": to}],
            "subject": subject,
            "htmlContent": html_content,
            "textContent": text_content,
        }).encode("utf-8")
        req = urllib.request.Request(
            "https://api.brevo.com/v3/smtp/email",
            data=payload,
            headers={"api-key": api_key, "Content-Type": "application/json"},
            method="POST",
        )
    else:
        # Resend API
        payload = json.dumps({
            "from": f"{from_name} <{from_email}>",
            "to": [to],
            "subject": subject,
            "html": html_content,
            "text": text_content,
        }).encode("utf-8")
        req = urllib.request.Request(
            "https://api.resend.com/emails",
            data=payload,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            method="POST",
        )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status not in (200, 201):
                raise RuntimeError(f"Email API HTTP {resp.status}")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Email API error {exc.code}: {exc.read().decode()}") from exc


def app_public_url() -> str:
    return os.getenv("APP_PUBLIC_URL", "http://localhost:5174").rstrip("/")


def generate_email_code() -> str:
    length = max(4, min(12, EMAIL_CODE_LENGTH))
    upper = 10 ** length
    return f"{secrets.randbelow(upper):0{length}d}"


def render_verification_email_html(code: str, email: str, name: str = "") -> str:
    ttl_minutes = max(1, round(EMAIL_CODE_TTL_SECONDS / 60))
    safe_code = html.escape(code)
    safe_email = html.escape(email)
    safe_name = html.escape(name.strip() or "сотрудник")
    safe_url = html.escape(app_public_url())
    year = datetime.now().year
    return f"""<!doctype html>
<html lang="ru">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width,initial-scale=1" />
    <title>Код подтверждения</title>
  </head>
  <body style="margin:0;padding:0;background:#f4f6f8;font-family:Arial,'Helvetica Neue',Helvetica,sans-serif;color:#151b24;">
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f4f6f8;padding:28px 12px;">
      <tr>
        <td align="center">
          <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:560px;background:#ffffff;border:1px solid #e2e7ee;border-radius:16px;overflow:hidden;">
            <tr>
              <td style="padding:22px 24px;background:#c91d32;color:#ffffff;">
                <div style="font-size:12px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;opacity:.86;">Корпоративный доступ</div>
                <div style="margin-top:8px;font-size:24px;line-height:1.15;font-weight:800;">Классификатор АЗС</div>
              </td>
            </tr>
            <tr>
              <td style="padding:26px 24px 10px;">
                <h1 style="margin:0;font-size:22px;line-height:1.25;color:#151b24;">Подтверждение корпоративной почты</h1>
                <p style="margin:12px 0 0;font-size:15px;line-height:1.55;color:#526071;">Здравствуйте, {safe_name}. Используйте этот код для входа в приложение. Код подтверждает доступ к корпоративному ящику <strong>{safe_email}</strong>.</p>
              </td>
            </tr>
            <tr>
              <td style="padding:12px 24px 8px;">
                <div style="border:1px solid #f1ccd1;background:#fff1f3;border-radius:14px;padding:18px;text-align:center;">
                  <div style="font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:#8f1425;">Ваш код</div>
                  <div style="margin-top:8px;font-size:38px;line-height:1;font-weight:900;letter-spacing:.18em;color:#c91d32;">{safe_code}</div>
                </div>
              </td>
            </tr>
            <tr>
              <td style="padding:12px 24px 24px;">
                <p style="margin:0;font-size:14px;line-height:1.55;color:#526071;">Код действует {ttl_minutes} минут. Если вы не запрашивали доступ, просто проигнорируйте письмо.</p>
                <p style="margin:16px 0 0;font-size:13px;line-height:1.5;color:#6f7c8d;">Открыть приложение: <a href="{safe_url}" style="color:#18579f;text-decoration:none;font-weight:700;">{safe_url}</a></p>
              </td>
            </tr>
            <tr>
              <td style="padding:16px 24px;background:#f8fafc;border-top:1px solid #e2e7ee;color:#7b8796;font-size:12px;line-height:1.45;">
                Это автоматическое письмо. © {year} Классификатор АЗС.
              </td>
            </tr>
          </table>
        </td>
      </tr>
    </table>
  </body>
</html>"""


def render_verification_email_text(code: str, email: str) -> str:
    ttl_minutes = max(1, round(EMAIL_CODE_TTL_SECONDS / 60))
    return (
        "Классификатор АЗС\n\n"
        f"Код подтверждения для {email}: {code}\n"
        f"Код действует {ttl_minutes} минут.\n\n"
        f"Открыть приложение: {app_public_url()}\n"
        "Если вы не запрашивали доступ, проигнорируйте письмо."
    )


def render_password_reset_email_html(code: str, email: str, name: str = "") -> str:
    ttl_minutes = max(1, round(EMAIL_CODE_TTL_SECONDS / 60))
    safe_code = html.escape(code)
    safe_email = html.escape(email)
    safe_name = html.escape(name.strip() or "сотрудник")
    safe_url = html.escape(app_public_url())
    year = datetime.now().year
    return f"""<!doctype html>
<html lang="ru">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width,initial-scale=1" />
    <title>Восстановление пароля</title>
  </head>
  <body style="margin:0;padding:0;background:#f4f6f8;font-family:Arial,'Helvetica Neue',Helvetica,sans-serif;color:#151b24;">
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f4f6f8;padding:28px 12px;">
      <tr>
        <td align="center">
          <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:560px;background:#ffffff;border:1px solid #e2e7ee;border-radius:16px;overflow:hidden;">
            <tr>
              <td style="padding:22px 24px;background:#c91d32;color:#ffffff;">
                <div style="font-size:12px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;opacity:.86;">Корпоративный доступ</div>
                <div style="margin-top:8px;font-size:24px;line-height:1.15;font-weight:800;">Классификатор АЗС</div>
              </td>
            </tr>
            <tr>
              <td style="padding:26px 24px 10px;">
                <h1 style="margin:0;font-size:22px;line-height:1.25;color:#151b24;">Восстановление пароля</h1>
                <p style="margin:12px 0 0;font-size:15px;line-height:1.55;color:#526071;">Здравствуйте, {safe_name}. Используйте этот код, чтобы задать новый пароль для корпоративного ящика <strong>{safe_email}</strong>.</p>
              </td>
            </tr>
            <tr>
              <td style="padding:12px 24px 8px;">
                <div style="border:1px solid #f1ccd1;background:#fff1f3;border-radius:14px;padding:18px;text-align:center;">
                  <div style="font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:#8f1425;">Код восстановления</div>
                  <div style="margin-top:8px;font-size:38px;line-height:1;font-weight:900;letter-spacing:.18em;color:#c91d32;">{safe_code}</div>
                </div>
              </td>
            </tr>
            <tr>
              <td style="padding:12px 24px 24px;">
                <p style="margin:0;font-size:14px;line-height:1.55;color:#526071;">Код действует {ttl_minutes} минут. Если вы не запрашивали смену пароля, сообщите администратору и проигнорируйте письмо.</p>
                <p style="margin:16px 0 0;font-size:13px;line-height:1.5;color:#6f7c8d;">Открыть приложение: <a href="{safe_url}" style="color:#18579f;text-decoration:none;font-weight:700;">{safe_url}</a></p>
              </td>
            </tr>
            <tr>
              <td style="padding:16px 24px;background:#f8fafc;border-top:1px solid #e2e7ee;color:#7b8796;font-size:12px;line-height:1.45;">
                Это автоматическое письмо. © {year} Классификатор АЗС.
              </td>
            </tr>
          </table>
        </td>
      </tr>
    </table>
  </body>
</html>"""


def render_password_reset_email_text(code: str, email: str) -> str:
    ttl_minutes = max(1, round(EMAIL_CODE_TTL_SECONDS / 60))
    return (
        "Классификатор АЗС\n\n"
        f"Код восстановления пароля для {email}: {code}\n"
        f"Код действует {ttl_minutes} минут.\n\n"
        f"Открыть приложение: {app_public_url()}\n"
        "Если вы не запрашивали смену пароля, сообщите администратору и проигнорируйте письмо."
    )


def send_verification_email(email: str, code: str, name: str = ""):
    if not email_configured():
        if email_dev_mode():
            print(f"[auth] verification code for {email}: {code}", flush=True)
            return
        raise HTTPException(status_code=500, detail="Email не настроен для отправки кода подтверждения")

    subject = "Код подтверждения для Классификатора АЗС"
    html_content = render_verification_email_html(code, email, name)
    text_content = render_verification_email_text(code, email)

    try:
        if resend_configured():
            _send_via_resend(email, subject, html_content, text_content)
            return

        from_email = os.getenv("SMTP_FROM_EMAIL", "").strip()
        from_name = os.getenv("SMTP_FROM_NAME", "Классификатор АЗС").strip()
        username = os.getenv("SMTP_USERNAME", "").strip()
        password = os.getenv("SMTP_PASSWORD", "")
        host = os.getenv("SMTP_HOST", "").strip()
        port = int(os.getenv("SMTP_PORT", "587"))
        timeout = int(os.getenv("SMTP_TIMEOUT_SECONDS", "10"))
        use_ssl = env_bool("SMTP_USE_SSL", False)
        use_tls = env_bool("SMTP_USE_TLS", not use_ssl)
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = f"{from_name} <{from_email}>"
        message["To"] = email
        message.set_content(text_content)
        message.add_alternative(html_content, subtype="html")
        if use_ssl:
            with smtplib.SMTP_SSL(host, port, timeout=timeout) as smtp:
                if username or password:
                    smtp.login(username or from_email, password)
                smtp.send_message(message)
        else:
            with smtplib.SMTP(host, port, timeout=timeout) as smtp:
                smtp.ehlo()
                if use_tls:
                    smtp.starttls()
                    smtp.ehlo()
                if username or password:
                    smtp.login(username or from_email, password)
                smtp.send_message(message)
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Не удалось отправить код подтверждения на email") from exc


def send_password_reset_email(email: str, code: str, name: str = ""):
    if not email_configured():
        if email_dev_mode():
            print(f"[auth] password reset code for {email}: {code}", flush=True)
            return
        raise HTTPException(status_code=500, detail="Email не настроен для отправки кода восстановления")

    subject = "Код восстановления пароля для Классификатора АЗС"
    html_content = render_password_reset_email_html(code, email, name)
    text_content = render_password_reset_email_text(code, email)

    try:
        if resend_configured():
            _send_via_resend(email, subject, html_content, text_content)
            return

        from_email = os.getenv("SMTP_FROM_EMAIL", "").strip()
        from_name = os.getenv("SMTP_FROM_NAME", "Классификатор АЗС").strip()
        username = os.getenv("SMTP_USERNAME", "").strip()
        password = os.getenv("SMTP_PASSWORD", "")
        host = os.getenv("SMTP_HOST", "").strip()
        port = int(os.getenv("SMTP_PORT", "587"))
        timeout = int(os.getenv("SMTP_TIMEOUT_SECONDS", "10"))
        use_ssl = env_bool("SMTP_USE_SSL", False)
        use_tls = env_bool("SMTP_USE_TLS", not use_ssl)
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = f"{from_name} <{from_email}>"
        message["To"] = email
        message.set_content(text_content)
        message.add_alternative(html_content, subtype="html")
        if use_ssl:
            with smtplib.SMTP_SSL(host, port, timeout=timeout) as smtp:
                if username or password:
                    smtp.login(username or from_email, password)
                smtp.send_message(message)
        else:
            with smtplib.SMTP(host, port, timeout=timeout) as smtp:
                smtp.ehlo()
                if use_tls:
                    smtp.starttls()
                    smtp.ehlo()
                if username or password:
                    smtp.login(username or from_email, password)
                smtp.send_message(message)
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Не удалось отправить код восстановления на email") from exc


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS)
    return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, iterations, salt_hex, digest_hex = stored.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            bytes.fromhex(salt_hex),
            int(iterations),
        )
        return secrets.compare_digest(digest.hex(), digest_hex)
    except (TypeError, ValueError):
        return False


def store_email_verification_code(conn: sqlite3.Connection, email: str) -> str:
    now = int(time.time())
    code = generate_email_code()
    conn.execute("DELETE FROM email_verification_codes WHERE expires_at <= ?", (now,))
    conn.execute(
        """
        INSERT INTO email_verification_codes (email, code_hash, attempts, created_at, expires_at, last_sent_at)
        VALUES (?, ?, 0, ?, ?, ?)
        ON CONFLICT(email) DO UPDATE SET
            code_hash = excluded.code_hash,
            attempts = 0,
            created_at = excluded.created_at,
            expires_at = excluded.expires_at,
            last_sent_at = excluded.last_sent_at
        """,
        (email, hash_password(code), now, now + EMAIL_CODE_TTL_SECONDS, now),
    )
    return code


def store_password_reset_code(conn: sqlite3.Connection, email: str) -> str:
    now = int(time.time())
    code = generate_email_code()
    conn.execute("DELETE FROM password_reset_codes WHERE expires_at <= ?", (now,))
    conn.execute(
        """
        INSERT INTO password_reset_codes (email, code_hash, attempts, created_at, expires_at, last_sent_at)
        VALUES (?, ?, 0, ?, ?, ?)
        ON CONFLICT(email) DO UPDATE SET
            code_hash = excluded.code_hash,
            attempts = 0,
            created_at = excluded.created_at,
            expires_at = excluded.expires_at,
            last_sent_at = excluded.last_sent_at
        """,
        (email, hash_password(code), now, now + EMAIL_CODE_TTL_SECONDS, now),
    )
    return code


def verification_response(email: str, message: str, code: str = "") -> AuthFlowResponse:
    return AuthFlowResponse(
        verificationRequired=True,
        email=email,
        message=message,
        devCode=code if email_dev_mode() else "",
    )


def send_and_respond_with_verification(email: str, name: str, code: str) -> AuthFlowResponse:
    send_verification_email(email, code, name)
    return verification_response(email, "Код подтверждения отправлен на корпоративную почту.", code)


def password_reset_response(email: str, code: str = "") -> AuthFlowResponse:
    return AuthFlowResponse(
        verificationRequired=True,
        email=email,
        message="Если адрес зарегистрирован и допущен к системе, мы отправили код восстановления на корпоративную почту.",
        devCode=code if email_dev_mode() else "",
    )


def hash_session_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def get_client_ip(request: Request) -> str:
    """Return the real client IP for rate-limiting purposes.

    Delegates to request_ip() so that TRUSTED_PROXY_DEPTH is the single
    source of truth for XFF handling across the whole codebase.
    """
    return request_ip(request) or "unknown"


def client_key(request: Request, scope: str) -> str:
    return f"{scope}:{get_client_ip(request)}"


def enforce_rate_limit_key(key: str, limit: int, window_seconds: int = 60):
    global _RATE_LIMIT_CLEANUP_COUNTER
    now = time.time()
    recent = [item for item in AUTH_RATE_LIMIT.get(key, []) if now - item < window_seconds]
    if len(recent) >= limit:
        raise HTTPException(status_code=429, detail="Слишком много попыток. Повторите позже.")
    recent.append(now)
    AUTH_RATE_LIMIT[key] = recent
    # Periodically evict stale entries to prevent unbounded memory growth
    # when the server is hit by many distinct IPs (e.g. a distributed attack).
    _RATE_LIMIT_CLEANUP_COUNTER += 1
    if _RATE_LIMIT_CLEANUP_COUNTER >= 500:
        _RATE_LIMIT_CLEANUP_COUNTER = 0
        cutoff = now - 600  # keep at most 10 min of history
        stale = [k for k, v in list(AUTH_RATE_LIMIT.items()) if not v or all(t < cutoff for t in v)]
        for k in stale:
            AUTH_RATE_LIMIT.pop(k, None)


def enforce_rate_limit(request: Request, scope: str, limit: int, window_seconds: int = 60):
    enforce_rate_limit_key(client_key(request, scope), limit, window_seconds)


def enforce_email_rate_limit(email: str, scope: str, limit: int, window_seconds: int = 600):
    email_key = hashlib.sha256(email.encode("utf-8")).hexdigest()[:20]
    enforce_rate_limit_key(f"{scope}:email:{email_key}", limit, window_seconds)


def user_from_row(row: sqlite3.Row) -> AuthUser:
    return AuthUser(id=int(row["id"]), email=str(row["email"]), name=str(row["name"] or ""))


def create_session(response: Response, request: Request, user_id: int):
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    expires_at = now + SESSION_TTL_SECONDS
    with auth_connection() as conn:
        conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))
        conn.execute(
            "INSERT INTO sessions (token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (hash_session_token(token), user_id, now, expires_at),
        )
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        secure=request_is_https(request),
        samesite="strict",  # strict prevents the cookie from being sent on cross-site navigations
        path="/",
    )


def clear_session(response: Response, request: Request):
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if token:
        with auth_connection() as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (hash_session_token(token),))
    response.delete_cookie(SESSION_COOKIE_NAME, path="/", samesite="strict")


def current_user_from_request(request: Request) -> Optional[AuthUser]:
    if not auth_enabled():
        return AuthUser(id=0, email="dev@local", name="Dev mode")

    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        return None

    now = int(time.time())
    with auth_connection() as conn:
        row = conn.execute(
            """
            SELECT users.id, users.email, users.name
            FROM sessions
            JOIN users ON users.id = sessions.user_id
            WHERE sessions.token_hash = ? AND sessions.expires_at > ? AND users.email_verified_at > 0
            """,
            (hash_session_token(token), now),
        ).fetchone()
        if not row:
            conn.execute("DELETE FROM sessions WHERE token_hash = ? OR expires_at <= ?", (hash_session_token(token), now))
            return None
        if not email_is_allowed(str(row["email"]), conn):
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (hash_session_token(token),))
            insert_auth_event(conn, str(row["email"]), "session_blocked", "email_policy_changed", request_ip(request))
            return None
        return user_from_row(row)


def require_user(request: Request) -> AuthUser:
    user = current_user_from_request(request)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    return user


def validate_period(period: str) -> str:
    try:
        datetime.strptime(period, "%Y-%m")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="period must use YYYY-MM format") from exc
    return period


# KSSS identifiers are alphanumeric codes up to 32 characters.
# Rejecting anything outside this pattern blocks path-traversal sequences
# (../../, %2F, etc.) and injection payloads before they reach any data layer.
_KSSS_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,31}$")


def validate_ksss(ksss: str) -> str:
    """Validate and return a station identifier, or raise HTTP 422."""
    if not ksss or not _KSSS_PATTERN.match(ksss):
        raise HTTPException(status_code=422, detail="Некорректный идентификатор станции")
    return ksss


def period_bounds(period: str) -> dict[str, date]:
    period_start = datetime.strptime(period, "%Y-%m").date().replace(day=1)
    if period_start.month == 12:
        period_end = period_start.replace(year=period_start.year + 1, month=1)
    else:
        period_end = period_start.replace(month=period_start.month + 1)

    if period_start.month == 1:
        previous_period_start = period_start.replace(year=period_start.year - 1, month=12)
    else:
        previous_period_start = period_start.replace(month=period_start.month - 1)

    return {
        "period_start": period_start,
        "period_end": period_end,
        "previous_period_start": previous_period_start,
        "previous_year_start": period_start.replace(year=period_start.year - 1),
    }


def previous_period(period: str) -> str:
    bounds = period_bounds(period)
    return bounds["previous_period_start"].strftime("%Y-%m")


def previous_year_period(period: str) -> str:
    bounds = period_bounds(period)
    return bounds["previous_year_start"].strftime("%Y-%m")


def validate_metric_date(value: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="date must use YYYY-MM-DD format") from exc


def kpi_import_max_records() -> int:
    try:
        return max(1, int(os.getenv("KPI_IMPORT_MAX_RECORDS", "10000")))
    except ValueError:
        return 10000


def fuel_stock_import_max_records() -> int:
    try:
        return max(1, int(os.getenv("FUEL_STOCK_IMPORT_MAX_RECORDS", "10000")))
    except ValueError:
        return 10000


def fuel_stock_import_max_body_bytes() -> int:
    try:
        return max(1024, int(os.getenv("FUEL_STOCK_IMPORT_MAX_BODY_BYTES", str(5 * 1024 * 1024))))
    except ValueError:
        return 5 * 1024 * 1024


def _require_bearer_token(request: Request, token: str, token_hash: str, label: str):
    if not token and not token_hash:
        raise HTTPException(status_code=503, detail=f"{label} token is not configured")

    auth_header = request.headers.get("authorization", "")
    scheme, _, provided_token = auth_header.partition(" ")
    provided_token = provided_token.strip()
    if scheme.lower() != "bearer" or not provided_token:
        raise HTTPException(status_code=401, detail=f"{label} token is required")

    if token and secrets.compare_digest(provided_token, token):
        return

    provided_hash = hashlib.sha256(provided_token.encode("utf-8")).hexdigest()
    if token_hash and secrets.compare_digest(provided_hash, token_hash):
        return

    raise HTTPException(status_code=403, detail=f"{label} token is invalid")


def require_kpi_import_token(request: Request):
    configured_token = os.getenv("KPI_IMPORT_TOKEN", "")
    configured_hash = os.getenv("KPI_IMPORT_TOKEN_SHA256", "").strip().lower()
    _require_bearer_token(request, configured_token, configured_hash, "KPI import")


def require_fuel_stock_import_token(request: Request):
    configured_token = os.getenv("FUEL_STOCK_IMPORT_TOKEN", "")
    configured_hash = os.getenv("FUEL_STOCK_IMPORT_TOKEN_SHA256", "").strip().lower()
    _require_bearer_token(request, configured_token, configured_hash, "Fuel stock import")


def validate_import_body_size(request: Request, payload: BaseModel, max_bytes: int):
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > max_bytes:
                raise HTTPException(status_code=413, detail="Import payload body is too large")
        except ValueError:
            pass
    if len(payload.model_dump_json().encode("utf-8")) > max_bytes:
        raise HTTPException(status_code=413, detail="Import payload body is too large")


def kpi_periods() -> list[str]:
    init_kpi_db()
    with kpi_connection() as conn:
        rows = conn.execute("SELECT DISTINCT period FROM station_kpi_daily ORDER BY period").fetchall()
    return [str(row["period"]) for row in rows]


def latest_kpi_updated_at(conn: sqlite3.Connection, period: Optional[str] = None, ksss_values: Optional[list[str]] = None) -> str:
    params: list[str] = []
    clauses = []
    if period:
        clauses.append("period = ?")
        params.append(period)
    if ksss_values:
        placeholders = ",".join("?" for _ in ksss_values)
        clauses.append(f"ksss IN ({placeholders})")
        params.extend(ksss_values)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    row = conn.execute(f"SELECT MAX(updated_at) AS updated_at FROM station_kpi_daily {where}", params).fetchone()
    return str(row["updated_at"] or datetime.now(timezone.utc).isoformat(timespec="seconds")) if row else datetime.now(timezone.utc).isoformat(timespec="seconds")


def empty_kpi_values(has_data: bool = False) -> dict[str, object]:
    return {"revenue": 0, "fuelVolume": 0, "checks": 0, "avgCheck": 0, "hasData": has_data}


def period_metric_values(
    conn: sqlite3.Connection,
    period: str,
    ksss_values: Optional[list[str]] = None,
    through_day: Optional[int] = None,
) -> dict[str, object]:
    validate_period(period)
    params: list[str] = [period]
    clauses = ["period = ?"]
    if ksss_values is not None:
        normalized = [validate_ksss(str(ksss)) for ksss in ksss_values if str(ksss).strip()]
        if not normalized:
            return empty_kpi_values()
        placeholders = ",".join("?" for _ in normalized)
        clauses.append(f"ksss IN ({placeholders})")
        params.extend(normalized)
    if through_day is not None:
        year, month = (int(part) for part in period.split("-"))
        cutoff_day = min(max(int(through_day), 1), monthrange(year, month)[1])
        clauses.append("metric_date <= ?")
        params.append(f"{period}-{cutoff_day:02d}")

    row = conn.execute(
        f"""
        SELECT
            COUNT(*) AS row_count,
            SUM(revenue) AS revenue,
            SUM(revenue_ntu) AS revenue_ntu,
            SUM(fuel_volume) AS fuel_volume,
            SUM(checks) AS checks,
            SUM(checks_ntu) AS checks_ntu,
            COUNT(checks_ntu) AS ntu_row_count,
            AVG(avg_check) AS imported_avg_check,
            MAX(metric_date) AS max_date
        FROM station_kpi_daily
        WHERE {' AND '.join(clauses)}
        """,
        params,
    ).fetchone()

    if not row or int(row["row_count"] or 0) == 0:
        return empty_kpi_values()

    revenue = float(row["revenue"] or 0)
    fuel_volume = float(row["fuel_volume"] or 0)
    checks = float(row["checks"] or 0)
    checks_ntu = float(row["checks_ntu"] or 0)
    if int(row["ntu_row_count"] or 0) > 0:
        avg_check = round(float(row["revenue_ntu"] or 0) / checks_ntu) if checks_ntu else 0
    elif row["imported_avg_check"] is not None:
        avg_check = round(float(row["imported_avg_check"]))
    else:
        avg_check = round(revenue / checks) if checks else 0
    return {
        "revenue": revenue,
        "fuelVolume": fuel_volume,
        "checks": checks,
        "avgCheck": avg_check,
        "hasData": True,
        "maxDate": str(row["max_date"] or ""),
    }


def pct_delta(current: float, baseline: float, baseline_has_data: bool) -> Optional[float]:
    if not baseline_has_data or not baseline:
        return None
    return round(((current - baseline) / abs(baseline)) * 100, 1)


def kpi_deltas(current: dict[str, object], previous: dict[str, object], year: dict[str, object]) -> dict[str, Optional[float]]:
    return {
        f"{metric_id}_mom_pct": pct_delta(
            float(current.get(metric_id, 0)),
            float(previous.get(metric_id, 0)),
            bool(previous.get("hasData")),
        )
        for metric_id in ("revenue", "fuelVolume", "checks", "avgCheck")
    } | {
        f"{metric_id}_yoy_pct": pct_delta(
            float(current.get(metric_id, 0)),
            float(year.get(metric_id, 0)),
            bool(year.get("hasData")),
        )
        for metric_id in ("revenue", "fuelVolume", "checks", "avgCheck")
    }


def local_metric_set(
    conn: sqlite3.Connection,
    period: str,
    ksss_values: list[str],
) -> tuple[dict[str, object], dict[str, Optional[float]]]:
    current = period_metric_values(conn, period, ksss_values)
    max_date = str(current.get("maxDate") or "")
    through_day: Optional[int] = None
    if max_date:
        period_year, period_month = (int(part) for part in period.split("-"))
        loaded_day = int(max_date[-2:])
        if loaded_day < monthrange(period_year, period_month)[1]:
            through_day = loaded_day
    previous = period_metric_values(conn, previous_period(period), ksss_values, through_day)
    year = period_metric_values(conn, previous_year_period(period), ksss_values, through_day)
    return current, kpi_deltas(current, previous, year)


@lru_cache(maxsize=1)
def load_station_payload() -> dict:
    path = PRIVATE_STATIONS_PATH if PRIVATE_STATIONS_PATH.exists() else STATIONS_PATH if STATIONS_PATH.exists() else STATIONS_SAMPLE_PATH
    if not path.exists():
        return {"meta": {"count": 0}, "stations": []}
    return json.loads(path.read_text(encoding="utf-8"))


def load_stations() -> list[dict]:
    return load_station_payload().get("stations", [])


def station_by_ksss(ksss: str) -> Optional[dict]:
    return next((station for station in load_stations() if str(station.get("ksss")) == str(ksss)), None)


@lru_cache(maxsize=1)
def load_staff_database() -> dict:
    path = PRIVATE_STAFF_RECOMMENDATIONS_PATH if PRIVATE_STAFF_RECOMMENDATIONS_PATH.exists() else STAFF_RECOMMENDATIONS_PATH
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def staff_periods() -> list[str]:
    payload = load_staff_database()
    return sorted((payload.get("periods") or {}).keys())


def staff_updated_at() -> str:
    payload = load_staff_database()
    return payload.get("meta", {}).get("generatedAt") or datetime.now(timezone.utc).isoformat(timespec="seconds")


def file_staff(ksss: str, period: str) -> Optional[StationStaffResponse]:
    payload = load_staff_database()
    period_data = (payload.get("periods") or {}).get(period)
    if not period_data:
        return None

    station = (period_data.get("stations") or {}).get(str(ksss))
    if not station:
        return None

    days = [
        StaffDay(
            date=str(item.get("date") or ""),
            label=str(item.get("label") or ""),
            day=float(item.get("day") or 0),
            night=float(item.get("night") or 0),
        )
        for item in station.get("days", [])
    ]
    days.sort(key=lambda item: item.date)
    if not days:
        return None

    today_iso = datetime.now().date().isoformat()
    today = next((item for item in days if item.date == today_iso), days[0])

    return StationStaffResponse(
        ksss=ksss,
        period=period,
        source="file",
        updatedAt=staff_updated_at(),
        staffTotal=float(station.get("staffTotal") or 0),
        today=today,
        days=days,
    )


def station_name(station: dict) -> str:
    return station.get("name") or f"АЗС № {station.get('stationNumber', '')}".strip()


def mock_number(ksss: str, period: str, metric_id: str, minimum: int, maximum: int) -> int:
    seed = f"{ksss}:{period}:{metric_id}".encode("utf-8")
    digest = hashlib.sha256(seed).hexdigest()
    value = int(digest[:10], 16)
    return minimum + value % (maximum - minimum + 1)


def mock_pct(ksss: str, period: str, metric_id: str, salt: str) -> float:
    raw = mock_number(ksss, period, f"{metric_id}:{salt}", -120, 180)
    return round(raw / 10, 1)


def mock_metric_values(ksss: str, period: str) -> dict[str, float]:
    revenue = mock_number(ksss, period, "revenue", 4_000_000, 28_000_000)
    fuel_volume = mock_number(ksss, period, "fuelVolume", 120_000, 850_000)
    checks = mock_number(ksss, period, "checks", 8_000, 62_000)
    avg_check = round(revenue / max(checks, 1))
    return {
        "revenue": revenue,
        "fuelVolume": fuel_volume,
        "checks": checks,
        "avgCheck": avg_check,
    }


def make_metrics(
    values: dict[str, float],
    period: str,
    seed_key: str,
    deltas: Optional[dict[str, Optional[float]]] = None,
) -> list[KpiMetric]:
    metrics = [
        ("revenue", "Выручка", values.get("revenue", 0), "₽"),
        ("fuelVolume", "Объем топлива", values.get("fuelVolume", 0), "л"),
        ("checks", "Чеки", values.get("checks", 0), "шт"),
        ("avgCheck", "Средний чек", values.get("avgCheck", 0), "₽"),
    ]
    use_mock_deltas = deltas is None
    resolved_deltas = deltas or {}
    return [
        KpiMetric(
            id=metric_id,
            label=label,
            value=value,
            unit=unit,
            momPct=(mock_pct(seed_key, period, metric_id, "mom") if use_mock_deltas else resolved_deltas.get(f"{metric_id}_mom_pct")),
            yoyPct=(mock_pct(seed_key, period, metric_id, "yoy") if use_mock_deltas else resolved_deltas.get(f"{metric_id}_yoy_pct")),
        )
        for metric_id, label, value, unit in metrics
    ]


def mock_kpis(ksss: str, period: str) -> StationKpiResponse:
    return StationKpiResponse(
        ksss=ksss,
        period=period,
        source="mock",
        updatedAt=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        metrics=make_metrics(mock_metric_values(ksss, period), period, ksss),
    )


def db_kpis(ksss: str, period: str) -> StationKpiResponse:
    try:
        import pandas as pd
        import psycopg2
    except ImportError as exc:
        raise HTTPException(status_code=500, detail="Install backend requirements to use KPI_DATA_MODE=db") from exc

    required = ["DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD"]
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        # Log the specific variable names server-side only; never expose them to the client.
        logger.error("Missing required database configuration keys: %s", ", ".join(missing))
        raise HTTPException(status_code=500, detail="Database configuration is incomplete. Check server logs.")

    if not SQL_TEMPLATE.exists():
        raise HTTPException(status_code=500, detail="SQL template not found")

    params = {
        "ksss": ksss,
        **period_bounds(period),
    }

    with psycopg2.connect(
        host=os.getenv("DB_HOST"),
        port=os.getenv("DB_PORT"),
        dbname=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        connect_timeout=10,
        options="-c statement_timeout=3600000",
    ) as conn:
        df = pd.read_sql(SQL_TEMPLATE.read_text(encoding="utf-8"), conn, params=params)

    if df.empty:
        raise HTTPException(status_code=404, detail="KPI data not found")

    row = df.iloc[0].to_dict()
    return StationKpiResponse(
        ksss=ksss,
        period=period,
        source="db",
        updatedAt=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        metrics=[
            KpiMetric(id="revenue", label="Выручка", value=float(row.get("revenue") or 0), unit="₽", momPct=float(row.get("revenue_mom_pct") or 0), yoyPct=float(row.get("revenue_yoy_pct") or 0)),
            KpiMetric(id="fuelVolume", label="Объем топлива", value=float(row.get("fuel_volume") or 0), unit="л", momPct=float(row.get("fuel_volume_mom_pct") or 0), yoyPct=float(row.get("fuel_volume_yoy_pct") or 0)),
            KpiMetric(id="checks", label="Чеки", value=float(row.get("checks") or 0), unit="шт", momPct=float(row.get("checks_mom_pct") or 0), yoyPct=float(row.get("checks_yoy_pct") or 0)),
            KpiMetric(id="avgCheck", label="Средний чек", value=float(row.get("avg_check") or 0), unit="₽", momPct=float(row.get("avg_check_mom_pct") or 0), yoyPct=float(row.get("avg_check_yoy_pct") or 0)),
        ],
    )


def local_kpis(ksss: str, period: str) -> StationKpiResponse:
    init_kpi_db()
    with kpi_connection() as conn:
        values, deltas = local_metric_set(conn, period, [ksss])
        if not values.get("hasData"):
            raise HTTPException(status_code=404, detail="KPI data not found")
        updated_at = latest_kpi_updated_at(conn, period, [ksss])

    return StationKpiResponse(
        ksss=ksss,
        period=period,
        source="local",
        updatedAt=updated_at,
        metrics=make_metrics(values, period, ksss, deltas),
    )


def mock_staff_total(ksss: str, period: str) -> int:
    return mock_number(ksss, period, "staffTotal", 9, 24)


def make_staff_day(ksss: str, day_date: date, staff_total: int) -> StaffDay:
    seed_period = day_date.strftime("%Y-%m")
    weekday = day_date.weekday()
    day_min = max(2, round(staff_total * 0.34))
    day_max = max(day_min, round(staff_total * 0.58))
    night_min = max(1, round(staff_total * 0.18))
    night_max = max(night_min, round(staff_total * 0.34))
    day_value = mock_number(ksss, seed_period, f"staffDay:{day_date.day}", day_min, day_max)
    night_value = mock_number(ksss, seed_period, f"staffNight:{day_date.day}", night_min, night_max)

    if weekday >= 5:
        day_value = max(day_min, day_value - 1)

    label = day_date.strftime("%a").replace(".", "")
    return StaffDay(
        date=day_date.isoformat(),
        label=label,
        day=day_value,
        night=night_value,
    )


def mock_staff(ksss: str, period: str) -> StationStaffResponse:
    bounds = period_bounds(period)
    period_start = bounds["period_start"]
    days_in_month = monthrange(period_start.year, period_start.month)[1]
    staff_total = mock_staff_total(ksss, period)
    days = [
        make_staff_day(ksss, period_start + timedelta(days=offset), staff_total)
        for offset in range(days_in_month)
    ]
    today_iso = datetime.now().date().isoformat()
    today = next((item for item in days if item.date == today_iso), days[0])

    return StationStaffResponse(
        ksss=ksss,
        period=period,
        source="mock",
        updatedAt=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        staffTotal=staff_total,
        today=today,
        days=days,
    )


def aggregate_metric_values(ksss_values: list[str], period: str) -> dict[str, float]:
    rows = [mock_metric_values(ksss, period) for ksss in ksss_values]
    if not rows:
        return {"revenue": 0, "fuelVolume": 0, "checks": 0, "avgCheck": 0}
    revenue = sum(row["revenue"] for row in rows)
    fuel_volume = sum(row["fuelVolume"] for row in rows)
    checks = sum(row["checks"] for row in rows)
    return {
        "revenue": revenue,
        "fuelVolume": fuel_volume,
        "checks": checks,
        "avgCheck": round(revenue / max(checks, 1)),
    }


def mock_overview(period: str, group_by: str) -> AnalyticsOverviewResponse:
    getter_map = {
        "territoryManager": lambda station: station.get("territoryManager") or "ТМ не заполнен",
        "regionalManager": lambda station: station.get("regionalManager") or "РУ не заполнен",
        "station": lambda station: station_name(station),
    }
    if group_by not in getter_map:
        raise HTTPException(status_code=422, detail="groupBy must be territoryManager, regionalManager or station")

    groups: dict[str, list[dict]] = {}
    for station in load_stations():
        if not station.get("ksss"):
            continue
        label = getter_map[group_by](station)
        groups.setdefault(label, []).append(station)

    rows = []
    for label, stations in groups.items():
        ksss_values = [str(station.get("ksss")) for station in stations if station.get("ksss")]
        values = aggregate_metric_values(ksss_values, period)
        rows.append(
            AnalyticsOverviewRow(
                id=label,
                label=label,
                count=len(stations),
                metrics=make_metrics(values, period, f"{group_by}:{label}"),
            )
        )

    rows.sort(key=lambda row: next((metric.value for metric in row.metrics if metric.id == "revenue"), 0), reverse=True)
    return AnalyticsOverviewResponse(
        period=period,
        groupBy=group_by,
        source="mock",
        updatedAt=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        rows=rows[:30],
    )


def overview_group_getter(group_by: str):
    getter_map = {
        "territoryManager": lambda station: station.get("territoryManager") or "ТМ не заполнен",
        "regionalManager": lambda station: station.get("regionalManager") or "РУ не заполнен",
        "station": lambda station: station_name(station),
    }
    if group_by not in getter_map:
        raise HTTPException(status_code=422, detail="groupBy must be territoryManager, regionalManager or station")
    return getter_map[group_by]


def local_overview(period: str, group_by: str) -> AnalyticsOverviewResponse:
    getter = overview_group_getter(group_by)
    groups: dict[str, list[dict]] = {}
    for station in load_stations():
        if not station.get("ksss"):
            continue
        groups.setdefault(getter(station), []).append(station)

    rows = []
    init_kpi_db()
    with kpi_connection() as conn:
        updated_at = latest_kpi_updated_at(conn, period)
        for label, stations in groups.items():
            ksss_values = [str(station.get("ksss")) for station in stations if station.get("ksss")]
            values, deltas = local_metric_set(conn, period, ksss_values)
            if not values.get("hasData"):
                continue
            rows.append(
                AnalyticsOverviewRow(
                    id=label,
                    label=label,
                    count=len(stations),
                    metrics=make_metrics(values, period, f"{group_by}:{label}", deltas),
                )
            )

    rows.sort(key=lambda row: next((metric.value for metric in row.metrics if metric.id == "revenue"), 0), reverse=True)
    return AnalyticsOverviewResponse(
        period=period,
        groupBy=group_by,
        source="local",
        updatedAt=updated_at,
        rows=rows[:30],
    )


def station_similarity(base: dict, candidate: dict, period: str) -> tuple[int, list[str]]:
    score = 42
    reasons = []

    for field, label, points in [
        ("formatLevel2", "формат", 14),
        ("location", "локация", 12),
        ("subject", "регион", 8),
        ("serviceCluster", "сервисный кластер", 8),
        ("paymentType", "тип оплаты", 6),
    ]:
        if base.get(field) and base.get(field) == candidate.get(field):
            score += points
            reasons.append(label)

    base_flags = base.get("flags", {})
    candidate_flags = candidate.get("flags", {})
    for flag, label in [("hasCafe", "кафе"), ("hasShop", "магазин"), ("hasToilet", "санузел")]:
        if base_flags.get(flag) and candidate_flags.get(flag):
            score += 4
            reasons.append(label)

    base_values = mock_metric_values(str(base.get("ksss")), period)
    candidate_values = mock_metric_values(str(candidate.get("ksss")), period)
    revenue_delta = abs(base_values["revenue"] - candidate_values["revenue"]) / max(base_values["revenue"], 1)
    volume_delta = abs(base_values["fuelVolume"] - candidate_values["fuelVolume"]) / max(base_values["fuelVolume"], 1)

    if revenue_delta < 0.18:
        score += 10
        reasons.append("близкая выручка")
    if volume_delta < 0.18:
        score += 10
        reasons.append("близкий объем")

    return min(score, 100), reasons[:4] or ["экономический профиль"]


def local_station_similarity(base: dict, candidate: dict, base_values: dict[str, object], candidate_values: dict[str, object]) -> tuple[int, list[str]]:
    score = 42
    reasons = []

    for field, label, points in [
        ("formatLevel2", "формат", 14),
        ("location", "локация", 12),
        ("subject", "регион", 8),
        ("serviceCluster", "сервисный кластер", 8),
        ("paymentType", "тип оплаты", 6),
    ]:
        if base.get(field) and base.get(field) == candidate.get(field):
            score += points
            reasons.append(label)

    base_flags = base.get("flags", {})
    candidate_flags = candidate.get("flags", {})
    for flag, label in [("hasCafe", "кафе"), ("hasShop", "магазин"), ("hasToilet", "санузел")]:
        if base_flags.get(flag) and candidate_flags.get(flag):
            score += 4
            reasons.append(label)

    base_revenue = float(base_values.get("revenue", 0))
    base_volume = float(base_values.get("fuelVolume", 0))
    if base_revenue and candidate_values.get("hasData"):
        revenue_delta = abs(base_revenue - float(candidate_values.get("revenue", 0))) / max(base_revenue, 1)
        if revenue_delta < 0.18:
            score += 10
            reasons.append("близкая выручка")
    if base_volume and candidate_values.get("hasData"):
        volume_delta = abs(base_volume - float(candidate_values.get("fuelVolume", 0))) / max(base_volume, 1)
        if volume_delta < 0.18:
            score += 10
            reasons.append("близкий объем")

    return min(score, 100), reasons[:4] or ["экономический профиль"]


def mock_similar(ksss: str, period: str, limit: int) -> SimilarStationsResponse:
    base = station_by_ksss(ksss)
    if not base:
        raise HTTPException(status_code=404, detail="Station not found")

    items = []
    for candidate in load_stations():
        candidate_ksss = str(candidate.get("ksss") or "")
        if not candidate_ksss or candidate_ksss == str(ksss):
            continue
        score, reasons = station_similarity(base, candidate, period)
        items.append(
            SimilarStation(
                ksss=candidate_ksss,
                stationNumber=str(candidate.get("stationNumber") or ""),
                name=station_name(candidate),
                subject=str(candidate.get("subject") or candidate.get("address") or ""),
                score=score,
                reasons=reasons,
                metrics=make_metrics(mock_metric_values(candidate_ksss, period), period, candidate_ksss),
            )
        )

    items.sort(key=lambda item: item.score, reverse=True)
    return SimilarStationsResponse(
        ksss=ksss,
        period=period,
        source="mock",
        updatedAt=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        items=items[:limit],
    )


def local_similar(ksss: str, period: str, limit: int) -> SimilarStationsResponse:
    base = station_by_ksss(ksss)
    if not base:
        raise HTTPException(status_code=404, detail="Station not found")

    items = []
    init_kpi_db()
    with kpi_connection() as conn:
        updated_at = latest_kpi_updated_at(conn, period)
        base_values = period_metric_values(conn, period, [str(base.get("ksss"))])
        for candidate in load_stations():
            candidate_ksss = str(candidate.get("ksss") or "")
            if not candidate_ksss or candidate_ksss == str(ksss):
                continue
            candidate_values, candidate_deltas = local_metric_set(conn, period, [candidate_ksss])
            score, reasons = local_station_similarity(base, candidate, base_values, candidate_values)
            items.append(
                SimilarStation(
                    ksss=candidate_ksss,
                    stationNumber=str(candidate.get("stationNumber") or ""),
                    name=station_name(candidate),
                    subject=str(candidate.get("subject") or candidate.get("address") or ""),
                    score=score,
                    reasons=reasons,
                    metrics=make_metrics(candidate_values, period, candidate_ksss, candidate_deltas),
                )
            )

    items.sort(key=lambda item: item.score, reverse=True)
    return SimilarStationsResponse(
        ksss=ksss,
        period=period,
        source="local",
        updatedAt=updated_at,
        items=items[:limit],
    )


def mock_compare(ksss_values: list[str], period: str) -> CompareResponse:
    unique_ksss = []
    for ksss in ksss_values:
        if ksss and ksss not in unique_ksss:
            unique_ksss.append(ksss)
    if len(unique_ksss) > 5:
        raise HTTPException(status_code=422, detail="Compare supports up to 5 stations")

    items = []
    for ksss in unique_ksss:
        station = station_by_ksss(ksss)
        if not station:
            continue
        items.append(
            CompareStation(
                ksss=ksss,
                stationNumber=str(station.get("stationNumber") or ""),
                name=station_name(station),
                subject=str(station.get("subject") or station.get("address") or ""),
                regionalManager=str(station.get("regionalManager") or ""),
                territoryManager=str(station.get("territoryManager") or ""),
                format=str(station.get("formatLevel2") or station.get("format") or ""),
                location=str(station.get("location") or ""),
                trkCount=station.get("trkCount"),
                postsCount=station.get("postsCount"),
                staffTotal=mock_staff_total(ksss, period),
                metrics=make_metrics(mock_metric_values(ksss, period), period, ksss),
            )
        )

    return CompareResponse(
        period=period,
        source="mock",
        updatedAt=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        items=items,
    )


def local_compare(ksss_values: list[str], period: str) -> CompareResponse:
    unique_ksss = []
    for ksss in ksss_values:
        if ksss and ksss not in unique_ksss:
            unique_ksss.append(validate_ksss(ksss))
    if len(unique_ksss) > 5:
        raise HTTPException(status_code=422, detail="Compare supports up to 5 stations")

    items = []
    init_kpi_db()
    with kpi_connection() as conn:
        updated_at = latest_kpi_updated_at(conn, period, unique_ksss)
        for ksss in unique_ksss:
            station = station_by_ksss(ksss)
            if not station:
                continue
            values, deltas = local_metric_set(conn, period, [ksss])
            items.append(
                CompareStation(
                    ksss=ksss,
                    stationNumber=str(station.get("stationNumber") or ""),
                    name=station_name(station),
                    subject=str(station.get("subject") or station.get("address") or ""),
                    regionalManager=str(station.get("regionalManager") or ""),
                    territoryManager=str(station.get("territoryManager") or ""),
                    format=str(station.get("formatLevel2") or station.get("format") or ""),
                    location=str(station.get("location") or ""),
                    trkCount=station.get("trkCount"),
                    postsCount=station.get("postsCount"),
                    staffTotal=mock_staff_total(ksss, period),
                    metrics=make_metrics(values, period, ksss, deltas),
                )
            )

    return CompareResponse(
        period=period,
        source="local",
        updatedAt=updated_at,
        items=items,
    )


def db_extension_not_ready(template: Path):
    raise HTTPException(
        status_code=501,
        detail=f"DB mode is reserved for real SQL. Fill {template.name} after table structure is known.",
    )


@app.get("/api/health")
def health():
    db_ok = False
    kpi_db_ok = False
    fuel_stock_db_ok = False
    active_sessions = 0
    kpi_period_count = 0
    kpi_updated_at = ""
    fuel_stock = {"rows": 0, "stations": 0, "snapshotAt": "", "importedAt": "", "stale": True}
    try:
        with sqlite3.connect(AUTH_DB_PATH) as conn:
            db_ok = True
            row = conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE expires_at > ?",
                (int(time.time()),),
            ).fetchone()
            active_sessions = row[0] if row else 0
    except Exception:
        pass
    try:
        init_kpi_db()
        with sqlite3.connect(KPI_DB_PATH) as conn:
            kpi_db_ok = True
            row = conn.execute("SELECT COUNT(DISTINCT period), MAX(updated_at) FROM station_kpi_daily").fetchone()
            if row:
                kpi_period_count = int(row[0] or 0)
                kpi_updated_at = str(row[1] or "")
    except Exception:
        pass
    try:
        fuel_stock = fuel_stock_health()
        fuel_stock_db_ok = True
    except Exception:
        pass
    return {
        "status": "ok" if db_ok else "degraded",
        "db": "ok" if db_ok else "error",
        "kpiDb": "ok" if kpi_db_ok else "error",
        "fuelStockDb": "ok" if fuel_stock_db_ok else "error",
        "kpiPeriods": kpi_period_count,
        "kpiUpdatedAt": kpi_updated_at,
        "fuelStock": fuel_stock,
        "activeSessions": active_sessions,
        "mode": data_mode(),
        "version": "0.4.0",
    }


@app.post("/api/internal/kpi/import", response_model=KpiImportResponse)
def import_kpi_metrics(payload: KpiImportPayload, request: Request):
    enforce_rate_limit(request, "kpi-import", 30, window_seconds=60)
    require_kpi_import_token(request)

    if len(payload.records) > kpi_import_max_records():
        raise HTTPException(status_code=413, detail="Too many KPI records in one request")

    period = validate_period(payload.period) if payload.period else ""
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    source = (payload.source or "dwh-sync").strip()[:120]
    rows = []

    for record in payload.records:
        metric_date = validate_metric_date(record.date)
        record_period = metric_date.strftime("%Y-%m")
        if period and record_period != period:
            raise HTTPException(status_code=422, detail="All KPI records must belong to the requested period")
        if not period:
            period = record_period

        fuel_volume = record.fuelVolume if record.fuelVolume is not None else record.fuel_volume
        if fuel_volume is None:
            raise HTTPException(status_code=422, detail="fuelVolume is required")
        revenue_ntu = record.revenueNtu if record.revenueNtu is not None else record.revenue_ntu
        checks_ntu = record.checksNtu if record.checksNtu is not None else record.checks_ntu
        avg_check = record.avgCheck if record.avgCheck is not None else record.avg_check

        rows.append(
            (
                metric_date.isoformat(),
                record_period,
                validate_ksss(record.ksss),
                float(record.revenue),
                float(revenue_ntu) if revenue_ntu is not None else None,
                float(fuel_volume),
                float(record.checks),
                float(checks_ntu) if checks_ntu is not None else None,
                float(avg_check) if avg_check is not None else None,
                (record.updatedAt or now_iso)[:80],
                source,
            )
        )

    init_kpi_db()
    with kpi_connection() as conn:
        if payload.replacePeriod and period:
            conn.execute("DELETE FROM station_kpi_daily WHERE period = ?", (period,))
        conn.executemany(
            """
            INSERT INTO station_kpi_daily (
                metric_date, period, ksss, revenue, revenue_ntu, fuel_volume,
                checks, checks_ntu, avg_check, updated_at, source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(metric_date, ksss) DO UPDATE SET
                period = excluded.period,
                revenue = excluded.revenue,
                revenue_ntu = excluded.revenue_ntu,
                fuel_volume = excluded.fuel_volume,
                checks = excluded.checks,
                checks_ntu = excluded.checks_ntu,
                avg_check = excluded.avg_check,
                updated_at = excluded.updated_at,
                source = excluded.source
            """,
            rows,
        )
        periods = [str(row["period"]) for row in conn.execute("SELECT DISTINCT period FROM station_kpi_daily ORDER BY period").fetchall()]

    logger.info("Imported %d KPI aggregate rows for period %s from %s", len(rows), period, source)
    return KpiImportResponse(
        ok=True,
        imported=len(rows),
        period=period,
        periods=periods,
        updatedAt=now_iso,
    )


@app.post("/api/internal/fuel-stock/import", response_model=FuelStockImportResponse)
def import_fuel_stock_snapshot(payload: FuelStockImportPayload, request: Request):
    enforce_rate_limit(request, "fuel-stock-import", 20, window_seconds=60)
    require_fuel_stock_import_token(request)
    validate_import_body_size(request, payload, fuel_stock_import_max_body_bytes())

    if len(payload.records) > fuel_stock_import_max_records():
        raise HTTPException(status_code=413, detail="Too many fuel stock records in one request")

    try:
        response = replace_fuel_stock_snapshot(payload)
    except FuelStockImportError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    logger.info(
        "Imported fuel stock snapshot accountDate=%s snapshotAt=%s rows=%d stations=%d unchanged=%s",
        response.accountDate,
        response.snapshotAt,
        response.imported,
        response.stations,
        response.unchanged,
    )
    return response


@app.get("/api/auth/me", response_model=AuthResponse)
def auth_me(request: Request):
    user = current_user_from_request(request)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    return AuthResponse(user=user)


@app.get("/api/auth/policy", response_model=AuthPolicyResponse)
def auth_policy():
    with auth_connection() as conn:
        allowlist_enabled = bool(env_allowed_emails() or db_allowed_emails(conn))
    return AuthPolicyResponse(
        allowedDomains=sorted(allowed_email_domains()),
        allowlistEnabled=allowlist_enabled,
    )


@app.post("/api/auth/register", response_model=AuthFlowResponse, status_code=status.HTTP_201_CREATED)
def auth_register(credentials: AuthCredentials, request: Request, response: Response):
    enforce_rate_limit(request, "register", 8)
    email = normalize_email(credentials.email)
    password = validate_password(credentials.password)
    name = credentials.name.strip()[:120]

    with auth_connection() as conn:
        if not email_is_allowed(email, conn):
            insert_auth_event(conn, email, "register_blocked", "email_not_allowed", request_ip(request))
            conn.commit()
            raise corporate_access_error()
        existing = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
        if existing:
            insert_auth_event(conn, email, "register_conflict", "email_exists", request_ip(request))
            conn.commit()
            raise HTTPException(status_code=409, detail="Пользователь с таким email уже зарегистрирован")
        cursor = conn.execute(
            "INSERT INTO users (email, name, password_hash, created_at, email_verified_at, last_login_at) VALUES (?, ?, ?, ?, 0, 0)",
            (email, name, hash_password(password), int(time.time())),
        )
        int(cursor.lastrowid)
        code = store_email_verification_code(conn, email)
        insert_auth_event(conn, email, "register_verification_sent", "corporate_email", request_ip(request))

    return send_and_respond_with_verification(email, name, code)


@app.post("/api/auth/login", response_model=AuthFlowResponse)
def auth_login(credentials: AuthCredentials, request: Request, response: Response):
    enforce_rate_limit(request, "login", 12)
    email = normalize_email(credentials.email)
    password = validate_password(credentials.password)

    with auth_connection() as conn:
        if not email_is_allowed(email, conn):
            insert_auth_event(conn, email, "login_blocked", "email_not_allowed", request_ip(request))
            conn.commit()
            raise corporate_access_error()
        row = conn.execute("SELECT id, email, name, password_hash, email_verified_at FROM users WHERE email = ?", (email,)).fetchone()
        if not row or not verify_password(password, str(row["password_hash"])):
            insert_auth_event(conn, email, "login_failed", "bad_credentials", request_ip(request))
            conn.commit()
            raise HTTPException(status_code=401, detail="Неверный email или пароль")
        if int(row["email_verified_at"] or 0) <= 0:
            code = store_email_verification_code(conn, email)
            insert_auth_event(conn, email, "login_verification_sent", "email_not_verified", request_ip(request))
            name = str(row["name"] or "")
            send_after_commit = (email, name, code)
            user = None
        else:
            conn.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (int(time.time()), int(row["id"])))
            user = user_from_row(row)
            send_after_commit = None
            insert_auth_event(conn, email, "login_success", "corporate_email", request_ip(request))

    if send_after_commit:
        send_email, send_name, send_code = send_after_commit
        return send_and_respond_with_verification(send_email, send_name, send_code)

    create_session(response, request, user.id)
    return AuthFlowResponse(user=user)


@app.post("/api/auth/verify-email", response_model=AuthFlowResponse)
def auth_verify_email(payload: EmailVerificationRequest, request: Request, response: Response):
    enforce_rate_limit(request, "verify-email", 10)
    email = normalize_email(payload.email)
    code = re.sub(r"\s+", "", payload.code.strip())
    now = int(time.time())

    with auth_connection() as conn:
        if not email_is_allowed(email, conn):
            insert_auth_event(conn, email, "verify_blocked", "email_not_allowed", request_ip(request))
            conn.commit()
            raise corporate_access_error()
        row = conn.execute(
            """
            SELECT users.id, users.email, users.name, users.email_verified_at,
                   email_verification_codes.code_hash, email_verification_codes.attempts,
                   email_verification_codes.expires_at
            FROM users
            LEFT JOIN email_verification_codes ON email_verification_codes.email = users.email
            WHERE users.email = ?
            """,
            (email,),
        ).fetchone()
        if not row:
            insert_auth_event(conn, email, "verify_failed", "user_not_found", request_ip(request))
            conn.commit()
            raise HTTPException(status_code=404, detail="Пользователь не найден")
        if int(row["email_verified_at"] or 0) > 0:
            user = user_from_row(row)
            create_session(response, request, user.id)
            return AuthFlowResponse(user=user)
        if not row["code_hash"] or int(row["expires_at"] or 0) <= now:
            conn.execute("DELETE FROM email_verification_codes WHERE email = ?", (email,))
            insert_auth_event(conn, email, "verify_failed", "code_expired", request_ip(request))
            conn.commit()
            raise HTTPException(status_code=410, detail="Код истек. Запросите новый код.")
        if int(row["attempts"] or 0) >= 5:
            conn.execute("DELETE FROM email_verification_codes WHERE email = ?", (email,))
            insert_auth_event(conn, email, "verify_failed", "too_many_attempts", request_ip(request))
            conn.commit()
            raise HTTPException(status_code=429, detail="Слишком много неверных попыток. Запросите новый код.")
        if not verify_password(code, str(row["code_hash"])):
            conn.execute("UPDATE email_verification_codes SET attempts = attempts + 1 WHERE email = ?", (email,))
            insert_auth_event(conn, email, "verify_failed", "bad_code", request_ip(request))
            conn.commit()
            raise HTTPException(status_code=422, detail="Неверный код подтверждения")

        verified_at = int(time.time())
        conn.execute("UPDATE users SET email_verified_at = ?, last_login_at = ? WHERE email = ?", (verified_at, verified_at, email))
        conn.execute("DELETE FROM email_verification_codes WHERE email = ?", (email,))
        user = user_from_row(row)
        insert_auth_event(conn, email, "verify_success", "email_verified", request_ip(request))

    create_session(response, request, user.id)
    return AuthFlowResponse(user=user)


@app.post("/api/auth/resend-code", response_model=AuthFlowResponse)
def auth_resend_code(payload: EmailResendRequest, request: Request):
    enforce_rate_limit(request, "resend-code", 4)
    email = normalize_email(payload.email)

    with auth_connection() as conn:
        if not email_is_allowed(email, conn):
            insert_auth_event(conn, email, "resend_blocked", "email_not_allowed", request_ip(request))
            conn.commit()
            raise corporate_access_error()
        row = conn.execute("SELECT id, email, name, email_verified_at FROM users WHERE email = ?", (email,)).fetchone()
        if not row:
            insert_auth_event(conn, email, "resend_failed", "user_not_found", request_ip(request))
            conn.commit()
            raise HTTPException(status_code=404, detail="Пользователь не найден")
        if int(row["email_verified_at"] or 0) > 0:
            return AuthFlowResponse(message="Почта уже подтверждена.")
        code = store_email_verification_code(conn, email)
        name = str(row["name"] or "")
        insert_auth_event(conn, email, "resend_success", "verification_code", request_ip(request))

    return send_and_respond_with_verification(email, name, code)


def password_reset_code_error() -> HTTPException:
    return HTTPException(status_code=422, detail="Код восстановления неверный или истек. Запросите новый код.")


@app.post("/api/auth/request-password-reset", response_model=AuthFlowResponse)
def auth_request_password_reset(payload: PasswordResetRequest, request: Request):
    enforce_rate_limit(request, "password-reset-request", 6)
    email = normalize_email(payload.email)
    enforce_email_rate_limit(email, "password-reset-request", 3, window_seconds=10 * 60)
    now = int(time.time())
    send_after_commit: Optional[tuple[str, str, str]] = None

    with auth_connection() as conn:
        if not email_is_allowed(email, conn):
            insert_auth_event(conn, email, "password_reset_request_ignored", "email_not_allowed", request_ip(request))
            return password_reset_response(email)

        row = conn.execute("SELECT id, email, name, email_verified_at FROM users WHERE email = ?", (email,)).fetchone()
        if not row:
            insert_auth_event(conn, email, "password_reset_request_ignored", "user_not_found", request_ip(request))
            return password_reset_response(email)
        if int(row["email_verified_at"] or 0) <= 0:
            insert_auth_event(conn, email, "password_reset_request_ignored", "email_not_verified", request_ip(request))
            return password_reset_response(email)

        existing = conn.execute("SELECT last_sent_at FROM password_reset_codes WHERE email = ?", (email,)).fetchone()
        if existing and now - int(existing["last_sent_at"] or 0) < 60:
            insert_auth_event(conn, email, "password_reset_request_throttled", "recent_code_exists", request_ip(request))
            return password_reset_response(email)

        code = store_password_reset_code(conn, email)
        name = str(row["name"] or "")
        send_after_commit = (email, name, code)
        insert_auth_event(conn, email, "password_reset_code_sent", "corporate_email", request_ip(request))

    if send_after_commit:
        send_email, send_name, send_code = send_after_commit
        send_password_reset_email(send_email, send_code, send_name)
        return password_reset_response(send_email, send_code)
    return password_reset_response(email)


@app.post("/api/auth/reset-password", response_model=AuthFlowResponse)
def auth_reset_password(payload: PasswordResetConfirmRequest, request: Request, response: Response):
    enforce_rate_limit(request, "password-reset-confirm", 10)
    email = normalize_email(payload.email)
    enforce_email_rate_limit(email, "password-reset-confirm", 10, window_seconds=10 * 60)
    code = re.sub(r"\s+", "", payload.code.strip())
    password = validate_password(payload.password)
    now = int(time.time())

    with auth_connection() as conn:
        if not email_is_allowed(email, conn):
            insert_auth_event(conn, email, "password_reset_failed", "email_not_allowed", request_ip(request))
            conn.commit()
            raise password_reset_code_error()

        row = conn.execute(
            """
            SELECT users.id, users.email, users.name, users.email_verified_at,
                   password_reset_codes.code_hash, password_reset_codes.attempts,
                   password_reset_codes.expires_at
            FROM users
            LEFT JOIN password_reset_codes ON password_reset_codes.email = users.email
            WHERE users.email = ?
            """,
            (email,),
        ).fetchone()
        if not row:
            insert_auth_event(conn, email, "password_reset_failed", "user_not_found", request_ip(request))
            conn.commit()
            raise password_reset_code_error()
        if int(row["email_verified_at"] or 0) <= 0:
            insert_auth_event(conn, email, "password_reset_failed", "email_not_verified", request_ip(request))
            conn.commit()
            raise password_reset_code_error()
        if not row["code_hash"] or int(row["expires_at"] or 0) <= now:
            conn.execute("DELETE FROM password_reset_codes WHERE email = ?", (email,))
            insert_auth_event(conn, email, "password_reset_failed", "code_expired", request_ip(request))
            conn.commit()
            raise password_reset_code_error()
        if int(row["attempts"] or 0) >= 5:
            conn.execute("DELETE FROM password_reset_codes WHERE email = ?", (email,))
            insert_auth_event(conn, email, "password_reset_failed", "too_many_attempts", request_ip(request))
            conn.commit()
            raise HTTPException(status_code=429, detail="Слишком много неверных попыток. Запросите новый код.")
        if not verify_password(code, str(row["code_hash"])):
            conn.execute("UPDATE password_reset_codes SET attempts = attempts + 1 WHERE email = ?", (email,))
            insert_auth_event(conn, email, "password_reset_failed", "bad_code", request_ip(request))
            conn.commit()
            raise password_reset_code_error()

        logged_in_at = int(time.time())
        conn.execute(
            "UPDATE users SET password_hash = ?, last_login_at = ? WHERE email = ?",
            (hash_password(password), logged_in_at, email),
        )
        conn.execute("DELETE FROM password_reset_codes WHERE email = ?", (email,))
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (int(row["id"]),))
        user = user_from_row(row)
        insert_auth_event(conn, email, "password_reset_success", "password_updated", request_ip(request))

    create_session(response, request, user.id)
    return AuthFlowResponse(user=user, message="Пароль обновлен.")


@app.post("/api/auth/logout")
def auth_logout(request: Request, response: Response):
    clear_session(response, request)
    return {"ok": True}


@app.get("/api/stations")
def stations_payload(response: Response, _user: AuthUser = Depends(require_user)):
    response.headers["Cache-Control"] = "private, max-age=3600"
    response.headers["Vary"] = "Cookie"
    return load_station_payload()


@app.get("/api/staff/periods")
def available_staff_periods(_user: AuthUser = Depends(require_user)):
    periods = staff_periods()
    return {
        "source": "file" if periods else "none",
        "updatedAt": staff_updated_at(),
        "periods": periods,
    }


@app.get("/api/kpis/periods", response_model=KpiPeriodsResponse)
def available_kpi_periods(_user: AuthUser = Depends(require_user)):
    mode = data_mode().lower()
    periods = kpi_periods() if mode in {"local", "file"} else []
    with kpi_connection() as conn:
        updated_at = latest_kpi_updated_at(conn) if periods else datetime.now(timezone.utc).isoformat(timespec="seconds")
    return KpiPeriodsResponse(
        source="local" if periods else mode,
        updatedAt=updated_at,
        periods=periods,
    )


@app.get("/api/stations/{ksss}/kpis", response_model=StationKpiResponse)
def station_kpis(
    ksss: str,
    period: str = Query(default_factory=current_period, pattern=r"^\d{4}-\d{2}$"),
    _user: AuthUser = Depends(require_user),
):
    ksss = validate_ksss(ksss)
    period = validate_period(period)
    mode = data_mode().lower()

    if mode == "mock":
        return mock_kpis(ksss, period)
    if mode in {"local", "file"}:
        return local_kpis(ksss, period)
    if mode == "db":
        return db_kpis(ksss, period)

    raise HTTPException(status_code=500, detail=f"Unsupported APP_DATA_MODE: {mode}")


@app.get("/api/stations/{ksss}/staff", response_model=StationStaffResponse)
def station_staff(
    ksss: str,
    period: str = Query(default_factory=current_period, pattern=r"^\d{4}-\d{2}$"),
    _user: AuthUser = Depends(require_user),
):
    ksss = validate_ksss(ksss)
    period = validate_period(period)
    mode = data_mode().lower()

    if mode in {"mock", "file", "local"}:
        staff = file_staff(ksss, period)
        if staff:
            return staff
        if mode in {"file", "local"}:
            raise HTTPException(status_code=404, detail="Staff data not found")
        return mock_staff(ksss, period)
    if mode == "db":
        db_extension_not_ready(STAFF_SQL_TEMPLATE)

    raise HTTPException(status_code=500, detail=f"Unsupported APP_DATA_MODE: {mode}")


@app.get("/api/stations/{ksss}/fuel-stock", response_model=FuelStockStationResponse)
def station_fuel_stock(
    ksss: str,
    _user: AuthUser = Depends(require_user),
):
    ksss = validate_ksss(ksss)
    stock = get_station_fuel_stock(ksss)
    if not stock:
        raise HTTPException(status_code=404, detail="Fuel stock data not found")
    return stock


@app.get("/api/analytics/overview", response_model=AnalyticsOverviewResponse)
def analytics_overview(
    period: str = Query(default_factory=current_period, pattern=r"^\d{4}-\d{2}$"),
    groupBy: str = Query("territoryManager"),
    _user: AuthUser = Depends(require_user),
):
    period = validate_period(period)
    mode = data_mode().lower()

    if mode == "mock":
        return mock_overview(period, groupBy)
    if mode in {"local", "file"}:
        return local_overview(period, groupBy)
    if mode == "db":
        db_extension_not_ready(ANALYTICS_SQL_TEMPLATE)

    raise HTTPException(status_code=500, detail=f"Unsupported APP_DATA_MODE: {mode}")


@app.get("/api/stations/{ksss}/similar", response_model=SimilarStationsResponse)
def station_similar(
    ksss: str,
    period: str = Query(default_factory=current_period, pattern=r"^\d{4}-\d{2}$"),
    limit: int = Query(10, ge=1, le=30),
    _user: AuthUser = Depends(require_user),
):
    ksss = validate_ksss(ksss)
    period = validate_period(period)
    mode = data_mode().lower()

    if mode == "mock":
        return mock_similar(ksss, period, limit)
    if mode in {"local", "file"}:
        return local_similar(ksss, period, limit)
    if mode == "db":
        db_extension_not_ready(SIMILAR_SQL_TEMPLATE)

    raise HTTPException(status_code=500, detail=f"Unsupported APP_DATA_MODE: {mode}")


@app.get("/api/analytics/compare", response_model=CompareResponse)
def analytics_compare(
    period: str = Query(default_factory=current_period, pattern=r"^\d{4}-\d{2}$"),
    ksss: list[str] = Query(default_factory=list),
    _user: AuthUser = Depends(require_user),
):
    period = validate_period(period)
    mode = data_mode().lower()

    if mode == "mock":
        return mock_compare(ksss, period)
    if mode in {"local", "file"}:
        return local_compare(ksss, period)
    if mode == "db":
        db_extension_not_ready(COMPARE_SQL_TEMPLATE)

    raise HTTPException(status_code=500, detail=f"Unsupported APP_DATA_MODE: {mode}")
