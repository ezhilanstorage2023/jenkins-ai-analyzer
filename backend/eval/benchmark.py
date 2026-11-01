"""Benchmark LLM configurations on a labeled set of Jenkins consoles.

Run from backend/ (the data lives outside git, in <repo>/eval-data):

    python -m eval.benchmark run --configs 20b-low,120b-low
    python -m eval.benchmark report

Each configuration overrides Settings through environment variables, runs
the real pipeline (graph + rules + reconcile) on every labeled log and
appends one JSON row per log to <out>/<config>.jsonl. Re-running skips logs
already recorded, so an interrupted or rate-limited run resumes.

Calls the real LLM provider (Groq). The consoles are already redacted; the
pipeline redacts again before anything is sent.
"""

import argparse
import csv
import json
import os
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DEFAULT_DATA = REPO / "eval-data"

# name -> Settings env overrides.
CONFIGS = {
    # The shipped defaults (see settings.py).
    "current": {},
    # The previous default: 120b at the provider's default (medium) effort.
    "120b-medium": {"LLM_MODEL": "openai/gpt-oss-120b", "LLM_REASONING_EFFORT": "medium"},
    "120b-low": {"LLM_MODEL": "openai/gpt-oss-120b", "LLM_REASONING_EFFORT": "low"},
    "20b-low": {
        "LLM_MODEL": "openai/gpt-oss-20b",
        "LLM_REASONING_EFFORT": "low",
    },
    "qwen-low": {
        "LLM_MODEL": "qwen/qwen3.8-27b",
        "LLM_REASONING_EFFORT": "low",
    },
    "20b-low-merged": {
        "LLM_MODEL": "openai/gpt-oss-20b",
        "LLM_REASONING_EFFORT": "low",
        "LLM_MERGE_DETECTOR": "true",
        "VALIDATOR_CHECKS_FIX": "false",
    },
    "120b-low-merged": {
        "LLM_MODEL": "openai/gpt-oss-120b",
        "LLM_REASONING_EFFORT": "low",
        "LLM_MERGE_DETECTOR": "true",
        "VALIDATOR_CHECKS_FIX": "false",
    },
    "qwen-low-merged": {
        "LLM_MODEL": "qwen/qwen3.8-27b",
        "LLM_REASONING_EFFORT": "low",
        "LLM_MERGE_DETECTOR": "true",
        "VALIDATOR_CHECKS_FIX": "false",
    },
}

_ENV_KEYS = {key for overrides in CONFIGS.values() for key in overrides}


def load_labels(data: Path) -> list[dict]:
    with open(data / "labels.csv", newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _apply(overrides: dict) -> None:
    from settings import get_settings

    for key in _ENV_KEYS:
        os.environ.pop(key, None)

    os.environ.update(overrides)
    get_settings.cache_clear()


class _Recorder:
    """Wraps the LLM client to count calls, tokens and time."""

    def __init__(self, real):
        self._real = real
        self.calls = []
        self.chat = type("Chat", (), {})()
        self.chat.completions = type("Completions", (), {"create": self._create})()

    def _create(self, **kwargs):
        started = time.monotonic()
        response = self._real.chat.completions.create(**kwargs)
        usage = getattr(response, "usage", None)
        self.calls.append({
            "seconds": time.monotonic() - started,
            "prompt_tokens": getattr(usage, "prompt_tokens", 0) or 0,
            "completion_tokens": getattr(usage, "completion_tokens", 0) or 0,
        })
        return response


def run_one(text: str, name: str) -> dict:
    import llm
    from classification.reconcile import reconcile
    from classification.rules import evaluate_rules
    from enrichment import failing_stage
    from graph import analyze_log, build_analysis_response

    real_factory = llm.get_llm_client
    recorder = _Recorder(real_factory())
    llm.get_llm_client = lambda: recorder
    started = time.monotonic()

    try:
        analysis = build_analysis_response(analyze_log(text), name)
    finally:
        llm.get_llm_client = real_factory

    matches = evaluate_rules(text, failing_stage(None, text))
    decision = reconcile(analysis, matches)

    return {
        "llm_category": analysis["llm_category"],
        "validated": analysis["validated"],
        "confidence": analysis["confidence"],
        "final_category": decision.final_category,
        "reason_codes": decision.reason_codes,
        "rule_categories": sorted({m.category for m in matches}),
        "attempts": analysis["attempts"],
        "calls": len(recorder.calls),
        "prompt_tokens": sum(c["prompt_tokens"] for c in recorder.calls),
        "completion_tokens": sum(c["completion_tokens"] for c in recorder.calls),
        "seconds": round(time.monotonic() - started, 1),
        "root_cause": analysis["root_cause"][:200],
    }


def run(args) -> None:
    import llm

    data = Path(args.data)
    out = Path(args.out or data / "results")
    out.mkdir(parents=True, exist_ok=True)
    labels = load_labels(data)

    if args.only:
        labels = [row for row in labels if args.only in row["file"]]

    for config in args.configs.split(","):
        _apply(CONFIGS[config])
        path = out / f"{config}.jsonl"
        done = set()

        if path.exists():
            done = {json.loads(line)["file"] for line in path.read_text("utf-8").splitlines()}

        print(f"== {config}: {len(labels) - len(done)} to run", flush=True)

        for row in labels:
            if row["file"] in done:
                continue

            text = (data / "logs" / row["file"]).read_text(encoding="utf-8")
            # label_confidence is how sure *we* are of the label; the LLM's own
            # "confidence" comes back from run_one and must not overwrite it.
            record = {"file": row["file"], "label": row["label"],
                      "label_confidence": row["confidence"], "config": config}

            try:
                record.update(run_one(text, row["file"]))
            except llm.LLMError as err:
                record.update({"error": type(err).__name__})

            with open(path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record) + "\n")

            print(f"  {row['file'][:46]:46} label={row['label']:12} "
                  f"final={record.get('final_category', record.get('error')):12} "
                  f"calls={record.get('calls', '-')} {record.get('seconds', '-')}s",
                  flush=True)
            time.sleep(args.pause)


