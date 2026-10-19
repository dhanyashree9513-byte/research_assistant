"""The research pipeline as a LangGraph graph: one shared state, one node per agent.

  planner -> researcher -> supervisor -> writer -> check
                 ^             |
                 +-- redo, once +

Budgets are checked in code before every step; when one runs out, the run skips to the writer
and reports what it has, with `stopped` saying why.
Run: uv run python src/pipeline.py start <run_id> "topic"   (state saved to Redis after every node)
     uv run python src/pipeline.py resume <run_id>         (continue a killed run)
"""

import hashlib
import operator
import sys
import time
from typing import Annotated, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph

from check import check, remove
from planner import plan
from researcher import research
from schemas import Finding, Report, SubQuestion
from store import config as run_config, remember, saver
from supervisor import review
from writer import write

def models(cls, items) -> list:
    """The state holds plain dicts (so Redis stores plain JSON); turn them back into Pydantic objects to use them."""
    return [cls.model_validate(x) for x in items]


def dicts(items) -> list[dict]:
    return [x.model_dump() for x in items]


BUDGET = {"sub_questions": 5, "searches": 10, "tokens": 40_000, "seconds": 240, "rounds": 1,  # ~6k tokens per live search
          "writer_reserve": 3_000}  # tokens kept back so a stopped run can still write its report


class State(TypedDict, total=False):
    topic: str
    budget: dict
    started: float
    sub_questions: list[dict]  # SubQuestion as dict
    todo: list[dict]  # SubQuestions the researcher should search next
    findings: Annotated[list[dict], operator.add]  # Finding as dict
    rounds: int  # how many times the supervisor has sent work back
    report: dict | None  # Report as dict
    citations: dict  # result of the citation check
    outcome: str  # one line: why the run ended the way it did
    stopped: str  # "" = finished normally, else which budget ran out
    searches: Annotated[int, operator.add]
    seen_urls: Annotated[list[str], operator.add]  # pages already used; never read twice
    failures: Annotated[list[dict], operator.add]  # {"q": id, "status": "timeout"} etc.
    tokens: Annotated[int, operator.add]
    trace: Annotated[list[str], operator.add]


def progress(msg: str) -> None:
    """Send a progress line to whoever is streaming the run (CLI now, SSE in P2.9); ignored otherwise."""
    try:
        get_stream_writer()({"progress": msg})
    except RuntimeError:  # not running inside a graph
        pass


def over_budget(state: State, tokens: int = 0, searches: int = 0) -> str:
    """Name the first budget that is used up, or "" if all are fine. Extra counts = spent inside the current node."""
    b = state["budget"]
    if state["tokens"] + tokens + b["writer_reserve"] >= b["tokens"]:
        return "tokens"
    if state["searches"] + searches >= b["searches"]:
        return "searches"
    if time.time() - state["started"] >= b["seconds"]:
        return "time"
    return ""


def planner_node(state: State) -> dict:
    r = plan(state["topic"])
    qs = r["parsed"].sub_questions[: state["budget"]["sub_questions"]]
    return {"sub_questions": dicts(qs), "todo": dicts(qs), "tokens": r["input_tokens"] + r["output_tokens"],
            "trace": [f"planner: {len(qs)} sub-questions"]}


def run_id_of(config: RunnableConfig) -> str | None:
    return config.get("configurable", {}).get("thread_id")


def research_saved(run_id: str | None, sq: SubQuestion, seen: set[str]) -> tuple[dict, bool]:
    """research() made safe to repeat: the result for this exact sub-question is saved, so a resumed run reuses it."""
    key = f"research:{sq.id}:{hashlib.sha1(sq.question.encode()).hexdigest()[:12]}"
    def work():
        r = research(sq, seen)
        return r | {"findings": dicts(r["findings"]), "dropped": len(r["dropped"])}
    return remember(run_id, key, work, keep=lambda r: r["status"] == "ok")  # failures are retried, not remembered


def researcher_node(state: State, config: RunnableConfig) -> dict:
    findings, tokens, searches, lines, stopped, urls, failures = [], 0, 0, [], "", [], []
    for sq in models(SubQuestion, state["todo"]):
        if stopped := over_budget(state, tokens, searches):  # check before each search, not only between nodes
            lines.append(f"researcher: stopped before q{sq.id} ({stopped} budget)")
            break
        r, cached = research_saved(run_id_of(config), sq, set(state["seen_urls"]) | set(urls))
        findings += r["findings"]
        urls += r["urls"]
        if r["status"] != "ok":
            failures.append({"q": sq.id, "status": r["status"]})
        tokens += r["input_tokens"] + r["output_tokens"]  # counted again when cached: the budget is about the run's work
        searches += 1
        progress(f"researcher: q{sq.id} " + ("from cache" if cached else "NEW LLM call" if r["pages"] else f"no LLM call ({r['status']})"))
        lines.append(f"researcher: q{sq.id} [{r['status']}] {r['pages']} pages (skipped {r['skipped'] or 0}) -> "
                     f"{len(r['findings'])} findings, {r['dropped']} dropped, total {state['tokens'] + tokens} tokens")
    return {"findings": findings, "tokens": tokens, "searches": searches, "trace": lines, "stopped": stopped,
            "seen_urls": urls, "failures": failures}


