"""Classify a folder of saved console logs with the same pipeline the worker
uses (rules + LangGraph + reconcile + shadow policy), and write a CSV.

Reads GROQ_API_KEY from backend/.env. Makes LLM calls only; never contacts
Jenkins or Gerrit.

    backend/.venv/bin/python scripts/classify_saved_logs.py failed-logs
"""

import argparse
import csv
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "backend"))

from classification.reconcile import reconcile  # noqa: E402
from classification.rules import evaluate_rules  # noqa: E402
from enrichment import failing_stage  # noqa: E402
from graph import analyze_log, build_analysis_response  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("folder")
    parser.add_argument("--out", default=None, help="CSV path (default: <folder>/results.csv)")
    args = parser.parse_args()

    folder = Path(args.folder)
    out = Path(args.out) if args.out else folder / "results.csv"
    logs = sorted(folder.glob("*.log"))
    print(f"Classifying {len(logs)} log(s) from {folder}/")

    with open(out, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow([
            "file", "failing_stage", "rule_categories", "llm_category",
            "validated", "confidence", "final_category", "reason_codes",
            "root_cause", "evidence",
        ])

        for path in logs:
            text = path.read_text(encoding="utf-8", errors="replace")
            stage = failing_stage(None, text)
            analysis = build_analysis_response(analyze_log(text), path.name)
            matches = evaluate_rules(text, stage)
            decision = reconcile(analysis, matches)

            writer.writerow([
                path.name,
                stage or "",
                ";".join(sorted({m.category for m in matches})),
                analysis["llm_category"] or "",
                analysis["validated"],
                analysis["confidence"],
                decision.final_category,
                ";".join(decision.reason_codes),
                analysis["root_cause"],
                " | ".join(analysis["evidence"][:2]),
            ])
            print(f"  {path.name}: {decision.final_category}"
                  f" (llm={analysis['llm_category']}, validated={analysis['validated']},"
                  f" rules={sorted({m.category for m in matches})})")

    print(f"Results: {out}")


if __name__ == "__main__":
    main()
