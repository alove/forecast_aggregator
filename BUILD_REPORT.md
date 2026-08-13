# f_collector v1.4.1 build report

## Purpose

Extends the GitHub-canonical collection/deployment workflow with a no-vendor test/recovery path while preserving the existing immutable ECS PostgreSQL architecture.

## v1.4.1 test/recovery controls

- `run --skip-collect` performs all GitHub synchronization, CSV hash/history checks, Git push verification, AWS freshness checks, and final repository cleanup without invoking any vendor adapter.
- When `--skip-collect` finds no CSV diff, an empty Git commit is intentionally not created; `git push` is still executed so remote authentication/pushability is exercised and failure remains loud.
- `run --skip-collect --force-deploy` additionally forces a fresh PostgreSQL image build, ECR push, ECS deployment, smoke test, and deployment-metadata verification even when the canonical CSV fingerprint is unchanged.
- The original dirty-worktree, fast-forward-only, append-only history, stable-secret, and GitHub-before-AWS guarantees are unchanged.

## New workflow

`sync_forecast_database.sh` now coordinates the routine run:

1. Requires the `f_collector` repository root, configured branch, and a completely clean Git worktree/index.
2. Acquires an atomic lock under `.git/` so scheduled runs cannot overlap.
3. Fetches the configured remote (`origin/main` by default) and only permits a fast-forward local update. A local-ahead or diverged branch fails loudly.
4. Requires both canonical CSVs to be Git tracked.
5. Materializes the exact remote CSV bytes with `git show` and then runs the collector locally.
6. Validates the resulting CSVs and enforces strict append-only history: every GitHub row must remain in the same order at the start of the candidate file.
7. Computes SHA-256 for both CSVs and a combined fingerprint incorporating export schema version.
8. Determines the distinct vendors represented in newly appended rows.
9. If data changed, stages with `git add --all`, permits only the two canonical CSV paths, commits the run, and pushes before AWS deployment.
10. If the push fails, the unpushed collector commit is discarded and the local branch is restored to the fetched remote state.
11. Deploys only when the canonical data fingerprint differs from AWS, when the deployer version differs, or when `--force-deploy` is supplied.
12. Rebuilds the existing immutable PostgreSQL image/service using the exact committed data and existing CloudFormation secret resources.
13. Runs a read-only database smoke test. If local `psql` is absent, the lifecycle script uses the official PostgreSQL Docker client.
14. Verifies AWS deployment provenance after deployment and leaves the local Git repository clean and synchronized with the remote.
15. Removes temporary remote CSV materializations, synchronization locks, and duplicate CSVs from the Docker build context.

## GitHub source of truth

`.gitignore` now tracks only:

- `collected_data/election_forecasts_2026_national.csv`
- `collected_data/election_forecasts_2026_state.csv`

Other files under `collected_data/`, including raw snapshots, remain ignored.

## ECS deployment provenance

Each deployed task now records these environment values and CloudFormation outputs:

- `FORECAST_DATA_GIT_SHA`
- `FORECAST_DATA_FINGERPRINT`
- `FORECAST_SCHEMA_VERSION`
- `FORECAST_DEPLOYER_VERSION`
- `FORECAST_DEPLOYED_AT`
- `FORECAST_NATIONAL_SHA256`
- `FORECAST_STATE_SHA256`

Data freshness is based on the CSV fingerprint plus deployer version; an unrelated Git documentation commit therefore does not force a database rebuild. The exact Git SHA is retained for provenance.

## Credential behavior

Ordinary rebuild/update deployments reuse the same CloudFormation stack and the same `AdminPasswordSecret` / `ReaderPasswordSecret` logical resources. The sync runner does not tear down or recreate these resources, so routine data refreshes do not intentionally rotate the Rhubarb reader credentials or fixed connection-secret path.

## Validation completed

- Python unit/integration suite: **47/47 PASS**.
- Includes temporary bare-Git remote integration tests exercising fetch, append-only collection, commit/push ordering, deployment metadata, idempotent reruns, clean final repository state, and `--skip-collect --force-deploy` without vendor calls.
- Dirty-worktree test confirms the collector is never run when unexplained local changes exist.
- History-guard test confirms modification/reordering of prior GitHub CSV rows is rejected.
- Bash syntax validation passes for lifecycle, sync, wrappers, local loader, and PostgreSQL init scripts.
- Python compilation passes for collector, source adapters, database preparer, and Git history guard.
- CloudFormation YAML parses successfully with 12 resources and 19 outputs; all seven forecast provenance environment variables are present on the task definition.
- Existing sample database preparation remains valid: 48 national rows and 8,460 state/district rows.

A real AWS deployment was not performed in the build environment because it does not have the user's AWS credentials or local Docker daemon. The existing ECS lifecycle remains the deployment mechanism and is exercised by the repository tests plus the Git/AWS orchestration stubs.
