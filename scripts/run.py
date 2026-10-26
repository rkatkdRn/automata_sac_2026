#!/usr/bin/env python3
"""Local server supervisor; restart crashed Uvicorn and back up SQLite every 5m."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
import time

from backup import ROOT, backup_once, load_environment, local_path, watchdog_running


def main():
    load_environment()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=int(os.getenv('PORT', '8000')))
    parser.add_argument('--no-backup', action='store_true', help='Disable local SQLite backups.')
    args = parser.parse_args()
    if os.getenv('VERCEL'):
        parser.error('The local supervisor must not run inside Vercel Functions.')
    if len(os.getenv('ADMIN_PIN', '')) < 16:
        parser.error('Set ADMIN_PIN to at least 16 characters in .env or your environment.')
    if not 1 <= args.port <= 65535:
        parser.error('PORT must be between 1 and 65535.')
    if watchdog_running():
        parser.error('A local watchdog is already running.')
    with socket.socket() as check:
        try:
            check.bind((args.host, args.port))
        except OSError:
            parser.error('The selected port is already in use or unavailable.')
    os.chdir(ROOT)
    os.environ['DB_PATH'] = str(local_path(os.getenv('DB_PATH', 'data/blotto.db')))
    runtime = ROOT / '.runtime'
    runtime.mkdir(parents=True, exist_ok=True)
    pidfile = runtime / 'watchdog.pid'
    pidfile.write_text(str(os.getpid()), encoding='ascii')
    stop = threading.Event()
    child = None

    def handle_signal(*_):
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, handle_signal)

    def backup_loop():
        # The first request initializes the DB. Wait until that has happened.
        database = Path(os.environ['DB_PATH'])
        while not stop.is_set() and not database.exists():
            stop.wait(2)
        while not stop.is_set():
            try:
                path = backup_once('backup', 48)
                print(f'[backup] {path.name}', flush=True)
            except Exception:
                print('[backup] Failed; inspect configuration and DB health. Next attempt in 5 minutes.', file=sys.stderr, flush=True)
            stop.wait(300)

    backups = None
    if not args.no_backup and not os.getenv('DATABASE_URL'):
        backups = threading.Thread(target=backup_loop, daemon=True)
        backups.start()
    elif os.getenv('DATABASE_URL'):
        print('Remote PostgreSQL selected. Run scripts/backup.py create --loop in a separate terminal for private backups.', flush=True)
    print(f'Local server: http://localhost:{args.port}  |  Stop supervisor and server together with Ctrl+C.', flush=True)
    try:
        while not stop.is_set():
            kwargs = {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == 'nt' else {'start_new_session': True}
            child = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'app:app',
                                      '--host', args.host, '--port', str(args.port),
                                      '--no-access-log'], cwd=ROOT, **kwargs)
            print(f'[watchdog] Server started (PID {child.pid}).', flush=True)
            while child.poll() is None and not stop.wait(0.5):
                pass
            if stop.is_set():
                break
            print(f'[watchdog] Server stopped (exit {child.returncode}); restarting in 2 seconds.', flush=True)
            stop.wait(2)
    finally:
        stop.set()
        if child is not None and child.poll() is None:
            if os.name == 'nt':
                try:
                    child.send_signal(signal.CTRL_BREAK_EVENT)
                except (OSError, ValueError):
                    child.terminate()
            else:
                os.killpg(child.pid, signal.SIGTERM)
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
        if backups is not None:
            backups.join(timeout=10)
        pidfile.unlink(missing_ok=True)
        print('Server stopped. Database and completed backups remain on disk.', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
