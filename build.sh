#!/bin/bash
# Builds the single-file executable dist/wattson (zipapp, stdlib only).
set -euo pipefail

cd "$(dirname "$(readlink -f "$0")")"

find src -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
rm -f dist/wattson
mkdir -p dist

# the whole src tree goes in, message catalogs of src/wattson/locales included
python3 -m zipapp src \
    --main wattson.cli:main \
    --python "/usr/bin/python3" \
    --compress \
    --output dist/wattson

chmod 755 dist/wattson
echo "built: $(pwd)/dist/wattson ($(du -h dist/wattson | cut -f1))"
