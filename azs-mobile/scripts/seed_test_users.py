"""Тестовые учётные записи для проверки ролевой модели.

Заводит по одному пользователю на роль, сразу с подтверждённой почтой и
назначенной привязкой, чтобы можно было войти и посмотреть приложение
глазами каждой роли. Только для локальной проверки.

    python3 scripts/seed_test_users.py            создать или обновить
    python3 scripts/seed_test_users.py --remove   убрать все тестовые записи
    python3 scripts/seed_test_users.py --password "своя строка"

Почты начинаются с test- — по этому признаку скрипт их и удаляет,
настоящих пользователей не трогает.
"""
from __future__ import annotations

import argparse
import hashlib
import secrets
import sqlite3
import sys
import time
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))

from backend import roles  # noqa: E402

AUTH_DB = APP / "data" / "auth.sqlite3"
PREFIX = "test-"
DEFAULT_PASSWORD = "AzsRoles2026!"
ITERATIONS = 260_000

# Привязки взяты из действующего справочника объектов.
TEST_USERS = [
    ("test-subadmin@lukoil.com", "ТЕСТ · Субадминистратор", "subadmin", ""),
    ("test-aup-seti@lukoil.com", "ТЕСТ · АУП сети", "aup_network", ""),
    ("test-aup-onpo@lukoil.com", "ТЕСТ · АУП общества УНП", "aup_npo", "УНП"),
    ("test-ru@lukoil.com", "ТЕСТ · РУ Ткач О. Н.", "regional_manager", "Ткач Олег Николаевич"),
    ("test-tm@lukoil.com", "ТЕСТ · ТМ Блинова М. А.", "territory_manager", "Блинова Мария Александровна"),
    ("test-agent@lukoil.com", "ТЕСТ · Агент, 3 АЗС", "agent", "5044, 9753, 9755"),
    ("test-azs@lukoil.com", "ТЕСТ · Управляющий АЗС 5044", "station", "5044"),
]


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, ITERATIONS)
    return f"pbkdf2_sha256${ITERATIONS}${salt.hex()}${digest.hex()}"


def connect(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise SystemExit(f"База авторизации не найдена: {path}\nЗапустите приложение хотя бы раз.")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_columns(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
    for column, ddl in (
        ("role", "role TEXT NOT NULL DEFAULT ''"),
        ("role_binding", "role_binding TEXT NOT NULL DEFAULT ''"),
        ("role_assigned_at", "role_assigned_at INTEGER NOT NULL DEFAULT 0"),
        ("role_assigned_by", "role_assigned_by TEXT NOT NULL DEFAULT ''"),
    ):
        if column not in columns:
            conn.execute(f"ALTER TABLE users ADD COLUMN {ddl}")


def remove(conn: sqlite3.Connection) -> int:
    rows = conn.execute("SELECT id, email FROM users WHERE email LIKE ?", (PREFIX + "%",)).fetchall()
    for row in rows:
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (row["id"],))
        conn.execute("DELETE FROM users WHERE id = ?", (row["id"],))
        print(f"  удалён {row['email']}")
    conn.commit()
    return len(rows)


def seed(conn: sqlite3.Connection, password: str) -> list[tuple]:
    now = int(time.time())
    created = []
    for email, name, role, binding in TEST_USERS:
        scope = roles.resolve(role, binding)
        existing = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
        if existing:
            conn.execute(
                "UPDATE users SET name = ?, password_hash = ?, email_verified_at = ?, "
                "role = ?, role_binding = ?, role_assigned_at = ?, role_assigned_by = ? WHERE id = ?",
                (name, hash_password(password), now, role, binding, now, "seed-script", existing["id"]),
            )
            action = "обновлён"
        else:
            conn.execute(
                "INSERT INTO users (email, name, password_hash, created_at, email_verified_at, "
                "last_login_at, role, role_binding, role_assigned_at, role_assigned_by) "
                "VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?)",
                (email, name, hash_password(password), now, now, role, binding, now, "seed-script"),
            )
            action = "создан"
        created.append((email, name, role, scope.label, len(scope.problems) and scope.problems[0] or "", action))
    conn.commit()
    return created


def main() -> int:
    parser = argparse.ArgumentParser(description="Тестовые пользователи по ролям")
    parser.add_argument("--remove", action="store_true", help="удалить все учётные записи test-*")
    parser.add_argument("--password", default=DEFAULT_PASSWORD, help="пароль для всех тестовых записей")
    parser.add_argument("--db", default=str(AUTH_DB))
    args = parser.parse_args()

    conn = connect(Path(args.db))
    try:
        ensure_columns(conn)
        if args.remove:
            count = remove(conn)
            print(f"\nУдалено тестовых записей: {count}")
            return 0

        rows = seed(conn, args.password)
        print("Тестовые учётные записи (только для локальной проверки)\n")
        width = max(len(r[0]) for r in rows)
        for email, name, role, label, problem, action in rows:
            print(f"  {email:<{width}}  {name}")
            print(f"  {'':<{width}}  роль {role} · {label}" + (f" · ВНИМАНИЕ: {problem}" if problem else ""))
            print()
        print(f"Пароль у всех один: {args.password}")
        print("\nАдминистратор — artem.manokhin@lukoil.com, права берутся из ADMIN_EMAILS,")
        print("назначать ему роль не нужно.")
        print("\nУбрать всё: python3 scripts/seed_test_users.py --remove")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    main()
