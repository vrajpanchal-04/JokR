#!/usr/bin/env bash
# PreToolUse(Write|Edit|MultiEdit): scan the text about to be written; block on a hit.
set -uo pipefail
input=$(cat)
text=$(jq -r '[.tool_input.content, .tool_input.new_string, (.tool_input.edits // [] | .[].new_string)] | map(select(. != null)) | join("\n")' <<<"$input")
[[ -n "$text" ]] || exit 0
GL=$(command -v gitleaks || true)
if [[ -z "$GL" ]]; then echo "gitleaks not on PATH; pre-write secret scan skipped (commit-time scan still applies)" >&2; exit 0; fi
if ! report=$("$GL" stdin --no-banner --redact --no-color --log-level error -v <<<"$text" 2>&1); then
  printf 'BLOCKED: gitleaks found a likely secret in content for %s. Read it from settings/env instead.\n%s\n' \
    "$(jq -r '.tool_input.file_path // "?"' <<<"$input")" "$report" >&2
  exit 2
fi
