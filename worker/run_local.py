"""Local dev loop -- calls the same run_cycle() that POST /tick calls,
on a plain sleep loop, no HTTP involved. Production never runs this;
cron-job.org hits the deployed /tick endpoint instead (spec section 3).
"""

from __future__ import annotations

import time

from core.config import settings
from worker.cycle import run_cycle
from worker.exit_guard import start_exit_guard

INTERVAL_SECONDS = 60


def main() -> None:
    start_exit_guard(settings.exit_guard_seconds)
    while True:
        print(run_cycle(holder="run_local"))
        time.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
