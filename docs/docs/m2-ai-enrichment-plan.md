# M2 — AI Enrichment (research agent) — plan & workflow

> **Decisions locked (2026-07-14):**
> 1. **No AI-set dates.** The agent returns only verdict / deprecated / successor /
>    summary / citations. **All EOL/EOS dates come from structured sources only** —
>    the model never sets a date (removes the hallucinated-date risk entirely).
> 2. **Default ON, toggleable from the UI.** `use_enrichment_agent` is a persisted
>    runtime setting (seeded `true`), editable via a header switch — no restart.
> 3. **Web search allowed, capped** via `WEB_SEARCH_MAX_USES`.

## Goal
Add a second, AI-powered enrichment pass that runs **only for packages the
structured sources can't resolve** (no EOL date, unknown verdict, or a stale lib
with no authoritative signal). The agent web-searches for deprecation notices,
archival, support timelines, and successor libraries, and returns a **cited**
verdict in the same `Enrichment` contract. Everything else keeps working exactly
as today; this only fills the gaps.

This is what turns cases like `cybox` (still on PyPI, but superseded by STIX 2.x)
from a bland "supported" into `eol / deprecated, successor: stix2, [citations]`.
Cases like `react 19` stay "supported" (correctly — no EOL announced).

## Where it plugs in
Second pass inside `enrichment_agent.enrich()`, after the existing deterministic
`_enrich_one()` results are computed and **before** they're cached:

```
enrich(packages)
 ├─ cache.get_many(keys)                      → cache hits (unchanged)
 ├─ for each miss: _enrich_one()  [structured: endoflife/PyPI/npm/GitHub]  (unchanged)
 │
 ├─ NEW: candidates = [e for e in fresh if _needs_ai(e)]      # the ambiguous ones only
 ├─ NEW: if use_enrichment_agent and candidates:
 │        for batch in chunk(candidates, BATCH):              # ~12 per conversation
 │            signals = structured data already gathered for the batch
 │            reply   = run_text_agent(WebSearch/WebFetch, JSON-only, from signals)
 │            verdicts = validate(AiVerdictBatch)  → retry once → else degrade
 │            merge(verdicts) into the batch's Enrichment objects
 │
 └─ cache.put_many(final)                      # AI results cached too (shared, TTL)
```

## Candidate selection — `_needs_ai(e)` (the cost lever)
Only run the AI for packages where structured sources were inconclusive:
- `verdict == unknown`, **or**
- `confidence == low`, **or**
- no EOL date **and** no endoflife match **and** last release older than
  `stale_years` (catches superseded/abandoned libs like `cybox`).

Skips: high-confidence endoflife hits, clear registry `deprecated`/`eol`, and
active libraries with recent releases. On a typical ~160-package service repo this is
expected to be a small minority of packages → most of the run stays AI-free.

## The agent call
- **Tools:** built-in `WebSearch` + `WebFetch` (server tools on the subscription).
  No custom tools needed.
- **Model:** `agent_model` (currently Haiku 4.5).
- **Prompt:** we **pass the structured signals we already gathered** (ecosystem,
  name, version, latest_version, last_release, archived, PyPI/npm notes) so the
  agent does not re-fetch them — it only web-searches to fill the gap, then
  reasons. Strict JSON-only output.
- **Batching:** ~12 packages per conversation to amortise overhead; `web_search`
  capped via `web_search_max_uses`; `max_turns` capped.
- **Structured output:** prompt → `AiVerdictBatch` Pydantic validation → one retry
  on mismatch → per-package degrade to the deterministic result (a bad batch never
  loses data or fails the scan).

## Output contract (new models)
```python
class AiVerdict(BaseModel):
    name: str
    ecosystem: Ecosystem
    verdict: Verdict            # supported | deprecated | eol | unknown
    eol_date: date | None
    eos_date: date | None
    successor: str | None
    confidence: Confidence
    summary: str
    sources: list[Source]       # every claim cited with a URL

class AiVerdictBatch(BaseModel):
    verdicts: list[AiVerdict]
```

## Merge rules (structured data stays authoritative)
For each candidate `e` and its matched AI verdict `v`:
- **Dates:** structured EOL/EOS dates **always win**. AI may *fill* a date only
  when `e` has none — and (per the date-policy decision below) only if the AI
  cited a source; otherwise the date stays null.
- **verdict:** if `e` already has a high-confidence structured verdict, keep it;
  otherwise adopt `v.verdict`.
- **successor / summary:** take from `v` (this is the AI's main value-add).
- **confidence:** `max(e.confidence, v.confidence)`.
- **sources:** `e.sources + v.sources`, deduped by URL.
- Flagging is unchanged — it just now has better `verdict`/dates/`successor` to
  work with (e.g. `cybox` → `ALREADY_EOL` or `DEPRECATED`).

## Config additions
| Var | Default | Purpose |
|---|---|---|
| `USE_ENRICHMENT_AGENT` | (decision below) | master switch for the AI pass |
| `ENRICHMENT_BATCH_SIZE` | 12 | packages per agent conversation |
| `WEB_SEARCH_MAX_USES` | 5 | web-search cap per conversation (exists) |
| `AI_MAY_SET_DATES` | (decision below) | whether AI can propose EOL/EOS dates |

## Observability (Activity page)
The scan trace gains, under stages `enrich` / `agent`:
- `AI enrichment: N candidates → M batches`
- each `WebSearch`/`WebFetch` tool call + result (already captured by transport)
- per-package `agent` verdict line: `cybox → eol (successor stix2)` with citations
So the Activity page shows exactly how the AI reached each verdict.

## Cost / safety controls (summary)
- Candidates only (small subset), cache-first, results cached with TTL (shared
  across repos/runs), batched, `web_search` capped, `max_turns` capped, graceful
  degrade to structured result on any failure. Structured dates are never
  overwritten by the model.

## Testing
- **Unit (no network/AI):** mock `run_text_agent` to return a canned
  `AiVerdictBatch`; assert merge rules (AI fills unknown verdict, keeps structured
  dates, dedups sources, matches by name+ecosystem, degrades on malformed JSON).
- **Unit:** `_needs_ai` (react w/ endoflife → skip; cybox stale → candidate;
  unknown → candidate).
- **Live smoke (flagged, not CI):** run against `cybox` and one abandoned npm lib;
  expect a cited deprecation/successor verdict.

## Milestone steps
1. Models (`AiVerdict`, `AiVerdictBatch`) + config flags.
2. `_needs_ai` candidate heuristic + unit tests.
3. Prompt builder + batch runner (JSON validate/retry/degrade).
4. Merge logic + wire as second pass in `enrich()` + scanlog emits.
5. Tests (mocked agent) + live smoke on a superseded lib.
