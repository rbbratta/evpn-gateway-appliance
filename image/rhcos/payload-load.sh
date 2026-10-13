#!/usr/bin/bash
set -euo pipefail

payload_directory=${1:-/var/lib/ega/payload}
inventory=${2:-/etc/ega/payloads.tsv}
cd "$payload_directory"
count=0
declare -A seen
while IFS=$'\t' read -r role archive expected_hash image_id extra; do
    [[ "$role" == frr || "$role" == node-exporter ]]
    [[ -z "${seen[$role]:-}" && -z "$extra" ]]
    [[ "$archive" == "$role.tar" && "$expected_hash" =~ ^[0-9a-f]{64}$ ]]
    [[ "$image_id" =~ ^sha256:[0-9a-f]{64}$ ]]
    seen[$role]=yes
    actual_hash=$(sha256sum -- "$archive")
    [[ "${actual_hash%% *}" == "$expected_hash" ]] || {
        echo "Archive checksum failed for $archive" >&2
        exit 1
    }
    if ! podman image exists "$image_id"; then
        podman load --input "$archive"
    fi
    actual_id=$(podman image inspect --format '{{.Id}}' "$image_id")
    [[ "${actual_id#sha256:}" == "${image_id#sha256:}" ]] || {
        echo "Loaded image identity failed for $role" >&2
        exit 1
    }
    count=$((count + 1))
done < "$inventory"
[[ "$count" == 2 && -n "${seen[frr]:-}" && -n "${seen[node-exporter]:-}" ]]
