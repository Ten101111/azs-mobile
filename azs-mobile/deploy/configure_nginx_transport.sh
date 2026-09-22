#!/usr/bin/env bash
set -euo pipefail

CONFIG_PATH="${NGINX_CONFIG_PATH:-/etc/nginx/sites-available/azs}"
CERTBOT_SSL_OPTIONS_PATH="${CERTBOT_SSL_OPTIONS_PATH:-/etc/letsencrypt/options-ssl-nginx.conf}"
NGINX_MAIN_CONFIG_PATH="${NGINX_MAIN_CONFIG_PATH:-/etc/nginx/nginx.conf}"

if [ ! -f "$CONFIG_PATH" ]; then
  echo "Missing nginx config: $CONFIG_PATH" >&2
  exit 1
fi

BACKUP_PATH="${CONFIG_PATH}.pre-transport"
cp -p "$CONFIG_PATH" "$BACKUP_PATH"

sed -E -i \
  -e 's/listen 443 ssl http2;/listen 443 ssl;/' \
  -e 's/ssl_protocols[[:space:]]+TLSv1\.2[[:space:]]+TLSv1\.3;/ssl_protocols TLSv1.2;/' \
  -e '/^[[:space:]]*http2[[:space:]]+on;[[:space:]]*$/d' \
  "$CONFIG_PATH"

if [ -f "$CERTBOT_SSL_OPTIONS_PATH" ]; then
  CERTBOT_BACKUP_PATH="${CERTBOT_SSL_OPTIONS_PATH}.pre-transport"
  cp -p "$CERTBOT_SSL_OPTIONS_PATH" "$CERTBOT_BACKUP_PATH"
  sed -E -i \
    -e 's/ssl_protocols[[:space:]]+TLSv1\.2[[:space:]]+TLSv1\.3;/ssl_protocols TLSv1.2;/' \
    "$CERTBOT_SSL_OPTIONS_PATH"
fi

if [ -f "$NGINX_MAIN_CONFIG_PATH" ]; then
  NGINX_MAIN_BACKUP_PATH="${NGINX_MAIN_CONFIG_PATH}.pre-transport"
  cp -p "$NGINX_MAIN_CONFIG_PATH" "$NGINX_MAIN_BACKUP_PATH"
  sed -E -i \
    -e 's/ssl_protocols[[:space:]]+TLSv1[[:space:]]+TLSv1\.1[[:space:]]+TLSv1\.2[[:space:]]+TLSv1\.3;/ssl_protocols TLSv1.2;/' \
    -e 's/ssl_protocols[[:space:]]+TLSv1\.2[[:space:]]+TLSv1\.3;/ssl_protocols TLSv1.2;/' \
    "$NGINX_MAIN_CONFIG_PATH"
fi

if ! nginx -t; then
  cp -p "$BACKUP_PATH" "$CONFIG_PATH"
  if [ -n "${CERTBOT_BACKUP_PATH:-}" ]; then
    cp -p "$CERTBOT_BACKUP_PATH" "$CERTBOT_SSL_OPTIONS_PATH"
  fi
  if [ -n "${NGINX_MAIN_BACKUP_PATH:-}" ]; then
    cp -p "$NGINX_MAIN_BACKUP_PATH" "$NGINX_MAIN_CONFIG_PATH"
  fi
  nginx -t
  echo "nginx transport change rolled back" >&2
  exit 1
fi

systemctl reload nginx.service
echo "nginx HTTPS transport configured for HTTP/1.1 and TLS 1.2 compatibility"
