# JokR — rules for Claude Code

You are building JokR, an AI venture engine owned by Luca.

## Non-negotiables
- Follow the Constitution (C1–C10) in JokR_BLUEPRINT.md. Enforce in code, not only prompts.
- Never add code that moves money, publishes content, or sends email without an approval_id.
- Official APIs only. Never scrape behind logins. Respect robots.txt and rate limits.
- No metric may be reported unless computed from logged data.

## Workflow
- Use ECC: /plan before coding, /tdd for every module, /code-review before merge,
  security-reviewer before any deploy.
- One phase at a time (see §11). Stop and report when acceptance passes.
- Small commits. Tests must pass before commit.

## Style
- Python 3.12, type hints, ruff + mypy clean.
- Simple code over clever code. Comment the why, not the what.
- Dashboard: minimal, easy to navigate, artistic touch.
