#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/azs}"
APP_USER="${APP_USER:-azs}"
ENV_FILE="$APP_DIR/.env"
SERVICE_NAME="azs-fuel-outage-email"

if [ ! -f "$ENV_FILE" ]; then
  echo "Missing $ENV_FILE" >&2
  exit 1
fi

if ! grep -q '^FUEL_OUTAGE_IMPORT_TOKEN=' "$ENV_FILE"; then
  TOKEN="$(openssl rand -hex 32)"
  printf '\nFUEL_OUTAGE_IMPORT_TOKEN=%s\n' "$TOKEN" >> "$ENV_FILE"
fi
if ! grep -q '^FUEL_OUTAGE_IMPORT_URL=' "$ENV_FILE"; then
  printf 'FUEL_OUTAGE_IMPORT_URL=http://127.0.0.1:8000/api/internal/fuel-outages/import\n' >> "$ENV_FILE"
fi

install -d -m 750 -o "$APP_USER" -g "$APP_USER" "$APP_DIR/data" "$APP_DIR/data/mail" "$APP_DIR/logs"
chown root:"$APP_USER" "$ENV_FILE"
chmod 640 "$ENV_FILE"

cat > "/etc/systemd/system/$SERVICE_NAME.service" <<SERVICE
[Unit]
Description=Import latest fuel-outage report from IMAP
After=network-online.target azs-api.service
Wants=network-online.target
Requires=azs-api.service

[Service]
Type=oneshot
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$APP_DIR/venv/bin/python $APP_DIR/scripts/sync_fuel_outages_from_email.py
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=strict
ReadWritePaths=$APP_DIR/data $APP_DIR/logs
StandardOutput=append:$APP_DIR/logs/fuel-outage-email.log
StandardError=append:$APP_DIR/logs/fuel-outage-email.error.log
SERVICE

cat > "/etc/systemd/system/$SERVICE_NAME.timer" <<TIMER
[Unit]
Description=Check mailbox for the latest fuel-outage report on the reporting schedule

[Timer]
OnCalendar=*-*-* 00,08,09,10,11,16:10:00
AccuracySec=1s
Persistent=true
Unit=$SERVICE_NAME.service

[Install]
WantedBy=timers.target
TIMER

systemctl daemon-reload
systemctl restart azs-api.service

if grep -q '^IMAP_PASSWORD=.' "$ENV_FILE"; then
  systemctl enable --now "$SERVICE_NAME.timer"
  echo "Fuel-outage email timer installed and enabled: $SERVICE_NAME.timer"
else
  systemctl disable --now "$SERVICE_NAME.timer" 2>/dev/null || true
  echo "Fuel-outage email timer installed but disabled until IMAP_PASSWORD is configured."
fi
