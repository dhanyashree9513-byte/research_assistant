"""Researcher agent: one sub-question -> findings. Each finding = claim + URL + exact quote + fetch time.

Run: uv run python src/researcher.py
"""

import re

from pydantic import BaseModel

from llm import ask_structured
from schemas import Finding, SubQuestion
from search import search

PAGE_CHARS = 3000  # pages can be 300k chars; the LLM sees the relevant part + the start of the page

SYSTEM = """Answer the sub-question using ONLY the pages given.
Write short factual claims. For each claim give the URL of the page it comes from and a quote:
the exact words copied from that page that support it (one sentence, no changes).
Say only what the page says; don't add your own conclusions. Ignore off-topic pages.
If no page helps, return no findings."""


class Draft(BaseModel):
    claim: str
    source_url: str
    quote: str


class Notes(BaseModel):
    findings: list[Draft]


def norm(text: str) -> str:
    return re.sub(r"\W+", " ", text).lower().strip()


def research(sq: SubQuestion, seen_urls: set[str] = frozenset()) -> dict:
    found = search(sq.question, seen_urls)
    pages = found["pages"]
    base = {"status": found["status"], "detail": found.get("detail", ""), "skipped": found["skipped"], "urls": [p["url"] for p in pages]}
    if not pages:  # failed or empty search: no LLM call, nothing spent
        return base | {"findings": [], "dropped": [], "pages": 0, "input_tokens": 0, "output_tokens": 0}
    prompt = f"Sub-question: {sq.question}\n\n" + "\n\n".join(
        f"URL: {p['url']}\n{p['title']}\n{p['content']}\n{p['page'][:PAGE_CHARS]}" for p in pages)
    r = ask_structured(SYSTEM, prompt, Notes)
    by_url = {p["url"]: p for p in pages}
    findings, dropped = [], []
    for d in r["parsed"].findings:
        page = by_url.get(d.source_url)
        # keep a claim only if its page was in the results AND its quote really is on that page
        if page and d.quote and norm(d.quote) in norm(page["content"] + " " + page["page"]):
            findings.append(Finding(sub_question_id=sq.id, claim=d.claim, source_urls=[d.source_url],
                                    snippet=d.quote, retrieved_at=page["retrieved_at"]))
        else:
            dropped.append(d)
    return base | {"findings": findings, "dropped": dropped, "pages": len(pages),
            "input_tokens": r["input_tokens"], "output_tokens": r["output_tokens"]}


if __name__ == "__main__":
    sq = SubQuestion(id=2, question="What are the challenges of implementing Kubernetes in a small startup?", why="")
    r = research(sq)
    print(f"{sq.question}\n{r['pages']} pages, {r['input_tokens']}+{r['output_tokens']} tokens, "
          f"kept {len(r['findings'])}, dropped {len(r['dropped'])}\n")
    for f in r["findings"]:
        print(f"- {f.claim}\n  {f.source_urls[0]}  @ {f.retrieved_at}\n  quote: \"{f.snippet}\"")
    for d in r["dropped"]:
        print(f"DROPPED: {d.claim}\n  {d.source_url}\n  quote: \"{d.quote}\"")
