# EOL research eval

Scores the AI research step on 18 packages with well-known answers
(`cases.json`): 10 dead or replaced (cybox, node-sass, tslint, pycrypto…) and
8 healthy controls (react, django…).

```bash
cd backend
python -m evals.eol_research.run                 # batch size = ENRICHMENT_BATCH_SIZE (18)
python -m evals.eol_research.run --batch-size 6 --out report.json
```

It uses whatever provider the environment configures (`AI_PROVIDER` and the
rest; the local Claude CLI when that's unset). Each case goes straight to the
research step, with the same prompt, parser and citation check the scanner
uses, skipping the structured lookups and `_needs_ai`. That makes it harder
than a real scan: there, the npm registry's own `deprecated` flag catches most
of the npm cases before any AI runs. **It spends real quota or money: run it by
hand, not in CI.**

What it reports:
- **accuracy**: the verdict is one the case accepts;
- **wrong "eol"**: a healthy package called end-of-life, the worst mistake;
- **successors**: the expected replacement, where the case names one;
- **unanswered**: no verdict, either not returned or dropped by the citation check;
- **uncited problem verdicts**: deprecated or eol with no citation (always 0 once the citation check runs);
- tokens, searches, estimated cost and time.

## Results

| Date | Provider · model | Batch | Accuracy | Wrong "eol" | Successors | Searches | Tokens | Time |
|---|---|---|---|---|---|---|---|---|
| 2026-11-07 | claude-cli · claude-haiku-4-5 | 18 | 16/18 | 0 | 3/6 | (not counted yet) | 128K | 65 s |
| 2026-11-07 | claude-cli · claude-haiku-4-5 | 18 | 13/18 | 0 | 4/6 | 5 | 88K | 68 s |
| 2026-11-07 | claude-cli · claude-haiku-4-5 | 6 | 16/18 | 0 | 4/6 | 15 | 366K | 136 s |

What the first runs showed:
- **No healthy package was ever called end-of-life.** The prompt's "be
  conservative" rule holds: when unsure, the model says "supported".
- **Recall depends on searches per package.** At 18 packages per batch and a
  cap of 5 searches, the model can't look up every dead package, and the ones
  it skips come back "supported". Results varied from 13 to 16 out of 18
  between identical runs. Batches of 6 (15 searches) got 16/18, at about four
  times the tokens through the CLI.
- **The citation rule was needed.** The first run returned 3 deprecated or eol
  verdicts with no citation at all. These are now dropped for every provider,
  and the packages keep their structured verdict.
- **The CLI hid its searches.** It reports `web_search_requests: 0` even when
  it searched. Searches are now counted from its event stream.
- **The CLI is token-heavy.** About 150K tokens for a single package, because
  Claude Code sends its large system prompt on every turn. The API providers
  send only the research prompt.

Next: the same runs against the Anthropic API (Claude Haiku 5.5) and a free
OpenAI-compatible provider plus Tavily, once their keys are in `backend/.env`.
