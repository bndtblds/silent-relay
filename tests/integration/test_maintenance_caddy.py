from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(
    os.environ.get("SILENTRELAY_RUN_DOCKER_INTEGRATION") != "1",
    reason="set SILENTRELAY_RUN_DOCKER_INTEGRATION=1 to run the Docker workflow",
)
def test_caddy_switches_an_arbitrary_url_from_maintenance_to_proxy(tmp_path: Path) -> None:
    if shutil.which("docker") is None:
        pytest.skip("docker is required")

    suffix = uuid4().hex[:12]
    network = f"silentrelay-maintenance-{suffix}"
    backend = f"silentrelay-maintenance-backend-{suffix}"
    gateway = f"silentrelay-maintenance-gateway-{suffix}"
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    marker = runtime / "maintenance.enabled"
    marker.touch()
    image = _caddy_image()

    try:
        _docker("network", "create", network)
        _docker(
            "run", "-d", "--name", backend, "--network", network,
            "--network-alias", "web", image,
            "caddy", "respond", "--listen", ":8000", "--body", "backend-ok",
        )
        _docker(
            "run", "-d", "--name", gateway, "--network", network,
            "-p", "127.0.0.1::8080",
            "-e", "CADDY_DOMAINS=http://localhost:8080",
            "-e", "CADDY_DOMAIN=localhost",
            "--mount", f"type=bind,src={ROOT / 'Caddyfile'},dst=/etc/caddy/Caddyfile,readonly",
            "--mount", f"type=bind,src={ROOT / 'maintenance'},dst=/srv/maintenance,readonly",
            "--mount", f"type=bind,src={runtime},dst=/run/silent-relay,readonly",
            image,
        )
        port = _docker("port", gateway, "8080/tcp").stdout.strip().rsplit(":", 1)[1]
        url = f"http://127.0.0.1:{port}/irgendwas/foo?probe=1"

        status, headers, body = _request_until(url, expected=503)
        assert status == 503
        assert headers["Retry-After"] == "30"
        assert headers["Cache-Control"] == "no-store"
        assert "Wartungsarbeiten" in body

        marker.unlink()
        status, _, body = _request_until(url, expected=200)
        assert status == 200
        assert body == "backend-ok"
    finally:
        subprocess.run(["docker", "rm", "-f", gateway, backend], capture_output=True)
        subprocess.run(["docker", "network", "rm", network], capture_output=True)


def _caddy_image() -> str:
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    for line in compose.splitlines():
        if line.strip().startswith("image: caddy:"):
            return line.split("image:", 1)[1].strip()
    raise AssertionError("Caddy image not found")


def _docker(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *arguments], text=True, capture_output=True, check=True
    )


def _request_until(url: str, *, expected: int) -> tuple[int, object, str]:
    deadline = time.monotonic() + 15
    while True:
        try:
            request = Request(url, headers={"Host": "localhost"})
            with urlopen(request, timeout=2) as response:
                result = response.status, response.headers, response.read().decode()
        except HTTPError as error:
            result = error.code, error.headers, error.read().decode()
        except OSError:
            result = None
        if result is not None and result[0] == expected:
            return result
        if time.monotonic() >= deadline:
            pytest.fail(f"Caddy did not return HTTP {expected}; last result: {result}")
        time.sleep(0.2)
