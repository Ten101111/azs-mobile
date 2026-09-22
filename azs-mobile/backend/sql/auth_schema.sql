CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE CHECK (instr(email, '@') > 1),
    name TEXT NOT NULL DEFAULT '',
    password_hash TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    email_verified_at INTEGER NOT NULL DEFAULT 0,
    last_login_at INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX idx_users_email ON users(email);
CREATE INDEX idx_users_email_verified ON users(email_verified_at);

CREATE TABLE email_allowlist (
    email TEXT NOT NULL PRIMARY KEY CHECK (instr(email, '@') > 1),
    note TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL
);

CREATE TABLE sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX idx_sessions_user ON sessions(user_id);
CREATE INDEX idx_sessions_expires ON sessions(expires_at);

CREATE TABLE email_verification_codes (
    email TEXT PRIMARY KEY CHECK (instr(email, '@') > 1),
    code_hash TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    last_sent_at INTEGER NOT NULL,
    FOREIGN KEY(email) REFERENCES users(email) ON DELETE CASCADE
);

CREATE INDEX idx_email_codes_expires ON email_verification_codes(expires_at);

CREATE TABLE password_reset_codes (
    email TEXT PRIMARY KEY CHECK (instr(email, '@') > 1),
    code_hash TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    last_sent_at INTEGER NOT NULL,
    FOREIGN KEY(email) REFERENCES users(email) ON DELETE CASCADE
);

CREATE INDEX idx_password_reset_codes_expires ON password_reset_codes(expires_at);

CREATE TABLE auth_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL CHECK (instr(email, '@') > 1),
    event TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    ip TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL
);

CREATE INDEX idx_auth_events_email ON auth_events(email);
CREATE INDEX idx_auth_events_created ON auth_events(created_at);

-- Usage analytics: one row per visit (continuous activity window per user).
-- A visit is extended by heartbeats/events and closed after
-- ANALYTICS_VISIT_GAP_SECONDS (default 30 min) of inactivity.
CREATE TABLE visits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    started_at INTEGER NOT NULL,
    last_seen_at INTEGER NOT NULL,
    ended_at INTEGER NOT NULL DEFAULT 0,
    duration_seconds INTEGER NOT NULL DEFAULT 0,
    ip TEXT NOT NULL DEFAULT '',
    device_type TEXT NOT NULL DEFAULT '',
    os TEXT NOT NULL DEFAULT '',
    browser TEXT NOT NULL DEFAULT '',
    screen_size TEXT NOT NULL DEFAULT '',
    pwa INTEGER NOT NULL DEFAULT 0,
    language TEXT NOT NULL DEFAULT '',
    timezone TEXT NOT NULL DEFAULT '',
    user_agent TEXT NOT NULL DEFAULT '',
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX idx_visits_user ON visits(user_id);
CREATE INDEX idx_visits_started ON visits(started_at);
CREATE INDEX idx_visits_open ON visits(user_id, ended_at);

-- In-app events: screen views and key actions (station_open, filter, ...).
CREATE TABLE usage_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    visit_id INTEGER NOT NULL DEFAULT 0,
    event TEXT NOT NULL,
    screen TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX idx_usage_events_user ON usage_events(user_id);
CREATE INDEX idx_usage_events_created ON usage_events(created_at);
CREATE INDEX idx_usage_events_event ON usage_events(event);
