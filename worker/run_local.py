"""Local dev loop -- calls the same run_cycle() that POST /tick calls,
on a plain sleep loop, no HTTP involved. Production never runs this;
cron-job.org hits the deployed /tick endpoint instead (spec section 3).
"""

from __future__ import annotations

import time

from worker.cycle import run_cycle

INTERVAL_SECONDS = 60


def main() -> None:
    while True:
        print(run_cycle(holder="run_local"))
        time.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
