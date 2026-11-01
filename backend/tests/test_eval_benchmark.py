"""The benchmark harness (backend/eval): summary math and run wiring."""

import json

from eval import benchmark
from tests.fakes import detected_json, root_cause_json, verdict_json
from utils.log_processor import extract_error_context

LOG = "Started\n[javac] Foo.java:3: error: cannot find symbol\nFinished: FAILURE"


def row(label, final, llm=None, conf="clear", validated=True, **extra):
    base = {
        "file": "x.log", "label": label, "label_confidence": conf, "confidence": "high",
        "llm_category": llm or final, "validated": validated,
        "final_category": final, "calls": 3, "seconds": 10.0,
        "prompt_tokens": 1000, "completion_tokens": 500,
    }
    base.update(extra)
    return base


def test_summarize_counts_wrong_actionable_and_ignores_errors():
    rows = [
        row("infra", "infra"),                                  # right + actionable
        row("devops", "needs_triage", llm="devops"),            # held back, LLM right
        row("infra", "devops", llm="devops", conf="likely"),    # wrong + actionable
        {"file": "e.log", "label": "infra", "label_confidence": "clear", "error": "LLMRateLimitError"},
    ]

    stats = benchmark.summarize(rows)

    assert stats["logs"] == 4 and stats["errors"] == 1
    assert stats["wrong_actionable"] == 1
    assert stats["actionable%"] == 66.7
    assert stats["llm_acc%"] == 66.7            # 2 of the 3 completed
    assert stats["llm_acc_clear%"] == 100.0     # both clear rows right
    assert stats["calls/log"] == 3.0 and stats["sec/log"] == 10.0
    assert stats["ptok/log"] == 1000 and stats["ctok/log"] == 500


def test_summarize_handles_all_errors():
    stats = benchmark.summarize([{"file": "e", "label": "infra",
                                  "label_confidence": "clear", "error": "X"}])

    assert stats["errors"] == 1 and stats["actionable%"] is None


def test_run_one_records_calls_and_final_category(fake_llm):
    parsed = extract_error_context(LOG)
    evidence = next(line for line in parsed.splitlines() if "cannot find symbol" in line)
    fake_llm([
        detected_json([evidence]),
        root_cause_json("compilation", [evidence]),
        verdict_json(True),
    ])

    result = benchmark.run_one(LOG, "x.log")

    assert result["llm_category"] == "compilation"
    assert result["final_category"] == "compilation"
    assert result["validated"] is True
    assert result["calls"] == 3 and result["attempts"] == 1
    # Stage unknown, so the compilation rule is not skipped and matches the line.
    assert result["rule_categories"] == ["compilation"]


def test_apply_sets_and_clears_overrides(monkeypatch):
    from settings import get_settings

    benchmark._apply(benchmark.CONFIGS["20b-low-merged"])
    settings = get_settings()
    assert settings.llm_model == "openai/gpt-oss-20b"
    assert settings.llm_merge_detector is True and settings.validator_checks_fix is False

    benchmark._apply(benchmark.CONFIGS["current"])
    settings = get_settings()
    assert settings.llm_model == "openai/gpt-oss-120b"
    assert settings.llm_merge_detector is False and settings.llm_reasoning_effort is None


def test_load_labels_reads_csv(tmp_path):
    (tmp_path / "labels.csv").write_text(
        "file,label,confidence,note\na.log,infra,clear,n\n", encoding="utf-8"
    )

    assert benchmark.load_labels(tmp_path) == [
        {"file": "a.log", "label": "infra", "confidence": "clear", "note": "n"}
    ]


def test_run_skips_logs_already_recorded(tmp_path, monkeypatch, capsys):
    data = tmp_path
    (data / "logs").mkdir()
    (data / "logs" / "a.log").write_text(LOG, encoding="utf-8")
    (data / "logs" / "b.log").write_text(LOG, encoding="utf-8")
    (data / "labels.csv").write_text(
        "file,label,confidence,note\na.log,infra,clear,\nb.log,infra,clear,\n",
        encoding="utf-8",
    )
    out = data / "results"
    out.mkdir()
    (out / "current.jsonl").write_text(json.dumps({"file": "a.log"}) + "\n", encoding="utf-8")
    seen = []
    monkeypatch.setattr(
        benchmark, "run_one",
        lambda text, name: seen.append(name) or {"final_category": "infra", "calls": 3, "seconds": 1.0},
    )

    class Args:
        pass

    args = Args()
    args.data, args.out, args.only, args.pause, args.configs = str(data), None, None, 0, "current"

    benchmark.run(args)

    assert seen == ["b.log"]
    assert len((out / "current.jsonl").read_text().splitlines()) == 2
