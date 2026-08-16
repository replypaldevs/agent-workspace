#!/usr/bin/env python3
import os, shlex, subprocess, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from validate_ssh_command import validate_ssh_command

if len(sys.argv) < 6:
    raise SystemExit("usage: upload-and-run-worker-assets.py <ssh-target> <out-path> <remote-script> <env-json-like-unused> <file...>")

ssh_target = sys.argv[1]
out_path = sys.argv[2]
remote_script = sys.argv[3]
sep = sys.argv.index('--env') if '--env' in sys.argv else None
if sep is None:
    raise SystemExit('missing --env separator')
files = [remote_script, *sys.argv[4:sep]]
env_items = sys.argv[sep+1:]
try:
    ssh_argv = validate_ssh_command(ssh_target)
except ValueError as exc:
    raise SystemExit(f"rejected unsafe ssh command: {exc}")
opts = []
dest = None
i = 1
while i < len(ssh_argv):
    token = ssh_argv[i]
    if token in ('-i', '-p', '-o'):
        opts.extend([token, ssh_argv[i + 1]])
        i += 2
        continue
    if token.startswith('-'):
        opts.append(token)
        i += 1
        continue
    dest = token
    break
if not dest:
    raise SystemExit('Could not parse SSH destination from: ' + ' '.join(ssh_argv))
scp_opts = []
i = 0
while i < len(opts):
    token = opts[i]
    if token == '-p':
        scp_opts.extend(['-P', opts[i + 1]])
        i += 2
    elif token in ('-i', '-o'):
        scp_opts.extend([token, opts[i + 1]])
        i += 2
    else:
        scp_opts.append(token)
        i += 1

default_remote_upload_dir = '/c/tmp' if 'runneradmin@' in dest.lower() else '/tmp'
remote_upload_dir = os.environ.get('REMOTE_UPLOAD_DIR', default_remote_upload_dir).rstrip('/') or default_remote_upload_dir
has_dirs = any(os.path.isdir(path) for path in files)

def upload_with_cat():
    if has_dirs:
        raise SystemExit('scp directory upload failed; fallback streaming upload only supports files')
    subprocess.run(['ssh', *opts, dest, f'mkdir -p {shlex.quote(remote_upload_dir)}'], check=True)
    for path in files:
        remote_path = f'{remote_upload_dir}/{os.path.basename(path)}'
        with open(path, 'rb') as fh:
            subprocess.run(['ssh', *opts, dest, f'cat > {shlex.quote(remote_path)}'], stdin=fh, check=True)

try:
    subprocess.run(['scp', *(('-r',) if has_dirs else ()), *scp_opts, *files, f'{dest}:{remote_upload_dir}/'], check=True)
except subprocess.CalledProcessError:
    upload_with_cat()
def quote_env_item(item):
    key, sep, value = item.partition('=')
    if not sep or not key:
        raise SystemExit(f'invalid env item: {item}')
    return f'{key}={shlex.quote(value)}'


remote_env = ' '.join(quote_env_item(item) for item in env_items)
remote_script_path = f'{remote_upload_dir}/{os.path.basename(remote_script)}'
proc = subprocess.run(['ssh', *opts, dest, f'{remote_env} bash {shlex.quote(remote_script_path)}'], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, check=False)
with open(out_path, 'w', encoding='utf-8') as out:
    out.write(proc.stdout or '')
raise SystemExit(proc.returncode)
