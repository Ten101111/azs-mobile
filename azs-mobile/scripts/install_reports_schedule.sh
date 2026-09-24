#!/bin/zsh
# Справки по сети: сборка по витрине ОХД на этом маке и отправка на сервер.
#
# Решение владельца 24.09.2026: справка строится только по витрине ОХД, а она
# видна только отсюда, под VPN. Каждый час с 07:10 до 21:10 (время мака) запуск
# спрашивает сервер, нужна ли справка: неделя не опубликована или администратор
# запросил перевыпуск. Если нужна — собирает её по витрине, пишет текст на
# локальной модели, сохраняет копию в PDF и Excel в ~/Справки АЗС и отправляет
# готовый выпуск. Сайт недоступен — выпуск ждёт в очереди и уходит при следующем
# запуске, из ОХД заново не собирается. Если нечего делать — один короткий запрос
# к серверу, витрина не трогается. О сбоях и о публикации — уведомление macOS.
#
# launchd не читает ~/Downloads без полного доступа к диску, поэтому код, каталог
# витрины и нужные переменные копируются в Application Support, и там же ставится
# своё окружение Python (sqlglot, psycopg2, reportlab и openpyxl — для копий).
# Токен сервера создаётся в .env.local, если его ещё нет; на сервер его SHA-256
# отвезёт bash deploy/deploy.sh.
#
# Что мешает справке выйти: ./scripts/python.sh -m backend.reports.publish --check
#
#   scripts/install_reports_schedule.sh              — поставить или обновить (после правок кода — тоже)
#   scripts/install_reports_schedule.sh --uninstall  — убрать
set -euo pipefail

SOURCE_PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="ru.azs-classifier.reports"
PLIST_PATH="$HOME/Library/LaunchAgents/$LABEL.plist"
RUNTIME_DIR="$HOME/Library/Application Support/AZS Classifier/reports"
LOG_DIR="$HOME/Library/Logs/AZS Classifier"
DOMAIN="gui/$(id -u)"
ENV_LOCAL="$SOURCE_PROJECT_DIR/.env.local"

if [[ "${1:-}" == "--uninstall" ]]; then
  launchctl bootout "$DOMAIN" "$PLIST_PATH" 2>/dev/null || true
  rm -f "$PLIST_PATH"
  echo "Расписание справок снято."
  exit 0
fi

if [[ ! -f "$SOURCE_PROJECT_DIR/data/ai_catalog.dwh.json" ]]; then
  echo "Нет каталога витрины data/ai_catalog.dwh.json — соберите его: scripts/python.sh backend/ai/build_dwh_catalog.py" >&2
  exit 1
fi

touch "$ENV_LOCAL"
if ! grep -q '^REPORTS_IMPORT_TOKEN=' "$ENV_LOCAL"; then
  printf '\nREPORTS_IMPORT_TOKEN=%s\n' "$(openssl rand -hex 32)" >> "$ENV_LOCAL"
  echo "Создан токен справок в .env.local — отвезите его на сервер: bash deploy/deploy.sh"
fi

# Python с готовыми сборками psycopg2 (Homebrew 3.14 не годится).
PYTHON_BIN="${PYTHON_BIN:-}"
if [[ -z "$PYTHON_BIN" ]]; then
  for candidate in python3.13 python3.12 python3.11 python3.10; do
    if command -v "$candidate" >/dev/null 2>&1; then PYTHON_BIN="$(command -v "$candidate")"; break; fi
  done
fi
[[ -n "$PYTHON_BIN" ]] || { echo "Нужен Python 3.10–3.13 (brew install python@3.12)" >&2; exit 1; }

mkdir -p "$(dirname "$PLIST_PATH")" "$RUNTIME_DIR" "$LOG_DIR"
chmod 700 "$RUNTIME_DIR" "$LOG_DIR"

"$PYTHON_BIN" - "$SOURCE_PROJECT_DIR" "$RUNTIME_DIR" <<'PY'
import shutil
import sys
from pathlib import Path

source, runtime = Path(sys.argv[1]), Path(sys.argv[2])
# Код целиком (без тестов и кэша) — сборщику нужны backend.reports, backend.summary и backend.ai.
target = runtime / "backend"
if target.exists():
    shutil.rmtree(target)
shutil.copytree(source / "backend", target, ignore=shutil.ignore_patterns("tests", "__pycache__", "*.pyc"))
(runtime / "data").mkdir(exist_ok=True)
shutil.copy2(source / "data" / "ai_catalog.dwh.json", runtime / "data" / "ai_catalog.dwh.json")

