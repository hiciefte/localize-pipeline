"""An enum-style key does not make a sentence safe to leave in English."""

import pytest

from localize.translation_quality_gate import (
    QualityGateConfig,
    analyze_source_identical_changes,
    build_quality_gate_report,
)


@pytest.mark.parametrize("locale", ["de", "fr", "es"])
def test_new_source_identical_prose_under_enum_keys_blocks(locale, tmp_path):
    resources = tmp_path / "resources"
    resources.mkdir()
    source = {
        "KEEP_RECEIPT": "Keep the receipt for {0} days.",
        "CHECK_PAYMENT": "Check your payment before continuing.",
        "brand.name": "Bisq",
        "network.term": "Internet",
        "placeholder.value": "{0}",
        "markup.break": "<br/>",
        "technical.token": "BTC",
    }
    (resources / "messages.properties").write_text(
        "".join(f"{key}={value}\n" for key, value in source.items()),
        encoding="utf-8",
    )
    target_path = f"resources/messages_{locale}.properties"

    def check(entries):
        diff = (
            f"diff --git a/{target_path} b/{target_path}\n"
            f"+++ b/{target_path}\n"
            + "".join(f"+{key}={value}\n" for key, value in entries.items())
        )
        stats = analyze_source_identical_changes(
            diff_text=diff,
            repo_root=str(tmp_path),
            input_folder=str(resources),
            locale_codes=[locale],
            brand_glossary=["Bisq"],
            source_identical_allowlist={locale: ["Internet"]},
        )
        report = build_quality_gate_report(
            source_stats=stats,
            semantic_stats=None,
            validation_summary={"files": {}, "pipeline_warnings": []},
            changed_files=[target_path],
            input_folder=str(resources),
            config=QualityGateConfig(),
        )
        return stats, report

    controls = {key: source[key] for key in (
        "brand.name", "network.term", "placeholder.value", "markup.break",
        "technical.token",
    )}
    control_stats, control_report = check(controls)
    assert control_stats.unexpected_source_identical_count == 0
    assert control_report["blocking"] is False

    stats, report = check({**controls, **{
        key: source[key] for key in ("KEEP_RECEIPT", "CHECK_PAYMENT")
    }})
    assert stats.new_source_identical_prose_count == 2
    assert {example["key"] for example in stats.examples} == {
        "KEEP_RECEIPT", "CHECK_PAYMENT"
    }
    assert report["blocking"] is True
    assert any("source-identical prose" in reason for reason in report["blocking_reasons"])
