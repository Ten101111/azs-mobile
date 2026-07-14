#!/usr/bin/env bash
# ============================================================
# setup-server.sh — одноразовая настройка свежего VPS
# Ubuntu 22.04 LTS | запускать от root
#
# Использование:
#   ssh root@<IP>
#   curl -sO https://... # или scp этот файл на сервер
#   bash setup-server.sh
# ============================================================
set -euo pipefail

DOMAIN="azs-classifier.ru"
APP_USER="azs"
APP_DIR="/opt/azs"
PYTHON_VERSION="3.11"

RED='\033[0;31m'; GREEN='\033[0;32m'; CYAN='\033[0;36m'; RESET='\033[0m'
info()    { echo -e "${CYAN}[setup]${RESET} $*"; }
success() { echo -e "${GREEN}[setup]${RESET} $*"; }
error()   { echo -e "${RED}[setup]${RESET} $*"; exit 1; }

[[ $EUID -ne 0 ]] && error "Запусти от root: sudo bash setup-server.sh"

# ── Обновление системы ────────────────────────────────────
info "Обновляем пакеты..."
apt-get update -q && apt-get upgrade -y -q

# ── Системные утилиты ─────────────────────────────────────
info "Устанавливаем зависимости..."
apt-get install -y -q \
  curl wget git ufw fail2ban \
  nginx certbot python3-certbot-nginx \
  python${PYTHON_VERSION} python${PYTHON_VERSION}-venv python3-pip \
  sqlite3

# ── Пользователь приложения ───────────────────────────────
info "Создаём пользователя $APP_USER..."
id "$APP_USER" &>/dev/null || useradd -r -s /bin/bash -m -d "$APP_DIR" "$APP_USER"
mkdir -p "$APP_DIR"/{data,logs}
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

# ── Python venv ───────────────────────────────────────────
info "Создаём Python virtual environment..."
sudo -u "$APP_USER" python${PYTHON_VERSION} -m venv "$APP_DIR/venv"

# ── Firewall ──────────────────────────────────────────────
info "Настраиваем UFW..."
ufw default deny incoming
ufw default allow outgoing
ufw allow ssh
ufw allow 'Nginx Full'
ufw --force enable

# ── Fail2ban ──────────────────────────────────────────────
info "Настраиваем fail2ban..."
systemctl enable --now fail2ban

# ── nginx конфиг ─────────────────────────────────────────
info "Настраиваем nginx..."
cat > /etc/nginx/sites-available/azs << NGINX
server {
    listen 80;
    server_name ${DOMAIN} www.${DOMAIN};

    # ACME challenge (Let's Encrypt)
    location /.well-known/acme-challenge/ { root /var/www/html; }

    # Редирект HTTP → HTTPS (после выдачи сертификата)
    location / { return 301 https://\$host\$request_uri; }
}

server {
    # HTTP/2 is intentionally disabled: the current VPS/nginx transport
    # intermittently resets Chromium streams before /api/auth/me completes.
    listen 443 ssl;
    server_name ${DOMAIN} www.${DOMAIN};

    ssl_certificate     /etc/letsencrypt/live/${DOMAIN}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/${DOMAIN}/privkey.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_prefer_server_ciphers on;
    ssl_session_cache   shared:SSL:10m;

    # Security headers
    add_header Strict-Transport-Security "max-age=63072000; includeSubDomains" always;
    add_header X-Frame-Options DENY always;
    add_header X-Content-Type-Options nosniff always;
    add_header Referrer-Policy "strict-origin-when-cross-origin" always;

    # Логи
    access_log /var/log/nginx/azs.access.log;
    error_log  /var/log/nginx/azs.error.log;

    root ${APP_DIR}/public;

    # PWA — статические ассеты (хэшированные имена — кэш 1 год)
    location /assets/ {
        expires 1y;
        add_header Cache-Control "public, immutable";
    }

    # Service worker и манифест — без кэша
    location ~* \.(webmanifest|sw\.js)$ {
        expires -1;
        add_header Cache-Control "no-store, no-cache, must-revalidate";
    }

    # API → FastAPI
    location /api/ {
        # Hourly fuel snapshots are uploaded atomically and are roughly 1-2 MB.
        client_max_body_size 5m;
        proxy_pass         http://127.0.0.1:8000;
        proxy_set_header   Host \$host;
        proxy_set_header   X-Real-IP \$remote_addr;
        proxy_set_header   X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto \$scheme;
        proxy_read_timeout 30s;
        proxy_buffering    off;
    }

    # SPA fallback — все роуты → index.html
    location / {
        try_files \$uri \$uri/ /index.html;
        expires -1;
        add_header Cache-Control "no-store";
    }
}
NGINX

ln -sf /etc/nginx/sites-available/azs /etc/nginx/sites-enabled/azs
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl reload nginx

# ── SSL сертификат ────────────────────────────────────────
info "Получаем SSL сертификат Let's Encrypt..."
certbot --nginx -d "$DOMAIN" -d "www.$DOMAIN" \
  --non-interactive --agree-tos \
  --email admin@${DOMAIN} \
  --redirect || {
    echo ""
    echo "⚠️  Certbot не смог получить сертификат."
    echo "   Убедись что DNS уже указывает на этот сервер (A-запись в reg.ru)."
    echo "   После настройки DNS запусти вручную:"
    echo "   certbot --nginx -d ${DOMAIN} -d www.${DOMAIN}"
}

# ── systemd сервис для FastAPI ────────────────────────────
info "Создаём systemd сервис..."
cat > /etc/systemd/system/azs-api.service << SERVICE
[Unit]
Description=АЗС Классификатор — FastAPI Backend
After=network.target

[Service]
Type=simple
User=${APP_USER}
WorkingDirectory=${APP_DIR}
EnvironmentFile=${APP_DIR}/.env
ExecStart=${APP_DIR}/venv/bin/python -m uvicorn backend.main:app \
  --host 127.0.0.1 \
  --port 8000 \
  --workers 2 \
  --log-level info
Restart=always
RestartSec=5
StandardOutput=append:${APP_DIR}/logs/api.log
StandardError=append:${APP_DIR}/logs/api.error.log

# Безопасность
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=${APP_DIR}/data ${APP_DIR}/logs

[Install]
WantedBy=multi-user.target
SERVICE

systemctl daemon-reload
systemctl enable azs-api

success ""
success "════════════════════════════════════════"
success "  Сервер настроен!"
success "════════════════════════════════════════"
success ""
success "Следующий шаг — задеплоить приложение:"
success "  (с локального Mac)"
success "  bash deploy/deploy.sh <IP_СЕРВЕРА>"
success ""
