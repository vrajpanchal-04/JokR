"""The Claude Code hooks in .claude/ block what they claim to block.

These guard the workflow (secret scan, pre-commit bypass), so they get tests
like any other guard. Each test skips when its runtime (node, jq, gitleaks) is
not installed, e.g. inside the slim test image.
"""

import json
import os
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HOOKS = ROOT / ".claude" / "hooks"
ECC = ROOT / ".claude" / "ecc-hooks"

needs_jq = pytest.mark.skipif(shutil.which("jq") is None, reason="jq not installed")
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
needs_gitleaks = pytest.mark.skipif(
    shutil.which("gitleaks") is None, reason="gitleaks not installed"
)


def _run(cmd: list[str], payload: Mapping[str, object]) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(ROOT), "CLAUDE_PLUGIN_ROOT": str(ECC)}
    return subprocess.run(  # noqa: S603 - fixed local scripts, no shell
        cmd, input=json.dumps(payload), capture_output=True, text=True, env=env, timeout=60
    )


def _bash(command: str) -> dict[str, object]:
    return {"tool_name": "Bash", "tool_input": {"command": command}}


def _block_no_verify(command: str) -> subprocess.CompletedProcess[str]:
    script = ECC / "scripts" / "hooks" / "run-with-flags.js"
    args = [
        "pre:bash:block-no-verify",
        "scripts/hooks/block-no-verify.js",
        "minimal,standard,strict",
    ]
    return _run(["node", str(script), *args], _bash(command))


# Bypass commands are assembled from parts so this file's own text never trips
# the Bash guard when a session greps or cats it.
PC = "pre-" + "commit"
NO_VERIFY = "--no-" + "verify"


@needs_node
@pytest.mark.parametrize(
    "command",
    [f"git commit {NO_VERIFY} -m x", f"git push {NO_VERIFY}", "git -c core.hooksPath=/x commit"],
)
def test_block_no_verify_blocks_bypass(command: str) -> None:
    assert _block_no_verify(command).returncode == 2


@needs_node
def test_block_no_verify_allows_normal_git() -> None:
    result = _block_no_verify("git commit -m 'feat: x'")
    assert result.returncode == 0
    assert result.stdout.strip() == ""


@needs_jq
@pytest.mark.parametrize(
    "command",
    [
        "SKIP=gitleaks git commit -m x",
        "cd repo && SKIP=ruff,mypy git commit -m x",
        f"{PC} uninstall",
        f"uv run {PC} uninstall",
        "git config core.hooksPath /tmp/none",
        "git -C . config --local core.hooksPath /tmp/none",
    ],
)
def test_bash_guard_blocks_precommit_bypass(command: str) -> None:
    result = _run([str(HOOKS / "pre_bash_guard.sh")], _bash(command))
    assert result.returncode == 2
    assert "BLOCKED" in result.stderr


@needs_jq
@pytest.mark.parametrize(
    "command",
    [
        "uv run pytest -q",
        f"git commit -m 'guard blocks {PC} uninstall and core.hooksPath changes'",
        f"echo 'never run {PC} uninstall'",
        "grep -n SKIP= .claude/hooks/pre_bash_guard.sh",
    ],
)
def test_bash_guard_allows_commands_that_only_mention_bypasses(command: str) -> None:
    assert _run([str(HOOKS / "pre_bash_guard.sh")], _bash(command)).returncode == 0


@needs_jq
@needs_gitleaks
def test_gitleaks_hook_blocks_secret() -> None:
    # Built at runtime so this file never contains a secret-shaped string.
    token = "ghp_" + "a1B2c3D4e5F6g7H8i9J0" + "k1L2m3N4o5P6q7R8s9T0"
    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": "x.py", "content": f'T = "{token}"'},
    }
    result = _run([str(HOOKS / "pre_write_gitleaks.sh")], payload)
    assert result.returncode == 2
    assert token not in result.stderr  # the report is redacted


@needs_jq
@needs_gitleaks
def test_gitleaks_hook_allows_env_lookup() -> None:
    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": "x.py", "content": 'T = os.environ["TOKEN"]'},
    }
    assert _run([str(HOOKS / "pre_write_gitleaks.sh")], payload).returncode == 0


@needs_jq
def test_ruff_hook_fixes_edited_file() -> None:
    if not (ROOT / ".venv").is_dir():
        pytest.skip("no .venv")
    target = ROOT / "tests" / f"_hook_probe_{os.getpid()}.py"
    try:
        target.write_text("import os\nx=1\n")
        payload = {"tool_name": "Write", "tool_input": {"file_path": str(target)}}
        result = _run([str(HOOKS / "post_edit_ruff.sh")], payload)
        # Unused import removed and formatting applied, so nothing is left to report.
        assert result.returncode == 0, result.stderr
        assert target.read_text() == "x = 1\n"
    finally:
        target.unlink(missing_ok=True)


@needs_jq
def test_ruff_hook_reports_unfixable_issue() -> None:
    if not (ROOT / ".venv").is_dir():
        pytest.skip("no .venv")
    target = ROOT / "tests" / f"_hook_probe_bad_{os.getpid()}.py"
    try:
        target.write_text("def f() -> None:\n    undefined_name\n")
        payload = {"tool_name": "Write", "tool_input": {"file_path": str(target)}}
        result = _run([str(HOOKS / "post_edit_ruff.sh")], payload)
        assert result.returncode == 2
        assert "F821" in result.stderr
    finally:
        target.unlink(missing_ok=True)


@needs_jq
def test_ruff_hook_ignores_files_outside_project(tmp_path: Path) -> None:
    outside = tmp_path / "x.py"
    outside.write_text("import os\n")
    payload = {"tool_name": "Write", "tool_input": {"file_path": str(outside)}}
    assert _run([str(HOOKS / "post_edit_ruff.sh")], payload).returncode == 0
    assert outside.read_text() == "import os\n"
