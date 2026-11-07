import datetime as dt

import app.agents.enrichment_agent as ea
from app import scanlog, settings_store
from app.models import Confidence, Ecosystem, Enrichment, Package, Source, Verdict


def _e(**kw):
    base = dict(name="lib", version="1.0.0", ecosystem=Ecosystem.pypi)
    base.update(kw)
    return Enrichment(**base)


def _tokens(**kw):
    """One agent call's token report, in the shape AgentResult.tokens() returns."""
    base = dict(input_tokens=1000, cache_read_input_tokens=200,
                cache_creation_input_tokens=100, output_tokens=300,
                total_tokens=1600, web_search_requests=5, web_fetch_requests=1,
                cost_usd=0.002, num_turns=3)
    base.update(kw)
    return base


def test_needs_ai_skips_endoflife_hit():
    e = _e(verdict=Verdict.supported, confidence=Confidence.medium,
           sources=[Source(url="https://endoflife.date/react", note="endoflife.date cycle 19")])
    assert ea._needs_ai(e, stale_years=2) is False


def test_needs_ai_catches_stale_no_endoflife():
    e = _e(verdict=Verdict.supported, confidence=Confidence.medium,
           last_release=dt.date(2018, 1, 1), sources=[Source(url="https://pypi.org/x", note="PyPI")])
    assert ea._needs_ai(e, stale_years=2) is True


def test_needs_ai_catches_unknown_and_low():
    assert ea._needs_ai(_e(verdict=Verdict.unknown), stale_years=2) is True
    assert ea._needs_ai(_e(confidence=Confidence.low), stale_years=2) is True


def test_merge_ai_never_sets_dates_and_dedups_sources():
    from app.models import AiVerdict
    e = _e(verdict=Verdict.supported, confidence=Confidence.low,
           sources=[Source(url="https://pypi.org/x", note="PyPI")])
    v = AiVerdict(name="lib", ecosystem=Ecosystem.pypi, verdict=Verdict.eol,
                  successor="stix2", confidence=Confidence.high, summary="superseded",
                  sources=[Source(url="https://pypi.org/x", note="dup"),
                           Source(url="https://oasis.example/", note="cite")])
    ea._merge_ai(e, v)
    assert e.verdict == Verdict.eol
    assert e.successor == "stix2"
    assert e.confidence == Confidence.high
    assert e.eol_date is None and e.eos_date is None      # dates never set by AI
    assert [s.url for s in e.sources] == ["https://pypi.org/x", "https://oasis.example/"]


async def test_ai_pass_only_touches_candidates(monkeypatch):
    settings_store.update_runtime_settings({"use_enrichment_agent": True})

    class FakeRes:
        is_error = False
        error_message = ""
        text = ('{"verdicts":[{"name":"cybox","ecosystem":"pypi","verdict":"eol",'
                '"successor":"stix2","confidence":"high","summary":"superseded",'
                '"sources":[{"url":"https://oasis.example/","note":"OASIS"}]}]}')

        def tokens(self):
            return _tokens()

    async def fake_agent(prompt, **kw):
        return FakeRes()

    monkeypatch.setattr(ea, "run_text_agent", fake_agent)

    react = _e(name="react", ecosystem=Ecosystem.npm, verdict=Verdict.supported,
               confidence=Confidence.medium,
               sources=[Source(url="https://endoflife.date/react", note="endoflife.date")])
    cybox = _e(name="cybox", verdict=Verdict.supported, confidence=Confidence.medium,
               last_release=dt.date(2018, 1, 1), sources=[Source(url="https://pypi.org/cybox", note="PyPI")])
    fresh = [react, cybox]
    await ea._ai_pass(fresh, today=dt.date(2026, 7, 14))

    assert cybox.verdict == Verdict.eol and cybox.successor == "stix2"
    assert react.verdict == Verdict.supported  # endoflife hit → skipped by AI


def _fake_structured(monkeypatch, verdict=Verdict.unknown, confidence=Confidence.low):
    """Stub the structured pass so `enrich()` never touches the network."""
    async def fake_enrich_one(client, pkg, *, today):
        return _e(name=pkg.name, version=pkg.version, ecosystem=pkg.ecosystem,
                  verdict=verdict, confidence=confidence)
    monkeypatch.setattr(ea, "_enrich_one", fake_enrich_one)


