#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
Usage:
  transfer-worker-snapshot-via-release.sh upload <snapshot-path> [snapshot-id]
  transfer-worker-snapshot-via-release.sh download <snapshot-id> <output-path>

Environment:
  REPO         GitHub repo to store the release assets in (default: alexcheng-dev/agent-workspace)
  RELEASE_TAG  Release tag to use as the asset bucket (default: worker-snapshots)
  CHUNK_SIZE   split chunk size for upload (default: 1900m)

Examples:
  ./scripts/transfer-worker-snapshot-via-release.sh upload /tmp/probe-vm.tar.gz probe-vm-20260724
  ./scripts/transfer-worker-snapshot-via-release.sh download probe-vm-20260724 /tmp/probe-vm.tar.gz
EOF
}

require() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Missing required command: $1" >&2
    exit 1
  }
}

file_size_bytes() {
  if stat -c '%s' "$1" >/dev/null 2>&1; then
    stat -c '%s' "$1"
  else
    stat -f '%z' "$1"
  fi
}

sanitize_id() {
  printf '%s' "$1" | tr '[:upper:]_' '[:lower:]-' | tr -cd 'a-z0-9.-'
}

ensure_release() {
  if gh release view "$RELEASE_TAG" --repo "$REPO" >/dev/null 2>&1; then
    return 0
  fi
  gh release create "$RELEASE_TAG" \
    --repo "$REPO" \
    --title "Worker snapshots" \
    --notes "Chunked worker snapshot assets for cross-worker restore handoff."
}

delete_matching_assets() {
  local snapshot_id="$1"
  while IFS=$'\t' read -r asset_id asset_name; do
    [[ -n "${asset_id:-}" ]] || continue
    gh api \
      --method DELETE \
      "repos/$REPO/releases/assets/$asset_id" >/dev/null
    printf 'deleted old asset: %s\n' "$asset_name" >&2
  done < <(
    gh api "repos/$REPO/releases/tags/$RELEASE_TAG" \
      --jq ".assets[]? | select(.name == \"${snapshot_id}.manifest\" or (.name | startswith(\"${snapshot_id}.part-\"))) | [.id, .name] | @tsv"
  )
}

upload_snapshot() {
  local snapshot_path="$1"
  local snapshot_id="${2:-}"
  local tmp_dir chunk_dir base_name size sha256 manifest_path chunk_prefix chunk_count

  [[ -f "$snapshot_path" ]] || { echo "Snapshot file not found: $snapshot_path" >&2; exit 1; }
  [[ -n "$snapshot_id" ]] || snapshot_id="$(sanitize_id "$(basename "$snapshot_path")-$(date -u +%Y%m%d-%H%M%S)")"
  snapshot_id="$(sanitize_id "$snapshot_id")"
  [[ -n "$snapshot_id" ]] || { echo "Snapshot id resolved empty" >&2; exit 1; }

  tmp_dir="$(mktemp -d)"
  chunk_dir="$tmp_dir/chunks"
  mkdir -p "$chunk_dir"

  base_name="$(basename "$snapshot_path")"
  size="$(file_size_bytes "$snapshot_path")"
  sha256="$(sha256sum "$snapshot_path" | awk '{print $1}')"
  manifest_path="$tmp_dir/${snapshot_id}.manifest"
  chunk_prefix="${snapshot_id}.part-"

  split -b "$CHUNK_SIZE" -d -a 4 "$snapshot_path" "$chunk_dir/$chunk_prefix"
  chunk_count="$(find "$chunk_dir" -maxdepth 1 -type f -name "${chunk_prefix}*" | wc -l | tr -d ' ')"

  cat > "$manifest_path" <<EOF
snapshot_id=$snapshot_id
file_name=$base_name
file_size=$size
sha256=$sha256
chunk_size=$CHUNK_SIZE
chunk_count=$chunk_count
created_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
EOF

  ensure_release
  delete_matching_assets "$snapshot_id"

  gh release upload "$RELEASE_TAG" \
    --repo "$REPO" \
    "$manifest_path" \
    "$chunk_dir"/"${chunk_prefix}"*

  printf 'repo=%s\n' "$REPO"
  printf 'release_tag=%s\n' "$RELEASE_TAG"
  printf 'snapshot_id=%s\n' "$snapshot_id"
  printf 'file_name=%s\n' "$base_name"
  printf 'file_size=%s\n' "$size"
  printf 'sha256=%s\n' "$sha256"
  printf 'chunk_count=%s\n' "$chunk_count"
  rm -rf "$tmp_dir"
}

download_snapshot() {
  local snapshot_id="$1"
  local output_path="$2"
  local tmp_dir manifest_path expected_name expected_size expected_sha actual_sha

  snapshot_id="$(sanitize_id "$snapshot_id")"
  [[ -n "$snapshot_id" ]] || { echo "Snapshot id resolved empty" >&2; exit 1; }

  tmp_dir="$(mktemp -d)"

  ensure_release
  gh release download "$RELEASE_TAG" \
    --repo "$REPO" \
    --dir "$tmp_dir" \
    --pattern "${snapshot_id}.manifest"
  gh release download "$RELEASE_TAG" \
    --repo "$REPO" \
    --dir "$tmp_dir" \
    --pattern "${snapshot_id}.part-*"

  manifest_path="$tmp_dir/${snapshot_id}.manifest"
  [[ -f "$manifest_path" ]] || { echo "Manifest missing for snapshot: $snapshot_id" >&2; exit 1; }
  # shellcheck disable=SC1090
  source "$manifest_path"

  mkdir -p "$(dirname "$output_path")"
  cat "$tmp_dir"/"${snapshot_id}".part-* > "$output_path"

  expected_name="${file_name:-}"
  expected_size="${file_size:-}"
  expected_sha="${sha256:-}"

  if [[ -n "$expected_size" ]]; then
    actual_size="$(file_size_bytes "$output_path")"
    [[ "$actual_size" == "$expected_size" ]] || {
      echo "Size mismatch: expected $expected_size got $actual_size" >&2
      exit 1
    }
  fi

  if [[ -n "$expected_sha" ]]; then
    actual_sha="$(sha256sum "$output_path" | awk '{print $1}')"
    [[ "$actual_sha" == "$expected_sha" ]] || {
      echo "SHA256 mismatch: expected $expected_sha got $actual_sha" >&2
      exit 1
    }
  fi

  printf 'output_path=%s\n' "$output_path"
  [[ -n "$expected_name" ]] && printf 'file_name=%s\n' "$expected_name"
  [[ -n "$expected_size" ]] && printf 'file_size=%s\n' "$expected_size"
  [[ -n "$expected_sha" ]] && printf 'sha256=%s\n' "$expected_sha"
  rm -rf "$tmp_dir"
}

require gh
require split
require sha256sum

REPO="${REPO:-alexcheng-dev/agent-workspace}"
RELEASE_TAG="${RELEASE_TAG:-worker-snapshots}"
CHUNK_SIZE="${CHUNK_SIZE:-1900m}"

[[ $# -ge 1 ]] || { usage; exit 1; }
cmd="$1"
shift

case "$cmd" in
  upload)
    [[ $# -ge 1 && $# -le 2 ]] || { usage; exit 1; }
    upload_snapshot "$@"
    ;;
  download)
    [[ $# -eq 2 ]] || { usage; exit 1; }
    download_snapshot "$@"
    ;;
  *)
    usage
    exit 1
    ;;
esac
