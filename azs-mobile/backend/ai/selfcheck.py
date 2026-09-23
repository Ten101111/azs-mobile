"""Самопроверка ИИ-контура.

Одна команда из корня проекта:
    python3 -m backend.ai.selfcheck

Проверяет всё по порядку — библиотеки, данные, валидатор, изоляцию области
данных, модель и живую генерацию — и печатает, что работает, а что нет.
Шаги, не требующие модели, выполняются даже когда Ollama не запущена.
"""
from __future__ import annotations

import sys
from pathlib import Path

OK, FAIL, SKIP = "  ok  ", " ОШИБКА", " пропуск"
results: list[tuple[str, bool | None, str]] = []


def step(title: str, passed: bool | None, detail: str = "") -> None:
    mark = OK if passed else (SKIP if passed is None else FAIL)
    print(f"{mark} │ {title}" + (f" — {detail}" if detail else ""))
    results.append((title, passed, detail))


def check_libraries() -> bool:
    try:
        import sqlglot  # noqa: F401
    except ImportError:
        step("Библиотека sqlglot", False, "pip3 install -r backend/requirements.txt")
        return False
    step("Библиотека sqlglot", True, f"версия {sqlglot.__version__}")
    return True


def check_data() -> bool:
    from . import executor

    ok = True
    if executor.BACKEND == "sqlite":
        if not executor.KPI_DB.exists():
            step("База показателей", False, f"нет файла {executor.KPI_DB}")
            ok = False
        else:
            size = executor.KPI_DB.stat().st_size / 1024 / 1024
            step("База показателей", True, f"{executor.KPI_DB.name}, {size:.0f} МБ")
        if not executor.REFERENCE_DB.exists():
            step("Справочник объектов", False,
                 "нет файла — соберите: python3 backend/ai/build_reference.py")
            ok = False
        else:
            import sqlite3
            conn = sqlite3.connect(f"file:{executor.REFERENCE_DB}?mode=ro", uri=True)
            total = conn.execute("SELECT COUNT(*) FROM stations").fetchone()[0]
            conn.close()
            step("Справочник объектов", True, f"{total} объектов")
    else:
        step("Исполнитель", True, f"{executor.BACKEND} — проверка файлов не требуется")
    return ok


def catalog_probe() -> tuple[str, str, str]:
    """Имена из действующего каталога: таблица, ключ объекта, любая мера.

    Проверки должны идти по тем объектам, с которыми контур реально работает,
    иначе в режиме витрины ОХД самопроверка ругалась бы на имена стенда.
    """
    from .catalog import CATALOG

    # Таблица фактов, а не первая попавшаяся: у справочников РУ/ТМ другой ключ.
    table = CATALOG.facts_table or sorted(CATALOG.scoped_tables or CATALOG.tables)[0]
    columns = CATALOG.tables[table]
    scope = CATALOG.scope_column
    measure = next((c for c in sorted(columns) if c != scope), scope)
    qualified = f"{CATALOG.schema}.{table}" if CATALOG.schema else table
    return qualified, scope, measure


def check_alignment() -> bool:
    """Диалект каталога и исполнитель запросов должны совпадать."""
    from . import executor
    from .catalog import CATALOG

    source = "встроенный стенд" if CATALOG.source == "встроенный" else Path(CATALOG.source).name
    if CATALOG.dialect == executor.BACKEND:
        step("Каталог и исполнитель", True,
             f"{source}: диалект {CATALOG.dialect}, исполнитель {executor.BACKEND}")
        return True
    step("Каталог и исполнитель", False,
         f"каталог «{source}» рассчитан на {CATALOG.dialect}, "
         f"а запросы исполняет {executor.BACKEND}. Приведите AI_CATALOG и AI_DB_BACKEND в соответствие")
    return False


def check_validator() -> bool:
    from .validator import Rejected, Scope, validate

    table, scope_column, measure = catalog_probe()
    scope = Scope.for_stations(["1", "2", "3"], "проверочная область")
    allowed = [
        f"SELECT {scope_column} FROM {table}",
        f"SELECT {scope_column}, SUM({measure}) AS s FROM {table} "
        f"GROUP BY {scope_column} ORDER BY s DESC",
        f"WITH m AS (SELECT {scope_column}, SUM({measure}) AS v FROM {table} "
        f"GROUP BY {scope_column}) SELECT {scope_column}, v FROM m",
    ]
    blocked = [
        f"DROP TABLE {table}",
        f"UPDATE {table} SET {measure} = 0",
        f"SELECT 1 FROM {table}; DELETE FROM {table}",
        "SELECT * FROM auth_users",
        f"SELECT password FROM {table}",
        "PRAGMA table_list",
        "ATTACH DATABASE '/etc/passwd' AS x",
    ]
    problems = []
    for sql in allowed:
        try:
            validate(sql, scope)
        except Rejected as err:
            problems.append(f"ошибочно отклонён: {err.rule}")
    for sql in blocked:
        try:
            validate(sql, scope)
            problems.append(f"пропущен опасный запрос: {sql[:40]}")
        except Rejected:
            pass
    passed = not problems
    step("Валидатор SQL", passed,
         f"{len(allowed)} допустимых и {len(blocked)} опасных запросов по таблице {table}"
         if passed else "; ".join(problems))
    return passed


