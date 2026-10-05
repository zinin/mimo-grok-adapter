#!/usr/bin/env python3
"""Install or update the MiMo adapter and its user service without changing Grok."""

import argparse
import datetime
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid

UNIT = 'mimo-grok-adapter.service'
ROOT = Path(__file__).resolve().parent


class InstallError(RuntimeError):
    pass


def systemctl(*args, required=True):
    try:
        result = subprocess.run(['systemctl', '--user', *args],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                timeout=30)
    except (OSError, subprocess.SubprocessError) as error:
        raise InstallError('systemctl --user is unavailable or timed out.') from error
    if required and result.returncode:
        raise InstallError('systemctl --user failed: ' + args[0] + '.')
    return result.returncode == 0


def main_pid():
    try:
        result = subprocess.run(['systemctl', '--user', 'show', UNIT, '--property', 'MainPID', '--value'],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=10)
        pid = int(result.stdout.strip())
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise InstallError('The user service PID is unavailable.') from error
    if result.returncode or pid <= 0:
        raise InstallError('The user service has no running process.')
    return pid


def wait_for_health(url='http://127.0.0.1:8320/health', timeout=10, expected_pid=None):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with opener.open(url, timeout=min(2, timeout)) as response:
                health = json.load(response)
                if (response.status == 200 and health.get('status') == 'ok'
                        and (expected_pid is None or health.get('pid') == expected_pid)):
                    return
        except (OSError, urllib.error.URLError, ValueError, AttributeError):
            pass
        time.sleep(0.2)
    raise InstallError('The adapter failed the local /health check.')


def atomic_write(path, data, mode):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.mimo-install-', delete=False) as file:
            temporary = Path(file.name)
            os.fchmod(file.fileno(), mode)
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def installation_files(home):
    return [
        (ROOT / 'mimo-grok-adapter', home / '.local/bin/mimo-grok-adapter', 0o755),
        (ROOT / 'check-mimo-grok', home / '.local/bin/check-mimo-grok', 0o755),
        (ROOT / 'systemd' / UNIT, home / '.config/systemd/user' / UNIT, 0o644),
    ]


def snapshot(files):
    previous = {}
    for source, target, mode in files:
        if not source.is_file() or source.is_symlink():
            raise InstallError('Missing regular source file: ' + source.name)
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise InstallError('The destination path must be a regular file: ' + str(target))
        if target.exists():
            if target.stat().st_uid != os.getuid():
                raise InstallError('The destination file belongs to another user: ' + str(target))
            previous[target] = (target.read_bytes(), target.stat().st_mode & 0o777)
        else:
            previous[target] = None
    return previous


def save_backups(home, previous):
    if not any(value is not None for value in previous.values()):
        return None
    label = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    directory = home / '.local/state/mimo-grok-adapter/backups' / (label + '-' + uuid.uuid4().hex[:8])
    directory.mkdir(parents=True, mode=0o700)
    manifest = []
    for target, value in previous.items():
        manifest.append({'path': str(target), 'existed': value is not None,
                         'mode': value[1] if value else None})
        if value is not None:
            atomic_write(directory / target.name, value[0], 0o600)
    atomic_write(directory / 'manifest.json', json.dumps(manifest, indent=2).encode() + b'\n', 0o600)
    return directory


def restore_files(previous, changed):
    failures = []
    for target in reversed(changed):
        try:
            value = previous[target]
            if value is None:
                target.unlink(missing_ok=True)
            else:
                atomic_write(target, value[0], value[1])
        except OSError:
            failures.append(str(target))
    return failures


def preflight(no_start):
    if sys.version_info < (3, 11) or sys.platform != 'linux':
        raise InstallError('Linux and Python 3.11+ are required.')
    if os.geteuid() == 0:
        raise InstallError('Run as your regular user; elevated privileges are unsupported.')
    try:
        version = subprocess.run(['/usr/bin/python3', '-c',
                                  'import sys; sys.exit(sys.version_info < (3, 11))'],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
    except (OSError, subprocess.SubprocessError) as error:
        raise InstallError('/usr/bin/python3 version 3.11+ is required.') from error
    if version.returncode:
        raise InstallError('/usr/bin/python3 version 3.11+ is required.')
    if not no_start:
        if shutil.which('systemctl') is None:
            raise InstallError('systemctl is required; use --no-start for file-only installation.')
        systemctl('show-environment')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--no-start', action='store_true',
                        help='install files only and leave systemd unchanged')
    args = parser.parse_args(argv)
    home = Path.home()
    previous, changed = {}, []
    backup = None
    service_touched = False
    was_enabled = was_active = False
    try:
        preflight(args.no_start)
        files = installation_files(home)
        previous = snapshot(files)
        if not args.no_start:
            was_enabled = systemctl('is-enabled', UNIT, required=False)
            was_active = systemctl('is-active', '--quiet', UNIT, required=False)
        backup = save_backups(home, previous)
        for source, target, mode in files:
            atomic_write(target, source.read_bytes(), mode)
            changed.append(target)
        if not args.no_start:
            service_touched = True
            systemctl('daemon-reload')
            systemctl('enable', UNIT)
            systemctl('restart', UNIT)
            pid = main_pid()
            wait_for_health(expected_pid=pid)
            if not systemctl('is-active', '--quiet', UNIT, required=False) or main_pid() != pid:
                raise InstallError('The user service process stopped or changed after startup.')
        print('Installed ~/.local/bin/mimo-grok-adapter, ~/.local/bin/check-mimo-grok')
        print('and ~/.config/systemd/user/' + UNIT)
        if backup:
            print('Backup: ' + str(backup))
        print('Files installed; systemd unchanged.' if args.no_start
              else 'User service enabled, started, and verified through /health.')
        return 0
    except (InstallError, OSError, subprocess.SubprocessError) as error:
        # Do not echo external command output, which can contain credentials.
        message = str(error) if isinstance(error, InstallError) else type(error).__name__
        print('Installation failed: ' + message, file=sys.stderr)
        failed_paths = restore_files(previous, changed)
        if service_touched:
            try:
                if not was_active:
                    systemctl('stop', UNIT, required=False)
                if not was_enabled:
                    systemctl('disable', UNIT, required=False)
                systemctl('daemon-reload')
                if was_active:
                    systemctl('restart', UNIT)
            except InstallError:
                print('The service state requires manual inspection.', file=sys.stderr)
        if failed_paths:
            print('Files requiring manual recovery: ' + ', '.join(failed_paths), file=sys.stderr)
        elif changed:
            print('Previous files restored.', file=sys.stderr)
        if backup:
            print('Backup: ' + str(backup), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
