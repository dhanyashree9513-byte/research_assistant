"""search() filters and failure statuses, without calling Tavily. Run: uv run pytest -q"""

import search
from tavily.errors import TimeoutError as TavilyTimeout, UsageLimitExceededError

LONG = "x" * 1000


def page(url, text=LONG):
    return {"url": url, "title": "", "content": "", "page": text, "retrieved_at": ""}


def fake_fetch(monkeypatch, result):
    calls = []
    def fetch(query, n):
        calls.append(query)
        if isinstance(result, Exception):
            raise result
        return result
    monkeypatch.setattr(search, "_fetch", fetch)
    monkeypatch.setattr(search.time, "sleep", lambda s: None)
    return calls


def test_filters_duplicates_domain_cap_and_paywalls(monkeypatch):
    fake_fetch(monkeypatch, [page("https://a.com/1"),        # already used this run
                             page("https://b.com/1"), page("https://www.b.com/2"), page("https://b.com/3"),  # 3rd from b.com
                             page("https://c.com/x", "Subscribe to read")])  # too short = paywall
    r = search.search("q", seen_urls={"https://a.com/1"})
    assert r["status"] == "ok"
    assert [p["url"] for p in r["pages"]] == ["https://b.com/1", "https://www.b.com/2"]
    assert r["skipped"] == {"duplicate": 1, "domain_cap": 1, "paywalled": 1}


def test_domain_cap_counts_pages_from_earlier_sub_questions(monkeypatch):
    fake_fetch(monkeypatch, [page("https://b.com/3")])
    r = search.search("q", seen_urls={"https://b.com/1", "https://b.com/2"})
    assert r["status"] == "no_results" and r["skipped"] == {"domain_cap": 1}


def test_timeout_is_retried_once_then_named(monkeypatch):
    calls = fake_fetch(monkeypatch, TavilyTimeout(30))
    r = search.search("q")
    assert (r["status"], r["pages"], len(calls)) == ("timeout", [], 2)


def test_rate_limit_is_named(monkeypatch):
    fake_fetch(monkeypatch, UsageLimitExceededError("429"))
    assert search.search("q")["status"] == "rate_limited"


def test_other_errors_never_raise(monkeypatch):
    fake_fetch(monkeypatch, ConnectionError("dns"))
    r = search.search("q")
    assert r["status"] == "error" and "dns" in r["detail"]
