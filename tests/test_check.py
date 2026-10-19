"""The citation gate catches a citation that isn't a real finding or whose page was never fetched."""

from check import check, remove
from schemas import Finding, Report, Section

REAL = Finding(sub_question_id=1, claim="EKS costs $73/month.", source_urls=["https://a.com/eks"], snippet="$0.10 per hour")


def report(*findings):
    return Report(topic="t", summary="s", sections=[Section(heading="h", findings=list(findings))])


def test_real_citations_pass():
    assert check(report(REAL), [REAL], {"https://a.com/eks"}) == {"citations": 1, "verified": 1, "unverified": []}


def test_invented_url_is_caught_and_removed():
    fake = REAL.model_copy(update={"source_urls": ["https://made-up.com/x"]})
    r = check(report(REAL, fake), [REAL], {"https://a.com/eks"})
    assert r["verified"] == 1 and r["unverified"] == [{"claim": REAL.claim, "urls": ["https://made-up.com/x"]}]
    assert [f.source_urls for f in remove(report(REAL, fake), r["unverified"]).sections[0].findings] == [["https://a.com/eks"]]


def test_reworded_claim_is_caught():
    changed = REAL.model_copy(update={"claim": "EKS is free."})
    assert check(report(changed), [REAL], {"https://a.com/eks"})["verified"] == 0


def test_page_never_fetched_is_caught():
    assert check(report(REAL), [REAL], set())["verified"] == 0
