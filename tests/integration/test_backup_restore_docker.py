from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.skipif(
    os.environ.get("SILENTRELAY_RUN_DOCKER_INTEGRATION") != "1",
    reason="set SILENTRELAY_RUN_DOCKER_INTEGRATION=1 to run the Docker workflow",
)
def test_real_backup_and_restore_workflow() -> None:
    required = ("age", "age-keygen", "docker", "git", "sh")
    missing = [command for command in required if shutil.which(command) is None]
    if missing:
        pytest.skip(f"missing integration tools: {', '.join(missing)}")

    script = Path(__file__).with_name("test_backup_restore_docker.sh")
    subprocess.run(["sh", str(script)], check=True, cwd=Path(__file__).parents[2])
