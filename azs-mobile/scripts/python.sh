#!/usr/bin/env bash
# Запускает Python из окружения проекта.
#
# Системный python3 в Homebrew — 3.14, а зависимости бэкенда собраны под
# 3.11–3.13 и лежат в .venv: pandas и psycopg2 под 3.14 сборок не имеют.
# Поэтому всё, что работает с бэкендом, ходит через этот резолвер, а не
# через голый python3.
#
# Использование:  ./scripts/python.sh -m uvicorn backend.main:app ...
set -e
APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"

if [ -x "$APP_DIR/.venv/bin/python" ]; then
  exec "$APP_DIR/.venv/bin/python" "$@"
fi

for V in python3.13 python3.12 python3.11; do
  if command -v "$V" &>/dev/null; then
    exec "$V" "$@"
  fi
done

echo "Окружение бэкенда не собрано." >&2
echo "Выполните: npm start   (создаст .venv и поставит зависимости)" >&2
exit 1
