# Public demo on Render Free

Use one Python web service for both the UI and API. Neon keeps cases, ownership,
history and counters; Groq selects reviewed supporting guidance. Once deployed,
your laptop does not need to run Uvicorn or Ollama.

This is a limited public demo. Render Free sleeps after 15 idle minutes and a
subsequent request can take about a minute to wake it. The workspace shares 750
free instance hours per month. Bandwidth, build minutes and outbound API/database
traffic are also limited. Keep Render, Neon and Groq on their free plans. With no
payment method, Render suspends service/builds when the relevant included usage
is exhausted instead of charging overages. If signup requires a payment method,
stop there and reassess the hosting choice before entering payment details.

## 1. Select a verified commit

PostgreSQL, Groq, hosting and CI support are on `main`. Deploy a reviewed commit
with a successful CI run. Keep automatic deployment off until the release
checks and browser walkthrough are complete.

```powershell
# Run from the root of your apex-medic checkout.
git fetch origin
if ($LASTEXITCODE -ne 0) { throw "Fetch failed." }

git switch main
if ($LASTEXITCODE -ne 0) { throw "Branch switch failed. Stop here." }

git pull --ff-only origin main
if ($LASTEXITCODE -ne 0) { throw "Pull failed. Stop here." }

.\.venv\Scripts\python.exe -m unittest discover -s tests
if ($LASTEXITCODE -ne 0) { throw "Fix the test failure before deploying." }

.\.venv\Scripts\python.exe -m backend.app.db.manage check
if ($LASTEXITCODE -ne 0) { throw "The database check failed." }
```

Expect `PostgreSQL connection: OK` and `Schema: current`. The 21 dedicated
PostgreSQL integration tests are skipped without an explicit disposable test
database; never point `APEX_TEST_POSTGRES_URL` at your Neon production database.
For a new database, complete the explicit migration in [PostgreSQL setup](postgres_setup.md)
first. Runtime checks its version on startup; it never migrates automatically
or falls back to SQLite. Future migrations remain explicit admin commands.

## 2. Create the Render service

