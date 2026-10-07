#!/usr/bin/env bash
# Stop: run strict mypy once per turn; on failure hand errors back once (no loop).
set -uo pipefail
input=$(cat)
[[ "$(jq -r '.stop_hook_active // false' <<<"$input")" == "true" ]] && exit 0
cd "$CLAUDE_PROJECT_DIR" || exit 0
# No venv yet (fresh clone): skip rather than block; `uv sync` first.
[[ -d .venv ]] || { echo "no .venv; run uv sync to enable this hook" >&2; exit 0; }
git diff --quiet HEAD -- '*.py' 2>/dev/null && [[ -z "$(git ls-files --others --exclude-standard -- '*.py')" ]] && exit 0
if ! out=$(uv run --no-sync --quiet mypy 2>&1); then
  printf 'mypy --strict failed:\n%s\n' "$(tail -n 40 <<<"$out")" >&2
  exit 2
fi
