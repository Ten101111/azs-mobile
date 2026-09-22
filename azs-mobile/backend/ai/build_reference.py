"""Сборка справочника объектов для ИИ-контура.

Читает data/stations.json и складывает плоскую таблицу stations в
data/ai_reference.sqlite3. Файл kpi_metrics.sqlite3 не изменяется:
исполнитель запросов открывает его только на чтение и подключает
этот справочник отдельным ATTACH.
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

DATA = Path(__file__).resolve().parents[2] / "data"
SRC = DATA / "stations.json"
DST = Path(os.environ.get("AI_REFERENCE_DB") or DATA / "ai_reference.sqlite3")

DDL = """
DROP TABLE IF EXISTS stations;
CREATE TABLE stations (
    ksss              TEXT PRIMARY KEY,
    station_number    TEXT,
    name              TEXT,
    station_type      TEXT,
    status            TEXT,
    npo               TEXT,
    region            TEXT,
    city              TEXT,
    address           TEXT,
    format            TEXT,
    location          TEXT,
    service_cluster   TEXT,
    shop              TEXT,
    shop_area         REAL,
    trk_count         REAL,
    posts_count       REAL,
    has_cafe          INTEGER,
    has_shop          INTEGER,
    is_active         INTEGER,
    is_agency         INTEGER,
    regional_manager  TEXT,
    territory_manager TEXT,
    manager           TEXT
);
CREATE INDEX idx_stations_npo ON stations(npo);
CREATE INDEX idx_stations_region ON stations(region);
CREATE INDEX idx_stations_tm ON stations(territory_manager);
CREATE INDEX idx_stations_rm ON stations(regional_manager);
"""

FIELDS = [
    ("ksss", "ksss"), ("station_number", "stationNumber"), ("name", "name"),
    ("station_type", "type"), ("status", "status"), ("npo", "npo"),
    ("region", "subject"), ("city", "city"), ("address", "address"),
    ("format", "format"), ("location", "environment"),
    ("service_cluster", "serviceCluster"), ("shop", "shop"),
    ("shop_area", "shopArea"), ("trk_count", "trkCount"), ("posts_count", "postsCount"),
    ("regional_manager", "regionalManager"), ("territory_manager", "territoryManager"),
    ("manager", "manager"),
]
FLAGS = [("has_cafe", "hasCafe"), ("has_shop", "hasShop"),
         ("is_active", "active"), ("is_agency", "agency")]

COLUMNS = [c for c, _ in FIELDS] + [c for c, _ in FLAGS]


def clean(value):
    if isinstance(value, str):
        value = value.strip()
        if value.lower() in {"", "отсутствует", "нет данных", "-"}:
            return None
    return value


def main() -> None:
    payload = json.loads(SRC.read_text(encoding="utf-8"))
    stations = payload["stations"]

    rows = []
    for item in stations:
        flags = item.get("flags") or {}
        row = [clean(item.get(src)) for _, src in FIELDS]
        row += [1 if flags.get(src) else 0 for _, src in FLAGS]
        rows.append(row)

    DST.unlink(missing_ok=True)
    conn = sqlite3.connect(DST)
    conn.executescript(DDL)
    placeholders = ",".join("?" * len(COLUMNS))
    conn.executemany(
        f"INSERT OR REPLACE INTO stations ({','.join(COLUMNS)}) VALUES ({placeholders})",
        rows,
    )
    conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM stations").fetchone()[0]
    named = conn.execute(
        "SELECT COUNT(*) FROM stations WHERE territory_manager IS NOT NULL"
    ).fetchone()[0]
    conn.close()
    print(f"{DST.name}: {total} объектов, из них {named} с назначенным ТМ")


if __name__ == "__main__":
    main()
