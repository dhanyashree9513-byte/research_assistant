"""Where a run's state lives: Redis. LangGraph saves a checkpoint after every node, keyed by run id,
so a killed run can continue from the last finished node.
"""

import json
import os
from functools import cache

from dotenv import load_dotenv
from langgraph.checkpoint.redis import RedisSaver
from redis import Redis

load_dotenv()


REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")


@cache
def saver() -> RedisSaver:
    s = RedisSaver(REDIS_URL)
    s.setup()  # creates the search indexes once; safe to repeat
    return s


def config(run_id: str) -> dict:
    return {"configurable": {"thread_id": run_id}}


@cache
def client() -> Redis:
    return Redis.from_url(REDIS_URL)


CACHE_TTL = 7 * 24 * 3600  # results expire after a week


def remember(run_id: str | None, key: str, work, keep=lambda result: True) -> tuple[dict, bool]:
    """Run work() once per (run, key): its JSON result is saved in Redis, and a later call with the same key
    returns the saved copy instead (True = from cache). keep(result) False = don't save it (e.g. a failed search,
    which should be tried again). No run id (in-memory run) = no caching."""
    if not run_id:
        return work(), False
    r = client()
    name = f"result:{run_id}:{key}"
    if (saved := r.get(name)) is not None:
        return json.loads(saved), True
    result = work()
    if keep(result):
        r.set(name, json.dumps(result), ex=CACHE_TTL)
    return result, False
