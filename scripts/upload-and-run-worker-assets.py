#!/usr/bin/env python3
import os, shlex, subprocess, sys

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
ssh_cmd = ssh_target if ssh_target.strip().startswith('ssh ') else f'ssh {shlex.quote(ssh_target)}'
ssh_argv = shlex.split(ssh_cmd)
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
    raise SystemExit(f'Could not parse SSH destination from: {ssh_cmd}')
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

subprocess.run(['scp', *scp_opts, *files, f'{dest}:/tmp/'], check=True)
remote_env = ' '.join(env_items)
proc = subprocess.run(['ssh', *opts, dest, f'{remote_env} bash /tmp/{os.path.basename(remote_script)}'], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, check=False)
with open(out_path, 'w', encoding='utf-8') as out:
    out.write(proc.stdout or '')
raise SystemExit(proc.returncode)
