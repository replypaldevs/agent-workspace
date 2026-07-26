#!/usr/bin/env python3
from pathlib import Path
import os


def main() -> None:
    out_path = Path("/tmp/incus-cloud-init.yaml")
    bootstrap_b64 = os.environ.get("INCUS_BOOTSTRAP_SCRIPT_B64", "")
    out_path.write_text(
        f"""#cloud-config
write_files:
  - path: /root/incus-heartbeat.sh
    permissions: '0755'
    content: |
      #!/usr/bin/env bash
      set -euo pipefail
      i=0
      while true; do
        printf '%s pid=%s i=%s\\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$$" "$i" >> /root/incus-heartbeat.log
        i=$((i+1))
        sleep 1
      done
  - path: /root/incus-bootstrap-extra.b64
    permissions: '0600'
    encoding: b64
    content: {bootstrap_b64}
runcmd:
  - [ bash, -lc, 'pgrep -af /root/incus-heartbeat.sh >/dev/null 2>&1 || nohup /root/incus-heartbeat.sh >/root/incus-heartbeat.stdout 2>&1 &' ]
  - [ bash, -lc, 'if [ -s /root/incus-bootstrap-extra.b64 ]; then base64 -d /root/incus-bootstrap-extra.b64 >/root/incus-bootstrap-extra.sh && chmod +x /root/incus-bootstrap-extra.sh && /root/incus-bootstrap-extra.sh; fi' ]
  - [ bash, -lc, 'echo BOOT_ID=$(cat /proc/sys/kernel/random/boot_id) > /root/incus-bootstrap-status.txt' ]
  - [ bash, -lc, 'echo HEARTBEAT_STARTED=1 >> /root/incus-bootstrap-status.txt' ]
""",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
