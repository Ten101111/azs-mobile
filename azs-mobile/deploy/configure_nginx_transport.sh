#!/usr/bin/env bash
set -euo pipefail

CONFIG_PATH="${NGINX_CONFIG_PATH:-/etc/nginx/sites-available/azs}"

if [ ! -f "$CONFIG_PATH" ]; then
  echo "Missing nginx config: $CONFIG_PATH" >&2
  exit 1
fi

BACKUP_PATH="${CONFIG_PATH}.pre-http1"
cp -p "$CONFIG_PATH" "$BACKUP_PATH"

sed -E -i \
  -e 's/listen 443 ssl http2;/listen 443 ssl;/' \
  -e '/^[[:space:]]*http2[[:space:]]+on;[[:space:]]*$/d' \
  "$CONFIG_PATH"

if ! nginx -t; then
  cp -p "$BACKUP_PATH" "$CONFIG_PATH"
  nginx -t
  echo "nginx transport change rolled back" >&2
  exit 1
fi

systemctl reload nginx.service
echo "nginx HTTPS transport configured for HTTP/1.1 compatibility"