async def test_ambiguous_package_is_ai_checked_on_every_scan(monkeypatch):
    """An ambiguous (unknown/low-confidence) package gets AI-checked when the
    toggle is on. There is no result cache, so each scan is independent and the
    AI pass runs again next scan — the accepted trade for always-fresh results."""
    settings_store.update_runtime_settings({"use_enrichment_agent": True})
    _fake_structured(monkeypatch)

    calls = {"n": 0}

    class FakeRes:
        is_error = False
        error_message = ""
        text = ('{"verdicts":[{"name":"obscurelib","ecosystem":"pypi","verdict":"deprecated",'
                '"successor":"newlib","confidence":"medium","summary":"moved",'
                '"sources":[{"url":"https://x/","note":"blog"}]}]}')

        def tokens(self):
            return _tokens()

    async def fake_agent(prompt, **kw):
        calls["n"] += 1
        return FakeRes()

    monkeypatch.setattr(ea, "run_text_agent", fake_agent)

    pkg = Package(name="obscurelib", version="1.0.0", ecosystem=Ecosystem.pypi, source_file="r.txt")

    out1 = await ea.enrich([pkg], today=dt.date(2026, 7, 14))
    assert out1[0].verdict == Verdict.deprecated
    assert out1[0].successor == "newlib"
    assert out1[0].ai_checked is True
    assert calls["n"] == 1

    # a second scan is fully independent: fresh structured lookup + fresh research
    out2 = await ea.enrich([pkg], today=dt.date(2026, 7, 14))
    assert out2[0].verdict == Verdict.deprecated
    assert calls["n"] == 2


async def test_enrich_dedups_repeated_packages_within_a_scan(monkeypatch):
    """Dedup by ecosystem:name:version is the only lookup-cost control left, so
    the same pinned library in several manifests must cost one lookup."""
    settings_store.update_runtime_settings({"use_enrichment_agent": False})
    looked_up: list[str] = []

    async def fake_enrich_one(client, pkg, *, today, distro=None, index=None):
        looked_up.append(pkg.key)
        return _e(name=pkg.name, version=pkg.version, verdict=Verdict.supported)

    monkeypatch.setattr(ea, "_enrich_one", fake_enrich_one)

    pkgs = [
        Package(name="celery", version="5.5.3", ecosystem=Ecosystem.pypi, source_file="a/requirements.txt"),
        Package(name="celery", version="5.5.3", ecosystem=Ecosystem.pypi, source_file="b/requirements.txt"),
        Package(name="celery", version="5.4.0", ecosystem=Ecosystem.pypi, source_file="c/requirements.txt"),
    ]
    out = await ea.enrich(pkgs, today=dt.date(2026, 7, 14))

    assert looked_up == ["pypi:celery:5.5.3", "pypi:celery:5.4.0"]  # 2 lookups, not 3
    assert len(out) == 2  # one Enrichment per unique key


def test_overall_latest_picks_newest_across_cycles():
    """endoflife.date reports `latest` per-cycle; we must surface the overall
    newest version, not the latest within the current version's cycle."""
    from app.agents.source_apis import _overall_latest
    releases = [
        {"name": "1.31", "latest": {"name": "1.31.2"}},
        {"name": "1.30", "latest": {"name": "1.30.3"}},
        {"name": "1.28", "latest": {"name": "1.28.3"}},
    ]
    assert _overall_latest(releases) == "1.31.2"
    # ordering-independent: newest may not be first
    mongo = [
        {"name": "6.0", "latest": {"name": "6.0.29"}},
        {"name": "8.3", "latest": {"name": "8.3.4"}},
        {"name": "8.0", "latest": {"name": "8.0.26"}},
    ]
    assert _overall_latest(mongo) == "8.3.4"


def test_overall_latest_handles_unparseable_versions():
    from app.agents.source_apis import _overall_latest
    assert _overall_latest([]) is None
    # falls back to the first name when none parse as versions
    assert _overall_latest([{"name": "x", "latest": {"name": "weird-tag"}}]) == "weird-tag"


# --------------------------------------------------------------------------- #
# latest STABLE version (separate from `latest`, which may be a prerelease)    #
# --------------------------------------------------------------------------- #
def test_overall_latest_stable_skips_prereleases():
    from app.agents.source_apis import _overall_latest
    releases = [
        {"name": "3.0", "latest": {"name": "3.0.0rc1"}},   # newest, but an rc
        {"name": "2.9", "latest": {"name": "2.9.4"}},
    ]
    # plain `latest` still surfaces the rc (unchanged behaviour)…
    assert _overall_latest(releases) == "3.0.0rc1"
    # …while stable_only skips it
    assert _overall_latest(releases, stable_only=True) == "2.9.4"


