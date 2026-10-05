#!/usr/bin/env bash
# PostToolUse(Edit|Write|MultiEdit): autofix + format the edited .py file with the
# project-pinned ruff (uv.lock), then feed any remaining lint errors back (exit 2).
set -uo pipefail
file=$(jq -r '.tool_input.file_path // empty')
[[ "$file" == *.py && -f "$file" ]] || exit 0
case "$(realpath "$file")" in "$(realpath "$CLAUDE_PROJECT_DIR")"/*) ;; *) exit 0 ;; esac
cd "$CLAUDE_PROJECT_DIR" || exit 0
# No venv yet (fresh clone): skip rather than block; `uv sync` first.
[[ -d .venv ]] || { echo "no .venv; run uv sync to enable this hook" >&2; exit 0; }
# Fix first, then format, so removed imports do not leave stray blank lines.
out=$(uv run --no-sync --quiet ruff check --fix --force-exclude --quiet -- "$file" 2>&1); status=$?
uv run --no-sync --quiet ruff format --force-exclude --quiet -- "$file" >/dev/null 2>&1
if [[ $status -ne 0 ]]; then
  printf 'ruff: unresolved issues in %s\n%s\n' "$file" "$out" >&2
  exit 2
fi
