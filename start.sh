#!/bin/bash
# Life HUD launcher: if the HUD is already listening on 8080, exit.
# Otherwise keep it running, restarting it if it ever exits.
cd "$(dirname "$0")"
while true; do
  if curl -fs -m 2 http://127.0.0.1:${LIFE_HUD_PORT:-8080}/ >/dev/null 2>&1; then
    sleep 30
  else
    python3 life-hud.py >> life-hud.log 2>&1
    sleep 5
  fi
done
