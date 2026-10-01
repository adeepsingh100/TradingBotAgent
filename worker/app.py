"""FastAPI app -- `POST /tick` runs exactly one full agent cycle and
returns a JSON summary; no long-running loop in production (cron-
job.org calls this on a schedule). See run_local.py for the local-dev
equivalent that loops in-process instead of over HTTP.
"""

from __future__ import annotations

from fastapi import FastAPI, Header, HTTPException

from core.config import settings
from worker.cycle import run_cycle

app = FastAPI(title="Survivor worker")


@app.post("/tick")
def tick(x_tick_token: str = Header(default="")):
    if x_tick_token != settings.tick_token:
        raise HTTPException(status_code=401, detail="invalid tick token")
    return run_cycle(holder="render")


@app.get("/health")
def health():
    return {"status": "ok"}
