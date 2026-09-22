#!/bin/sh
set -e
# bind-mounted workspace may arrive owned by the host user
chown agent:agent /workspace 2>/dev/null || true
mkdir -p /workspace/skills && chown agent:agent /workspace/skills 2>/dev/null || true
chmod 700 /runner
exec "$@"
