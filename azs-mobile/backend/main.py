import json
import hashlib
import html
import os
import re
import secrets
import smtplib
import sqlite3
import time
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
from pydantic import BaseModel, Field

APP_DIR = Path(__file__).resolve().parent
PROJECT_DIR = APP_DIR.parent
DATA_DIR = PROJECT_DIR / "data"

if load_dotenv:
    load_dotenv(PROJECT_DIR / ".env")
    load_dotenv(PROJECT_DIR / ".env.local", override=True)

AUTH_DB_PATH = DATA_DIR / "auth.sqlite3"
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


class KpiMetric(BaseModel):
    id: str
    label: str
    value: float
    unit: str
    momPct: float
    yoyPct: float


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


app = FastAPI(title="AZS KPI API", version="0.2.0")
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


@app.middleware("http")
async def add_security_headers(request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Permissions-Policy", "geolocation=(self), camera=(), microphone=()")
    if request.url.scheme == "https":
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response


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


def request_ip(request: Optional[Request] = None) -> str:
    if not request:
        return ""
    forwarded = request.headers.get("x-forwarded-for", "")
    return forwarded.split(",")[0].strip() or (request.client.host if request.client else "")


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


def email_dev_mode() -> bool:
    return env_bool("AUTH_EMAIL_DEV_MODE", default=not smtp_configured())


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
    if not smtp_configured():
        if email_dev_mode():
            print(f"[auth] verification code for {email}: {code}", flush=True)
            return
        raise HTTPException(status_code=500, detail="SMTP не настроен для отправки кода подтверждения")

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
    message["Subject"] = "Код подтверждения для Классификатора АЗС"
    message["From"] = f"{from_name} <{from_email}>"
    message["To"] = email
    message.set_content(render_verification_email_text(code, email))
    message.add_alternative(render_verification_email_html(code, email, name), subtype="html")

    try:
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
    if not smtp_configured():
        if email_dev_mode():
            print(f"[auth] password reset code for {email}: {code}", flush=True)
            return
        raise HTTPException(status_code=500, detail="SMTP не настроен для отправки кода восстановления")

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
    message["Subject"] = "Код восстановления пароля для Классификатора АЗС"
    message["From"] = f"{from_name} <{from_email}>"
    message["To"] = email
    message.set_content(render_password_reset_email_text(code, email))
    message.add_alternative(render_password_reset_email_html(code, email, name), subtype="html")

    try:
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


def client_key(request: Request, scope: str) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    host = forwarded.split(",")[0].strip() or (request.client.host if request.client else "unknown")
    return f"{scope}:{host}"


def enforce_rate_limit_key(key: str, limit: int, window_seconds: int = 60):
    now = time.time()
    recent = [item for item in AUTH_RATE_LIMIT.get(key, []) if now - item < window_seconds]
    if len(recent) >= limit:
        raise HTTPException(status_code=429, detail="Слишком много попыток. Повторите позже.")
    recent.append(now)
    AUTH_RATE_LIMIT[key] = recent


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
        samesite="lax",
        path="/",
    )


def clear_session(response: Response, request: Request):
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if token:
        with auth_connection() as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (hash_session_token(token),))
    response.delete_cookie(SESSION_COOKIE_NAME, path="/", samesite="lax")


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


def make_metrics(values: dict[str, float], period: str, seed_key: str) -> list[KpiMetric]:
    metrics = [
        ("revenue", "Выручка", values.get("revenue", 0), "₽"),
        ("fuelVolume", "Объем топлива", values.get("fuelVolume", 0), "л"),
        ("checks", "Чеки", values.get("checks", 0), "шт"),
        ("avgCheck", "Средний чек", values.get("avgCheck", 0), "₽"),
    ]
    return [
        KpiMetric(
            id=metric_id,
            label=label,
            value=value,
            unit=unit,
            momPct=mock_pct(seed_key, period, metric_id, "mom"),
            yoyPct=mock_pct(seed_key, period, metric_id, "yoy"),
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
        raise HTTPException(status_code=500, detail=f"Missing DB env vars: {', '.join(missing)}")

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


def db_extension_not_ready(template: Path):
    raise HTTPException(
        status_code=501,
        detail=f"DB mode is reserved for real SQL. Fill {template.name} after table structure is known.",
    )


@app.get("/api/health")
def health():
    return {"status": "ok", "mode": data_mode()}


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
def stations_payload(_user: AuthUser = Depends(require_user)):
    return load_station_payload()


@app.get("/api/staff/periods")
def available_staff_periods(_user: AuthUser = Depends(require_user)):
    periods = staff_periods()
    return {
        "source": "file" if periods else "none",
        "updatedAt": staff_updated_at(),
        "periods": periods,
    }


@app.get("/api/stations/{ksss}/kpis", response_model=StationKpiResponse)
def station_kpis(
    ksss: str,
    period: str = Query(default_factory=current_period, pattern=r"^\d{4}-\d{2}$"),
    _user: AuthUser = Depends(require_user),
):
    period = validate_period(period)
    mode = data_mode().lower()

    if mode == "mock":
        return mock_kpis(ksss, period)
    if mode == "db":
        return db_kpis(ksss, period)

    raise HTTPException(status_code=500, detail=f"Unsupported APP_DATA_MODE: {mode}")


@app.get("/api/stations/{ksss}/staff", response_model=StationStaffResponse)
def station_staff(
    ksss: str,
    period: str = Query(default_factory=current_period, pattern=r"^\d{4}-\d{2}$"),
    _user: AuthUser = Depends(require_user),
):
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
    period = validate_period(period)
    mode = data_mode().lower()

    if mode == "mock":
        return mock_similar(ksss, period, limit)
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
    if mode == "db":
        db_extension_not_ready(COMPARE_SQL_TEMPLATE)

    raise HTTPException(status_code=500, detail=f"Unsupported APP_DATA_MODE: {mode}")
