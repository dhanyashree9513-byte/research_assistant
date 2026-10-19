"""Supervisor agent: reads the findings per sub-question and names the ones that need another search.
It only judges; the pipeline decides whether there is budget and a round left to act on it.
"""

from pydantic import BaseModel

from llm import ask_structured
from schemas import Finding, SubQuestion

SYSTEM = """Review research findings. For each sub-question decide if its findings answer it.
enough=false when there are no findings, or they don't address the question.
When enough=false, give retry_query: a different, more specific web search. Otherwise retry_query is ""."""


class Verdict(BaseModel):
    sub_question_id: int
    enough: bool
    retry_query: str


class Review(BaseModel):
    verdicts: list[Verdict]


def review(sub_questions: list[SubQuestion], findings: list[Finding], failures: dict[int, str] = {}) -> dict:
    blocks = []
    for sq in sub_questions:
        claims = [f"  - {f.claim}" for f in findings if f.sub_question_id == sq.id] or [f"  (no findings{': search ' + failures[sq.id] if sq.id in failures else ''})"]
        blocks.append(f"{sq.id}. {sq.question}\n" + "\n".join(claims))
    r = ask_structured(SYSTEM, "\n\n".join(blocks), Review)
    redo = [SubQuestion(id=v.sub_question_id, question=v.retry_query, why="retry")
            for v in r["parsed"].verdicts if not v.enough and v.retry_query]
    return {"redo": redo, "input_tokens": r["input_tokens"], "output_tokens": r["output_tokens"]}
