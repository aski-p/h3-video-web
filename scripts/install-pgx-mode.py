#!/usr/bin/env python3
"""Root-only setup; no models downloaded and no live services stopped by setup."""
import getpass
import grp
import hashlib
import os
from pathlib import Path
import pwd
import re
import subprocess


def main():
    if os.geteuid() != 0:
        raise SystemExit('Run this installer with sudo on PGX after preparing the recipe/model.')
    user = os.environ.get('H3_SERVICE_USER', 'aski')
    info = pwd.getpwnam(user)
    home = Path(info.pw_dir)
    recipe = Path(os.environ.get('QWEN_RECIPE_DIR', str(home/'Qwen3.8-Flash-Next-EXL3-DGX-Spark-recipe'))).resolve()
    engine = Path(os.environ.get('EXL3_ROOT', str(home/'exllamav3'))).resolve()
    model = Path(os.environ.get('MODEL_DIR', str(home/'models/Qwen3.8-Flash-Next-EXL3'))).resolve()
    for value in (user, str(home), str(recipe), str(engine), str(model)):
        if not re.fullmatch(r'[A-Za-z0-9_./-]+', value):
            raise SystemExit('Use paths without spaces or shell metacharacters.')
    for p in (recipe/'scripts/exl3_native/serve_openai.sh', engine/'.venv/bin/python', model/'config.json'):
        if not p.is_file():
            raise SystemExit(f'Required file missing: {p}')
    # Never replace an unrelated pre-existing service.
    unit = Path('/etc/systemd/system/qwen38-exl3.service')
    if unit.exists() and '# Managed by h3-pgx-mode' not in unit.read_text():
        raise SystemExit('Existing qwen38-exl3.service must be reviewed before installation.')
    pin = getpass.getpass('모드 전환 비밀번호를 입력하세요 (요청하신 값: 1229): ')
    if len(pin)<4:
        raise SystemExit('At least 4 characters required.')
    salt = os.urandom(24); rounds = 240000
    digest = hashlib.pbkdf2_hmac('sha256', pin.encode(), salt, rounds)
    descriptor = f'pbkdf2_sha256${rounds}${salt.hex()}${digest.hex()}'
    directory=Path('/etc/h3-pgx-mode');directory.mkdir(exist_ok=True,mode=0o750)
    os.chown(directory,0,info.pw_gid)
    env=directory/'mode.env'
    env.write_text(f'H3_PGX_MODE_ENABLED=1\nH3_MODE_PIN_PBKDF2={descriptor}\n')
    os.chown(env,0,info.pw_gid);env.chmod(0o640)
    unit.write_text(f'''# Managed by h3-pgx-mode
[Unit]
Description=Qwen 3.8 Flash Next EXL3 dedicated PGX API
After=network-online.target
[Service]
Type=simple
User={user}
WorkingDirectory={recipe}
Environment=EXL3_ROOT={engine}
Environment=MODEL_DIR={model}
Environment=HOST=127.0.0.1
Environment=PORT=8899
Environment=CS=262144
ExecStart=/bin/bash {recipe}/scripts/exl3_native/serve_openai.sh
KillMode=control-group
TimeoutStartSec=600
TimeoutStopSec=90
Restart=no
[Install]
WantedBy=multi-user.target
''')
    policy=Path('/etc/polkit-1/rules.d/49-h3-pgx-mode.rules')
    policy.write_text(f'''// Managed by h3-pgx-mode: only start/stop these two fixed units.
polkit.addRule(function(action, subject) {{
 if (action.id == "org.freedesktop.systemd1.manage-units" && subject.user == "{user}" &&
     ["start", "stop"].indexOf(action.lookup("verb")) >= 0 &&
     ["comfyui-minimax-h3.service", "qwen38-exl3.service"].indexOf(action.lookup("unit")) >= 0)
   return polkit.Result.YES;
}});
''')
    dropdir=home/'.config/systemd/user/h3-web-backend.service.d'
    dropdir.mkdir(parents=True,exist_ok=True)
    drop=dropdir/'30-pgx-mode.conf'
    drop.write_text('[Service]\nEnvironmentFile=/etc/h3-pgx-mode/mode.env\n')
    os.chown(drop,info.pw_uid,info.pw_gid)
    subprocess.run(['/usr/bin/systemctl','daemon-reload'],check=True)
    print('Configured. No model/service has been started or stopped.')
    print('After checking PGX jobs are idle, reload the user daemon and restart h3-web-backend as its service user.')

if __name__ == '__main__': main()