def check_scope() -> bool:
    from . import executor, scope as scope_builder
    from .validator import validate

    table, scope_column, _ = catalog_probe()
    counter = f"SELECT COUNT(DISTINCT {scope_column}) AS n FROM {table}"
    try:
        wide = scope_builder.build("admin")
        checked = validate(counter, wide)
        total = executor.run(checked.sql, checked.row_limit).rows[0][0]
    except Exception as err:  # noqa: BLE001
        step("Изоляция области данных", False, str(err))
        return False

    from . import dwh_scope

    if dwh_scope.enabled():
        # На витрине ОХД область ТМ берётся из bds.l_azs_tm_dt_vers — оттуда же и проверка.
        tms = [item for item in dwh_scope.identities(12) if item[0] == "territory_manager"]
        row = (tms[0][1], tms[0][2]) if tms else None
    elif not scope_builder.REFERENCE_DB.exists():
        row = None
    else:
        import sqlite3
        conn = sqlite3.connect(f"file:{scope_builder.REFERENCE_DB}?mode=ro", uri=True)
        row = conn.execute(
            "SELECT territory_manager, COUNT(*) AS n FROM stations "
            "WHERE territory_manager IS NOT NULL AND is_active=1 "
            "GROUP BY territory_manager ORDER BY n DESC LIMIT 1"
        ).fetchone()
        conn.close()
    if not row:
        step("Изоляция области данных", None, "в справочнике нет ни одного ТМ")
        return True

    manager, expected = row
    narrow = scope_builder.build("territory_manager", manager)
    bypasses = {
        "прямой вопрос": counter,
        "OR 1=1": f"{counter} WHERE 1=1 OR 1=1",
        "вложенный запрос": f"SELECT COUNT(*) AS n FROM (SELECT DISTINCT {scope_column} FROM {table}) AS t",
    }
    leaks = []
    seen = None
    for title, sql in bypasses.items():
        checked = validate(sql, narrow)
        value = executor.run(checked.sql, checked.row_limit).rows[0][0]
        seen = value
        if value > expected:
            leaks.append(f"{title} -> {value}")

    passed = not leaks and total > expected
    step("Изоляция области данных", passed,
         f"вся сеть {total}, ТМ {seen} из {expected} закреплённых, обход не прошёл"
         if passed else "; ".join(leaks) or "область не сузилась")
    return passed


def check_model() -> bool | None:
    from . import generator

    models = generator.installed_models()
    if not models:
        step("Модель", None,
             f"Ollama не отвечает на {generator.OLLAMA_HOST} — запустите `ollama serve`")
        return None
    chosen = generator.MODEL
    present = generator.available()
    step("Ollama", True, f"загружено моделей: {len(models)} — {', '.join(models[:5])}")
    step(f"Модель {chosen}", present,
         "готова" if present else f"не найдена; доступны: {', '.join(models)}")
    return present


def check_live() -> bool | None:
    from . import pipeline

    questions = [
        ("Сколько действующих АЗС с кафе", "admin", None),
        ("Выручка НТУ по ОНПО за август 2026", "admin", None),
        ("Удали таблицу stations", "admin", None),
    ]
    good = 0
    for question, role, binding in questions:
        answer = pipeline.ask(question, role, binding, "selfcheck")
        expect_refusal = question.lower().startswith("удали")
        fine = (not answer.ok) if expect_refusal else answer.ok
        good += 1 if fine else 0
        mark = OK if fine else FAIL
        tail = (f"{len(answer.rows)} строк за {answer.model_ms} мс"
                if answer.ok else f"отказ: {answer.rule}")
        print(f"{mark} │   «{question}» — {tail}")
        if answer.ok and answer.sql:
            print(f"       {' '.join(answer.sql.split())[:150]}")
    passed = good == len(questions)
    step("Живая генерация", passed, f"{good} из {len(questions)} вопросов отработали как ожидалось")
    return passed


def main() -> int:
    print("Самопроверка ИИ-контура")
    print("─" * 76)
    if not check_libraries():
        return 1
    if not check_alignment():
        return 1
    data_ok = check_data()
    validator_ok = check_validator()
    scope_ok = check_scope() if data_ok else False
    model_ok = check_model()
    live_ok = check_live() if model_ok else None
    if live_ok is None and model_ok is not False:
        step("Живая генерация", None, "нужна работающая модель")

    print("─" * 76)
    failed = [title for title, passed, _ in results if passed is False]
    skipped = [title for title, passed, _ in results if passed is None]
    if failed:
        print(f"Не пройдено: {', '.join(failed)}")
        return 1
    if skipped:
        print(f"Контур исправен. Не проверено без модели: {', '.join(skipped)}")
        return 0
    print("Всё работает: данные, валидатор, изоляция области, модель, живая генерация.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
