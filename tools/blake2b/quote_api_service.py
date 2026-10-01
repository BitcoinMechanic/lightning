"""Install a loopback quote API user unit without starting services or payments."""
import argparse
import json
import os
from pathlib import Path
import sys

from service_manager import private_load, quote_arg
from reverse_quote_api import token_file

NAME = 'cln-swap-quotes.service'


def render(settings_path, token_path, python, port, auto_process):
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError('invalid port')
    if type(auto_process) is not bool:
        raise ValueError('invalid mode')
    argv = [python, str(Path(__file__).with_name('reverse_quote_api.py').resolve()),
            'serve', '--settings', str(settings_path), '--token-file', str(token_path),
            '--port', str(port)]
    if auto_process:
        argv.append('--auto-process')
    return ('[Unit]\nDescription=Experimental localhost swap quote API\n'
            'After=cln-btc-operator.service cln-xbt-operator.service cln-swap-recovery.service\n'
            'Wants=cln-btc-operator.service cln-xbt-operator.service cln-swap-recovery.service\n'
            'PartOf=cln-swaps.target\n\n[Service]\nType=simple\nUMask=0077\n'
            'ExecStart=' + ' '.join(map(quote_arg, argv)) + '\n'
            'Restart=on-failure\nRestartSec=10\nTimeoutStopSec=120\n'
            'NoNewPrivileges=true\n\n[Install]\nWantedBy=cln-swaps.target\n')


def install(settings_path, unit_dir, port=19840, auto_process=False):
    settings_path = Path(settings_path).expanduser().absolute()
    settings = private_load(settings_path)
    if settings.get('deployment') != 'operator-pair-v1':
        raise ValueError('separate customer deployment required')
    python = settings['python']
    if not Path(python).is_absolute() or not os.access(python, os.X_OK):
        raise ValueError('configured Python unavailable')
    token_path = settings_path.parent / 'customer-api.json'
    unit = render(settings_path, token_path, python, port, auto_process)
    unit_dir = Path(unit_dir)
    dest = unit_dir / NAME
    # Refuse edits before creating credentials. Never silently change spending mode.
    if dest.is_symlink() or (dest.exists() and dest.read_text() != unit):
        raise ValueError('existing unit differs; inspect locally')
    token_file(token_path, settings['receiver_id'])
    unit_dir.mkdir(parents=True, exist_ok=True)
    if not dest.exists():
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as stream:
            stream.write(unit)
            stream.flush()
            os.fsync(stream.fileno())
    return dict(installed=True, auto_process_new_quotes=auto_process,
                listening='127.0.0.1', port=port, services_started=False,
                daemon_reload_required=True)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--settings', type=Path, default=Path.home()/'.config/cln-swaps/settings.json')
    p.add_argument('--port', type=int, default=19840)
    p.add_argument('--auto-process', action='store_true')
    args = p.parse_args()
    os.umask(0o077)
    try:
        print(json.dumps(install(args.settings, Path.home()/'.config/systemd/user',
                                 args.port, args.auto_process)))
        return 0
    except Exception:
        print(json.dumps(dict(event='quote_service_install_failed', details='withheld')))
        return 1


if __name__ == '__main__':
    sys.exit(main())
