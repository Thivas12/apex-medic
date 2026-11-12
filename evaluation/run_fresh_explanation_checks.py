import asyncio

from evaluation.run_explanation_checks import main, reading, result


FRESH_SCENARIOS = [
    (
        "latency_reading_missing",
        "collect_performance_readings",
        {
            "symptom": "high_latency",
            "player_description": "Synthetic demo: high ping is reported.",
            "observations": [
                {**reading(0), "latency_ms": None},
            ],
        },
    ),
    (
        "rubber_banding_readings_missing",
        "collect_performance_readings",
        {
            "symptom": "rubber_banding",
            "observations": [
                {
                    **reading(0),
                    "latency_ms": None,
                    "packet_loss_pct": None,
                },
            ],
        },
    ),
    (
        "background_comparison_improved",
        "verify_improvement",
        {
            "completed_tests": [
                result("compare_data_centers", "unchanged"),
                result("compare_background_traffic", "improved"),
            ],
        },
    ),
    (
        "background_comparison_worsened",
        "clarify_test_result",
        {
            "completed_tests": [
                result("compare_data_centers", "unchanged"),
                result("compare_background_traffic", "worsened"),
            ],
        },
    ),
    (
        "background_comparison_inconclusive",
        "clarify_test_result",
        {
            "completed_tests": [
                result("compare_data_centers", "unchanged"),
                result("compare_background_traffic", "inconclusive"),
            ],
        },
    ),
    (
        "verification_worsened",
        "review_verification",
        {
            "completed_tests": [
                result("compare_data_centers", "improved"),
                result("verify_improvement", "worsened"),
            ],
        },
    ),
    (
        "comparisons_exhausted",
        "summarise_unresolved",
        {
            "completed_tests": [
                result("compare_data_centers", "unchanged"),
                result("compare_background_traffic", "unchanged"),
            ],
        },
    ),
    (
        "unsupported_test_reported_improved",
        "review_unsupported_test",
        {
            "completed_tests": [
                result("unreviewed_tweak", "improved"),
            ],
        },
    ),
]


if __name__ == "__main__":
    raise SystemExit(
        asyncio.run(
            main(
                scenarios=FRESH_SCENARIOS,
                suite="fresh-v1",
            )
        )
    )