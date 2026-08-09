#!/usr/bin/env bash
# Emit `how_many` consecutive integers starting at `start`, one per line.
# Usage: count.sh <start> <how_many>
set -euo pipefail

start="${1:-1}"
how_many="${2:-10}"

for ((i = 0; i < how_many; i++)); do
  echo "$((start + i))"
done
