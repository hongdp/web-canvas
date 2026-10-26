#!/usr/bin/env bash
# restart.sh — restart the web-canvas systemd user services (docs/deployment.md)
set -e
services="${*:-web-canvas-api web-canvas-vite}"
echo "[restart] Restarting: $services"
# shellcheck disable=SC2086
systemctl --user restart $services
sleep 4
# shellcheck disable=SC2086
systemctl --user status $services --no-pager | grep -E "Active|Main PID"
