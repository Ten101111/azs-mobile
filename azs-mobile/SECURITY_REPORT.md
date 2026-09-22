# Security Audit Report — Классификатор АЗС
**Date:** 2026-06-30
**Auditor:** Security Architect (automated review + manual analysis)
**Scope:** FastAPI backend (`backend/main.py`), React PWA frontend (`src/main.jsx`), environment configuration (`.env.example`), Vite config (`vite.config.js`)

---

## Executive Summary

The application implements a solid authentication foundation (PBKDF2 password hashing at 260 000 iterations, session token hashing, OTP brute-force lockout after 5 attempts, domain-allowlist enforcement, structured audit logging). No SQL injection vulnerabilities were found in the current codebase — all SQLite queries use parameterized statements and all PostgreSQL queries use `%(param)s` placeholders via `psycopg2`.

However, several configuration defaults and implementation gaps would create exploitable conditions in a production deployment. All critical and high-severity issues have been remediated in this review.

---

## Findings and Remediations

### P0 — Critical (remediated)

#### 1. CORS Wildcard in `.env.example`
**Severity:** Critical
**CVE class:** CWE-942 (Permissive Cross-domain Policy)

**Was:** `CORS_ORIGINS=*`
When a developer copies `.env.example` to `.env` (the standard onboarding step), the backend accepted credentialed cross-origin requests from any domain. An attacker hosting a malicious web page could trigger authenticated API calls on behalf of a logged-in LUKOIL employee.

**Fixed:** Changed to `CORS_ORIGINS=http://localhost:5173,http://localhost:5174`. The code's built-in safe default (`DEFAULT_CORS_ORIGINS`) already listed only localhost, but the example file was overriding it with a wildcard.

---

#### 2. OTP Code in API Response (`AUTH_EMAIL_DEV_MODE=true`)
**Severity:** Critical
**CVE class:** CWE-200 (Exposure of Sensitive Information)

**Was:** `.env.example` had `AUTH_EMAIL_DEV_MODE=true`. When this flag is active, the 6-digit OTP code is included in the API response body (`devCode` field) and displayed on the login screen. Any attacker able to observe network traffic or browser devtools could complete email verification without access to the corporate mailbox.

**Fixed:** Changed default in `.env.example` to `AUTH_EMAIL_DEV_MODE=false`. Added startup log warnings (`CRITICAL` for `AUTH_DISABLED`, `WARNING` for `AUTH_EMAIL_DEV_MODE`) so operators see these flags immediately on server start.

**For production:** SMTP must be configured. `AUTH_EMAIL_DEV_MODE` must be absent or explicitly `false`.

---

#### 3. IP Spoofing Bypass of Rate Limits
**Severity:** Critical
**CVE class:** CWE-348 (Use of Less Trusted Source for IP Address)

**Was:** Both `request_ip()` and `get_client_ip()` unconditionally read the **leftmost** IP from `X-Forwarded-For`. This header is entirely attacker-controlled when no trusted proxy enforces it. An attacker could set `X-Forwarded-For: 1.2.3.4` in every request to appear as a fresh IP address, bypassing all IP-based rate limits on `/api/auth/login`, `/api/auth/register`, `/api/auth/verify-email`, and `/api/auth/resend-code`.

**Fixed:** Introduced `TRUSTED_PROXY_DEPTH` environment variable (default `0`). With depth 0, `X-Forwarded-For` is ignored entirely and the direct TCP peer address is used. With depth N, the function takes the first entry to the left of the N rightmost entries (those added by trusted proxies). This is the correct algorithm for multi-hop proxy setups (same as what Django, Rails, and Express `trust proxy` implement).

Both `request_ip()` (used for audit logging) and `get_client_ip()` (used for rate limiting) now delegate to the same logic, eliminating the inconsistency.

**For production:** Set `TRUSTED_PROXY_DEPTH=1` if nginx/Caddy/ALB sits in front. Never set it higher than the actual number of trusted proxy hops.

---

### P1 — High (remediated)

#### 4. Missing Content-Security-Policy Headers
**Severity:** High
**CVE class:** CWE-1021 (Improper Restriction of Rendered UI Layers)

**Was:** The FastAPI security middleware set `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`, and `Permissions-Policy`, but no `Content-Security-Policy`.

