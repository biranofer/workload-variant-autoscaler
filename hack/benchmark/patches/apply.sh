#!/usr/bin/env bash
# Applies our local patches to the cloned llm-d-benchmark/ repo.
# Idempotent: skips a patch if it's already applied (git apply --check).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
PATCH_DIR="$REPO_ROOT/hack/benchmark/patches"
BENCH_DIR="${LLMDBENCH_DIR:-$REPO_ROOT/llm-d-benchmark}"

if [ ! -d "$BENCH_DIR" ]; then
    echo "ERROR: $BENCH_DIR not found. Clone llm-d-benchmark first (e.g. via 'make benchmark-prereq')." >&2
    exit 1
fi

cd "$BENCH_DIR"

for patch in "$PATCH_DIR"/*.patch; do
    [ -f "$patch" ] || continue
    name=$(basename "$patch")
    if git apply --check --reverse "$patch" 2>/dev/null; then
        echo "[skip] $name already applied"
    elif git apply --check "$patch" 2>/dev/null; then
        git apply "$patch"
        echo "[ok]   $name applied"
    else
        echo "[fail] $name does not apply cleanly to $BENCH_DIR — investigate manually" >&2
    fi
done
