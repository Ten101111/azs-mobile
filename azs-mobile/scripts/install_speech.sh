#!/usr/bin/env bash
# ИИ-01: голосовой ввод — поставить локальный движок распознавания речи и скачать модель.
#
# На маке с Apple Silicon ставится mlx-whisper (распознаёт на видеокарте), на остальных
# компьютерах — faster-whisper (процессор). Модель по умолчанию — whisper-large-v3-turbo
# (≈1,6 ГБ; скачивается один раз с Hugging Face в ~/.cache/huggingface). Звук никуда не
# уходит: распознавание идёт на этом компьютере, запись не сохраняется.
#
# Если Hugging Face недоступен из сети компании — скачайте папку модели на другом компьютере
# и укажите путь к ней: AI_STT_MODEL=/путь/к/whisper-large-v3-turbo.
# Выключить голосовой ввод (например, до согласования ИБ на сервере): AI_STT_ENGINE=off.
set -euo pipefail
cd "$(dirname "$0")/.."
./scripts/python.sh -m pip install --quiet -r backend/requirements-speech.txt
./scripts/python.sh - <<'PY'
import time

import numpy as np

from backend.ai import speech

kind = speech.engine()
if not kind:
    raise SystemExit("Движок распознавания не установился — см. сообщения pip выше.")
name = speech.model_name(kind)
print(f"Движок: {kind}, модель: {name}. Загружаю модель (в первый раз — скачивание ≈1,6 ГБ)…")
started = time.monotonic()
speech._run_engine(kind, np.zeros(speech.SAMPLE_RATE, dtype=np.float32))
print(f"Готово за {time.monotonic() - started:.0f} с. Перезапустите приложение (npm start) — "
      "у поля вопроса появится микрофон.")
PY
