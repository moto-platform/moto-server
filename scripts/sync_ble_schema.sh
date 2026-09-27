#!/usr/bin/env bash
# Copy the BLE telemetry packet schema from moto-connectivity-node, the single
# source of truth (see src/moto_server/schemas/ble_telemetry_packet_schema.json
# and the docstring in ble_schema.py). Run this after moto-connectivity-node's
# schema changes; commit the result.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
default_source="$repo_root/../moto-connectivity-node/docs/ble_telemetry_packet_schema.json"
source_path="${1:-$default_source}"
dest_path="$repo_root/src/moto_server/schemas/ble_telemetry_packet_schema.json"

if [ ! -f "$source_path" ]; then
    echo "error: schema source not found: $source_path" >&2
    echo "usage: $0 [path-to-moto-connectivity-node/docs/ble_telemetry_packet_schema.json]" >&2
    exit 1
fi

cp "$source_path" "$dest_path"
echo "Synced BLE schema from $source_path -> $dest_path"
