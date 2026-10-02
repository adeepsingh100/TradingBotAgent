"""FastAPI app -- `POST /tick` runs exactly one full agent cycle and
returns a JSON summary (cron-job.org calls it every minute). The only
in-process loop is the exit guard (worker/exit_guard.py), started at
boot: a mechanical stop/target check every few seconds between ticks.
See run_local.py for the local-dev equivalent that loops in-process
instead of over HTTP.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException

from core.config import settings
from worker.cycle import run_cycle
from worker.exit_guard import start_exit_guard


@asynccontextmanager
async def _lifespan(app: FastAPI):
    start_exit_guard(settings.exit_guard_seconds)  # daemon thread, dies with the process
    yield


app = FastAPI(title="Survivor worker", lifespan=_lifespan)


@app.post("/tick")
def tick(x_tick_token: str = Header(default="")):
    if x_tick_token != settings.tick_token:
        raise HTTPException(status_code=401, detail="invalid tick token")
    return run_cycle(holder="render")


@app.get("/health")
def health():
    return {"status": "ok"}
