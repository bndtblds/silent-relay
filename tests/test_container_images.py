import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DIGEST = r"sha256:[0-9a-f]{64}"


def test_runtime_base_image_is_pinned_by_digest():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert re.search(rf"^FROM python:3\.12-slim@{DIGEST} AS runtime$", dockerfile, re.M)


def test_uv_builder_image_is_pinned_by_digest():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    image = "ghcr.io/astral-sh/uv:0.11.31"
    digest = "sha256:ecd4de2f060c64bea0ff8ecb182ddf46ba3fcccdc8a60cfdbaf20d1a047d7437"

    assert re.search(
        rf"^COPY --from={re.escape(image)}@{DIGEST} /uv /uvx /bin/$",
        dockerfile,
        re.M,
    )
    assert f"COPY --from={image}@{digest} /uv /uvx /bin/" in dockerfile
    assert f"COPY --from={image} /uv /uvx /bin/" not in dockerfile


def test_caddy_image_is_pinned_by_digest():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert re.search(rf"^    image: caddy:2-alpine@{DIGEST}$", compose, re.M)
