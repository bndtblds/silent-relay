from __future__ import annotations

import time

from app.config import get_settings
from app.logging_config import configure_logging
from app.scheduler.jobs import run_jobs


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    while True:
        run_jobs(settings)
        time.sleep(settings.scheduler_interval_seconds)


if __name__ == "__main__":
    main()
