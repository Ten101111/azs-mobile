"""Файлы пользователя для ИИ-аналитика (ИИ-07, ИИ-11; решения Р-5, Р-6).

Хранится только разобранное содержимое (таблицы и текст с подписью места),
исходный файл не сохраняется. Файл принадлежит одному человеку и лежит в
одном месте:
  * диалог — учитывается во всех вопросах этого диалога (ИИ-07);
  * папка — учитывается во всех диалогах папки (ИИ-11), до files_per_folder;
  * черновик — приложен к первому вопросу нового диалога, при сохранении ответа
    переходит в диалог; непривязанный черновик удаляется через сутки.

Лимиты — ИИ-02 (quotas.py): файлов на диалог или папку — files_per_folder,
общий объём файлов человека — user_files_mb (по размеру исходных файлов).
Удаляются вместе с диалогом или папкой (dialogs.py) и по сроку истории.
В журнал ответа пишутся имя, вид, размер и контрольная сумма — не содержимое.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from typing import Any, Callable

from . import dialogs, file_parse, quotas

DRAFT_TTL_S = 24 * 3600
MB = 1024 * 1024


class Invalid(Exception):
    pass


def _now() -> int:
    return int(time.time())


def _summary(kind: str, meta: dict, parts: list[dict]) -> str:
    tables = [p for p in parts if p["kind"] == "table"]
    rows = sum(len(p.get("rows") or []) for p in tables)

    def word(n, one, few, many):
        n10, n100 = n % 10, n % 100
        return one if n10 == 1 and n100 != 11 else few if 2 <= n10 <= 4 and not 12 <= n100 <= 14 else many

    if kind == "xlsx":
        return f"{len(tables)} {word(len(tables), 'лист', 'листа', 'листов')}, {rows:,} {word(rows, 'строка', 'строки', 'строк')}".replace(",", " ")
    if kind == "csv":
        return f"{rows:,} {word(rows, 'строка', 'строки', 'строк')}".replace(",", " ")
    if kind == "pdf":
        pages = int(meta.get("pages") or 0)
        return f"{pages} {word(pages, 'страница', 'страницы', 'страниц')}"
    if kind == "pptx":
        slides = int(meta.get("slides") or 0)
        return f"{slides} {word(slides, 'слайд', 'слайда', 'слайдов')}"
    chars = sum(len(p.get("text") or "") for p in parts)
    extra = f", {len(tables)} {word(len(tables), 'таблица', 'таблицы', 'таблиц')}" if tables else ""
    return f"{chars:,} знаков{extra}".replace(",", " ")


def _describe(row: sqlite3.Row, parts: list[dict] | None = None) -> dict:
    meta = json.loads(row["meta_json"] or "{}")
    return {
        "id": int(row["id"]), "name": row["name"], "kind": row["kind"],
        "kindTitle": file_parse.KIND_TITLES.get(row["kind"], row["kind"]),
        "size": int(row["size"]), "sha256": row["sha256"],
        "dialogId": row["dialog_id"], "folderId": row["folder_id"],
        "draft": row["dialog_id"] is None and row["folder_id"] is None,
        "summary": meta.get("summary") or "", "notes": json.loads(row["notes_json"] or "[]"),
        "createdAt": int(row["created_at"]),
    }


def _container_count(conn, user_id: int, dialog_id: int | None, folder_id: int | None, since: int) -> int:
    if dialog_id:
        where, args = "dialog_id = ?", [dialog_id]
    elif folder_id:
        where, args = "folder_id = ?", [folder_id]
    else:
        where, args = "dialog_id IS NULL AND folder_id IS NULL AND created_at >= ?", [since]
    return int(conn.execute(f"SELECT COUNT(*) FROM ai_files WHERE user_id = ? AND {where}", [user_id, *args]).fetchone()[0])


def used_bytes(user_id: int, conn: sqlite3.Connection | None = None) -> int:
    own = conn is None
    conn = conn or dialogs._connect()
    try:
        return int(conn.execute("SELECT COALESCE(SUM(size), 0) FROM ai_files WHERE user_id = ?", (user_id,)).fetchone()[0])
    finally:
        if own:
            conn.close()


def add(user_id: int, role: str | None, name: str, data: bytes, *, dialog_id: int | None = None,
        folder_id: int | None = None, parse: Callable[[str, bytes], dict] | None = None) -> dict:
    """Принять файл: проверки, разбор, сохранение. Отказ — file_parse.Rejected или dialogs.Limit."""
    name = " ".join((name or "").replace("/", " ").replace("\\", " ").split())[:180] or "файл"
    if dialog_id and folder_id:
        raise Invalid("Файл лежит либо в диалоге, либо в папке")
    file_parse.detect(name, data)
    digest = hashlib.sha256(data).hexdigest()
    now = _now()
    conn = dialogs._connect()
    try:
        if dialog_id:
            dialogs._owned(conn, dialog_id, user_id)
        if folder_id:
            dialogs._folder(conn, folder_id, user_id)
        same = conn.execute(
            "SELECT * FROM ai_files WHERE user_id = ? AND sha256 = ? AND COALESCE(dialog_id, 0) = ? "
            "AND COALESCE(folder_id, 0) = ?", (user_id, digest, dialog_id or 0, folder_id or 0)).fetchone()
        if same is not None:
            return {**_describe(same), "duplicate": True}
        per_place = int(quotas.value(role, "files_per_folder"))
        if _container_count(conn, user_id, dialog_id, folder_id, now - DRAFT_TTL_S) >= per_place:
            quotas.record_hit(role, "files_per_folder")
            where = "в папке" if folder_id else "в диалоге"
            raise dialogs.Limit("files_per_folder", f"Файлов {where} — не больше {per_place}. Удалите лишний и загрузите снова.")
        budget = int(quotas.value(role, "user_files_mb")) * MB
        if used_bytes(user_id, conn) + len(data) > budget:
            quotas.record_hit(role, "user_files_mb")
            raise dialogs.Limit("user_files_mb", f"Объём ваших файлов — не больше {budget // MB} МБ. "
                                                 "Удалите ненужные файлы в диалогах или папках.")
    finally:
        conn.close()

    parsed = (parse or file_parse.parse_isolated)(name, data)
    meta = dict(parsed.get("meta") or {})
    meta["summary"] = _summary(parsed["kind"], meta, parsed["parts"])
    conn = dialogs._connect()
    try:
        cursor = conn.execute(
            "INSERT INTO ai_files (user_id, dialog_id, folder_id, name, kind, size, sha256, meta_json, notes_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, dialog_id, folder_id, name, parsed["kind"], len(data), digest,
             json.dumps(meta, ensure_ascii=False), json.dumps(parsed.get("notes") or [], ensure_ascii=False), now))
        file_id = int(cursor.lastrowid)
        for seq, part in enumerate(parsed["parts"]):
            conn.execute(
                "INSERT INTO ai_file_parts (file_id, seq, kind, label, columns_json, rows_json, text, first_row, truncated) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (file_id, seq, part["kind"], part["label"],
                 json.dumps(part.get("columns"), ensure_ascii=False) if part["kind"] == "table" else None,
                 json.dumps(part.get("rows"), ensure_ascii=False, default=str) if part["kind"] == "table" else None,
                 part.get("text"), part.get("firstRow"), 1 if part.get("truncated") else 0))
        if dialog_id:
            conn.execute("UPDATE ai_dialogs SET updated_at = ? WHERE id = ?", (now, dialog_id))
        conn.commit()
        row = conn.execute("SELECT * FROM ai_files WHERE id = ?", (file_id,)).fetchone()
    finally:
        conn.close()
    return _describe(row)


def list_files(user_id: int, *, dialog_id: int | None = None, folder_id: int | None = None,
               ids: list[int] | None = None) -> list[dict]:
    conn = dialogs._connect()
    try:
        if dialog_id:
            rows = conn.execute("SELECT * FROM ai_files WHERE user_id = ? AND dialog_id = ? ORDER BY id",
                                (user_id, dialog_id)).fetchall()
        elif folder_id:
            rows = conn.execute("SELECT * FROM ai_files WHERE user_id = ? AND folder_id = ? ORDER BY id",
                                (user_id, folder_id)).fetchall()
        elif ids:
            marks = ",".join("?" * len(ids))
            rows = conn.execute(f"SELECT * FROM ai_files WHERE user_id = ? AND id IN ({marks}) ORDER BY id",
                                (user_id, *[int(i) for i in ids])).fetchall()
        else:
            rows = []
    finally:
        conn.close()
    return [_describe(r) for r in rows]


def delete(file_id: int, user_id: int) -> None:
    conn = dialogs._connect()
    try:
        row = conn.execute("SELECT id FROM ai_files WHERE id = ? AND user_id = ?", (file_id, user_id)).fetchone()
        if row is None:
            raise dialogs.NotFound("Файл не найден")
        conn.execute("DELETE FROM ai_file_parts WHERE file_id = ?", (file_id,))
        conn.execute("DELETE FROM ai_files WHERE id = ?", (file_id,))
        conn.commit()
    finally:
        conn.close()


def link_drafts(user_id: int, file_ids: list[int], dialog_id: int) -> int:
    """Черновики, приложенные к первому вопросу, переходят в созданный диалог."""
    ids = [int(i) for i in file_ids or [] if str(i).isdigit()]
    if not ids:
        return 0
    conn = dialogs._connect()
    try:
        dialogs._owned(conn, dialog_id, user_id)
        marks = ",".join("?" * len(ids))
        moved = conn.execute(
            f"UPDATE ai_files SET dialog_id = ? WHERE user_id = ? AND dialog_id IS NULL AND folder_id IS NULL "
            f"AND id IN ({marks})", (dialog_id, user_id, *ids)).rowcount
        conn.commit()
    finally:
        conn.close()
    return int(moved)


def for_question(user_id: int, dialog_id: int | None, draft_ids: list[int] | None = None) -> list[dict]:
    """Файлы, которые учитывает вопрос: диалога, его папки и черновики этого вопроса — с содержимым."""
    conn = dialogs._connect()
    try:
        where, args = [], []
        if dialog_id:
            where.append("dialog_id = ?")
            args.append(int(dialog_id))
            folder = conn.execute("SELECT folder_id FROM ai_dialogs WHERE id = ? AND user_id = ?",
                                  (int(dialog_id), user_id)).fetchone()
            if folder and folder["folder_id"]:
                where.append("folder_id = ?")
                args.append(int(folder["folder_id"]))
        drafts = [int(i) for i in draft_ids or [] if str(i).isdigit()]
        if drafts:
            where.append(f"(dialog_id IS NULL AND folder_id IS NULL AND id IN ({','.join('?' * len(drafts))}))")
            args.extend(drafts)
        if not where:
            return []
        rows = conn.execute(f"SELECT * FROM ai_files WHERE user_id = ? AND ({' OR '.join(where)}) "
                            "ORDER BY folder_id IS NULL, id", (user_id, *args)).fetchall()
        out = []
        for row in rows:
            item = _describe(row)
            item["place"] = "folder" if row["folder_id"] else "dialog"
            item["parts"] = [
                {"kind": p["kind"], "label": p["label"],
                 "columns": json.loads(p["columns_json"]) if p["columns_json"] else None,
                 "rows": json.loads(p["rows_json"]) if p["rows_json"] else None,
                 "text": p["text"], "firstRow": p["first_row"], "truncated": bool(p["truncated"])}
                for p in conn.execute("SELECT * FROM ai_file_parts WHERE file_id = ? ORDER BY seq", (row["id"],))]
            out.append(item)
        return out
    finally:
        conn.close()


def for_journal(files: list[dict]) -> list[dict]:
    """Что пишется в журнал ответа: без содержимого."""
    return [{"name": f["name"], "kind": f["kind"], "size": f["size"], "sha256": f["sha256"],
             "place": f.get("place", "dialog")} for f in files]


def cleanup_drafts(now: int | None = None) -> int:
    now = int(now if now is not None else time.time())
    conn = dialogs._connect()
    try:
        ids = [int(r[0]) for r in conn.execute(
            "SELECT id FROM ai_files WHERE dialog_id IS NULL AND folder_id IS NULL AND created_at < ?",
            (now - DRAFT_TTL_S,))]
        for file_id in ids:
            conn.execute("DELETE FROM ai_file_parts WHERE file_id = ?", (file_id,))
            conn.execute("DELETE FROM ai_files WHERE id = ?", (file_id,))
        conn.commit()
    finally:
        conn.close()
    return len(ids)


def storage(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """Файлы по владельцам для отчёта о хранении: {user_id: {"files", "bytes"}}."""
    return {int(r["user_id"]): {"files": int(r["n"]), "bytes": int(r["b"])} for r in conn.execute(
        "SELECT user_id, COUNT(*) AS n, COALESCE(SUM(size), 0) AS b FROM ai_files GROUP BY user_id")}
