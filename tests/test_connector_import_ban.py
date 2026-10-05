"""C3 structurally: connector code cannot open its own network, process or XML parser.

Connectors only get the GuardedClient (and defusedxml) that Scout hands them.
The ban is a ruff TID251 rule scoped to jokr/connectors, so `ruff check` in
pre-commit and CI fails the build.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RUFF = shutil.which("ruff", path=str(ROOT / ".venv" / "bin"))

pytestmark = pytest.mark.skipif(RUFF is None, reason="ruff not installed in .venv")


def _ruff(code: str, filename: str) -> subprocess.CompletedProcess[str]:
    assert RUFF is not None
    return subprocess.run(  # noqa: S603 - fixed binary, code passed on stdin
        [RUFF, "check", "--no-cache", "--select", "TID251", "--stdin-filename", filename, "-"],
        input=code,
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
    )


@pytest.mark.parametrize(
    "code",
    [
        "import httpx\n",
        "from httpx import AsyncClient\n",
        "import urllib.request\n",
        "from urllib.parse import urlencode\n",
        "import http.client\n",
        "import socket\n",
        "import requests\n",
        "import aiohttp\n",
        "import subprocess\n",
        "import xml.etree.ElementTree as ET\n",
        "from xml.dom import minidom\n",
        "from xml.sax import parse\n",
        "import ssl\n",
        "from asyncio import open_connection\n",
        "import asyncio\nasyncio.create_subprocess_exec('x')\n",
        "import os\nos.system('x')\n",
    ],
)
def test_banned_imports_fail_in_connectors(code: str) -> None:
    result = _ruff(code, "jokr/connectors/probe.py")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "TID251" in result.stdout


@pytest.mark.parametrize(
    "code",
    [
        "from defusedxml.ElementTree import iterparse\n",
        "from jokr.guards.tos import GuardedClient\n",
        "import asyncio\n",
        "import json\n",
    ],
)
def test_allowed_imports_pass_in_connectors(code: str) -> None:
    result = _ruff(code, "jokr/connectors/probe.py")
    assert result.returncode == 0, result.stdout + result.stderr


def test_ban_does_not_apply_to_the_guard_itself() -> None:
    result = _ruff("import httpx\nimport socket\n", "jokr/guards/probe.py")
    assert result.returncode == 0, result.stdout + result.stderr
