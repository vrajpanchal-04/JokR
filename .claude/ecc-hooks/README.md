# Vendored ECC hook: block-no-verify

From github.com/affaan-m/ECC at ef648e0 (MIT, see LICENSE). Only the files needed
to run `block-no-verify` through `run-with-flags.js` are copied here. It blocks
`git commit/push --no-verify` and `-c core.hooksPath=` so the pre-commit checks
(gitleaks, ruff, mypy) cannot be skipped. Wired up in `.claude/settings.json`.

All other ECC hooks were audited and left out (data egress, writes to ~/.claude,
reminders, JS/TS-only). See the P1 plan, section 7.
