#!/usr/bin/env bash
set -euo pipefail
IFACE="${1:-enp5s0}"
rm -f /run/systemd/netif/leases/* /var/lib/systemd/network/* 2>/dev/null || true
systemctl restart systemd-networkd 2>/dev/null || true
for _ in $(seq 1 30); do
  if ip -4 addr show "$IFACE" | grep -q ' inet '; then
    break
  fi
  sleep 1
done
ip route || true
