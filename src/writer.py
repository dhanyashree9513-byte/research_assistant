"""Writer agent: findings -> report. The LLM writes the summary and headings and picks finding numbers
for each section; code builds the report from the real findings. So the writer can't change a claim,
a quote or a URL.

Run: uv run python src/writer.py
"""

from pydantic import BaseModel

from llm import ask_structured
from schemas import Finding, Report, Section

SYSTEM = """Organise numbered research findings into a report.
- Group them into 2-4 sections with clear headings; list each section's finding numbers.
- Put each finding in at most one section; leave out repeats of the same point.
- The summary answers the topic in 2-3 sentences, using only the findings."""


class DraftSection(BaseModel):
    heading: str
    finding_ids: list[int]


class Draft(BaseModel):
    summary: str
    sections: list[DraftSection]


def write(topic: str, findings: list[Finding]) -> dict:
    prompt = f"Topic: {topic}\n\nFindings:\n" + "\n".join(f"{i}. {f.claim}" for i, f in enumerate(findings))
    r = ask_structured(SYSTEM, prompt, Draft)
    d, seen, bad = r["parsed"], set(), []
    sections = []
    for s in d.sections:
        ids = [i for i in s.finding_ids if 0 <= i < len(findings) and i not in seen]
        bad += [i for i in s.finding_ids if not 0 <= i < len(findings)]
        seen |= set(ids)
        if ids:
            sections.append(Section(heading=s.heading, findings=[findings[i] for i in ids]))
    report = Report(topic=topic, summary=d.summary, sections=sections)
    return r | {"parsed": report, "bad_ids": bad, "left_out": len(findings) - len(seen)}


if __name__ == "__main__":
    findings = [  # fixed input, so the Writer is tested alone
        Finding(sub_question_id=1, claim="Kubernetes gives self-healing, rolling deploys and autoscaling.", source_urls=["https://example.com/k8s-benefits"]),
        Finding(sub_question_id=2, claim="A production cluster needs a large part of one engineer's time.", source_urls=["https://example.com/k8s-small-teams"]),
        Finding(sub_question_id=2, claim="EKS costs about $73 per month per cluster before worker nodes.", source_urls=["https://example.com/eks-pricing"]),
        Finding(sub_question_id=3, claim="Teams under 20 engineers often ship faster on a PaaS or Cloud Run.", source_urls=["https://example.com/paas-vs-k8s"]),
        Finding(sub_question_id=3, claim="Small teams often ship faster on a PaaS than on Kubernetes.", source_urls=["https://example.com/paas-vs-k8s"]),
    ]
    r = write("Should a 10-person startup use Kubernetes?", findings)
    rep = r["parsed"]
    print(f"({r['input_tokens']}+{r['output_tokens']} tokens, bad ids: {r['bad_ids'] or 'none'}, left out: {r['left_out']})\n")
    print(f"SUMMARY: {rep.summary}")
    for s in rep.sections:
        print(f"\n## {s.heading}")
        for f in s.findings:
            print(f"  - {f.claim} {f.source_urls}")
