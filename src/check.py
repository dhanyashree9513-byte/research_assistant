"""Last gate before a report goes out: every citation in it must be a Finding the researcher really made,
from a page this run really fetched. The writer builds sections from findings, so this should never fail;
it is here so that a future change to the writer can't quietly ship an invented citation.
"""

from schemas import Finding, Report


def check(report: Report, findings: list[Finding], fetched_urls: set[str]) -> dict:
    real = {(f.claim, tuple(f.source_urls), f.snippet) for f in findings}
    cited = [f for s in report.sections for f in s.findings]
    bad = [f for f in cited
           if (f.claim, tuple(f.source_urls), f.snippet) not in real or not set(f.source_urls) <= fetched_urls]
    return {"citations": len(cited), "verified": len(cited) - len(bad),
            "unverified": [{"claim": f.claim, "urls": f.source_urls} for f in bad]}


def remove(report: Report, bad: list[dict]) -> Report:
    """Drop unverified findings (and sections left empty) instead of shipping them."""
    drop = {(b["claim"], tuple(b["urls"])) for b in bad}
    sections = [s.model_copy(update={"findings": [f for f in s.findings if (f.claim, tuple(f.source_urls)) not in drop]})
                for s in report.sections]
    return report.model_copy(update={"sections": [s for s in sections if s.findings]})
