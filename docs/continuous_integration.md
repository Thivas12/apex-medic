# Automated checks on GitHub

`.github/workflows/tests.yml` checks pull requests targeting `main`, pushes to
`main`, and manual runs. It does not deploy the application.

## Public repositories

The test job runs automatically for public repositories on a standard
`ubuntu-24.04` runner. It does not need the `APEX_CI_ENABLED` variable. GitHub
currently provides standard hosted runners free for public repositories;
larger runners have different billing. The workflow uses no application secrets,
uploads no artifacts and creates no dependency caches.

Forks may need Actions enabled in their own repository. GitHub can also require
maintainer approval before running a first-time contributor's pull request.
Review the changes before approving a run. Keep the workflow on `pull_request`;
do not use privileged `pull_request_target` execution to run a fork's code.

## Private repositories

Private repositories use the owner's included Actions compute allowance;
additional usage can be billed. The test job is disabled until the repository
variable `APEX_CI_ENABLED` is `true`.

1. Open [GitHub billing](https://github.com/settings/billing), then **Budgets and
   alerts**. Create or edit an **Actions** product-level budget covering this
   repository, set it to **$0**, and enable **Stop usage when budget limit is
   reached**. Save it. An alert-only budget does not stop spending.
2. In the repository, open **Settings > Secrets and variables > Actions >
   Variables > New repository variable**. Set `APEX_CI_ENABLED` to `true`.
   This is a configuration variable, not a secret.
3. For the first installation, merge the workflow pull request into `main`.
   The resulting push starts the checks. If the switch was enabled later,
   open **Actions > ApexMedic tests > Run workflow**, choose `main`, and run it.
4. Open the run and confirm that **Python, PostgreSQL and browser tests** actually
   executed and passed. A skipped job is not evidence that the tests passed,
   even if GitHub allows the pull request to merge.

For private repositories, the switch prevents runner allocation while disabled.
It is not a billing cap:
keep the budget's hard stop enabled. If the included allowance is exhausted,
leave the budget at $0 and use local tests until it resets. Do not increase the
budget to unblock a run. This workflow needs no Neon URL, Groq key, Render key,
or other application secret in GitHub Actions.

The job uses one standard Ubuntu runner, a ten-minute timeout, cancellation of
superseded runs, and no uploaded artifacts or Actions dependency caches. Future
changes to the workflow can affect usage; review them along with code changes.

References: [Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions),
[budgets and hard stops](https://docs.github.com/en/billing/how-tos/set-up-budgets),
[repository variables](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/use-variables).

## Dependency updates

[Dependabot](../.github/dependabot.yml) checks Python dependencies and pinned
GitHub Actions monthly. It opens grouped pull requests; it does not merge them
or deploy. Review the dependency changes and test results before merging.
Dependabot alerts and security updates are separate repository settings.

## What runs

- Python from `.python-version`, matching the Render runtime, with dependencies
  pinned in `requirements.txt`.
- The complete unittest suite against SQLite and a disposable PostgreSQL 18
  service. At introduction, this is 201 tests with zero skipped tests. The job
  fails for test failures, unexpected skips, or an empty suite.
- Syntax checks for both frontend JavaScript files, then the eight Node tests
  for the case-management page. These use a mocked DOM/fetch environment;
  they are not real-browser end-to-end tests.
- Provider tests use mocked responses. No Ollama server or paid/cloud model call
  is required. Live provider evaluations are intentionally not part of CI.

The PostgreSQL tests drop and recreate their test schema. Their URL is hardcoded
to the disposable localhost service and a database named `apexmedic_test_ci`.
Never replace it with a Neon or other production connection string. Test
credentials in the workflow are synthetic and the database disappears when the
job ends.

The workflow has read-only repository permissions, pins action code to commit
SHAs, and does not persist the checkout credential. It uses normal pull-request
events, not privileged `pull_request_target` execution.

## Routine release process

1. Push a feature branch and open a pull request targeting `main`.
2. Inspect the actual check run, resolve failures, and review the diff.
3. Merge the pull request; confirm the `main` check run passes.
4. Manually deploy that tested commit in Render, then check `/health` and the UI.

Keep Render auto-deploy disabled for this process. This workflow alone does not
prevent merges or deployments; it provides test results for the release decision.
If a run fails, inspect the first failing step under **Actions** before changing
application code. For a PostgreSQL failure, check that the service started and
the integration tests ran. For an Actions quota error, keep the spending limit
unchanged.
