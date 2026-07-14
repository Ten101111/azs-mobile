#!/usr/bin/env python3
"""Import the latest matching fuel-outage email as a replace-all XLSX snapshot."""

from __future__ import annotations

import argparse
import imaplib
import json
import os
import ssl
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import timezone
from email import policy
from email.header import decode_header, make_header
from email.parser import BytesParser
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path
from typing import Any

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - local dependency guidance
    load_dotenv = None


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

from backend.fuel_outages import FuelOutageImportError, rows_from_email, xlsx_from_rows  # noqa: E402


DEFAULT_SENDER = "Artem.Manokhin@lukoil.com"
DEFAULT_SUBJECT = "Отчет по простоям объектов из-за отсутствия топлива"
DEFAULT_STATE_PATH = PROJECT_DIR / "data" / "fuel_outage_mail_state.json"
DEFAULT_WORKBOOK_PATH = PROJECT_DIR / "data" / "mail" / "fuel_outages_latest.xlsx"


def load_env() -> None:
    if not load_dotenv:
        return
    load_dotenv(PROJECT_DIR / ".env")
    load_dotenv(PROJECT_DIR / ".env.local", override=True)


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def positive_int(name: str, default: int) -> int:
    try:
        return max(1, int(env(name, str(default))))
    except ValueError:
        return default


def decode_mail_header(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value))).strip()
    except (LookupError, UnicodeDecodeError):
        return value.strip()


def normalize_subject(value: str) -> str:
    return " ".join(value.split()).casefold()


def derived_imap_host() -> str:
    explicit = env("IMAP_HOST")
    if explicit:
        return explicit
    smtp_host = env("SMTP_HOST")
    return "imap." + smtp_host[5:] if smtp_host.casefold().startswith("smtp.") else smtp_host


def import_api_url() -> str:
    explicit = env("FUEL_OUTAGE_IMPORT_URL")
    if explicit:
        return explicit
    return f"{env('APP_PUBLIC_URL', 'http://127.0.0.1:8000').rstrip('/')}/api/internal/fuel-outages/import"


def state_path() -> Path:
    configured = env("FUEL_OUTAGE_MAIL_STATE_PATH")
    return Path(configured) if configured else DEFAULT_STATE_PATH


def workbook_path() -> Path:
    configured = env("FUEL_OUTAGE_MAIL_XLSX_PATH")
    return Path(configured) if configured else DEFAULT_WORKBOOK_PATH


def read_state() -> dict[str, Any]:
    try:
        payload = json.loads(state_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        temporary_path.chmod(0o600)
        os.replace(temporary_path, path)
        path.chmod(0o600)
    finally:
        temporary_path.unlink(missing_ok=True)


def write_state(payload: dict[str, Any]) -> None:
    atomic_write(state_path(), json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def response_bytes(response: list[Any]) -> bytes:
    for item in response:
        if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], bytes):
            return item[1]
    return b""


def latest_matching_message(client: imaplib.IMAP4, sender: str, subject_fragment: str, scan_limit: int):
    status, data = client.uid("search", None, "ALL")
    if status != "OK" or not data or not data[0]:
        return None

    expected_sender = sender.casefold()
    expected_subject = normalize_subject(subject_fragment)
    skipped_messages = 0
    last_parse_error: FuelOutageImportError | None = None
    for uid in reversed(data[0].split()[-scan_limit:]):
        status, header_data = client.uid(
            "fetch",
            uid,
            "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID)])",
        )
        if status != "OK":
            continue
        header_bytes = response_bytes(header_data)
        if not header_bytes:
            continue
        headers = BytesParser(policy=policy.default).parsebytes(header_bytes, headersonly=True)
        from_email = parseaddr(decode_mail_header(headers.get("From")))[1].casefold()
        subject = decode_mail_header(headers.get("Subject"))
        if from_email != expected_sender or expected_subject not in normalize_subject(subject):
            continue

        status, message_data = client.uid("fetch", uid, "(BODY.PEEK[])")
        if status != "OK":
            continue
        message_bytes = response_bytes(message_data)
        if not message_bytes:
            continue
        message = BytesParser(policy=policy.default).parsebytes(message_bytes)
        try:
            rows = rows_from_email(message)
        except FuelOutageImportError as exc:
            # Forwarded or test messages can match the headers without carrying the report.
            skipped_messages += 1
            last_parse_error = exc
            continue
        return uid.decode("ascii", errors="ignore"), message, rows, skipped_messages
    if last_parse_error:
        raise last_parse_error
    return None


