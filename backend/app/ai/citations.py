"""Keep the AI honest about its sources.

When we know exactly which URLs a research conversation saw (our own
web_search / fetch_url tools, or the Anthropic API's search and fetch result
blocks), every citation the model returns must be one of them. A model can't
cite a page it never saw, and small free models are the ones most tempted to.

The rules follow the research prompt: every verdict other than "supported",
and every successor, must be backed by a citation. A verdict left with no
valid citation is dropped, so the package keeps its structured verdict. An
uncited successor is cleared.
"""
from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

from ..models import AiVerdictBatch, Verdict


def normalize(url: str) -> str:
    """Compare URLs loosely: case-insensitive scheme, host and path, no
    fragment, no trailing slash, and http and https treated alike. Models
    often re-case a path (a GitHub owner name, say), and a citation that
    differs only by case is the same page in practice. Dropping it would
    lose a real source."""
    p = urlsplit(url.strip())
    scheme = "https" if p.scheme.lower() in ("http", "https") else p.scheme.lower()
    path = p.path.rstrip("/").lower()
    return urlunsplit((scheme, p.netloc.lower(), path, p.query, ""))


def enforce(batch: AiVerdictBatch, seen_urls: set[str] | None) -> tuple[AiVerdictBatch, dict]:
    """Return the batch with bad citations removed, plus a report of what was
    dropped: {"citations": n, "verdicts": [names], "successors": [names]}.

    `seen_urls` None means the provider doesn't show what its searches
    returned (the Claude CLI). Then only the presence rule applies: a
    problem verdict with no citation at all is dropped. The first live eval
    caught Haiku doing exactly that for 3 of 18 packages."""
    seen = None if seen_urls is None else {normalize(u) for u in seen_urls if u}
    report = {"citations": 0, "verdicts": [], "successors": []}
    kept = []
    for v in batch.verdicts:
        good = [s for s in v.sources if s.url and (seen is None or normalize(s.url) in seen)]
        report["citations"] += len(v.sources) - len(good)
        v.sources = good
        if v.verdict not in (Verdict.supported, Verdict.unknown) and not good:
            report["verdicts"].append(v.name)
            continue  # unbacked deprecated/eol claim: keep the structured verdict instead
        if v.successor and not good:
            report["successors"].append(v.name)
            v.successor = None
        kept.append(v)
    return AiVerdictBatch(verdicts=kept), report
