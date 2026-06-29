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
