# Evaluation

There are two synthetic scenario suites: eight development cases in
[run_explanation_checks.py](run_explanation_checks.py) and eight additional
cases in [run_fresh_explanation_checks.py](run_fresh_explanation_checks.py).
Both have been used during development; neither is a held-out benchmark.

## Automated checks

The test suite covers policy actions, source attribution, exact wording
preservation, allowed option IDs and provider failures. Provider responses are
mocked. Run it before a live evaluation:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

The relevant tests are `test_next_step.py`, `test_knowledge.py`,
`test_explanation_selection.py`, `test_explanations.py` and `test_groq_provider.py`.

## Live checks

Configure Ollama or Groq, then run:

```powershell
.\.venv\Scripts\python.exe -m evaluation.run_explanation_checks
.\.venv\Scripts\python.exe -m evaluation.run_fresh_explanation_checks
```

These commands use provider capacity. Cloud requests are paced and failures are
not retried automatically. Reports are saved under ignored `evaluation/runs/`,
including the input, decision, output, provider, model, prompt version, knowledge
revision and elapsed time.

Each report has review fields to complete manually. Check that the action and
conditions are preserved, the selected card helps explain this step, and the
underlying guidance is still accurate. An allowed selection can be redundant;
validation alone does not measure usefulness.

Elapsed time includes network, model loading and cache effects. Report steps
skip inference. Do not combine their timings with model latency or use these
small synthetic suites to estimate player resolution rates.

## Earlier results

The [archived Qwen report](reports/fresh-v1-explain-v3.json) uses the retired
`explain-v3` paraphrasing protocol. Its review fields are incomplete.

The [Groq content review](../docs/explanation_review.md) describes 16 later
outputs that passed structural validation but included changes to conditions
and unsupported causal wording. Full reports from those runs remain local.
These findings led to `select-guidance-v1`: the model returns an option ID and
the server supplies the reviewed text.

The current tests enforce that contract. They do not establish whether model
selection is more useful than a fixed card-selection rule. A next evaluation
should compare those approaches using the same cases and blind usefulness
ratings.
