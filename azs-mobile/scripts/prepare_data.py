import json
import math
import re
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[2]
APP = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "cls_2026_05_AZS.xlsx"
OUT = APP / "data" / "stations.json"


def clean(value):
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    text = str(value).strip()
    if text.lower() in {"nan", "none", "nat"}:
        return ""
    return text


def number(value):
    text = clean(value).replace(" ", "").replace(",", ".")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def yes(value):
    text = clean(value).lower()
    return text in {"да", "есть", "1", "true", "торговый зал"}


def normalize_phone(value):
    digits = re.sub(r"\D+", "", clean(value))
    if len(digits) == 10:
        return "+7" + digits
    if len(digits) == 11 and digits.startswith("8"):
        return "+7" + digits[1:]
    if len(digits) == 11 and digits.startswith("7"):
        return "+" + digits
    return clean(value)


def pick(row, *names):
    for name in names:
        if name in row and clean(row[name]):
            return clean(row[name])
    return ""


def flag(row, name):
    return clean(row.get(name, "")) in {"1", "1.0", "да", "Да", "true", "True"}


def quality_issues(item):
    issues = []
    if not item["address"]:
        issues.append("Не заполнен адрес")
    if not item["status"]:
        issues.append("Не заполнен статус")
    if not item["manager"] and not item["regionalManager"]:
        issues.append("Не заполнен ответственный")
    lat = item["lat"]
    lon = item["lon"]
    if lat is None or lon is None:
        issues.append("Нет координат")
    elif lat == 0 or lon == 0:
        issues.append("Координаты равны 0")
    if item["status"].lower() in {"действующая", "codo"} and (lat is None or lon is None or lat == 0 or lon == 0):
        issues.append("Действующий объект без валидной точки на карте")
    return issues


def main():
    df = pd.read_excel(SOURCE, sheet_name="cls_AZS")
    stations = []

    for _, row in df.iterrows():
        lat = number(row.get("Широта"))
        lon = number(row.get("Долгота"))
        ksss = pick(row, "КССС_union", "КССС_нов", "КССС_пред")
        station = {
            "id": ksss,
            "ksss": ksss,
            "ksssNew": pick(row, "КССС_нов"),
            "stationNumber": pick(row, "Номер_АЗС"),
            "name": pick(row, "Название_АЗС") or f"АЗС № {pick(row, 'Номер_АЗС')}",
            "type": pick(row, "Тип_АЗС"),
            "status": pick(row, "Статус"),
            "npo": pick(row, "НПО"),
            "subject": pick(row, "Субъект", "Субъект кратко"),
            "city": pick(row, "Город"),
            "address": pick(row, "Адрес_АЗС"),
            "lat": lat,
            "lon": lon,
            "regionalManager": pick(row, "ФИО_РУ", "ФИО_РУ.1"),
            "regionalManagerPhone": normalize_phone(pick(row, "Телефон_РУ", "Телефон_РУ.1")),
            "territoryManager": pick(row, "ФИО_ТМ_Агента", "ФИО_ТМ"),
            "territoryManagerPhone": normalize_phone(pick(row, "Телефон_ТМ_Агента", "Телефон_ТМ")),
            "manager": pick(row, "ФИО_менеджер_АЗС", "ФИО_менеджер_АЗС.1"),
            "managerPhone": normalize_phone(pick(row, "Телефон_менеджер_АЗС", "Телефон_менеджер_АЗС.1")),
            "seniorOperator": pick(row, "ФИО_старший_оператор_АЗС", "ФИО_старший_оператор_АЗС.1"),
            "seniorOperatorPhone": normalize_phone(pick(row, "Телефон_старший_оператор_АЗС", "Телефон_старший_оператор_АЗС.1")),
            "format": pick(row, "Формат"),
            "formatLevel2": pick(row, "Формат_level2"),
            "formatMinale": pick(row, "Формат_Minale"),
            "location": pick(row, "Локация").capitalize(),
            "environment": pick(row, "Тип_окружающей_местности"),
            "shop": pick(row, "Торговый_зал_магазин"),
            "shopArea": number(row.get("Площадь_торгзал")),
            "operatorArea": number(row.get("Площадь_операторной")),
            "toilet": pick(row, "Наличие_санузла"),
            "serviceCluster": pick(row, "Кластер_по_сервису"),
            "paymentType": pick(row, "Тип_оплаты"),
            "trkCount": number(row.get("Количество_ТРК")),
            "postsCount": number(row.get("Количество_постов_ТРК")),
            "roadFederal": pick(row, "Федеральная_трасса_учетный_номер"),
            "roadNumber": pick(row, "Трасса_учетный_номер"),
            "roadName": pick(row, "Трасса_наименование"),
            "lukCafeL1": pick(row, "Формат LukCafe_L1"),
            "lukCafeL2": pick(row, "Формат LukCafe_L2"),
            "dateChanged": pick(row, "Дата_добавления_изменения"),
            "comments": pick(row, "Комментарии"),
            "flags": {
                "active": flag(row, "Статус_действующая") or pick(row, "Статус").lower() in {"действующая", "codo"},
                "likard": flag(row, "Передача_АЗС_Ликард"),
                "teboil": flag(row, "Передача_АЗС_Тебойл"),
                "agency": flag(row, "Передача_АЗС_агентская_схема"),
                "md": flag(row, "Признак_MD"),
                "landmark": flag(row, "Знаковые_проекты"),
                "m11": flag(row, "Знаковые_проекты_1_М-11"),
                "m12": flag(row, "Знаковые_проекты_2_М-12"),
                "cityFlagship": flag(row, "Знаковые_проекты_3_ городские флагманы"),
                "roadFlagship": flag(row, "Знаковые_проекты_4_ трассовые флагманы"),
                "hasCafe": "кафе" in pick(row, "Кластер_по_сервису", "Формат LukCafe_L1").lower(),
                "hasShop": pick(row, "Торговый_зал_магазин").lower() not in {"", "нет магазина", "отсутствует"},
                "hasToilet": yes(row.get("Наличие_санузла")),
            },
        }
        station["qualityIssues"] = quality_issues(station)
        station["search"] = " ".join(
            clean(station.get(k, ""))
            for k in ["ksss", "stationNumber", "name", "status", "npo", "subject", "city", "address", "format", "regionalManager", "territoryManager"]
        ).lower()
        stations.append(station)

    meta = {
        "source": SOURCE.name,
        "generatedAt": pd.Timestamp.now().isoformat(timespec="seconds"),
        "count": len(stations),
    }
    OUT.write_text(json.dumps({"meta": meta, "stations": stations}, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {len(stations)} stations to {OUT}")


if __name__ == "__main__":
    main()
