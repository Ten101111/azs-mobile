#!/bin/zsh
set -e

APP_DIR="$(cd "$(dirname "$0")/azs-mobile" && pwd)"
cd "$APP_DIR"

if [ ! -d "node_modules" ]; then
  npm install
fi

npm start
