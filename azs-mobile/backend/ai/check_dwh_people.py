"""Проверка справочников РУ, ТМ, статуса и типа АЗС в ОХД перед подключением к ИИ.

    scripts/python.sh -m backend.ai.check_dwh_people

Запускается на машине с доступом к витрине (VPN). Соединение только для чтения,
все запросы агрегатные: ФИО не печатаются и никуда не записываются. Скрипт
отвечает на вопросы, от которых зависит правильность сумм по руководителю:

  * типы столбцов, по которым идёт соединение, совпадают с витриной;
  * у действующих периодов конец пустой или проставлен датой (от этого зависит
    форма соединения — с COALESCE или без);
  * периоды одного объекта не пересекаются — иначе день попадёт к двум
    руководителям и сумма задвоится;
  * какая доля дней последнего месяца витрины находит РУ и ТМ;
  * в справочнике статуса и типа одна строка на объект, и какие там значения.

Итог пишется в DWH_outputs/bds_values.json: точные значения статуса и типа АЗС,
признак пустого конца периода и предупреждения. Затем пересоберите каталог:

    scripts/python.sh backend/ai/build_dwh_catalog.py
"""
from __future__ import annotations

import datetime as dt
import json
import os
from decimal import Decimal
from pathlib import Path

APP = Path(__file__).resolve().parents[2]
ROOT = APP.parent
OUT = ROOT / "DWH_outputs" / "bds_values.json"

FACTS = "dm.data_for_ai_analytic_part_1"
PEOPLE = {"РУ": ("bds.l_azs_rm_dt_vers", "rm_fio"), "ТМ": ("bds.l_azs_tm_dt_vers", "tm_fio")}
KSSS_REF = "bds.s_azs_ksss"
USED_COLUMNS = {
    "l_azs_rm_dt_vers": ["ksss_code", "dt_vers_start", "dt_vers_end", "rm_fio"],
    "l_azs_tm_dt_vers": ["ksss_code", "dt_vers_start", "dt_vers_end", "tm_fio"],
    "s_azs_ksss": ["ksss_azs_code", "status_azs_name", "type_azs_name"],
}


def _load_env() -> None:
    """Реквизиты витрины из окружения, а при их отсутствии — из .env.local.

    Значения не печатаются. Читаются только переменные DWH_*.
    """
    if os.getenv("DWH_DB_HOST"):
        return
    path = APP / ".env.local"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("DWH_") and key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")


def _connect():
    try:
        import psycopg2
    except ImportError as err:  # pragma: no cover
        raise SystemExit("Не установлен psycopg2: запускайте через scripts/python.sh") from err
    _load_env()
    missing = [n for n in ("DWH_DB_HOST", "DWH_DB_NAME", "DWH_DB_USER", "DWH_DB_PASSWORD") if not os.getenv(n)]
    if missing:
        raise SystemExit("Не заданы переменные окружения: " + ", ".join(missing))
    conn = psycopg2.connect(
        host=os.environ["DWH_DB_HOST"], port=os.getenv("DWH_DB_PORT", "5432"),
        dbname=os.environ["DWH_DB_NAME"], user=os.environ["DWH_DB_USER"],
        password=os.environ["DWH_DB_PASSWORD"],
        connect_timeout=int(os.getenv("DWH_CONNECT_TIMEOUT_SECONDS", "10")),
        options="-c statement_timeout=120000",
    )
    conn.set_session(readonly=True, autocommit=False)
    return conn


def _plain(value):
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    return value


def _rows(conn, sql: str) -> list[tuple]:
    with conn.cursor() as cursor:
        cursor.execute(sql)
        return [tuple(_plain(v) for v in row) for row in cursor.fetchall()]


def _one(conn, sql: str) -> tuple:
    rows = _rows(conn, sql)
    return rows[0] if rows else ()