def summarize(rows: list[dict]) -> dict:
    ok = [r for r in rows if "error" not in r]
    actionable = [r for r in ok if r["final_category"] != "needs_triage"]
    wrong = [r for r in actionable if r["final_category"] != r["label"]]
    clear = [r for r in ok if r.get("label_confidence") == "clear"]
    n = len(ok) or 1

    def pct(num, den):
        return round(100 * num / den, 1) if den else None

    return {
        "logs": len(rows),
        "errors": len(rows) - len(ok),
        "llm_acc%": pct(sum(r["llm_category"] == r["label"] for r in ok), len(ok)),
        "llm_acc_clear%": pct(sum(r["llm_category"] == r["label"] for r in clear), len(clear)),
        "validated%": pct(sum(r["validated"] for r in ok), len(ok)),
        "actionable%": pct(len(actionable), len(ok)),
        "wrong_actionable": len(wrong),
        "calls/log": round(sum(r["calls"] for r in ok) / n, 2),
        "sec/log": round(sum(r["seconds"] for r in ok) / n, 1),
        "ptok/log": round(sum(r["prompt_tokens"] for r in ok) / n),
        "ctok/log": round(sum(r["completion_tokens"] for r in ok) / n),
    }


def report(args) -> None:
    out = Path(args.out or Path(args.data) / "results")
    label_confidence = {r["file"]: r["confidence"] for r in load_labels(Path(args.data))}
    results = {}

    for path in sorted(out.glob("*.jsonl")):
        if args.configs and path.stem not in args.configs.split(","):
            continue

        rows = [json.loads(line) for line in path.read_text("utf-8").splitlines()]

        for row in rows:
            row["label_confidence"] = label_confidence.get(row["file"])

        results[path.stem] = rows

    if args.common:
        # Apples to apples: only logs that every config completed.
        done = [{r["file"] for r in rows if "error" not in r} for rows in results.values()]
        shared = set.intersection(*done) if done else set()
        results = {name: [r for r in rows if r["file"] in shared] for name, rows in results.items()}
        print(f"common logs: {len(shared)}\n")

    table = {name: summarize(rows) for name, rows in results.items()}

    if not table:
        print("no results")
        return

    columns = list(next(iter(table.values())))
    print("| config | " + " | ".join(columns) + " |")
    print("|---|" + "---|" * len(columns))

    for name, stats in table.items():
        print(f"| {name} | " + " | ".join(str(stats[c]) for c in columns) + " |")


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m eval.benchmark")
    parser.add_argument("--data", default=str(DEFAULT_DATA))
    parser.add_argument("--out", default=None)
    commands = parser.add_subparsers(dest="command", required=True)

    runner = commands.add_parser("run")
    runner.add_argument("--configs", required=True, help=",".join(CONFIGS))
    runner.add_argument("--only", help="substring filter on log file names")
    runner.add_argument("--pause", type=float, default=0.0,
                        help="seconds between logs (rate-limit pacing)")

    reporter = commands.add_parser("report")
    reporter.add_argument("--configs", help="comma-separated subset")
    reporter.add_argument("--common", action="store_true",
                          help="compare only logs every selected config completed")

    args = parser.parse_args()
    run(args) if args.command == "run" else report(args)


if __name__ == "__main__":
    main()
