#!/usr/bin/env python3
"""Private database backups. Never sends credentials in command arguments.

Use a direct/unpooled PostgreSQL connection for administration. The files this
script creates contain participant information and must not be committed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
from datetime import datetime
from urllib.parse import parse_qs, unquote, urlsplit
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
TABLES = ('participants', 'submissions', 'entitlements', 'inventory_ledger',
          'day_config', 'threshold_history', 'rank_freeze')


def load_environment():
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(ROOT / '.env', override=False)


def local_path(value: str | Path) -> Path:
    value = Path(value).expanduser()
    return value.resolve() if value.is_absolute() else (ROOT / value).resolve()


def postgres_environment(url: str) -> dict[str, str]:
    """Translate URL to libpq variables; never return a URL to command arguments."""
    parsed = urlsplit(url)
    if parsed.scheme not in ('postgres', 'postgresql') or not parsed.hostname:
        raise ValueError('PostgreSQL connection URL is invalid.')
    if '-pooler' in parsed.hostname:
        raise ValueError('Use DATABASE_URL_UNPOOLED with a direct database hostname for backups/restores.')
    if not parsed.path.strip('/'):
        raise ValueError('The connection URL must include a database name.')
    env = os.environ.copy()
    # Ignore inherited libpq routing parameters so the explicit URL is authoritative.
    for key in list(env):
        if key.startswith('PG'):
            env.pop(key)
    env.update(PGHOST=parsed.hostname,
               PGPORT=str(parsed.port or 5432),
               PGDATABASE=unquote(parsed.path.lstrip('/')),
               PGCONNECT_TIMEOUT='15', PGAPPNAME='blotto-backup')
    if parsed.username is not None:
        env['PGUSER'] = unquote(parsed.username)
    if parsed.password is not None:
        env['PGPASSWORD'] = unquote(parsed.password)
    params = parse_qs(parsed.query)
    for name, target in [('sslmode', 'PGSSLMODE'), ('sslrootcert', 'PGSSLROOTCERT'),
                         ('sslcert', 'PGSSLCERT'), ('sslkey', 'PGSSLKEY'),
                         ('channel_binding', 'PGCHANNELBINDING')]:
        if name in params:
            env[target] = params[name][-1]
    env.setdefault('PGSSLMODE', 'require')
    return env


def target() -> tuple[str, str | Path]:
    url = os.getenv('DATABASE_URL_UNPOOLED') or os.getenv('DATABASE_URL')
    if url:
        return 'postgres', url
    if os.getenv('VERCEL'):
        raise ValueError('Vercel needs PostgreSQL. Local disk cannot be used as its database.')
    return 'sqlite', local_path(os.getenv('DB_PATH', 'data/blotto.db'))


def secure_file(path: Path):
    # Windows ACLs must be managed by the operator; chmod restricts Unix systems.
    try:
        path.chmod(0o600)
    except OSError:
        pass


def run_pg(args: list[str], env: dict[str, str]):
    if not shutil.which(args[0]):
        raise RuntimeError(f'{args[0]} is not installed or is not on PATH. Install PostgreSQL client tools.')
    try:
        result = subprocess.run(args, env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                timeout=180)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f'{args[0]} timed out after 180 seconds.') from None
    if result.returncode:
        # Raw stderr can contain connection parameters. Do not print it.
        raise RuntimeError(f'{args[0]} failed (exit {result.returncode}). Check network, database permissions and client/server versions.')


def validate_sqlite(path: Path) -> dict[str, int]:
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as conn:
        checks = [row[0] for row in conn.execute('PRAGMA integrity_check')]
        if checks != ['ok']:
            raise RuntimeError('SQLite integrity check failed; do not restore this file.')
        available = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing = set(TABLES) - available
        if missing:
            raise RuntimeError('The SQLite file is not a complete Blotto database.')
        return {name: conn.execute(f'SELECT COUNT(*) FROM {name}').fetchone()[0]
                for name in TABLES}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def backup_once(output: str | Path = 'backup', retain: int = 48,
                selected: tuple[str, str | Path] | None = None) -> Path:
    if retain < 1:
        raise ValueError('Retention must be at least 1.')
    kind, source = selected or target()
    directory = local_path(output)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(ZoneInfo('Asia/Seoul')).strftime('%Y%m%d-%H%M%S-%f')
    suffix = '.db' if kind == 'sqlite' else '.dump'
    final = directory / f'blotto-{stamp}{suffix}'
    temporary = final.with_suffix(final.suffix + '.tmp')
    counts = None
    try:
        if kind == 'sqlite':
            source = Path(source)
            if not source.is_file():
                raise FileNotFoundError('Database does not exist yet. Start the application before backing up.')
            # SQLite's online backup API includes committed WAL contents safely.
            with sqlite3.connect(source.as_uri() + '?mode=ro', uri=True, timeout=30) as src:
                with sqlite3.connect(temporary) as dst:
                    src.backup(dst, pages=256, sleep=0.05)
            counts = validate_sqlite(temporary)
        else:
            env = postgres_environment(str(source))
            run_pg(['pg_dump', '--format=custom', '--no-owner', '--no-acl',
                    '--file', str(temporary)], env)
            run_pg(['pg_restore', '--list', str(temporary)], env)
        secure_file(temporary)
        os.replace(temporary, final)
        manifest = {'format': 'blotto-private-backup-v1', 'kind': kind,
                    'created_at': datetime.now(ZoneInfo('Asia/Seoul')).isoformat(),
                    'sha256': sha256(final), 'table_counts': counts,
                    'contains_personal_data': True}
        manifest_path = final.with_suffix(final.suffix + '.json')
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
        secure_file(manifest_path)
        # Retain only completed backups from this format; never delete arbitrary files.
        completed = sorted(directory.glob(f'blotto-*{suffix}'))
        for old in completed[:-retain]:
            old.unlink()
            old.with_suffix(old.suffix + '.json').unlink(missing_ok=True)
        return final
    finally:
        temporary.unlink(missing_ok=True)


def verify_manifest(path: Path, kind: str):
    sidecar = path.with_suffix(path.suffix + '.json')
    if not sidecar.is_file():
        raise ValueError('Backup checksum manifest is missing. Use a complete backup + .json pair.')
    value = json.loads(sidecar.read_text(encoding='utf-8'))
    if value.get('format') != 'blotto-private-backup-v1' or value.get('kind') != kind:
        raise ValueError('Backup format/database type does not match.')
    if value.get('sha256') != sha256(path):
        raise ValueError('Backup checksum differs; the file may be incomplete or damaged.')


def watchdog_running() -> bool:
    pidfile = ROOT / '.runtime' / 'watchdog.pid'
    if not pidfile.exists():
        return False
    try:
        pid = int(pidfile.read_text().strip())
        if os.name == 'nt':
            # os.kill(pid, 0) is not a portable Windows existence check.
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
            kernel.GetExitCodeProcess.restype = wintypes.BOOL
            kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
            handle = kernel.OpenProcess(0x1000, False, pid)
            if not handle:
                return ctypes.get_last_error() != 87  # invalid PID => process gone
            try:
                status = wintypes.DWORD()
                return not kernel.GetExitCodeProcess(handle, ctypes.byref(status)) or status.value == 259
            finally:
                kernel.CloseHandle(handle)
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except (ValueError, OSError):
        # Unknown or inaccessible process: refuse destructive restore.
        return True


def restore_file(source: str | Path, confirm: str, server_stopped: bool,
                 safety_dir: str | Path = 'backup/pre-restore') -> Path | None:
    if confirm != 'RESTORE' or not server_stopped:
        raise ValueError('Stop the application, then pass --server-stopped --confirm RESTORE.')
    if watchdog_running():
        raise ValueError('The local watchdog is still running. Stop it with Ctrl+C first.')
    kind, destination = target()
    source = local_path(source)
    if not source.is_file():
        raise ValueError('Backup file does not exist.')
    verify_manifest(source, kind)
    if kind == 'sqlite':
        validate_sqlite(source)
        destination = Path(destination)
        if source == destination:
            raise ValueError('Backup and live database paths must differ.')
        destination.parent.mkdir(parents=True, exist_ok=True)
        safety = backup_once(safety_dir, retain=10000) if destination.exists() else None
        temporary = destination.with_suffix(destination.suffix + '.restore.tmp')
        try:
            shutil.copyfile(source, temporary)
            secure_file(temporary)
            validate_sqlite(temporary)
            # Previous WAL/SHM must not be replayed into the restored snapshot.
            # Keep them for diagnosis rather than deleting them.
            for suffix in ('-wal', '-shm'):
                old = Path(str(destination) + suffix)
                if old.exists():
                    preserved = local_path(safety_dir) / f'{datetime.now().strftime("%Y%m%d-%H%M%S-%f")}{suffix}'
                    preserved.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(old, preserved)
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        return safety
    env = postgres_environment(str(destination))
    run_pg(['pg_restore', '--list', str(source)], env)
    safety = backup_once(safety_dir, retain=10000)
    # A failed restore rolls back all destructive SQL instead of leaving half a schema.
    run_pg(['pg_restore', '--clean', '--if-exists', '--no-owner', '--no-acl',
            '--exit-on-error', '--single-transaction', '--dbname', env['PGDATABASE'],
            str(source)], env)
    return safety


def main():
    load_environment()
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    create = sub.add_parser('create', help='Create a full private database backup.')
    create.add_argument('--output', default='backup')
    create.add_argument('--retain', type=int, default=48)
    create.add_argument('--loop', action='store_true')
    create.add_argument('--interval', type=int, default=300)
    restore = sub.add_parser('restore', help='Restore after stopping the entire application.')
    restore.add_argument('file')
    restore.add_argument('--confirm', required=True)
    restore.add_argument('--server-stopped', action='store_true')
    restore.add_argument('--safety-dir', default='backup/pre-restore')
    args = parser.parse_args()
    try:
        if args.command == 'restore':
            safety = restore_file(args.file, args.confirm, args.server_stopped, args.safety_dir)
            print('Restore completed. Verify /healthz, submission totals and inventory before reopening.')
            if safety:
                print(f'Pre-restore backup: {safety}')
            return
        if args.interval < 30:
            raise ValueError('Backup interval must be at least 30 seconds.')
        stop = threading.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: stop.set())
        while not stop.is_set():
            try:
                print(f'Backup completed: {backup_once(args.output, args.retain)}', flush=True)
            except Exception as exc:
                # The messages above are deliberately free of credentials.
                print(f'Backup failed: {type(exc).__name__}. Check configuration and database connectivity.', file=sys.stderr, flush=True)
                if not args.loop:
                    return 1
            if not args.loop or stop.wait(args.interval):
                break
    except (ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
