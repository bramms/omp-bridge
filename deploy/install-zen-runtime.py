#!/usr/bin/env python3
"""Install only qualified Bun from its official npm tarball, checking SHA-512."""
import importlib.util
import os
from pathlib import Path
import secrets
import shutil
import signal
import tempfile


def main():
    spec = importlib.util.spec_from_file_location('qualify_zen', Path(__file__).with_name('qualify-zen.py'))
    qualify = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(qualify)
    def interrupted(signum, _):
        raise SystemExit(128 + signum)
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, interrupted)
    target = Path(os.environ.get('OMP_FREE_HOME', '~/.config/omp-free-bridge')).expanduser() / 'runtime' / 'bun'
    with tempfile.TemporaryDirectory(prefix='omp-zen-runtime-', dir='/tmp') as folder:
        binary, digest = qualify.cli_binary(Path(folder), bun=True)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        staging = target.with_name('bun.' + secrets.token_hex(8) + '.tmp')
        try:
            with staging.open('xb') as stream, binary.open('rb') as source:
                os.chmod(staging, 0o700)
                shutil.copyfileobj(source, stream)
            os.replace(staging, target)
        finally:
            staging.unlink(missing_ok=True)
    print('Installed qualified Bun 1.3.14:', target)
    print('Official npm integrity:', digest)


if __name__ == '__main__':
    main()
