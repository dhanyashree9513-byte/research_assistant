"""Planner agent: topic -> 3-5 sub-questions, each answerable by one web search.

Run: uv run python src/planner.py
"""

from llm import ask_structured
from schemas import Plan

SYSTEM = """You plan web research. Split the topic into 3-5 sub-questions.
Rules:
- Each one can be answered by a single web search (concrete, no "and").
- No two overlap; together they cover the topic.
- Number them from 1."""


def plan(topic: str) -> dict:
    r = ask_structured(SYSTEM, topic, Plan)
    n = len(r["parsed"].sub_questions)
    if not 3 <= n <= 5:  # the schema can't enforce a count, so code does
        raise ValueError(f"planner returned {n} sub-questions")
    return r


if __name__ == "__main__":
    for topic in ["How do solid-state batteries compare to lithium-ion for EVs?",
                  "What is retrieval-augmented generation and when does it fail?",
                  "Should a 10-person startup use Kubernetes?"]:
        r = plan(topic)
        print(f"\n{topic}  ({r['input_tokens']}+{r['output_tokens']} tokens)")
        for q in r["parsed"].sub_questions:
            print(f"  {q.id}. {q.question}\n     why: {q.why}")
