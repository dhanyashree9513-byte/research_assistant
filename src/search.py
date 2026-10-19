"""Web search with Tavily: each result has url, title, content (the part relevant to the query),
page (full page text) and retrieved_at. SEARCH_STUB=1 in the environment returns the fixed pages below instead
(free and repeatable, for tests).

search() never raises: it returns {"status": ..., "pages": [...]}, where status is one of
ok | no_results | rate_limited | timeout | error, so the pipeline can route around a failure.
SEARCH_SIMULATE=timeout@N makes the N-th distinct query time out (and every retry of it), to test that.
"""

import os
import time
from collections import Counter
from datetime import datetime, timezone
from urllib.parse import urlsplit

from dotenv import load_dotenv
from tavily import TavilyClient
from tavily.errors import TimeoutError as TavilyTimeout, UsageLimitExceededError

load_dotenv()

FAKE_RESULTS = [
    {"url": "https://example.com/k8s-small-teams", "title": "Kubernetes for small teams",
     "content": "Running a production Kubernetes cluster typically needs at least one engineer spending a large part of their time on upgrades, networking and security. Managed services like GKE and EKS remove the control plane work but not the rest."},
    {"url": "https://example.com/paas-vs-k8s", "title": "PaaS or Kubernetes?",
     "content": "Startups under 20 engineers often ship faster on a PaaS such as Render, Fly.io or Heroku, or on serverless containers like Cloud Run. Teams usually move to Kubernetes when they run many services with custom networking needs."},
    {"url": "https://example.com/eks-pricing", "title": "EKS pricing explained",
     "content": "Amazon EKS charges $0.10 per hour per cluster for the control plane, about $73 per month, plus the cost of worker nodes."},
    {"url": "https://example.com/k8s-benefits", "title": "Why teams adopt Kubernetes",
     "content": "Kubernetes gives self-healing, rolling deploys and autoscaling, and avoids lock-in to one cloud provider."},
    {"url": "https://example.com/pizza", "title": "Best pizza dough",
     "content": "Let the dough rest for 48 hours in the fridge for better flavour."},  # irrelevant on purpose
]


MAX_PER_DOMAIN = 2  # one site can't fill the report
MIN_PAGE_CHARS = 300  # less text than this = paywall, login wall or an empty page

_queries: list[str] = []  # for SEARCH_SIMULATE


def domain(url: str) -> str:
    return urlsplit(url).netloc.removeprefix("www.")


def _fetch(query: str, max_results: int) -> list[dict]:
    if query not in _queries:
        _queries.append(query)
    kind, _, n = os.getenv("SEARCH_SIMULATE", "").partition("@")
    if kind == "timeout" and _queries.index(query) + 1 == int(n):
        raise TavilyTimeout(60)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if os.getenv("SEARCH_STUB") == "1":
        tag = f"{abs(hash(query)) % 10_000:04d}"  # each query gets its own fake addresses, so filters don't empty later ones
        return [r | {"url": r["url"].replace("example.com", f"q{tag}-{i}.example.com"), "page": r["content"],
                     "retrieved_at": now, "stub": True} for i, r in enumerate(FAKE_RESULTS[:max_results])]
    r = TavilyClient(os.environ["TAVILY_API_KEY"]).search(query, max_results=max_results, include_raw_content=True, timeout=30)
    return [{"url": x["url"], "title": x["title"], "content": x["content"],
             "page": x.get("raw_content") or x["content"], "retrieved_at": now} for x in r["results"]]


def search(query: str, seen_urls: set[str] = frozenset(), max_results: int = 5) -> dict:
    """Search once, retry once on timeout/rate limit, then drop repeats, over-cap sites and paywalled pages.
    seen_urls = pages this run already used (any sub-question)."""
    for attempt in range(2):
        try:
            pages = _fetch(query, max_results)
            break
        except TavilyTimeout:
            status = "timeout"
        except UsageLimitExceededError:  # Tavily answers 429 for both rate and plan limits
            status = "rate_limited"
        except Exception as e:  # any other failure is named, never raised into the pipeline
            return {"status": "error", "detail": repr(e)[:200], "pages": [], "skipped": {}}
        time.sleep(2 * (attempt + 1))
    else:
        return {"status": status, "pages": [], "skipped": {}}

    per_site = Counter(domain(u) for u in seen_urls)
    kept, skipped = [], Counter()
    for p in pages:
        if p["url"] in seen_urls:
            skipped["duplicate"] += 1
        elif per_site[domain(p["url"])] >= MAX_PER_DOMAIN:
            skipped["domain_cap"] += 1
        elif len(p["page"]) < MIN_PAGE_CHARS and not p.get("stub"):  # fake pages are short on purpose
            skipped["paywalled"] += 1
        else:
            kept.append(p)
            per_site[domain(p["url"])] += 1
    status = "ok" if kept else "no_results"
    return {"status": status, "pages": kept, "skipped": dict(skipped)}
