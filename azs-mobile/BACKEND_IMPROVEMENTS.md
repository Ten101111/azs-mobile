# Backend Improvements — Классификатор АЗС

Applied to `backend/main.py` on 2026-06-30.

---

## 1. GZip Middleware

`GZipMiddleware` (minimum_size=1000 bytes) was added by the preceding security agent before this run. Placement is correct: it is registered via `add_middleware` before `CORSMiddleware`, which in Starlette's LIFO stack means CORS is the outermost layer and GZip sits just inside it, compressing API responses transparently for all clients that advertise `Accept-Encoding: gzip`.

No change needed here; documented for completeness.

---

## 2. Structured Request Logging with Request ID

Added imports: `import logging`, `import uuid`.

Added at module level:

```python
logger = logging.getLogger("azs-api")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
```

Added `@app.middleware("http") async def log_requests(...)` after the security-headers middleware. Each request emits a single INFO line:

```
2026-06-30 12:00:00 azs-api INFO [a1b2c3d4] POST /api/auth/login -> 200 (142ms)
```

The same 8-character `request_id` is also attached as the `X-Request-ID` response header, enabling end-to-end tracing from client logs to server logs without a distributed tracing backend.

---

## 3. OTP Brute-Force Protection — Status

No code changes were necessary. The protection is already fully implemented in the `email_verification_codes` table via the `attempts INTEGER` column (defined in `sql/auth_schema.sql` and mirrored in `init_auth_db()`). The `auth_verify_email` endpoint:

- Increments `attempts` on every wrong code (`UPDATE ... SET attempts = attempts + 1`)
- Checks `attempts >= 5` before the verify step; if true, deletes the record and returns HTTP 429
- Deletes the record on successful verification (resetting the counter implicitly)

The same pattern is applied in `auth_reset_password` via `password_reset_codes.attempts`. Creating a separate `otp_attempts` table would have been redundant and inconsistent with the existing schema.

---

## 4. Rate Limiting — Hardened X-Forwarded-For Handling

Added `import ipaddress`.

Added `get_client_ip(request: Request) -> str`:

```python
def get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        candidate = forwarded.split(",")[0].strip()
        try:
            ipaddress.ip_address(candidate)
            return candidate
        except ValueError:
            pass
    return (request.client.host if request.client else None) or "unknown"
```

The previous `client_key` function extracted `X-Forwarded-For` but did not validate the result against `ipaddress.ip_address`. A malformed or spoofed header value (e.g., `"evil-hostname, 1.2.3.4"`) could have been used as the rate-limit key, potentially bypassing per-IP limits. The new function validates the candidate with the stdlib `ipaddress` module and falls back to `request.client.host` if the header value is not a valid IP address.

`client_key` is simplified to a one-liner that delegates to `get_client_ip`.

---

## 5. Improved Health Endpoint

`GET /api/health` now performs an active SQLite probe and returns:

```json
{
  "status": "ok",
  "db": "ok",
  "activeSessions": 3,
  "mode": "mock",
  "version": "0.2.0"
}
```

- `status` is `"degraded"` when the SQLite connection fails.
- `db` mirrors the connection state explicitly for load-balancer health checks.
- `activeSessions` is the count of non-expired sessions; useful for operational monitoring.
- The probe uses a parameterized query (`WHERE expires_at > ?`) — no string interpolation.
- Exceptions are swallowed so a DB error returns HTTP 200 with `status: degraded` rather than HTTP 500, keeping reverse-proxy health checks non-disruptive.

---

## 6. Global Exception Handler

Added `from fastapi.responses import JSONResponse`.

Added `@app.exception_handler(Exception) async def global_exception_handler(...)`:

- Logs the full traceback at ERROR level via `logger.error(..., exc_info=True)` — visible in server logs but never in API responses.
- Returns a generic `{"detail": "Internal server error"}` with HTTP 500, preventing stack trace leakage to clients.
- Does not interfere with `HTTPException` (FastAPI handles those via its own handler before this one is invoked).

---

## 7. SQL Files — Parametrization Audit

All six SQL files in `backend/sql/` were audited:

| File | Status | Notes |
|---|---|---|
| `auth_schema.sql` | Safe | DDL only; no runtime parameters |
| `station_kpis.sql` | Safe | Uses `%(name)s` psycopg2 named params passed via `pd.read_sql(params=...)` |
| `analytics_overview.sql` | Placeholder | No executable SQL yet — comment scaffold only |
| `analytics_compare.sql` | Placeholder | No executable SQL yet — comment scaffold only |
| `station_similar.sql` | Placeholder | No executable SQL yet — comment scaffold only |
| `station_staff.sql` | Placeholder | No executable SQL yet — comment scaffold only |

No string concatenation found anywhere in the SQL files. All inline queries in `main.py` use SQLite `?` positional placeholders exclusively. When the placeholder files are replaced with real queries, they must continue to use `%(name)s` (psycopg2) or equivalent driver-level parameters — never f-strings or `.format()`.

---

## Validation

```
python3 -c "import backend.main; print('OK')"
# Output: OK
```
