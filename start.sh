#!/usr/bin/env bash
# Boots a fresh hosted box: makes the signing key and one demo grant exist if
# they do not yet, then starts the server. Every step is idempotent, so this
# runs the same way on the very first boot and on every boot after that.
set -euo pipefail

cd "$(dirname "$0")"
export PYTHONPATH="$(pwd)/src"

if [ ! -f keys/grant_signing_key.pem ]; then
  echo "no signing key yet — generating one"
  python execution/issue_grant.py keygen
fi

if [ -z "$(ls -A data/grants 2>/dev/null || true)" ]; then
  echo "no grants yet — issuing the demo grant"
  python execution/issue_grant.py issue --hours 168
fi

echo "starting Ambit on ${AMBIT_HOST:-0.0.0.0}:${PORT:-8000}"
exec python -m uvicorn ambit.app:app \
  --host "${AMBIT_HOST:-0.0.0.0}" \
  --port "${PORT:-8000}"