**Fixed:**
- **FastAPI API responses** now include `Content-Security-Policy: default-src 'none'; frame-ancestors 'none'`. Since the API returns JSON only, this is the most restrictive safe policy.
- **Frontend `index.html`** now includes a CSP meta tag allowing Yandex Maps scripts (`api-maps.yandex.ru`, `yandex.ru`), inline styles (required by React's style props), blob/https images, and the Vite PWA service worker.
- **`vite.config.js` preview server** now sends security headers so that `vite preview` behaves closer to production.

**Note:** `frame-ancestors` in a `<meta>` tag is ignored by browsers — it must be in an HTTP response header set by nginx/Caddy in production.

---

#### 5. HSTS Not Sent Behind TLS-Terminating Proxy
**Severity:** High
**CVE class:** CWE-311 (Missing Encryption of Sensitive Data)

**Was:** `add_security_headers` checked `request.url.scheme == "https"` to decide whether to send `Strict-Transport-Security`. When the app runs behind nginx with TLS termination, `request.url.scheme` is always `"http"`, so HSTS was never sent.

**Fixed:** Changed condition to `request_is_https(request)`, which correctly inspects `X-Forwarded-Proto` and `CF-Visitor` headers set by trusted reverse proxies.

---

#### 6. Session Cookie `SameSite=Lax` Instead of `Strict`
**Severity:** High
**CVE class:** CWE-352 (Cross-Site Request Forgery)

**Was:** `samesite="lax"` on the session cookie. With `Lax`, the cookie is sent on top-level cross-site GET navigations (e.g., a `<a href>` link from an external page). For this internal corporate tool where all navigation originates from the same origin, there is no legitimate need to send the cookie cross-site.

**Fixed:** Changed to `samesite="strict"` in both `create_session()` and `clear_session()`. This means the cookie is never sent on cross-site requests regardless of the HTTP method, providing maximum CSRF protection without requiring CSRF tokens.

---

#### 7. Error Message Exposes Environment Variable Names
**Severity:** High
**CVE class:** CWE-209 (Generation of Error Message Containing Sensitive Information)

**Was:** When `APP_DATA_MODE=db` and database environment variables are missing, the HTTP 500 response body contained the exact variable names: `"Missing DB env vars: DB_HOST, DB_USER, DB_PASSWORD"`. This reveals internal configuration structure to any authenticated user.

**Fixed:** The server now logs the missing variable names at `ERROR` level internally and returns a generic message to the client: `"Database configuration is incomplete. Check server logs."`

---

#### 8. In-Memory Rate Limiter Memory Leak
**Severity:** Medium (could amplify a DoS into a memory exhaustion attack)

**Was:** `AUTH_RATE_LIMIT` accumulated entries indefinitely. Under a distributed attack with many spoofed IPs (or even normal traffic with many unique clients), the dict would grow without bound until the process ran out of memory.

**Fixed:** Every 500 calls to `enforce_rate_limit_key()`, stale entries (no timestamps within the last 10 minutes) are evicted. This bounds memory to the number of active unique rate-limit keys times the window size.

---

#### 9. No `validate_ksss()` — Unvalidated Path Parameter
**Severity:** Medium
**CVE class:** CWE-20 (Improper Input Validation)

**Was:** The `ksss` path parameter in `/api/stations/{ksss}/kpis`, `/staff`, and `/similar` was passed directly to downstream functions without any format check. While the current data layer (mock: hash computation; db: parameterized queries) is not exploitable, the absence of validation means future code changes could introduce vulnerabilities without warning.

**Fixed:** Added `validate_ksss()` enforcing the pattern `^[A-Za-z0-9][A-Za-z0-9_.:-]{0,31}$`. This rejects path-traversal sequences (`../`), null bytes, and injection payloads before they reach any data layer. Applied to all three affected endpoints.

---

### P0 — Critical Finding (requires operator action, not a code fix)

#### 10. Real Credentials in `.env.local`
**Severity:** Critical
**CVE class:** CWE-312 (Cleartext Storage of Sensitive Information)

**Finding:** `.env.local` contains a production SMTP password and a Yandex Maps API key in plaintext:
```
SMTP_PASSWORD=Qt7ZOIqYIdN9eNtTW8PN
VITE_YANDEX_MAPS_API_KEY=0aa56689-7a98-4c75-a0fd-a293c702581f
```

**Required actions:**
1. **Immediately rotate the SMTP password** for `lukoil_website_notifier@mail.ru`. Assume the password is compromised if this file was ever committed to version control.
2. **Rotate or restrict the Yandex Maps API key** to the specific production domain in the Yandex developer console.
3. A `.gitignore` file has been created to prevent `.env.local` from being committed in future. If the repo's history contains this file, use `git filter-repo` or `BFG Repo Cleaner` to purge it before next push.

---

## What Was Not Found

- **SQL injection:** All SQLite queries use `?` placeholders and all PostgreSQL queries use `%(name)s` psycopg2 parameters. No string-concatenated SQL was found.
- **Stored XSS:** API responses are JSON-only. The frontend renders user-supplied content through React's JSX (which escapes by default) and does not use `dangerouslySetInnerHTML`.
- **Authentication bypass:** The session validation query correctly checks `email_verified_at > 0`, `expires_at > NOW()`, and re-checks the email allowlist on every request.
- **OTP brute force (code level):** The 5-attempt limit per code is correctly enforced at the database level. Exhausting attempts deletes the code, forcing a new request-and-send cycle.
- **IDOR:** Station data is returned to any authenticated user. There is no per-user data scoping in the current model, which is consistent with the stated design (all corporate employees see all station data).

---

## Production Deployment Checklist

### Mandatory before go-live

- [ ] Rotate SMTP password for `lukoil_website_notifier@mail.ru`
- [ ] Restrict Yandex Maps API key to production domain in Yandex console
- [ ] Set `CORS_ORIGINS` to the exact production frontend URL (e.g. `https://azs-classifier.ru`)
- [ ] Set `AUTH_EMAIL_DEV_MODE=false` (or leave unset — it defaults to false when SMTP is configured)
- [ ] Set `AUTH_DISABLED=false` (or leave unset)
- [ ] Set `TRUSTED_PROXY_DEPTH=1` if nginx/Caddy/ALB terminates TLS in front of uvicorn
- [ ] Confirm SMTP is functional (send a test email to a lukoil.com address)
- [ ] Run the application as a non-root OS user
- [ ] Mount `data/` directory on a volume that is not world-readable

### nginx / reverse proxy layer

Add these headers on the static-file virtual host (the one serving `index.html`):

```nginx
add_header X-Content-Type-Options "nosniff" always;
add_header X-Frame-Options "DENY" always;
add_header Referrer-Policy "strict-origin-when-cross-origin" always;
add_header Permissions-Policy "geolocation=(self), camera=(), microphone=()" always;
add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
# frame-ancestors cannot be set in a <meta> tag; it must come from this header:
add_header Content-Security-Policy "
  default-src 'self';
  script-src 'self' https://api-maps.yandex.ru https://yandex.ru;
  style-src 'self' 'unsafe-inline';
  img-src 'self' data: blob: https:;
  connect-src 'self' https://api-maps.yandex.ru https://geocode-maps.yandex.ru https://suggest-maps.yandex.ru https://yandex.ru;
  font-src 'self' data:;
  worker-src 'self' blob:;
  object-src 'none';
  base-uri 'self';
  frame-ancestors 'none'
" always;
```

### Secrets management (P2 recommendation)

Move secrets out of `.env` files into a proper secrets manager before production:
- **HashiCorp Vault** (self-hosted) or **Yandex Lockbox** (cloud-native for the Russia region)
- Inject secrets as environment variables at container/VM startup, not baked into images or files
- Enable secret rotation for the SMTP credential (minimum: rotate quarterly, or immediately on any suspected compromise)

### Monitoring and alerting

The application writes structured auth events to `auth_events` table (login_failed, verify_failed, session_blocked, etc.). Wire up alerts for:
- More than 20 `login_failed` events for any single email in 10 minutes → credential stuffing
- More than 5 `verify_failed` events on a single email → OTP brute force
- Any `session_blocked` event (email removed from allowlist while session was active)
- CRITICAL log line from `azs.security` logger (AUTH_DISABLED or dev mode active)

---

## Summary of Code Changes Made

| File | Change |
|------|--------|
| `.env.example` | `CORS_ORIGINS` changed from `*` to `localhost` origins; `AUTH_EMAIL_DEV_MODE` changed to `false`; added `AUTH_SESSION_TTL_SECONDS`, `AUTH_DISABLED`, `TRUSTED_PROXY_DEPTH` with secure defaults and comments |
| `backend/main.py` | Added `TRUSTED_PROXY_DEPTH` constant; rewrote `request_ip()` and `get_client_ip()` to use rightmost-IP logic; added CSP header and fixed HSTS condition in `add_security_headers`; changed session cookie `samesite` from `lax` to `strict`; added `validate_ksss()` and applied to 3 endpoints; fixed env-var leak in `db_kpis`; added rate-limiter memory cleanup; added startup warnings for insecure modes |
| `index.html` | Added CSP `<meta http-equiv>` tag covering Yandex Maps, blob images, service worker |
| `vite.config.js` | Added security headers to `preview` server configuration |
| `.gitignore` | Created to prevent `.env.local`, SQLite database, and private data files from being committed |