def supervisor_node(state: State) -> dict:
    failed = {f["q"]: f["status"] for f in state["failures"]}
    r = review(models(SubQuestion, state["sub_questions"]), models(Finding, state["findings"]), failed)
    ids = [sq.id for sq in r["redo"]]
    return {"todo": dicts(r["redo"]), "tokens": r["input_tokens"] + r["output_tokens"],
            "trace": [f"supervisor: redo {ids or 'nothing'}"]}


def writer_node(state: State) -> dict:
    if not state["findings"]:
        return {"report": None, "trace": ["writer: no findings, no report"]}
    r = write(state["topic"], models(Finding, state["findings"]))
    return {"report": r["parsed"].model_dump(), "tokens": r["input_tokens"] + r["output_tokens"],
            "trace": [f"writer: {len(r['parsed'].sections)} sections, left out {r['left_out']} repeats, bad ids {len(r['bad_ids'])}"]}


def check_node(state: State) -> dict:
    if not state.get("report"):
        why = f"no report: {state['stopped']} budget ran out" if state.get("stopped") else "no report: no findings could be verified"
        return {"citations": {"citations": 0, "verified": 0, "unverified": []}, "outcome": why, "trace": [f"check: {why}"]}
    report = Report.model_validate(state["report"])
    c = check(report, models(Finding, state["findings"]), set(state["seen_urls"]))
    if c["unverified"]:
        report = remove(report, c["unverified"])
    why = f"report with {c['verified']} verified citations" + (f", stopped early ({state['stopped']} budget)" if state.get("stopped") else "")
    return {"report": report.model_dump(), "citations": c, "outcome": why,
            "trace": [f"check: {c['verified']}/{c['citations']} citations verified, {len(c['unverified'])} removed"]}


def mark_stopped(state: State) -> dict:
    return {"stopped": state.get("stopped") or over_budget(state)}


def after_research(state: State) -> str:
    return "writer" if state.get("stopped") or over_budget(state) else "supervisor"


def after_review(state: State) -> str:
    if state["todo"] and state["rounds"] < state["budget"]["rounds"] and not over_budget(state):
        return "redo"
    return "writer"


builder = StateGraph(State)
builder.add_node("planner", planner_node)
builder.add_node("researcher", researcher_node)
builder.add_node("supervisor", supervisor_node)
builder.add_node("redo", lambda s: {"rounds": s["rounds"] + 1})
builder.add_node("stop_check", mark_stopped)
builder.add_node("writer", writer_node)
builder.add_edge(START, "planner")
builder.add_conditional_edges("planner", lambda s: "stop_check" if over_budget(s) else "researcher")
builder.add_conditional_edges("researcher", lambda s: "stop_check" if after_research(s) == "writer" else "supervisor")
builder.add_conditional_edges("supervisor", lambda s: after_review(s) if after_review(s) == "redo" else "stop_check")
builder.add_edge("redo", "researcher")
builder.add_edge("stop_check", "writer")
builder.add_node("check", check_node)
builder.add_edge("writer", "check")
builder.add_edge("check", END)
graph = builder.compile()  # in memory, nothing saved


def saved_graph():
    return builder.compile(checkpointer=saver())  # connects to Redis only when called


def initial(topic: str, **budget) -> State:
    return {"topic": topic, "budget": BUDGET | budget, "started": time.time(), "rounds": 0, "stopped": "",
            "tokens": 0, "searches": 0, "findings": [], "trace": [], "seen_urls": [], "failures": []}


def run(topic: str, **budget) -> dict:
    return graph.invoke(initial(topic, **budget))


def events(run_id: str, topic: str | None = None):
    """Start run_id (topic given) or resume it (topic None) from its last saved node.
    Yields one event per progress line and per finished node; the CLI prints them, the API streams them."""
    g, cfg = saved_graph(), run_config(run_id)
    if topic is None:
        yield {"type": "resume", "next": list(g.get_state(cfg).next)}
        if not g.get_state(cfg).next:
            return
    for mode, chunk in g.stream(initial(topic) if topic else None, cfg, stream_mode=["updates", "custom"]):
        if mode == "custom":
            yield {"type": "progress", "msg": chunk["progress"]}
            continue
        for node, out in chunk.items():
            for line in (out or {}).get("trace", []) or [f"{node}: done"]:
                yield {"type": "node", "node": node, "msg": line}


def final_state(run_id: str) -> dict:
    return saved_graph().get_state(run_config(run_id)).values


if __name__ == "__main__":
    # uv run python src/pipeline.py start <run_id> "topic"    |    uv run python src/pipeline.py resume <run_id>
    cmd, run_id = sys.argv[1], sys.argv[2]
    for e in events(run_id, sys.argv[3] if cmd == "start" else None):
        print(f"[{time.strftime('%X')}] {e}", flush=True)
    final = final_state(run_id)
    print(f"\n{final['tokens']} tokens, {final['searches']} searches, stopped: {final['stopped'] or 'no'}")
    if rep := final.get("report"):
        rep = Report.model_validate(rep)
        print(f"SUMMARY: {rep.summary}\nsections: {[(s.heading, len(s.findings)) for s in rep.sections]}")