def main() -> int:
    conn = _connect()
    warnings: list[str] = []
    report: dict = {"checked_at": dt.datetime.now().isoformat(timespec="seconds"), "values": {}}
    try:
        print("1. Типы столбцов, которые видит ИИ")
        names = ", ".join(f"'{t}'" for t in USED_COLUMNS)
        types = _rows(conn, f"""
            SELECT table_name, column_name, data_type
            FROM information_schema.columns
            WHERE table_schema = 'bds' AND table_name IN ({names})""")
        found = {}
        for table, column, type_ in types:
            found.setdefault(table, {})[column] = type_
        facts_key = _one(conn, """
            SELECT data_type FROM information_schema.columns
            WHERE table_schema = 'dm' AND table_name = 'data_for_ai_analytic_part_1'
              AND column_name = 'ksss_azs_code'""")
        print(f"   dm.data_for_ai_analytic_part_1.ksss_azs_code: {facts_key[0] if facts_key else '?'}")
        for table, columns in USED_COLUMNS.items():
            present = found.get(table, {})
            others = len(present) - len([c for c in columns if c in present])
            listed = ", ".join(f"{c} {present.get(c, 'НЕТ')}" for c in columns)
            print(f"   bds.{table}: {listed}; прочих столбцов (ИИ их не видит): {others}")
            for column in columns:
                if column not in present:
                    warnings.append(f"в bds.{table} нет столбца {column}")
        report["types"] = {t: {c: found.get(t, {}).get(c) for c in cols} for t, cols in USED_COLUMNS.items()}

        print("\n2. Периоды закрепления РУ и ТМ")
        open_end_any = False
        for role, (table, fio) in PEOPLE.items():
            row = _one(conn, f"""
                SELECT COUNT(*), COUNT(DISTINCT ksss_code), COUNT(DISTINCT {fio}),
                       COUNT(*) FILTER (WHERE dt_vers_end IS NULL),
                       COUNT(*) FILTER (WHERE dt_vers_start > dt_vers_end),
                       COUNT(*) FILTER (WHERE {fio} IS NULL OR TRIM({fio}) = ''),
                       MIN(dt_vers_start), MAX(dt_vers_start), MAX(dt_vers_end)
                FROM {table}""")
            versions, objects, people, open_end, inverted, no_name, first, last_start, last_end = row
            print(f"   {role}: периодов {versions}, объектов {objects}, людей {people}; "
                  f"пустой конец {open_end}, конец раньше начала {inverted}, без ФИО {no_name}; "
                  f"начала с {first} по {last_start}, последний конец {last_end}")
            overlap = _one(conn, f"""
                WITH v AS (SELECT ksss_code, dt_vers_start AS s,
                                  COALESCE(dt_vers_end, DATE '9999-12-31') AS e,
                                  ROW_NUMBER() OVER (ORDER BY ksss_code, dt_vers_start) AS rn
                           FROM {table})
                SELECT COUNT(*), COUNT(*) FILTER (WHERE a.e = b.s)
                FROM v a JOIN v b ON a.ksss_code = b.ksss_code AND a.rn < b.rn
                                 AND a.s <= b.e AND b.s <= a.e""")
            print(f"   {role}: пересекающихся пар периодов {overlap[0]}, из них стык в один день {overlap[1]}")
            report[role] = {"versions": versions, "objects": objects, "open_end": open_end,
                            "overlaps": overlap[0], "boundary_overlaps": overlap[1], "last_end": last_end}
            if open_end:
                open_end_any = True
            if overlap[0]:
                warnings.append(f"{role}: {overlap[0]} пересечений периодов — дни на стыке попадут к двум "
                                f"руководителям и суммы задвоятся; нужно правило выбора периода")
            if inverted:
                warnings.append(f"{role}: {inverted} периодов с концом раньше начала")

        print("\n3. Покрытие дней последнего месяца витрины")
        for role, (table, _fio) in PEOPLE.items():
            end = "COALESCE(r.dt_vers_end, DATE '9999-12-31')" if open_end_any else "r.dt_vers_end"
            row = _one(conn, f"""
                WITH b AS (SELECT DATE_TRUNC('month', MAX(account_date))::date AS d0, MAX(account_date) AS d1
                           FROM {FACTS}),
                     m AS (SELECT f.ksss_azs_code, f.account_date, COUNT(r.ksss_code) AS n
                           FROM {FACTS} f
                           CROSS JOIN b
                           LEFT JOIN {table} r ON r.ksss_code = f.ksss_azs_code
                                AND f.account_date >= r.dt_vers_start AND f.account_date <= {end}
                           WHERE f.account_date BETWEEN b.d0 AND b.d1
                           GROUP BY f.ksss_azs_code, f.account_date)
                SELECT (SELECT d0 FROM b), (SELECT d1 FROM b), COUNT(*),
                       COUNT(*) FILTER (WHERE n = 0), COUNT(*) FILTER (WHERE n > 1)
                FROM m""")
            d0, d1, days, unmatched, duplicated = row
            share = f"{100.0 * unmatched / days:.1f} %" if days else "—"
            print(f"   {role}: {d0}…{d1}, объекто-дней {days}; без руководителя {unmatched} ({share}), "
                  f"с двумя и более {duplicated}")
            report[role].update({"month": [d0, d1], "object_days": days, "unmatched": unmatched,
                                 "duplicated": duplicated})
            if duplicated:
                warnings.append(f"{role}: {duplicated} объекто-дней нашли больше одного руководителя — суммы задвоятся")
            if days and unmatched / days > 0.05:
                warnings.append(f"{role}: {share} объекто-дней без руководителя — суммы по людям будут неполными")

        print("\n4. Статус и тип АЗС")
        total, objects = _one(conn, f"SELECT COUNT(*), COUNT(DISTINCT ksss_azs_code) FROM {KSSS_REF}")
        print(f"   строк {total}, объектов {objects}")
        if total != objects:
            warnings.append(f"{KSSS_REF}: {total - objects} лишних строк на объект — соединение умножит факты")
        coverage = _one(conn, f"""
            SELECT COUNT(DISTINCT f.ksss_azs_code),
                   COUNT(DISTINCT f.ksss_azs_code) FILTER (WHERE k.ksss_azs_code IS NULL)
            FROM {FACTS} f LEFT JOIN {KSSS_REF} k ON k.ksss_azs_code = f.ksss_azs_code
            WHERE f.account_date >= (SELECT DATE_TRUNC('month', MAX(account_date))::date FROM {FACTS})""")
        print(f"   объектов витрины за последний месяц {coverage[0]}, из них нет в справочнике {coverage[1]}")
        report["ksss_ref"] = {"rows": total, "objects": objects, "facts_objects": coverage[0],
                              "missing": coverage[1]}
        for column in ("status_azs_name", "type_azs_name"):
            values = _rows(conn, f"""
                SELECT {column}, COUNT(*) FROM {KSSS_REF}
                WHERE {column} IS NOT NULL GROUP BY 1 ORDER BY 2 DESC LIMIT 30""")
            report["values"][column] = [{"value": v, "objects": n} for v, n in values]
            print(f"   {column}: " + "; ".join(f"{v} — {n}" for v, n in values))
    finally:
        conn.rollback()
        conn.close()

    report["open_end"] = open_end_any
    report["warnings"] = warnings
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\nИтог:")
    for warning in warnings or ["замечаний нет"]:
        print(f"   {warning}")
    print(f"Записано: {OUT}. Теперь: scripts/python.sh backend/ai/build_dwh_catalog.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
