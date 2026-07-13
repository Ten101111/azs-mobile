#!/usr/bin/env bash
# ============================================================
# deploy.sh — деплой обновлений на VPS с локального Mac
#
# Использование:
#   bash deploy/deploy.sh <IP или hostname>
#
# Первый раз:
#   bash deploy/deploy.sh 1.2.3.4
#
# Повторно (IP запоминается в deploy/.server):
#   bash deploy/deploy.sh
# ============================================================
set -euo pipefail

APP_DIR_LOCAL="$(cd "$(dirname "$0")/.." && pwd)"
APP_DIR_REMOTE="/opt/azs"
APP_USER="azs"
SERVER_FILE="$(dirname "$0")/.server"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

info()    { echo -e "${CYAN}[deploy]${RESET} $*"; }
success() { echo -e "${GREEN}[deploy]${RESET} $*"; }
warn()    { echo -e "${YELLOW}[deploy]${RESET} $*"; }
error()   { echo -e "${RED}[deploy]${RESET} $*"; exit 1; }

# ── IP сервера ────────────────────────────────────────────
SERVER_IP="${1:-}"
if [ -z "$SERVER_IP" ] && [ -f "$SERVER_FILE" ]; then
  SERVER_IP=$(cat "$SERVER_FILE")
  info "Используем сохранённый сервер: $SERVER_IP"
fi
[ -z "$SERVER_IP" ] && error "Укажи IP: bash deploy/deploy.sh <IP>"
echo "$SERVER_IP" > "$SERVER_FILE"

SSH="ssh -o StrictHostKeyChecking=no root@$SERVER_IP"

# ── 1. Сборка фронтенда локально ──────────────────────────
info "Собираем фронтенд..."
cd "$APP_DIR_LOCAL"
npm run build --silent
success "Сборка готова (dist/)"

# ── 2. Загружаем файлы на сервер ──────────────────────────
info "Синхронизируем файлы с сервером..."

# Создаём временную директорию для загрузки
$SSH "mkdir -p /tmp/azs-deploy"

# Синхронизируем только нужные папки (не data/, не .env)
rsync -az --delete \
  --exclude='.env*' \
  --exclude='data/' \
  --exclude='__pycache__/' \
  --exclude='*.pyc' \
  --exclude='.git/' \
  --exclude='node_modules/' \
  --exclude='scripts/' \
  --exclude='deploy/' \
  --exclude='tests/' \
  "$APP_DIR_LOCAL/dist/"    "root@$SERVER_IP:$APP_DIR_REMOTE/public/"
rsync -az \
  --exclude='__pycache__/' \
  --exclude='*.pyc' \
  "$APP_DIR_LOCAL/backend/" "root@$SERVER_IP:$APP_DIR_REMOTE/backend/"

# Рекомендации содержат рабочие агрегаты, поэтому храним их вне public/.
# rsync передаёт только изменения и не открывает файл через веб-сервер.
if [ -f "$APP_DIR_LOCAL/data/staff_recommendations.json" ]; then
  info "Синхронизируем рекомендации по персоналу..."
  $SSH "install -d -m 750 -o $APP_USER -g $APP_USER $APP_DIR_REMOTE/data"
  rsync -az \
    "$APP_DIR_LOCAL/data/staff_recommendations.json" \
    "root@$SERVER_IP:$APP_DIR_REMOTE/data/staff_recommendations.json"
  $SSH "chown $APP_USER:$APP_USER $APP_DIR_REMOTE/data/staff_recommendations.json && chmod 600 $APP_DIR_REMOTE/data/staff_recommendations.json"
fi

# requirements.txt если есть
[ -f "$APP_DIR_LOCAL/backend/requirements.txt" ] && \
  scp -q "$APP_DIR_LOCAL/backend/requirements.txt" \
       "root@$SERVER_IP:$APP_DIR_REMOTE/requirements.txt"

success "Файлы загружены"

# ── 3. Устанавливаем Python-зависимости ───────────────────
info "Устанавливаем Python-зависимости..."
$SSH "
  cd $APP_DIR_REMOTE
  venv/bin/pip install -q -r requirements.txt
  chown -R $APP_USER:$APP_USER $APP_DIR_REMOTE/backend $APP_DIR_REMOTE/public
"
success "Зависимости установлены"

# ── 4. Перезапускаем FastAPI ──────────────────────────────
info "Перезапускаем FastAPI сервис..."
$SSH "systemctl restart azs-api && sleep 2 && systemctl is-active azs-api"
success "API перезапущен"

# ── 5. Проверяем работоспособность ────────────────────────
info "Проверяем сайт..."
sleep 3
HTTP=$(curl -s -o /dev/null -w "%{http_code}" "https://azs-classifier.ru/" 2>/dev/null || echo "???")
API=$(curl -s "https://azs-classifier.ru/api/health" 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('status','?'))" 2>/dev/null || echo "???")

echo ""
echo -e "${BOLD}════════════════════════════════════════${RESET}"
echo -e "${GREEN}${BOLD}  Деплой завершён!${RESET}"
echo -e "${BOLD}════════════════════════════════════════${RESET}"
echo -e "  Сайт:  ${CYAN}https://azs-classifier.ru${RESET}  [HTTP $HTTP]"
echo -e "  API:   ${CYAN}https://azs-classifier.ru/api/health${RESET}  [status: $API]"
echo -e "${BOLD}════════════════════════════════════════${RESET}"
echo ""
