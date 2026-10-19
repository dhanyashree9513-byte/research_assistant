"""The web API. A run takes about a minute, so POST returns a run id at once and the work happens in a
background thread. Every event goes into a Redis stream, so any number of clients can watch, join late,
or reconnect, and see the whole history.

  POST /research                {"topic": "..."}  -> {"run_id": "..."}
  GET  /research/{id}           status + report
  GET  /research/{id}/events    live progress (Server-Sent Events)
  GET  /research/{id}/trace     every step with timings, budgets used, citation check
  POST /research/{id}/resume    continue a run that died

Run: uv run uvicorn api:app --app-dir src --port 8001
"""

import json
import threading
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from pipeline import events, final_state
from store import client


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Runs live in this server's threads, so at startup none can still be running: any run marked
    # "running" died with the previous server. Mark it "interrupted" so it can be resumed. (One API server.)
    r = client()
    for key in r.scan_iter("run:*"):
        if r.hget(key, "status") in (b"running", b"queued"):
            r.hset(key, "status", "interrupted")
    yield


app = FastAPI(title="Research Assistant", lifespan=lifespan)


class Topic(BaseModel):
    topic: str = Field(min_length=5, max_length=300)


def meta_key(run_id: str) -> str:
    return f"run:{run_id}"


def stream_key(run_id: str) -> str:
    return f"events:{run_id}"


def execute(run_id: str, topic: str | None) -> None:
    """Runs in a background thread: pipeline events -> Redis stream, status kept in a Redis hash."""
    r = client()
    r.hset(meta_key(run_id), "status", "running")
    r.xadd(stream_key(run_id), {"data": json.dumps({"type": "start", "resume": topic is None})})  # t = 0 for /trace
    try:
        for e in events(run_id, topic):
            r.xadd(stream_key(run_id), {"data": json.dumps(e)})
        r.hset(meta_key(run_id), "status", "done")
        r.xadd(stream_key(run_id), {"data": json.dumps({"type": "done"})})
    except Exception as e:  # a failed run is reported, never hidden; it can be resumed
        r.hset(meta_key(run_id), mapping={"status": "failed", "error": repr(e)[:300]})
        r.xadd(stream_key(run_id), {"data": json.dumps({"type": "failed", "error": repr(e)[:300]})})


def start_thread(run_id: str, topic: str | None) -> None:
    threading.Thread(target=execute, args=(run_id, topic), daemon=True).start()


@app.post("/research", status_code=202)
def create(body: Topic) -> dict:
    run_id = uuid.uuid4().hex[:12]
    client().hset(meta_key(run_id), mapping={"topic": body.topic, "status": "queued", "created": time.time()})
    start_thread(run_id, body.topic)
    return {"run_id": run_id, "events": f"/research/{run_id}/events"}


def meta(run_id: str) -> dict:
    m = client().hgetall(meta_key(run_id))
    if not m:
        raise HTTPException(404, "unknown run id")
    return {k.decode(): v.decode() for k, v in m.items()}


@app.get("/research/{run_id}")
def status(run_id: str) -> dict:
    m = meta(run_id)
    state = final_state(run_id)
    return {"run_id": run_id, "topic": m["topic"], "status": m["status"], "error": m.get("error"),
            "tokens": state.get("tokens", 0), "searches": state.get("searches", 0),
            "stopped": state.get("stopped") or None, "outcome": state.get("outcome"),
            "citations": state.get("citations"), "report": state.get("report")}


@app.get("/research/{run_id}/trace")
def trace(run_id: str) -> dict:
    """Why does the report say what it says? Every event with its time, plus what each budget used."""
    m, state = meta(run_id), final_state(run_id)
    raw = client().xrange(stream_key(run_id))
    t0 = int(raw[0][0].split(b"-")[0]) if raw else 0
    timeline = [{"t_s": round((int(eid.split(b"-")[0]) - t0) / 1000, 1)} | json.loads(f[b"data"]) for eid, f in raw]
    budget = state.get("budget", {})
    return {"run_id": run_id, "topic": m["topic"], "status": m["status"], "outcome": state.get("outcome"),
            "seconds": timeline[-1]["t_s"] if timeline else None,
            "used": {"tokens": [state.get("tokens", 0), budget.get("tokens")], "searches": [state.get("searches", 0), budget.get("searches")],
                     "redo_rounds": [state.get("rounds", 0), budget.get("rounds")]},
            "sub_questions": state.get("sub_questions", []), "failures": state.get("failures", []),
            "citations": state.get("citations"), "timeline": timeline}


@app.post("/research/{run_id}/resume", status_code=202)
def resume(run_id: str) -> dict:
    if meta(run_id)["status"] == "running":
        raise HTTPException(409, "run is still running")
    start_thread(run_id, None)
    return {"run_id": run_id, "events": f"/research/{run_id}/events"}


@app.get("/research/{run_id}/events")
def watch(run_id: str, last_event_id: str | None = Header(None)) -> StreamingResponse:
    meta(run_id)  # 404 before opening a stream

    def sse():
        # A reconnecting browser sends the id of the last event it got (Last-Event-ID): continue after it.
        # A new client sends none: "0" = replay from the start, so late joiners see everything.
        r, last = client(), last_event_id or "0"
        while True:
            batch = r.xread({stream_key(run_id): last}, block=15_000, count=100)
            if not batch:
                yield ": keep-alive\n\n"  # comment line stops proxies closing an idle connection
                continue
            for event_id, fields in batch[0][1]:
                last = event_id
                e = json.loads(fields[b"data"])
                yield f"id: {event_id.decode()}\nevent: {e['type']}\ndata: {json.dumps(e)}\n\n"
                if e["type"] in ("done", "failed"):
                    return

    return StreamingResponse(sse(), media_type="text/event-stream")
