#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -f "hub.config.json" ]; then
  cp hub.config.example.json hub.config.json
fi

python3 server.py
