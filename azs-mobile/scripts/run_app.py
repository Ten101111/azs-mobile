"""Запуск приложения одной командой.

Поднимает бэкенд и фронтенд в одном терминале и сам готовит всё, что для этого
нужно: виртуальное окружение Python, зависимости, справочник объектов для
ИИ-контура. Сам скрипт пользуется только стандартной библиотекой, поэтому
запускается любым системным python3, включая 3.14.

    npm start        рабочий режим: сборка + preview + service worker
    npm run dev:full  режим разработки: Vite с горячей перезагрузкой
"""
import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path


APP = Path(__file__).resolve().parents[1]
REQUIREMENTS = APP / "backend" / "requirements.txt"
VENV = APP / ".venv"
FRONTEND_PORT = 5174

# Библиотеки, без которых бэкенд не поднимется.
REQUIRED_MODULES = ("fastapi", "uvicorn", "sqlglot")
# Версии Python, под которые есть готовые сборки pandas и psycopg2.
SUPPORTED_PYTHONS = ("python3.13", "python3.12", "python3.11", "python3.10")

OLLAMA_HOST = os.environ.get("AI_OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")


# --- подготовка окружения ---------------------------------------------------

def venv_python() -> Path | None:
    path = VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    return path if path.exists() else None


def interpreter_works(python: Path) -> bool:
    """Окружение могло остаться от версии Python, которую Homebrew уже удалил."""
    try:
        return subprocess.run(
            [str(python), "-c", "pass"], capture_output=True, timeout=30
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def ensure_venv() -> Path:
    existing = venv_python()
    if existing and interpreter_works(existing):
        return existing
    if existing:
        print("Окружение .venv нерабочее — пересобираю его …")
        shutil.rmtree(VENV, ignore_errors=True)

    interpreter = next((shutil.which(name) for name in SUPPORTED_PYTHONS if shutil.which(name)), None)
    if not interpreter:
        raise SystemExit(
            "Не найден Python 3.11-3.13, а под 3.14 нет готовых сборок pandas и psycopg2.\n"
            "Установите его и запустите команду снова:\n"
            "    brew install python@3.12"
        )

    print(f"Создаю окружение .venv на {interpreter} …")
    subprocess.run([interpreter, "-m", "venv", str(VENV)], check=True)
    created = venv_python()
    if not created:
        raise SystemExit("Окружение .venv создано, но интерпретатор в нём не найден.")
    return created


def missing_modules(python: Path) -> list[str]:
    probe = (
        "import importlib.util, sys;"
        "print(','.join(m for m in sys.argv[1:] if importlib.util.find_spec(m) is None))"
    )
    result = subprocess.run(
        [str(python), "-c", probe, *REQUIRED_MODULES],
        capture_output=True, text=True, cwd=APP,
    )
    return [name for name in result.stdout.strip().split(",") if name]


def ensure_backend_dependencies(python: Path) -> None:
    missing = missing_modules(python)
    if not missing:
        return
    print(f"Ставлю зависимости бэкенда ({', '.join(missing)}) из {REQUIREMENTS.name} …")
    subprocess.run(
        [str(python), "-m", "pip", "install", "--quiet", "-r", str(REQUIREMENTS)],
        cwd=APP, check=True,
    )
    still_missing = missing_modules(python)
    if still_missing:
        raise SystemExit(f"Не удалось установить: {', '.join(still_missing)}")


def ensure_frontend_dependencies() -> None:
    if (APP / "node_modules" / "vite").exists():
        return
    print("Ставлю зависимости фронтенда (npm install) …")
    subprocess.run(["npm", "install"], cwd=APP, check=True)


def check_entry_point() -> None:
    if (APP / "index.html").exists():
        return
    raise SystemExit(
        "В корне проекта нет index.html — Vite не с чего начинать страницу,\n"
        "и на любой адрес будет приходить 404. Восстановите файл из git:\n"
        '    git checkout -- azs-mobile/index.html'
    )


def ensure_ai_reference(python: Path) -> None:
    builder = APP / "backend" / "ai" / "build_reference.py"
    reference = APP / "data" / "ai_reference.sqlite3"
    if reference.exists() or not builder.exists():
        return
    print("Собираю справочник объектов для ИИ-контура …")
    subprocess.run([str(python), str(builder)], cwd=APP, check=False)


def ai_status_line() -> str:
    if (os.environ.get("AI_DEMO_ENABLED") or "").strip().lower() not in {"1", "true", "yes", "on"}:
        return "ИИ-раздел:  выключен (AI_DEMO_ENABLED не задан)"
    wanted = os.environ.get("AI_MODEL", "")
    try:
        with urllib.request.urlopen(f"{OLLAMA_HOST}/api/tags", timeout=3) as response:
            models = [m.get("name", "") for m in json.loads(response.read()).get("models", [])]
    except Exception:  # noqa: BLE001
        return f"ИИ-раздел:  включён, но Ollama не отвечает на {OLLAMA_HOST} — запустите `ollama serve`"
    if not models:
        return "ИИ-раздел:  включён, но в Ollama не загружено ни одной модели"
    if wanted and wanted not in models:
        return f"ИИ-раздел:  включён, но модели {wanted} нет. Загружены: {', '.join(models)}"
    return f"ИИ-раздел:  включён, модель {wanted or models[0]}"


# --- процессы ---------------------------------------------------------------

def stream_output(name, process):
    for line in process.stdout:
        print(f"[{name}] {line}", end="")


def terminate(processes):
    for process in processes:
        if process.poll() is None:
            process.terminate()

    deadline = time.time() + 5
    while time.time() < deadline:
        if all(process.poll() is not None for process in processes):
            return
        time.sleep(0.1)

    for process in processes:
        if process.poll() is None:
            process.kill()


def start_process(name, command):
    process = subprocess.Popen(
        command, cwd=APP, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
    )
    thread = threading.Thread(target=stream_output, args=(name, process), daemon=True)
    thread.start()
    return process


def build_frontend():
    print("Собираю фронтенд для режима PWA …")
    subprocess.run(["npm", "run", "build"], cwd=APP, check=True)


# --- точка входа ------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Запуск приложения АЗС: API и фронтенд.")
    parser.add_argument(
        "--mode", choices=("pwa", "dev"), default="pwa",
        help="pwa — сборка и preview с service worker; dev — Vite с горячей перезагрузкой.",
    )
    args = parser.parse_args()

    check_entry_point()
    python = ensure_venv()
    ensure_backend_dependencies(python)
    ensure_frontend_dependencies()
    ensure_ai_reference(python)

    if args.mode == "pwa":
        build_frontend()

    commands = [
        (
            "api",
            [str(python), "-m", "uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
            + (["--reload"] if args.mode == "dev" else []),
        ),
        ("web", ["npm", "run", "dev"] if args.mode == "dev" else ["npm", "run", "preview"]),
    ]
    processes = [start_process(name, command) for name, command in commands]

    def handle_stop(_signum=None, _frame=None):
        print("\nОстанавливаю приложение …")
        terminate(processes)

    signal.signal(signal.SIGINT, handle_stop)
    signal.signal(signal.SIGTERM, handle_stop)

    print(f"\nРежим: {args.mode.upper()}   Python: {python}")
    print(f"Фронтенд:   http://localhost:{FRONTEND_PORT}/")
    print("Бэкенд:     http://localhost:8000/api/health")
    print(ai_status_line())
    print("Остановить оба процесса — Ctrl+C.\n")

    exit_code = 0
    try:
        while True:
            for process in processes:
                code = process.poll()
                if code is not None:
                    exit_code = code
                    terminate(processes)
                    raise SystemExit(exit_code)
            time.sleep(0.5)
    except KeyboardInterrupt:
        handle_stop()
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
