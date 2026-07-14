#!/bin/zsh
set -euo pipefail

SOURCE_PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="ru.azs-classifier.kpi-sync"
WATCH_LABEL="ru.azs-classifier.kpi-vpn-watch"
PLIST_PATH="$HOME/Library/LaunchAgents/$LABEL.plist"
WATCH_PLIST_PATH="$HOME/Library/LaunchAgents/$WATCH_LABEL.plist"
RUNTIME_DIR="$HOME/Library/Application Support/AZS Classifier/kpi-sync"
LOG_DIR="$HOME/Library/Logs/AZS Classifier"
DOMAIN="gui/$(id -u)"

if [[ "${1:-}" == "--uninstall" ]]; then
  launchctl bootout "$DOMAIN" "$PLIST_PATH" 2>/dev/null || true
  launchctl bootout "$DOMAIN" "$WATCH_PLIST_PATH" 2>/dev/null || true
  rm -f "$PLIST_PATH" "$WATCH_PLIST_PATH"
  echo "KPI sync schedule and VPN watcher removed."
  exit 0
fi

PYTHON_BIN="${PYTHON_BIN:-$(command -v python3)}"
mkdir -p "$(dirname "$PLIST_PATH")" "$RUNTIME_DIR" "$LOG_DIR"
chmod 700 "$RUNTIME_DIR" "$LOG_DIR"

"$PYTHON_BIN" - "$SOURCE_PROJECT_DIR" "$RUNTIME_DIR" "$PLIST_PATH" "$WATCH_PLIST_PATH" "$LOG_DIR" "$PYTHON_BIN" "$LABEL" "$WATCH_LABEL" <<'PY'
import plistlib
import shutil
import sys
from pathlib import Path

source_dir, runtime_dir, plist_path, watch_plist_path, log_dir, python_bin, label, watch_label = sys.argv[1:]
source = Path(source_dir)
runtime = Path(runtime_dir)

files = (
    (source / "scripts" / "sync_kpi_metrics.py", runtime / "scripts" / "sync_kpi_metrics.py", 0o700),
    (source / "scripts" / "watch_kpi_vpn.py", runtime / "scripts" / "watch_kpi_vpn.py", 0o700),
    (source / "backend" / "sql" / "dwh_kpi_daily_export.sql", runtime / "backend" / "sql" / "dwh_kpi_daily_export.sql", 0o600),
)
for source_path, target_path, mode in files:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_path, target_path)
    target_path.chmod(mode)

allowed_env_keys = (
    "DWH_DB_HOST", "DWH_DB_PORT", "DWH_DB_NAME", "DWH_DB_USER", "DWH_DB_PASSWORD",
    "DWH_MIN_DATE", "DWH_CONNECT_TIMEOUT_SECONDS", "DWH_STATEMENT_TIMEOUT_MS",
    "KPI_IMPORT_TOKEN", "KPI_IMPORT_URL", "KPI_IMPORT_SOURCE", "KPI_IMPORT_TIMEOUT_SECONDS",
    "KPI_IMPORT_CHUNK_SIZE", "KPI_CATCHUP_AFTER_SECONDS", "KPI_VPN_PROBE_TIMEOUT_SECONDS",
    "KPI_SERVER_HEALTH_TIMEOUT_SECONDS",
)
source_env = {}
for line in (source / ".env.local").read_text(encoding="utf-8").splitlines():
    if not line or line.lstrip().startswith("#") or "=" not in line:
        continue
    key, value = line.split("=", 1)
    source_env[key.strip()] = value

required = ("DWH_DB_HOST", "DWH_DB_NAME", "DWH_DB_USER", "DWH_DB_PASSWORD", "KPI_IMPORT_TOKEN", "KPI_IMPORT_URL")
missing = [key for key in required if not source_env.get(key)]
if missing:
    raise SystemExit(f"Missing schedule environment variables: {', '.join(missing)}")

runtime_env = runtime / ".env.local"
runtime_env.write_text("\n".join(f"{key}={source_env[key]}" for key in allowed_env_keys if key in source_env) + "\n", encoding="utf-8")
runtime_env.chmod(0o600)

sync_payload = {
    "Label": label,
    "ProgramArguments": [python_bin, str(runtime / "scripts" / "sync_kpi_metrics.py")],
    "WorkingDirectory": str(runtime),
    "StartCalendarInterval": [{"Hour": hour, "Minute": 15} for hour in range(0, 24, 4)],
    "ProcessType": "Background", "LowPriorityIO": True, "Nice": 5,
    "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"},
    "StandardOutPath": str(Path(log_dir) / "kpi-sync.log"),
    "StandardErrorPath": str(Path(log_dir) / "kpi-sync.error.log"),
}
with Path(plist_path).open("wb") as output:
    plistlib.dump(sync_payload, output, sort_keys=False)
Path(plist_path).chmod(0o600)

watch_payload = {
    "Label": watch_label,
    "ProgramArguments": [python_bin, str(runtime / "scripts" / "watch_kpi_vpn.py")],
    "WorkingDirectory": str(runtime), "RunAtLoad": True, "StartInterval": 300,
    "ProcessType": "Background", "LowPriorityIO": True, "Nice": 5,
    "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"},
    "StandardOutPath": str(Path(log_dir) / "kpi-vpn-watch.log"),
    "StandardErrorPath": str(Path(log_dir) / "kpi-vpn-watch.error.log"),
}
with Path(watch_plist_path).open("wb") as output:
    plistlib.dump(watch_payload, output, sort_keys=False)
Path(watch_plist_path).chmod(0o600)
PY

launchctl bootout "$DOMAIN" "$PLIST_PATH" 2>/dev/null || true
launchctl bootout "$DOMAIN" "$WATCH_PLIST_PATH" 2>/dev/null || true
launchctl bootstrap "$DOMAIN" "$PLIST_PATH"
launchctl bootstrap "$DOMAIN" "$WATCH_PLIST_PATH"
launchctl enable "$DOMAIN/$LABEL"
launchctl enable "$DOMAIN/$WATCH_LABEL"
launchctl kickstart -k "$DOMAIN/$WATCH_LABEL"

echo "KPI sync installed: $PLIST_PATH"
echo "Schedule: every four hours at :15."
echo "VPN catch-up watcher installed: $WATCH_PLIST_PATH"
echo "Private runtime: $RUNTIME_DIR"
echo "Logs: $LOG_DIR/kpi-sync.log"
echo "Watcher logs: $LOG_DIR/kpi-vpn-watch.log"
