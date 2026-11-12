# Cloud explanations with Groq

This step adds a cloud explanation provider while preserving local Ollama and
the existing troubleshooting rules. It does not publish the app or change your
Neon database. The cloud model selects one explanation from reviewed options.
Every option starts with the exact recommended action and may append one of its
attached guidance cards verbatim. The model returns only an option ID; the server
assembles the displayed text. It cannot update cases, rewrite advice, choose
additional tests, or establish a diagnosis.

## Free account setup

1. Sign in at [Groq API Keys](https://console.groq.com/keys).
2. Keep your organization on the **Free** plan. Do not upgrade to Developer for
   this project. The application cannot select or verify your account's billing
   tier; a key from a paid organization follows that organization's billing.
3. Create an API key named `apex-medic`.
4. Open the project's ignored `.env.local` in your editor. Keep the existing Neon
   variables and add these two settings, replacing the key placeholder privately:

```dotenv
APEX_LLM_PROVIDER=groq
GROQ_API_KEY=replace_this_with_your_actual_key
```

Do not paste the key into chat, source code, frontend files, terminal commands,
screenshots or Git. The server reads the key from this file or from its process
environment. Environment values override file values. If a later Neon CLI action
rewrites `.env.local`, re-add the Groq settings privately before restarting.

Current Groq documentation lists `openai/gpt-oss-20b` on the Free plan and supports
strict structured outputs for it. Free usage is limited, not unlimited hosting.
The documented base limits are 30 requests/minute, 1,000 requests/day, 8,000
tokens/minute and 200,000 tokens/day; account-specific limits can differ. Existing
ApexMedic request limits also apply, but do not guarantee staying below the
provider's token quota. A provider 429 leaves the original guidance available.

References, checked 2026-11-11:
- [Groq rate limits](https://console.groq.com/docs/rate-limits)
- [Structured outputs](https://console.groq.com/docs/structured-outputs)
- [Billing FAQs](https://console.groq.com/docs/billing-faqs)
- [Reasoning parameters](https://console.groq.com/docs/reasoning)

## Check on your PC

Stop Uvicorn with Ctrl+C. From the project directory:

```powershell
# Run from the root of your apex-medic checkout.

.\.venv\Scripts\python.exe -m unittest discover -s tests
if ($LASTEXITCODE -ne 0) { throw "Fix the test failure before continuing." }

.\.venv\Scripts\python.exe -m backend.app.check_model --config-only
if ($LASTEXITCODE -ne 0) { throw "Model settings need correction." }
```

The configuration check does not make an API request and does not print the key.
It should show `Provider: groq`, `Model: openai/gpt-oss-20b`, and
`Configuration: OK`. This verifies settings, not authentication.

Now send **one synthetic test request**:

```powershell
.\.venv\Scripts\python.exe -m backend.app.check_model
if ($LASTEXITCODE -ne 0) { throw "The provider check failed." }
```

Expected: `Provider request: OK`, a selected card ID (or action only), and the
exact recommendation to compare data centres while keeping the game mode and
connection unchanged and recording both names and readings. One reviewed
supporting paragraph may follow. The check uses a synthetic case and the real
rules/knowledge catalogue, with no database access or saved player data.

Restart the existing Uvicorn command and use **Explain this step** on an active
question or test in the UI. Report/review stages deliberately return reviewed
guidance without calling any model. API responses identify the selected provider
and model. `source: model_selected_guidance` means the model chose reviewed text;
`selected_card_id` identifies its supporting card, or is null for action only.
Report stages use `source: reviewed_guidance` with null provider/model fields.

## Evaluate content before public deployment

Run the existing synthetic development and fresh-case suites:

```powershell
.\.venv\Scripts\python.exe -m evaluation.run_explanation_checks
.\.venv\Scripts\python.exe -m evaluation.run_fresh_explanation_checks
```

Cloud evaluations wait 30 seconds between non-report scenarios to reduce bursts.
They never retry failed requests automatically. This pacing does not guarantee
that organization-wide quotas remain available. Reports identify the configured
provider/model and each result's provenance, including the selected card. Review
whether the selected context is useful and whether the underlying guidance is
accurate. Runtime validation preserves the action and restricts all displayed
model-assisted text to the prepared options; it does not prove the catalogue's
accuracy or that the model chose the most helpful option.

These tests do not modify saved cases. Their reports remain under ignored
`evaluation/runs/`. Actual account access and selection behavior require a live
check; automated provider tests use simulated responses. After updating the
selection protocol, one provider check verifies the live request format. The
full suites are useful when changing guidance, selection policy or provider.

## Failure behavior

| Condition | Result |
| --- | --- |
| Missing/invalid server key | Configuration error; no model request |
| Provider rejects authentication or model access | Generic 503; no provider error body exposed |
| Provider quota exhausted | 429 with bounded `Retry-After`; no automatic retry |
| Cloud request exceeds 30 seconds | 504; original recommendation remains available |
| Invalid JSON, unknown option ID, extra prose/fields, truncation, refusal, or tool call | 502; selection rejected |
| Full action exceeds the 80-word/1,000-character option limit | 413 before calling the provider; action is not truncated |
| Recommendation changes during generation | 409; stale explanation discarded |
| Review/report stage | Existing reviewed text, no model call |

The key is sent only to the fixed HTTPS Groq chat-completions endpoint.
Redirects and environment proxies are disabled. Requests contain only the selected
action, reason and prepared options, and responses do not expose reasoning content
or credentials. Options exceeding 80 words or 1,000 characters are omitted;
the complete action must always fit. Both the provider schema and application
validate the selection. Model prose is never used as displayed explanation text.
See [the content review](explanation_review.md) for the failures this addresses
and the remaining limits.

## Local provider and cloud hosting

Set `APEX_LLM_PROVIDER=ollama` to use the existing local Qwen model again. No
cloud request is made in that mode. There is no automatic provider switching.

For cloud hosting, set `APEX_LLM_PROVIDER=groq` and `GROQ_API_KEY` in the
host's secret environment settings. Do not upload `.env.local`. The backend will
then use Groq independently of your laptop. Follow [Render deployment](render_deployment.md) for the public origin, trusted
proxy configuration and browser smoke test.
