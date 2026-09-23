#!/usr/bin/env python3
"""Point Hermes at the PGX Flash Next EXL3 service and clear stale context metadata."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
from datetime import datetime, timezone

import yaml

MODEL = 'Qwen3.8-Flash-Next-EXL3'
BASE_URL = 'http://127.0.0.1:8899/v1'
CONTEXT_LENGTH = 262144


def configure(home: Path) -> tuple[Path, list[Path]]:
    config = home / 'config.yaml'
    if not config.is_file():
        raise SystemExit(f'Hermes config missing: {config}')
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup = config.with_name(f'config.yaml.before-qwen38-flash-{stamp}')
    shutil.copy2(config, backup)
    data = yaml.safe_load(config.read_text()) or {}
    if not isinstance(data, dict):
        raise SystemExit('Hermes config must be a YAML mapping.')
    model = data.get('model')
    if not isinstance(model, dict):
        model = {}
    model.update({
        'provider': 'custom',
        'default': MODEL,
        'base_url': BASE_URL,
        'api_mode': 'chat_completions',
        'context_length': CONTEXT_LENGTH,
    })
    model.pop('model', None)
    model.pop('name', None)
    data['model'] = model
    temp = config.with_suffix('.yaml.tmp')
    temp.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
    os.chmod(temp, config.stat().st_mode & 0o777)
    os.chown(temp, config.stat().st_uid, config.stat().st_gid)
    os.replace(temp, config)
    cleared = []
    for cache in (home / 'context_length_cache.yaml', home / 'cache' / 'context_length_cache.yaml'):
        if cache.exists():
            cached_backup = cache.with_name(f'{cache.name}.before-qwen38-flash-{stamp}')
            shutil.copy2(cache, cached_backup)
            cache.unlink()
            cleared.append(cache)
    return backup, cleared


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--home', type=Path, required=True)
    parser.add_argument('--restart', action='store_true')
    parser.add_argument('--service', default='hermes-gateway-native.service')
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit('Run as root so Hermes ownership and permissions are preserved.')
    backup, cleared = configure(args.home.resolve())
    print(f'Updated Hermes route: {MODEL} at {BASE_URL}, context={CONTEXT_LENGTH}.')
    print(f'Backup: {backup}')
    print(f'Cleared context caches: {len(cleared)}')
    if args.restart:
        subprocess.run(['/usr/bin/systemctl', 'restart', args.service], check=True)
        subprocess.run(['/usr/bin/systemctl', 'is-active', '--quiet', args.service], check=True)
        print(f'Restarted: {args.service}')


if __name__ == '__main__':
    main()
