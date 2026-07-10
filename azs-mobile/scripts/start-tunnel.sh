#!/usr/bin/env bash
# Запускает приложение + ngrok туннель.
# Использование:  ./scripts/start-tunnel.sh
# Остановить:     Ctrl+C (останавливает всё)

set -e
APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ENV_LOCAL="$APP_DIR/.env.local"
FRONTEND_PORT=5174
BACKEND_PORT=8000

# ── Цвета ──────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

info()    { echo -e "${CYAN}[tunnel]${RESET} $*"; }
success() { echo -e "${GREEN}[tunnel]${RESET} $*"; }
warn()    { echo -e "${YELLOW}[tunnel]${RESET} $*"; }
error()   { echo -e "${RED}[tunnel]${RESET} $*"; }

# ── Проверки ───────────────────────────────────────────────
if ! command -v ngrok &>/dev/null; then
  error "ngrok не найден. Установи: brew install ngrok"
  exit 1
fi

# ── Очистка портов ─────────────────────────────────────────
info "Освобождаем порты $FRONTEND_PORT и $BACKEND_PORT..."
lsof -ti :$FRONTEND_PORT | xargs kill -9 2>/dev/null || true
lsof -ti :$BACKEND_PORT  | xargs kill -9 2>/dev/null || true
pkill -f "ngrok http" 2>/dev/null || true
sleep 1

PIDS=()

cleanup() {
  echo ""
  info "Останавливаем все процессы..."
  for pid in "${PIDS[@]}"; do
    kill "$pid" 2>/dev/null || true
  done
  lsof -ti :$FRONTEND_PORT | xargs kill -9 2>/dev/null || true
  lsof -ti :$BACKEND_PORT  | xargs kill -9 2>/dev/null || true
  pkill -f "ngrok http" 2>/dev/null || true
  success "Готово."
}
trap cleanup INT TERM EXIT

# ── Сборка фронтенда ───────────────────────────────────────
info "Собираем фронтенд..."
cd "$APP_DIR"
npm run build --silent

# ── Запуск бэкенда (временно без нового CORS) ──────────────
info "Запускаем FastAPI бэкенд на порту $BACKEND_PORT..."
python3 -m uvicorn backend.main:app \
  --host 0.0.0.0 --port $BACKEND_PORT \
  --log-level warning > /tmp/azs-backend.log 2>&1 &
BACKEND_PID=$!
PIDS+=($BACKEND_PID)

# ── Запуск фронтенда ───────────────────────────────────────
info "Запускаем Vite preview на порту $FRONTEND_PORT..."
npm run preview > /tmp/azs-frontend.log 2>&1 &
FRONTEND_PID=$!
PIDS+=($FRONTEND_PID)

# Ждём пока оба поднимутся
sleep 5
if ! kill -0 $BACKEND_PID 2>/dev/null; then
  error "Бэкенд упал. Лог: /tmp/azs-backend.log"
  cat /tmp/azs-backend.log
  exit 1
fi

# ── Запуск ngrok ───────────────────────────────────────────
info "Запускаем ngrok туннель..."
ngrok http $FRONTEND_PORT \
  --log=stdout \
  --log-format=json \
  > /tmp/azs-ngrok.log 2>&1 &
NGROK_PID=$!
PIDS+=($NGROK_PID)

# Ждём URL из ngrok API
TUNNEL_URL=""
for i in $(seq 1 20); do
  sleep 1
  TUNNEL_URL=$(curl -s http://localhost:4040/api/tunnels 2>/dev/null \
    | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    for t in d.get('tunnels', []):
        if t.get('proto') == 'https':
            print(t['public_url'])
            break
except:
    pass
" 2>/dev/null)
  [ -n "$TUNNEL_URL" ] && break
done

if [ -z "$TUNNEL_URL" ]; then
  error "Не удалось получить URL туннеля. Проверь: cat /tmp/azs-ngrok.log"
  exit 1
fi

# ── Обновляем CORS в .env.local ────────────────────────────
info "Обновляем CORS в .env.local → $TUNNEL_URL"
python3 - "$ENV_LOCAL" "$TUNNEL_URL" << 'PYEOF'
import sys, re
path, tunnel_url = sys.argv[1], sys.argv[2]
base = "http://localhost:5173,http://localhost:5174"
new_cors = f"CORS_ORIGINS={base},{tunnel_url}"
try:
    with open(path) as f:
        content = f.read()
    content = re.sub(r'^CORS_ORIGINS=.*\n?', '', content, flags=re.MULTILINE)
    content = content.rstrip('\n') + '\n' + new_cors + '\n'
except FileNotFoundError:
    content = new_cors + '\n'
with open(path, 'w') as f:
    f.write(content)
PYEOF

# ── Перезапускаем бэкенд с новым CORS ─────────────────────
info "Перезапускаем бэкенд с обновлённым CORS..."
kill $BACKEND_PID 2>/dev/null || true
sleep 2
python3 -m uvicorn backend.main:app \
  --host 0.0.0.0 --port $BACKEND_PORT \
  --log-level warning > /tmp/azs-backend.log 2>&1 &
BACKEND_PID=$!
PIDS[0]=$BACKEND_PID
sleep 3

# ── Финальная проверка ─────────────────────────────────────
LOCAL_STATUS=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:$FRONTEND_PORT/)
TUNNEL_STATUS=$(curl -s -o /dev/null -w "%{http_code}" "$TUNNEL_URL/" \
  -H "ngrok-skip-browser-warning: true" 2>/dev/null || echo "???")

echo ""
echo -e "${BOLD}════════════════════════════════════════════════${RESET}"
echo -e "${GREEN}${BOLD}  АЗС Классификатор — туннель активен${RESET}"
echo -e "${BOLD}════════════════════════════════════════════════${RESET}"
echo -e "  Локально:  ${CYAN}http://localhost:$FRONTEND_PORT/${RESET}  [HTTP $LOCAL_STATUS]"
echo -e "  Публично:  ${GREEN}${BOLD}$TUNNEL_URL${RESET}  [HTTP $TUNNEL_STATUS]"
echo -e "  API:       ${CYAN}http://localhost:$BACKEND_PORT/api/health${RESET}"
echo -e "  ngrok UI:  ${CYAN}http://localhost:4040${RESET}"
echo ""
echo -e "${YELLOW}  Отправь этот URL кому угодно:${RESET}"
echo -e "  ${BOLD}$TUNNEL_URL${RESET}"
echo ""
echo -e "${YELLOW}  Нажми Ctrl+C чтобы остановить всё.${RESET}"
echo -e "${BOLD}════════════════════════════════════════════════${RESET}"
echo ""

# ── Ждём завершения ────────────────────────────────────────
wait $NGROK_PID
