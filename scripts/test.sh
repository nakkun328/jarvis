#!/bin/sh
set -eu

cd "$(dirname "$0")/.."
for test_file in tests/test_*.py; do
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest --assert=plain -q "$test_file"
done
.venv/bin/python -m scripts.smoke_api
