from __future__ import annotations

import base64
import hashlib
from pathlib import Path
import re
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _run(script: Path, command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", str(script), command],
        cwd=script.parent,
        text=True,
        capture_output=True,
        check=False,
    )


@pytest.mark.skipif(shutil.which("sh") is None, reason="maintenance script requires sh")
def test_maintenance_commands_are_idempotent_and_scriptable(tmp_path: Path) -> None:
    script = tmp_path / "maintenance.sh"
    shutil.copy(ROOT / "maintenance.sh", script)
    marker = tmp_path / ".runtime" / "maintenance.enabled"

    inactive = _run(script, "status")
    assert inactive.returncode == 1
    assert "inactive" in inactive.stdout

    assert _run(script, "off").returncode == 0
    assert _run(script, "on").returncode == 0
    assert _run(script, "on").returncode == 0
    assert marker.is_file()
    assert list(marker.parent.iterdir()) == [marker]

    active = _run(script, "status")
    assert active.returncode == 0
    assert "active" in active.stdout

    assert _run(script, "off").returncode == 0
    assert _run(script, "off").returncode == 0
    assert not marker.exists()
    assert _run(script, "unknown").returncode == 2


def test_caddy_checks_the_marker_and_serves_every_path_as_503() -> None:
    caddyfile = (ROOT / "Caddyfile").read_text(encoding="utf-8")

    assert "root /run/silent-relay" in caddyfile
    assert "try_files maintenance.enabled" in caddyfile
    assert "rewrite * /maintenance.html" in caddyfile
    assert "status 503" in caddyfile
    assert 'Retry-After "30"' in caddyfile
    assert 'Cache-Control "no-store"' in caddyfile
    assert caddyfile.index("handle @maintenance") < caddyfile.index(
        "reverse_proxy web:8000"
    )


def test_maintenance_page_navigation_and_csp_hashes_are_current() -> None:
    page = (ROOT / "maintenance" / "maintenance.html").read_text(encoding="utf-8")
    caddyfile = (ROOT / "Caddyfile").read_text(encoding="utf-8")

    assert "Neuer Versuch in 30 Sekunden" in page
    assert "Jetzt erneut versuchen" in page
    assert "location.reload" not in page
    assert "location.assign(location.pathname + location.search + location.hash)" in page

    for tag in ("style", "script"):
        content = re.search(rf"<{tag}>(?P<body>.*?)</{tag}>", page, re.S)
        assert content is not None
        digest = base64.b64encode(
            hashlib.sha256(content.group("body").encode()).digest()
        ).decode()
        assert f"'sha256-{digest}'" in caddyfile

    attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "maintenance/maintenance.html text eol=lf" in attributes
