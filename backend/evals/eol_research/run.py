"""Score the AI research step against packages with known answers.

    cd backend
    python -m evals.eol_research.run [--batch-size N] [--out results.json]

It uses whatever provider the environment configures (AI_PROVIDER etc., or
the local Claude CLI when that's unset). Each case goes straight to the AI
research step, with the same prompt, parser and citation check the scanner
uses, skipping the structured lookups and `_needs_ai`. Then the answers are
scored. It spends real quota or money, so run it by hand, not in CI.

What's measured, per provider and model:
- accuracy: the verdict is one the case accepts;
- wrong "eol": calling a healthy package end-of-life, the worst mistake;
- successors: the expected replacement, where the case names one;
- unanswered: no verdict, either not returned or dropped by the citation check;
- tokens, searches, estimated cost and time.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

from app.agents import enrichment_agent as ea
from app.ai import config as ai_config
from app.ai.result import sum_tokens
from app.config import get_settings
from app.models import Ecosystem, Enrichment, Verdict

CASES = Path(__file__).with_name("cases.json")


def load_cases() -> list[dict]:
    return json.loads(CASES.read_text(encoding="utf-8"))


def score(case: dict, verdict) -> dict:
    accept = case["accept"]
    got = verdict.verdict.value if verdict else None
    successor = (verdict.successor or "") if verdict else ""
    expected_successor = case.get("successor_any")
    return {
        "package": f"{case['ecosystem']}:{case['name']}@{case['version']}",
        "expected": "/".join(accept),
        "got": got or "—",
        "ok": got in accept,
        "wrong_eol": got == Verdict.eol.value and "eol" not in accept,
        "successor": successor or None,
        "successor_ok": (None if not expected_successor else
                         any(s in successor.lower() for s in expected_successor)),
        "citations": len(verdict.sources) if verdict else 0,
    }


async def run(batch_size: int | None = None) -> dict:
    cfg, why = ai_config.resolve()
    if cfg is None:
        raise SystemExit(f"No usable AI provider: {why}")
    settings = get_settings()
    size = batch_size or settings.enrichment_batch_size
    cases = load_cases()
    rows: list[dict] = []
    totals: dict = {}
    failures: list[str] = []
    t0 = time.monotonic()
    for i in range(0, len(cases), size):
        batch_cases = cases[i:i + size]
        batch = [Enrichment(name=c["name"], version=c["version"], ecosystem=Ecosystem(c["ecosystem"]))
                 for c in batch_cases]
        errors: list[tuple[str, str]] = []
        result, spent = await ea._run_ai_batch(batch, max_searches=settings.web_search_max_uses,
                                               errors=errors)
        sum_tokens(totals, spent)
        if result is None:
            failures.append(errors[-1][1] if errors else "no answer")
        verdicts = {(v.ecosystem, v.name.lower()): v for v in (result.verdicts if result else [])}
        rows += [score(c, verdicts.get((e.ecosystem, e.name.lower()))) for c, e in zip(batch_cases, batch)]

    successors = [r for r in rows if r["successor_ok"] is not None]
    return {
        "provider": cfg.label,
        "model": cfg.model,
        "cases": len(rows),
        "accuracy": round(sum(r["ok"] for r in rows) / len(rows), 3),
        "wrong_eol": sum(r["wrong_eol"] for r in rows),
        "successors_found": f"{sum(r['successor_ok'] for r in successors)}/{len(successors)}",
        "unanswered": sum(r["got"] == "—" for r in rows),
        "uncited_problem_verdicts": sum(r["got"] in ("deprecated", "eol") and r["citations"] == 0 for r in rows),
        "batch_failures": failures,
        "tokens": totals.get("total_tokens", 0),
        "searches": totals.get("web_search_requests", 0),
        "cost_usd": None if totals.get("cost_unknown") else totals.get("cost_usd"),
        "seconds": round(time.monotonic() - t0, 1),
        "rows": rows,
    }


def _print(report: dict) -> None:
    print(f"\nProvider {report['provider']} · model {report['model']}")
    print(f"{'package':<34} {'expected':<16} {'got':<11} ok  successor")
    for r in report["rows"]:
        mark = "✓" if r["ok"] else ("✗ EOL!" if r["wrong_eol"] else "✗")
        print(f"{r['package']:<34} {r['expected']:<16} {r['got']:<11} {mark:<3} {r['successor'] or ''}")
    keys = ("accuracy", "wrong_eol", "successors_found", "unanswered", "uncited_problem_verdicts",
            "batch_failures", "tokens", "searches", "cost_usd", "seconds")
    print("\n" + "\n".join(f"{k:>25}: {report[k]}" for k in keys))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--batch-size", type=int, default=None, help="packages per AI call (default: ENRICHMENT_BATCH_SIZE)")
    p.add_argument("--out", type=Path, default=None, help="also write the full report as JSON")
    args = p.parse_args(argv)
    report = asyncio.run(run(args.batch_size))
    # Save before printing: the run costs quota, and losing it to a print
    # error would be a waste.
    if args.out:
        args.out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    if hasattr(sys.stdout, "reconfigure"):  # Windows consoles default to cp1252
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    _print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
