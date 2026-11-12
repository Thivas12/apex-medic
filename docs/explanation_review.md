# Explanation content review — 2026-11-11

Two live Groq runs returned structurally valid output for all 16 synthetic
scenarios. Twelve results used the model and four used the deterministic report
path. Structural success did not establish content correctness.

| Scenario | Observed issue |
| --- | --- |
| Missing packet-loss reading | Internal field text appeared as `packet_loss_pctdata`. |
| Zero packet loss | The explanation said "Ask the player" instead of addressing the player. |
| Recorded packet loss | The data-centre comparison was described as locating the issue, which its evidence does not establish. |
| Reported improvement, including background traffic | "Current settings that led to improvement" assumed current settings and a causal link. |
| Unchanged comparison | "Isolates background traffic" overstated what a single comparison establishes, despite a later caveat. |

The reviewed run directories were `20261111T094447_750508Z` and
`20261111T094753_803093Z`. Full reports remain local under ignored
`evaluation/runs/`; they are not included in this repository.

## Selection contract

1. The deterministic policy selects the action; the knowledge layer attaches
   only the cards mapped to that action.
2. The server builds options containing the exact action alone, or the exact
   action followed by one attached card's exact text. Options must fit 80 words
   and 1,000 characters. An oversized action is rejected, never shortened.
3. The model selects an option ID using a dynamically constrained JSON schema.
   It receives the action, limited reason, and options, not the full case history.
4. The application independently validates the ID and retrieves its text from
   the server's options. Free-form explanation text and extra fields are rejected.
5. Report stages return the existing action and reason without a model call.
   Existing ownership, concurrency and stale-decision checks still apply.

`model_selected_guidance` identifies model-assisted selection. It replaces the
old `model` response label, because the model no longer writes the displayed
wording. `selected_card_id` is null when only the action is selected. Prompt
version `select-guidance-v1` distinguishes this protocol from the earlier
free-form paraphrasing runs.

This intentionally trades flexible phrasing for faithful action wording. The
model chooses context; it does not infer a diagnosis or select troubleshooting
actions. A returned option can be less helpful than another valid option.
Catalogue errors remain possible, so sources and action mappings still require
review. This change does not establish public-deployment readiness by itself.

## Regression coverage

Automated tests exercise every permitted option across both synthetic suites
and both provider adapters. They verify exact action/card preservation, word
limits, unchanged decisions, rejection of the observed bad paraphrases,
rejection of unrelated option IDs, and preservation of verification conditions
and the required test ID. Provider tests simulate network responses; they do
not claim a new live Groq evaluation has passed.