1. Open [Render](https://dashboard.render.com/), sign in, and finish GitHub
   authorization in your own browser. Grant access to `Thivas12/apex-medic`.
2. Choose **New > Web Service** and select that repository.
3. Apply these settings:

| Setting | Value |
| --- | --- |
| Name | `apex-medic` (Render may add a suffix to the public address) |
| Branch | `main` |
| Language/runtime | Python 3 |
| Root directory | Leave blank |
| Build command | `python -m pip install -r requirements.txt` |
| Start command | `python -m backend.app.serve` |
| Instance type | Free |
| Health check path | `/health` |
| Auto deploy | Off for this initial demo |

The repository pins Python 3.12.14, the version used for the server-side tests.
Your Windows Python 3.14 environment can remain unchanged. Pick the closest
available Render region to the existing Neon database when practical.

Do not add a Render database, persistent disk, background worker or paid service.
The single web service uses your existing Neon database.

## 3. Set server environment variables privately

In Render's environment section, add:

| Name | Value |
| --- | --- |
| `APEX_DATABASE_BACKEND` | `postgres` |
| `APEX_LLM_PROVIDER` | `groq` |
| `DATABASE_URL` | Copy the **pooled** Neon value from the project's `.env.local` |
| `GROQ_API_KEY` | Copy the existing Groq key from that file |

Open `.env.local` in your local editor. Copy each secret value only, without
surrounding quotes or the `NAME=` prefix. Do not paste the file into chat, source
code, build/start commands, Git, or screenshots. The runtime does not need
`DATABASE_URL_UNPOOLED`, the Neon CLI token or any OpenAI API key.

Leave `APEX_PUBLIC_ORIGIN` unset for the first deploy. The app derives it from
Render's `RENDER_EXTERNAL_URL`, so the exact generated HTTPS address is used for
browser write checks and Secure cookies. Keep frontend and API on that same
address. A later custom domain requires an explicit canonical HTTPS origin.

## 4. Deploy and check the public site

Click **Deploy Web Service**. A successful startup ends with application startup
complete and the service becomes Live. Open the exact HTTPS URL shown by Render.
The root redirects to `/ui/`; `/health` returns `{"status":"ok"}`.

The health endpoint checks the running process. Database schema/connectivity is
checked at startup; health polling does not continually wake Neon or call Groq.
An outage after startup can still make case operations return 503 while health
is 200. Test the case flow as well as health.

Create a **new synthetic case on the public site**. Existing localhost browser
cookies do not transfer to the public domain, even if the database contains the
old case. Do not change production ownership records to make imported cases
publicly claimable.

Verify:

1. Create a synthetic case, classify packet loss, and record synthetic readings
   such as 55 ms latency, 8% packet loss and 144 FPS.
2. Confirm the next step is the data-centre comparison. Click **Explain this
   step** once; its first paragraph must preserve the recommendation exactly.
3. Keep the case ID locally. Refresh and confirm the reading/history remain.
4. Open the same case ID in a private browser window: it must be inaccessible.
5. Use Render's restart control, or manually redeploy the same commit. In the
   original browser, reopen the saved case after startup and confirm its history
   remains. This tests hosted persistence and session ownership together.
6. Stop local Uvicorn and Ollama. Refresh the public site and confirm it still
   works. Use **Manage or delete a saved case** to remove the synthetic case when
   finished.

Send only the public site URL and a short result summary. If startup fails,
share the final error lines after removing any credentials. Keep complete
environment screens, connection strings, cookies and private case IDs out of chat.

## Hosting boundary

`backend.app.serve` runs one Uvicorn worker on `0.0.0.0:$PORT`, with reload off,
bounded concurrency, and access logging disabled. One worker preserves the
existing in-process model concurrency control. Generic proxy-header parsing is
explicitly disabled, including Render's default `FORWARDED_ALLOW_IPS=*`.

The Render-only ingress adapter uses the single `CF-Connecting-IP` value that
Render documents as overwritten by Cloudflare. It ignores `X-Forwarded-For`,
`Forwarded`, `X-Real-IP`, and caller-supplied protocol headers. It validates the
public host, marks the edge request HTTPS, and supplies the normalized client
address to the existing rate limiter. Missing/ambiguous edge identity is rejected
before case operations. Internal GET/HEAD `/health` probes are exempt and cannot
access case data.

This trust comes from Render's network boundary, not from an HTTP header being
inherently trustworthy. Use this adapter only for the documented Render Free web
service, whose public requests traverse Cloudflare and which cannot receive
private-network traffic. Do not expose this startup profile directly to the
internet, put another worker/proxy in front of it, or enable it on another host.
Those changes need a separate ingress review. Local development keeps the
existing Uvicorn command with `--no-proxy-headers` and no Render adapter.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Hosting configuration error | Required server variables and Python start command; no `.env.local` upload is needed |
| Database startup fails | Pooled Neon URL, TLS parameters, and local `db.manage check` showing current schema |
| Page loads, writes get 403 | Use the exact Render HTTPS URL; remove an old localhost `APEX_PUBLIC_ORIGIN` setting |
| Hosting-edge 503 | Check deployment/network configuration; do not bypass the edge check or trust arbitrary forwarding headers |
| API/provider 429 | Respect the displayed delay; free quotas are shared and there are no automatic provider retries |
| First visit is slow | Allow the Free service to wake; avoid repeated deploys or retry loops |
| Old case gives 404 | Use the original browser and domain; knowing a case ID alone grants no access |

## Sources checked 2026-11-11

- [Render Free limitations](https://render.com/docs/free)
- [FastAPI deployment](https://render.com/docs/deploy-fastapi)
- [Python versions](https://render.com/docs/python-version)
- [Render environment variables](https://render.com/docs/environment-variables)
- [Render's documented Cloudflare client-IP behavior](https://render.com/articles/host-pocketbase-on-render)
- [Render health checks](https://render.com/docs/health-checks)
- [Uvicorn proxy trust](https://uvicorn.dev/settings/)
