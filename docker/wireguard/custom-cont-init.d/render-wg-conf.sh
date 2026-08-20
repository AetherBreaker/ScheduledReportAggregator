#!/usr/bin/env bash
set -euo pipefail

TEMPLATE=/config/wg_confs/wg0.conf.template
OUTPUT=/config/wg_confs/wg0.conf

sed \
  -e "s#\${TAXES_JOB_PRIVATE_KEY}#${TAXES_JOB_PRIVATE_KEY:?}#g" \
  -e "s#\${SERVER_PUBLIC_KEY}#${SERVER_PUBLIC_KEY:?}#g" \
  -e "s#\${SERVER_ENDPOINT}#${SERVER_ENDPOINT:?}#g" \
  "$TEMPLATE" > "$OUTPUT"

echo "[render-wg-conf] rendered $OUTPUT from environment"