def source_received_at(message) -> str:
    try:
        received = parsedate_to_datetime(message.get("Date", ""))
    except (TypeError, ValueError, OverflowError):
        return ""
    if received is None:
        return ""
    if received.tzinfo is None:
        received = received.replace(tzinfo=timezone.utc)
    return received.astimezone(timezone.utc).isoformat(timespec="seconds")


def safe_http_header(value: str, max_length: int) -> str:
    compact = " ".join(value.replace("\r", " ").replace("\n", " ").split())[:max_length]
    return compact.encode("ascii", errors="ignore").decode("ascii")


def upload_xlsx(content: bytes, message_id: str, received_at: str, from_email: str) -> dict[str, Any]:
    token = env("FUEL_OUTAGE_IMPORT_TOKEN")
    if not token:
        raise SystemExit("FUEL_OUTAGE_IMPORT_TOKEN is required")
    request = urllib.request.Request(
        import_api_url(),
        data=content,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "Accept": "application/json",
            "X-Source-Message-Id": safe_http_header(message_id, 500),
            "X-Source-Received-At": safe_http_header(received_at, 80),
            "X-Source-Email-From": safe_http_header(from_email, 254),
        },
        method="POST",
    )
    timeout = positive_int("FUEL_OUTAGE_IMPORT_TIMEOUT_SECONDS", 60)
    retries = positive_int("FUEL_OUTAGE_IMPORT_RETRIES", 3)
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
                return payload if isinstance(payload, dict) else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            retryable = exc.code == 429 or 500 <= exc.code < 600
            if not retryable or attempt >= retries:
                raise SystemExit(f"Fuel outage import failed with HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt >= retries:
                raise SystemExit(f"Fuel outage import connection failed: {getattr(exc, 'reason', exc)}") from exc
        time.sleep(min(30, 2**attempt))
    raise SystemExit("Fuel outage import failed")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import the latest fuel-outage email")
    parser.add_argument("--force", action="store_true", help="Process the latest message even if its ID was already imported")
    return parser.parse_args()


def main() -> int:
    load_env()
    args = parse_args()
    host = derived_imap_host()
    username = env("IMAP_USERNAME", env("SMTP_USERNAME", env("SMTP_FROM_EMAIL")))
    password = os.getenv("IMAP_PASSWORD") or os.getenv("SMTP_PASSWORD") or ""
    if not host or not username or not password:
        raise SystemExit("IMAP/SMTP mailbox credentials are not configured")

    context = ssl.create_default_context()
    timeout = positive_int("IMAP_TIMEOUT_SECONDS", 30)
    try:
        with imaplib.IMAP4_SSL(host, positive_int("IMAP_PORT", 993), ssl_context=context, timeout=timeout) as client:
            client.login(username, password)
            status, _ = client.select(env("IMAP_MAILBOX", "INBOX"), readonly=True)
            if status != "OK":
                raise SystemExit("Unable to open the configured IMAP mailbox")
            match = latest_matching_message(
                client,
                env("FUEL_OUTAGE_EMAIL_FROM", DEFAULT_SENDER),
                env("FUEL_OUTAGE_EMAIL_SUBJECT", DEFAULT_SUBJECT),
                positive_int("FUEL_OUTAGE_EMAIL_SCAN_LIMIT", 300),
            )
    except imaplib.IMAP4.error as exc:
        raise SystemExit(f"IMAP authentication failed: {exc}") from exc
    except OSError as exc:
        raise SystemExit(f"IMAP connection failed: {exc}") from exc

    if not match:
        print("No matching fuel-outage email found.", flush=True)
        return 0

    uid, message, rows, skipped_messages = match
    message_id = decode_mail_header(message.get("Message-ID")) or f"imap-uid:{uid}"
    if not args.force and read_state().get("messageId") == message_id:
        return 0

    xlsx_content = xlsx_from_rows(rows)
    atomic_write(workbook_path(), xlsx_content)
    received_at = source_received_at(message)
    from_email = parseaddr(decode_mail_header(message.get("From")))[1]
    response = upload_xlsx(xlsx_content, message_id, received_at, from_email)
    write_state(
        {
            "messageId": message_id,
            "imapUid": uid,
            "receivedAt": received_at,
            "importedAt": response.get("importedAt", ""),
            "rows": response.get("imported", len(rows)),
        }
    )
    print(
        "Fuel outage email import complete: "
        f"rows={response.get('imported', len(rows))}, stations={response.get('stations', 0)}, "
        f"active={response.get('active', 0)}, unchanged={bool(response.get('unchanged'))}, "
        f"newer_messages_skipped={skipped_messages}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
