#!/bin/zsh
set -e

cd "$(dirname "$0")/.."
python3 scripts/prepare_data.py

echo
echo "data/stations.json updated. Press Enter to close."
read
