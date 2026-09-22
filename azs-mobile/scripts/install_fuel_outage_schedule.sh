#!/bin/zsh
set -euo pipefail

SOURCE_PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="ru.azs-classifier.fuel-outage-email"
PLIST_PATH="$HOME/Library/LaunchAgents/$LABEL.plist"
RUNTIME_DIR="$HOME/Library/Application Support/AZS Classifier/fuel-outage-email"
LOG_DIR="$HOME/Library/Logs/AZS Classifier"
DOMAIN="gui/$(id -u)"

if [[ "${1:-}" == "--uninstall" ]]; then
  launchctl bootout "$DOMAIN" "$PLIST_PATH" 2>/dev/null || true
  rm -f "$PLIST_PATH"
  echo "Fuel outage email schedule removed."
  exit 0
fi

PYTHON_BIN="${PYTHON_BIN:-$(command -v python3)}"
mkdir -p "$(dirname "$PLIST_PATH")" "$RUNTIME_DIR" "$LOG_DIR"
chmod 700 "$RUNTIME_DIR" "$LOG_DIR"

"$PYTHON_BIN" - "$SOURCE_PROJECT_DIR" "$RUNTIME_DIR" "$PLIST_PATH" "$LOG_DIR" "$PYTHON_BIN" "$LABEL" <<'PY'
import plistlib
import shutil
import sys
from pathlib import Path

source_dir, runtime_dir, plist_path, log_dir, python_bin, label = sys.argv[1:]
source = Path(source_dir)
runtime = Path(runtime_dir)

for relative_path, mode in (
    (Path("scripts/sync_fuel_outages_from_email.py"), 0o700),
    (Path("backend/fuel_outages.py"), 0o600),
    (Path("backend/__init__.py"), 0o600),
):
    source_path = source / relative_path
    target_path = runtime / relative_path
    target_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_path, target_path)
    target_path.chmod(mode)

runtime_env_values = {
    "FUEL_OUTAGE_SOURCE_ENV_PATH": str(source / ".env.local"),
}
runtime_env_values.update(
    {
        "FUEL_OUTAGE_SNAPSHOT_PATH": str(source / "data" / "fuel_outages_latest.json"),
        "FUEL_OUTAGE_XLSX_PATH": str(source / "data" / "fuel_outages_latest.xlsx"),
        "FUEL_OUTAGE_MAIL_STATE_PATH": str(runtime / "fuel_outage_mail_state.json"),
        "FUEL_OUTAGE_MAIL_XLSX_PATH": str(runtime / "mail" / "fuel_outages_latest.xlsx"),
    }
)
runtime_env = runtime / ".env.local"
runtime_env.write_text("\n".join(f"{key}={value}" for key, value in runtime_env_values.items()) + "\n", encoding="utf-8")
runtime_env.chmod(0o600)

payload = {
    "Label": label,
    "ProgramArguments": [python_bin, str(runtime / "scripts" / "sync_fuel_outages_from_email.py"), "--local-only"],
    "WorkingDirectory": str(runtime),
    "RunAtLoad": True,
    "StartInterval": 3600,
    "ProcessType": "Background",
    "LowPriorityIO": True,
    "Nice": 5,
    "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"},
    "StandardOutPath": str(Path(log_dir) / "fuel-outage-email.log"),
    "StandardErrorPath": str(Path(log_dir) / "fuel-outage-email.error.log"),
}
with Path(plist_path).open("wb") as output:
    plistlib.dump(payload, output, sort_keys=False)
Path(plist_path).chmod(0o600)
PY

launchctl bootout "$DOMAIN" "$PLIST_PATH" 2>/dev/null || true
launchctl bootstrap "$DOMAIN" "$PLIST_PATH"
launchctl enable "$DOMAIN/$LABEL"

echo "Fuel outage email schedule installed: $PLIST_PATH"
echo "Schedule: once an hour and when you sign in to macOS."
echo "Logs: $LOG_DIR/fuel-outage-email.log"
