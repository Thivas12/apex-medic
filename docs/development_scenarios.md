# Scenario coverage

The scenario suites exercise a bounded connection-troubleshooting policy using
synthetic cases. Their inputs are not observed player outcomes.

| Implemented scenario | Expected behavior |
| --- | --- |
| Symptom unknown | Ask whether the player observes latency, packet loss or rubber-banding. |
| Required readings absent | Request Performance Display measurements during an affected match. |
| Latest packet-loss reading is zero | Ask whether symptoms persist and request a reading during the symptom. |
| Relevant readings available | Propose the first unrecorded supported comparison. |
| A comparison is unchanged | Continue to the next supported comparison. |
| A comparison reportedly improves symptoms | Request comparable-session verification against the original baseline. |
| A comparison worsens or is inconclusive | Ask what changed and request before-and-after readings. |
| Verification recorded | Ask for a review; do not infer a cause or automatically resolve the case. |
| Supported comparisons exhausted | Summarise evidence and uncertainty. |
| An unsupported procedure was reported | Stop and request review before further changes. |
| Case already inactive | Review the stored case without proposing new tests. |

See [policy tests](../tests/test_next_step.py) and
[evaluation instructions](../evaluation/README.md).

## Exploratory ideas, not implemented behavior

The initial project notes considered cross-game symptom comparisons,
connection-specific alternatives, requiring a data-centre name before every
step, and automatic detection of guidance that conflicts with newer sources.
The current policy does not implement those branches. The source catalogue is
manually reviewed; it does not monitor websites for updates.
