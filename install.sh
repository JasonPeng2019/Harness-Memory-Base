#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PYTHON=${MA_HARNESS_PYTHON:-python3}
exec "$PYTHON" "$SCRIPT_DIR/scripts/install_product.py" "$@"
