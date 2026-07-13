#!/bin/zsh
set -euo pipefail

SOURCE_PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="ru.azs-classifier.fuel-stock-sync"
PLIST_PATH="$HOME/Library/LaunchAgents/$LABEL.plist"
RUNTIME_DIR="$HOME/Library/Application Support/AZS Classifier/fuel-stock-sync"
LOG_DIR="$HOME/Library/Logs/AZS Classifier"
DOMAIN="gui/$(id -u)"

if [[ "${1:-}" == "--uninstall" ]]; then
  launchctl bootout "$DOMAIN" "$PLIST_PATH" 2>/dev/null || true
  rm -f "$PLIST_PATH"
  echo "Hourly fuel-stock sync removed."
  exit 0
fi

PYTHON_BIN="${PYTHON_BIN:-$(command -v python3)}"
mkdir -p "${PLIST_PATH:h}" "$RUNTIME_DIR" "$LOG_DIR"
chmod 700 "$RUNTIME_DIR" "$LOG_DIR"

"$PYTHON_BIN" - "$SOURCE_PROJECT_DIR" "$RUNTIME_DIR" "$PLIST_PATH" "$LOG_DIR" "$PYTHON_BIN" "$LABEL" <<'PY'
import plistlib
import shutil
import sys
from pathlib import Path

source_dir, runtime_dir, plist_path, log_dir, python_bin, label = sys.argv[1:]
source = Path(source_dir)
runtime = Path(runtime_dir)

files = (
    (source / "scripts" / "sync_fuel_stock.py", runtime / "scripts" / "sync_fuel_stock.py", 0o700),
    (source / "backend" / "__init__.py", runtime / "backend" / "__init__.py", 0o600),
    (source / "backend" / "fuel_stock.py", runtime / "backend" / "fuel_stock.py", 0o600),
    (
        source / "backend" / "sql" / "dwh_fuel_stock_current.sql",
        runtime / "backend" / "sql" / "dwh_fuel_stock_current.sql",
        0o600,
    ),
)
for source_path, target_path, mode in files:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_path, target_path)
    target_path.chmod(mode)

allowed_env_keys = (
    "DWH_DB_HOST",
    "DWH_DB_PORT",
    "DWH_DB_NAME",
    "DWH_DB_USER",
    "DWH_DB_PASSWORD",
    "DWH_CONNECT_TIMEOUT_SECONDS",
    "DWH_FUEL_STOCK_STATEMENT_TIMEOUT_MS",
    "DWH_FUEL_VOLUME_TO_LITERS",
    "DWH_SOURCE_TIMEZONE",
    "FUEL_STOCK_IMPORT_TOKEN",
    "FUEL_STOCK_IMPORT_URL",
    "FUEL_STOCK_IMPORT_SOURCE",
    "FUEL_STOCK_IMPORT_TIMEOUT_SECONDS",
    "FUEL_STOCK_IMPORT_RETRIES",
    "FUEL_STOCK_IMPORT_MAX_BODY_BYTES",
    "FUEL_STOCK_MIN_LOCAL_STATIONS",
)
source_env = {}
for line in (source / ".env.local").read_text(encoding="utf-8").splitlines():
    if not line or line.lstrip().startswith("#") or "=" not in line:
        continue
    key, value = line.split("=", 1)
    source_env[key.strip()] = value

required = (
    "DWH_DB_HOST",
    "DWH_DB_NAME",
    "DWH_DB_USER",
    "DWH_DB_PASSWORD",
    "FUEL_STOCK_IMPORT_TOKEN",
    "FUEL_STOCK_IMPORT_URL",
)
missing = [key for key in required if not source_env.get(key)]
if missing:
    raise SystemExit(f"Missing schedule environment variables: {', '.join(missing)}")

runtime_env = runtime / ".env.local"
runtime_env.write_text(
    "\n".join(f"{key}={source_env[key]}" for key in allowed_env_keys if key in source_env) + "\n",
    encoding="utf-8",
)
runtime_env.chmod(0o600)

payload = {
    "Label": label,
    "ProgramArguments": [python_bin, str(runtime / "scripts" / "sync_fuel_stock.py")],
    "WorkingDirectory": str(runtime),
    "RunAtLoad": True,
    # The DWH source is normally refreshed near :31, so read it shortly after.
    "StartCalendarInterval": {"Minute": 40},
    "ProcessType": "Background",
    "LowPriorityIO": True,
    "Nice": 5,
    "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"},
    "StandardOutPath": str(Path(log_dir) / "fuel-stock-sync.log"),
    "StandardErrorPath": str(Path(log_dir) / "fuel-stock-sync.error.log"),
}
with Path(plist_path).open("wb") as output:
    plistlib.dump(payload, output, sort_keys=False)
Path(plist_path).chmod(0o600)
PY

launchctl bootout "$DOMAIN" "$PLIST_PATH" 2>/dev/null || true
launchctl bootstrap "$DOMAIN" "$PLIST_PATH"
launchctl enable "$DOMAIN/$LABEL"
launchctl kickstart -k "$DOMAIN/$LABEL"

echo "Hourly fuel-stock sync installed: $PLIST_PATH"
echo "Private runtime: $RUNTIME_DIR"
echo "Logs: $LOG_DIR/fuel-stock-sync.log"
