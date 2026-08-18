#!/usr/bin/env bash
set -euo pipefail

# Recover an Incus renewal chain from the newest immutable release snapshot.
# This deliberately fails when no snapshot exists; recovery must never create
# an unrelated blank VM under the stateful VM name.
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
REPO="${REPO:-replypaldevs/agent-workspace}"
VM_NAME="${INCUS_STATEFUL_VM_NAME:-worker-agents-vm}"
RELEASE_TAG="${INCUS_STATEFUL_RELEASE_TAG:-worker-snapshots}"
WORKER_REF="${WORKER_AGENTS_GIT_REF:-main}"
TEST_SLEEP_SECONDS="${TEST_SLEEP_SECONDS:-3600}"
PROVISION_TRACE="${PROVISION_TRACE:-1}"

command -v gh >/dev/null || { echo "gh is required" >&2; exit 1; }
command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }

SNAPSHOT_ID="$(REPO="$REPO" RELEASE_TAG="$RELEASE_TAG" \
  "$ROOT_DIR/scripts/transfer-worker-snapshot-via-release.sh" latest "${VM_NAME}-stateful-")"
if [[ -z "$SNAPSHOT_ID" ]]; then
  echo "No immutable Incus snapshot found for VM: $VM_NAME" >&2
  exit 1
fi

echo "Recovering $VM_NAME from snapshot $SNAPSHOT_ID" >&2
REPO="$REPO" \
INCUS_STATEFUL_VM_NAME="$VM_NAME" \
INCUS_STATEFUL_RELEASE_TAG="$RELEASE_TAG" \
INCUS_STATEFUL_SNAPSHOT_ID="$SNAPSHOT_ID" \
WORKER_AGENTS_GIT_REF="$WORKER_REF" \
TEST_SLEEP_SECONDS="$TEST_SLEEP_SECONDS" \
PROVISION_TRACE="$PROVISION_TRACE" \
SYNC_WORKER_AGENTS_PUBLIC="${SYNC_WORKER_AGENTS_PUBLIC:-1}" \
"$ROOT_DIR/scripts/run-incus-stateful-worker-agents-worker.sh"
