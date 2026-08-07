import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DIGEST = r"sha256:[0-9a-f]{64}"


def test_runtime_base_image_is_pinned_by_digest():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert re.search(rf"^FROM python:3\.12-slim@{DIGEST} AS runtime$", dockerfile, re.M)


def test_caddy_image_is_pinned_by_digest():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert re.search(rf"^    image: caddy:2-alpine@{DIGEST}$", compose, re.M)