def test_overall_latest_stable_is_none_when_all_prerelease():
    """No prerelease fallback — that's what the plain `latest` is for."""
    from app.agents.source_apis import _overall_latest
    releases = [{"name": "1.0", "latest": {"name": "1.0.0b1"}}]
    assert _overall_latest(releases, stable_only=True) is None
    assert _overall_latest([], stable_only=True) is None
    # unparseable tags can't be proven stable either
    assert _overall_latest([{"name": "x", "latest": {"name": "weird-tag"}}],
                           stable_only=True) is None


def test_latest_stable_pypi_prefers_stable_and_skips_yanked():
    from app.agents.source_apis import latest_stable_pypi
    releases = {
        "1.9.2": [{"yanked": False}],
        "2.0.0rc1": [{"yanked": False}],          # prerelease → skipped
        "1.9.3": [{"yanked": True}],              # fully yanked → skipped
        "1.9.1": [{"yanked": False}, {"yanked": True}],  # partially yanked → kept
        "1.8.0": [],                              # no files (stub) → skipped
    }
    assert latest_stable_pypi(releases) == "1.9.2"


def test_latest_stable_pypi_none_when_nothing_qualifies():
    from app.agents.source_apis import latest_stable_pypi
    assert latest_stable_pypi({}) is None
    assert latest_stable_pypi({"2.0.0rc1": [{"yanked": False}]}) is None
    assert latest_stable_pypi({"1.0.0": [{"yanked": True}]}) is None


def test_latest_stable_npm_skips_semver_prereleases():
    from app.agents.source_apis import latest_stable_npm
    versions = {"1.2.0": {}, "1.4.0": {}, "2.0.0-beta.1": {}, "2.0.0-rc.2": {}}
    assert latest_stable_npm(versions) == "1.4.0"
    assert latest_stable_npm({"1.0.0-alpha.1": {}}) is None
    assert latest_stable_npm({}) is None


async def test_products_index_failure_is_not_cached(monkeypatch):
    """A transient failure fetching the endoflife product index must not be
    cached: `[]` would make every later slug lookup miss for the rest of the
    process, silently stripping EOL dates from every package that isn't in
    EOL_ALIASES — while the scan still reported success."""
    from app.agents import source_apis

    monkeypatch.setattr(source_apis, "_products_index_cache", None)
    calls = {"n": 0}

    async def flaky(client, url, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return None  # network blip
        return {"result": [{"name": "nginx", "aliases": []}]}

    monkeypatch.setattr(source_apis, "_get_json", flaky)

    assert await source_apis.endoflife_products(None) == []          # not cached
    assert await source_apis.resolve_eol_slug(None, "nginx") == "nginx"  # retried


async def test_ai_pass_records_token_totals(monkeypatch):
    """Every agent call's tokens must reach the scan log, summed across batches.
    Without this the AI cost of a scan is invisible once the run is over."""
    settings_store.update_runtime_settings({"use_enrichment_agent": True})

    class FakeRes:
        is_error = False
        error_message = ""
        text = '{"verdicts":[]}'

        def tokens(self):
            return _tokens()

    async def fake_agent(prompt, **kw):
        return FakeRes()

    monkeypatch.setattr(ea, "run_text_agent", fake_agent)

    # one package over the batch size, so exactly two batches run
    batch_size = ea.get_settings().enrichment_batch_size
    pool = [_e(name=f"lib{i}", verdict=Verdict.unknown) for i in range(batch_size + 1)]

    logger = scanlog.start("repo", "branch")
    await ea._ai_pass(pool, today=dt.date(2026, 7, 14))

    per_batch = [e.data for e in logger.entries if e.message.startswith("Batch tokens:")]
    totals = [e.data for e in logger.entries if e.message.startswith("AI enrichment spent")]
    one = _tokens()

    assert len(per_batch) == 2
    assert len(totals) == 1
    assert totals[0]["total_tokens"] == 2 * one["total_tokens"]
    assert totals[0]["output_tokens"] == 2 * one["output_tokens"]
    assert totals[0]["cache_read_input_tokens"] == 2 * one["cache_read_input_tokens"]
    assert totals[0]["web_search_requests"] == 2 * one["web_search_requests"]
    assert totals[0]["cost_usd"] == round(2 * one["cost_usd"], 6)
