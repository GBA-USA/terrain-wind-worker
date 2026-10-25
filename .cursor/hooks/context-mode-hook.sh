#!/usr/bin/env bash
# Fail-open wrapper for context-mode Cursor hooks (github.com/mksglu/context-mode).
# If Node < 22.5, npx/context-mode is unavailable, the hook errors, or it times out,
# we emit a valid no-op payload and exit 0 so the agent's tools are never blocked.
EVENT="${1:-}"
PKG="context-mode@1.0.169"

noop() {
  if [ "$EVENT" = "pretooluse" ]; then
    printf '{"agent_message":""}\n'
  else
    printf '{}\n'
  fi
  exit 0
}

[ -n "$EVENT" ] || noop
command -v node >/dev/null 2>&1 || noop
command -v npx >/dev/null 2>&1 || noop

# context-mode requires Node >= 22.5
node -e 'const [a,b]=process.versions.node.split(".").map(Number);process.exit(a>22||(a===22&&b>=5)?0:1)' 2>/dev/null || noop

INPUT="$(cat)"
if command -v timeout >/dev/null 2>&1; then
  OUT="$(printf '%s' "$INPUT" | timeout 60 npx -y "$PKG" hook cursor "$EVENT" 2>/dev/null)" || noop
else
  OUT="$(printf '%s' "$INPUT" | npx -y "$PKG" hook cursor "$EVENT" 2>/dev/null)" || noop
fi

[ -n "$OUT" ] || noop
printf '%s\n' "$OUT"
exit 0
