import importlib.util
import argparse
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path


APP = Path(__file__).resolve().parents[1]
REQUIREMENTS = APP / "backend" / "requirements.txt"
FRONTEND_PORT = 5174


def missing_backend_modules():
    return [
        module
        for module in ("fastapi", "uvicorn")
        if importlib.util.find_spec(module) is None
    ]


def ensure_backend_dependencies():
    missing = missing_backend_modules()
    if not missing:
        return

    print(f"Backend dependencies missing: {', '.join(missing)}")
    print(f"Installing Python packages from {REQUIREMENTS}...")
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "-r", str(REQUIREMENTS)],
        cwd=APP,
        check=True,
    )


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
        command,
        cwd=APP,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    thread = threading.Thread(target=stream_output, args=(name, process), daemon=True)
    thread.start()
    return process


def build_frontend():
    print("Building frontend for PWA precache...")
    subprocess.run(["npm", "run", "build"], cwd=APP, check=True)


def main():
    parser = argparse.ArgumentParser(description="Run AZS app with API and frontend.")
    parser.add_argument(
        "--mode",
        choices=("pwa", "dev"),
        default="pwa",
        help="pwa runs production preview with generated service worker; dev runs Vite dev server with HMR.",
    )
    args = parser.parse_args()

    ensure_backend_dependencies()
    if args.mode == "pwa":
        build_frontend()

    commands = [
        (
            "api",
            [sys.executable, "-m", "uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
            + (["--reload"] if args.mode == "dev" else []),
        ),
        (
            "web",
            ["npm", "run", "dev"]
            if args.mode == "dev"
            else ["npm", "run", "preview"],
        ),
    ]
    processes = [start_process(name, command) for name, command in commands]

    def handle_stop(_signum=None, _frame=None):
        print("\nStopping app...")
        terminate(processes)

    signal.signal(signal.SIGINT, handle_stop)
    signal.signal(signal.SIGTERM, handle_stop)

    print(f"\nApp is starting in {args.mode.upper()} mode.")
    print(f"Frontend: http://localhost:{FRONTEND_PORT}/")
    print("Backend:  http://localhost:8000/api/health")
    print("Press Ctrl+C to stop both servers.\n")

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
