#!/usr/bin/env bash
# PreToolUse(Bash): close the pre-commit bypasses ECC's block-no-verify does not cover.
# Patterns are anchored to command position (start, or after ; & | && || or a
# subshell paren) so a commit message or echo that merely mentions them is allowed.
set -uo pipefail
cmd=$(jq -r '.tool_input.command // empty')
start='(^|[;&|(]|&&|\|\|)[[:space:]]*'
if grep -Eq "${start}SKIP=[^[:space:]]+[[:space:]]+([A-Za-z_]+=[^[:space:]]*[[:space:]]+)*(uv run[[:space:]]+)?git[[:space:]]" <<<"$cmd" \
  || grep -Eq "${start}(uv run[[:space:]]+|python3? -m[[:space:]]+)?pre-commit[[:space:]]+uninstall" <<<"$cmd" \
  || grep -Eiq "${start}git([[:space:]]+-C[[:space:]]+[^[:space:]]+)?[[:space:]]+config[[:space:]]+([^;&|]*[[:space:]])?core\.hookspath" <<<"$cmd"; then
  echo "BLOCKED: this would bypass or disable the repo's pre-commit hooks (gitleaks/ruff/mypy)." >&2
  exit 2
fi