allowed = ("DWH_DB_HOST", "DWH_DB_PORT", "DWH_DB_NAME", "DWH_DB_USER", "DWH_DB_PASSWORD",
           "DWH_CONNECT_TIMEOUT_SECONDS", "AI_MODEL", "AI_OLLAMA_HOST", "AI_NUM_CTX", "AI_AGENT_NUM_CTX",
           "AI_MODEL_TIMEOUT", "REPORTS_IMPORT_TOKEN", "REPORTS_IMPORT_URL", "KPI_IMPORT_URL", "REPORTS_SQL_TIMEOUT",
           "REPORT_FUEL_DROP_PCT", "REPORT_NO_SALES_DAYS", "REPORT_TOP_DROPS", "REPORT_TOP_LEADERS",
           "REPORT_COMPLETE_SHARE_PCT", "REPORT_MIN_BASE_SHARE", "REPORTS_LOCAL_DIR", "REPORTS_NOTIFY")
values = {}
for line in (source / ".env.local").read_text(encoding="utf-8").splitlines():
    if not line or line.lstrip().startswith("#") or "=" not in line:
        continue
    key, value = line.split("=", 1)
    if key.strip() in allowed:
        values[key.strip()] = value.strip()
missing = [k for k in ("DWH_DB_HOST", "DWH_DB_NAME", "DWH_DB_USER", "DWH_DB_PASSWORD", "REPORTS_IMPORT_TOKEN")
           if not values.get(k)]
if missing:
    raise SystemExit("В .env.local нет: " + ", ".join(missing))
if not (values.get("REPORTS_IMPORT_URL") or values.get("KPI_IMPORT_URL")):
    raise SystemExit("В .env.local нет адреса сайта: REPORTS_IMPORT_URL или KPI_IMPORT_URL")
# Только витрина ОХД: каталог и исполнитель задаются здесь, а не наследуются из настроек приложения.
values["AI_CATALOG"] = str(runtime / "data" / "ai_catalog.dwh.json")
values["AI_DB_BACKEND"] = "postgres"
env_file = runtime / ".env.local"
env_file.write_text("\n".join(f"{k}={v}" for k, v in values.items()) + "\n", encoding="utf-8")
env_file.chmod(0o600)
PY

if [[ ! -x "$RUNTIME_DIR/venv/bin/python" ]]; then
  "$PYTHON_BIN" -m venv "$RUNTIME_DIR/venv"
fi
"$RUNTIME_DIR/venv/bin/python" -m pip install --quiet --upgrade pip
"$RUNTIME_DIR/venv/bin/python" -m pip install --quiet \
  "$(grep -E '^sqlglot==' "$SOURCE_PROJECT_DIR/backend/requirements.txt")" \
  "$(grep -E '^psycopg2-binary==' "$SOURCE_PROJECT_DIR/backend/requirements.txt")" \
  "$(grep -E '^reportlab==' "$SOURCE_PROJECT_DIR/backend/requirements.txt")" \
  "$(grep -E '^openpyxl==' "$SOURCE_PROJECT_DIR/backend/requirements.txt")"

"$RUNTIME_DIR/venv/bin/python" - "$RUNTIME_DIR" "$PLIST_PATH" "$LOG_DIR" "$LABEL" <<'PY'
import plistlib
import sys
from pathlib import Path

runtime, plist_path, log_dir, label = sys.argv[1:]
payload = {
    "Label": label,
    "ProgramArguments": [str(Path(runtime) / "venv" / "bin" / "python"), "-m", "backend.reports.publish"],
    "WorkingDirectory": runtime,
    # Каждый день, каждый час 07:10…21:10: неделя выходит, как только данные за воскресенье
    # есть в витрине и VPN подключён; перевыпуск по запросу администратора — в ближайший час.
    "StartCalendarInterval": [{"Hour": hour, "Minute": 10} for hour in range(7, 22)],
    "ProcessType": "Background", "LowPriorityIO": True, "Nice": 5,
    "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"},
    "StandardOutPath": str(Path(log_dir) / "reports.log"),
    "StandardErrorPath": str(Path(log_dir) / "reports.error.log"),
}
with Path(plist_path).open("wb") as output:
    plistlib.dump(payload, output, sort_keys=False)
Path(plist_path).chmod(0o600)
PY

launchctl bootout "$DOMAIN" "$PLIST_PATH" 2>/dev/null || true
launchctl bootstrap "$DOMAIN" "$PLIST_PATH"
launchctl enable "$DOMAIN/$LABEL"

echo "Справки: расписание установлено ($PLIST_PATH)."
echo "Каждый день, каждый час с 07:10 до 21:10 по времени этого мака; для сборки нужен подключённый VPN."
echo "Копии выпусков (PDF и Excel): ${REPORTS_LOCAL_DIR:-$HOME/Справки АЗС}"
echo "Проверить, что мешает справке выйти: ./scripts/python.sh -m backend.reports.publish --check"
echo "Запустить сейчас: launchctl kickstart -k $DOMAIN/$LABEL"
echo "Журнал: $LOG_DIR/reports.log"
echo "Уведомления приходят от «Редактора скриптов» — если их не видно, разрешите их в Системных настройках → Уведомления."
