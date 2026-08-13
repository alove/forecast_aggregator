#!/usr/bin/env bash
# GitHub-canonical forecast collection + immutable ECS PostgreSQL deployment.
#
# Invariants:
#   * origin/main (configurable) is the canonical historical record.
#   * any unexplained local Git change aborts the run before collection.
#   * forecast CSV history may only grow by appended rows.
#   * a successful data change is committed and pushed before AWS deployment.
#   * the ECS task records the exact Git SHA and CSV fingerprints it serves.
#   * a failed deployment leaves GitHub canonical and is retried next run.
set -Eeuo pipefail
export AWS_PAGER=""

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
DATA_DIR="$SCRIPT_DIR/collected_data"
NATIONAL_REL="collected_data/election_forecasts_2026_national.csv"
STATE_REL="collected_data/election_forecasts_2026_state.csv"
NATIONAL_CSV="$SCRIPT_DIR/$NATIONAL_REL"
STATE_CSV="$SCRIPT_DIR/$STATE_REL"
HISTORY_GUARD="$SCRIPT_DIR/forecast_database_ecs/git_history_guard.py"
DEPLOYER="$SCRIPT_DIR/election_forecasts_ecs.sh"
COLLECTOR="$SCRIPT_DIR/run.sh"

ACTION="run"
PIPELINE_VERSION="1.4.1"
REMOTE="${FORECAST_GIT_REMOTE:-origin}"
BRANCH="${FORECAST_GIT_BRANCH:-main}"
FORCE_DEPLOY=false
SKIP_COLLECT=false
SAVE_RAW=false
COLLECT_SOURCES=()
LOCK_DIR=""
TEMP_DIR=""
COLLECTION_STARTED=false
COLLECTION_COMMITTED=false

RED="$(printf '\033[31m')"
GRN="$(printf '\033[32m')"
YLW="$(printf '\033[33m')"
BLU="$(printf '\033[34m')"
RST="$(printf '\033[0m')"
ts() { date '+%Y-%m-%d %H:%M:%S'; }
log() { printf '%s[%s]%s %s\n' "$BLU" "$(ts)" "$RST" "$*"; }
ok() { printf '%s✅ %s%s\n' "$GRN" "$*" "$RST"; }
warn() { printf '%s⚠️  %s%s\n' "$YLW" "$*" "$RST"; }
fail() { printf '%s❌ %s%s\n' "$RED" "$*" "$RST" >&2; }

usage() {
  cat <<EOF
GitHub-canonical 2026 forecast collection/deployment runner

Usage:
  ./sync_forecast_database.sh [run] [options]
  ./sync_forecast_database.sh deploy [options]
  ./sync_forecast_database.sh status [options]
  ./sync_forecast_database.sh unlock

Actions:
  run       Default. Fast-forward from GitHub, collect forecasts, validate append-only
            history, commit/push changes, then deploy GitHub HEAD if AWS is stale.
  deploy    Do not poll vendors. Fast-forward to GitHub and deploy the current canonical
            commit when AWS is stale (or always with --force-deploy).
  status    Fetch GitHub and report local, GitHub, and deployed Git SHAs. Makes no commit
            and does not deploy. Fails if the worktree is dirty or local has diverged.
  unlock    Remove a stale synchronization lock after confirming its recorded PID is not running.

Options:
  --source SLUG       Limit collection to one adapter; repeat for multiple adapters.
  --save-raw          Also preserve raw vendor payloads under collected_data/raw_snapshots/.
  --skip-collect      Do not contact forecast vendors. Compare existing canonical CSVs, verify
                      the Git push path, and deploy if AWS is stale.
  --force-deploy      Rebuild/redeploy even when AWS already serves the current data fingerprint.
                      Combine with --skip-collect to exercise Git/ECR/ECS without polling sites.
  --remote NAME       Git remote to use (default: $REMOTE).
  --branch NAME       Git branch to use (default: $BRANCH).
  -h, --help          Show this help.

Required normal state:
  * this directory is the root of a Git repository;
  * the current branch is the configured branch;
  * the worktree/index are clean before the run;
  * the configured remote/branch exists and is pushable;
  * the two collected_data CSVs are tracked by Git.
EOF
}

need() {
  command -v "$1" >/dev/null 2>&1 || { fail "Missing required command: $1"; exit 127; }
}

parse_args() {
  if [ "$#" -gt 0 ]; then
    case "$1" in
      run|deploy|status|unlock|help|-h|--help)
        ACTION="$1"
        shift
        ;;
    esac
  fi
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --source)
        [ "$#" -ge 2 ] || { fail "--source requires a value"; exit 2; }
        COLLECT_SOURCES+=("$2")
        shift 2
        ;;
      --save-raw) SAVE_RAW=true; shift ;;
      --skip-collect) SKIP_COLLECT=true; shift ;;
      --force-deploy) FORCE_DEPLOY=true; shift ;;
      --remote)
        [ "$#" -ge 2 ] || { fail "--remote requires a value"; exit 2; }
        REMOTE="$2"; shift 2
        ;;
      --branch)
        [ "$#" -ge 2 ] || { fail "--branch requires a value"; exit 2; }
        BRANCH="$2"; shift 2
        ;;
      -h|--help) ACTION="help"; shift ;;
      *) fail "Unknown option: $1"; usage; exit 2 ;;
    esac
  done
}

repo_git_dir() {
  git -C "$SCRIPT_DIR" rev-parse --git-dir 2>/dev/null
}

acquire_lock() {
  local git_dir
  git_dir="$(repo_git_dir)" || { fail "$SCRIPT_DIR is not a Git repository"; exit 2; }
  if [[ "$git_dir" != /* ]]; then
    git_dir="$SCRIPT_DIR/$git_dir"
  fi
  LOCK_DIR="$git_dir/forecast-sync.lock.d"
  if mkdir "$LOCK_DIR" 2>/dev/null; then
    printf '%s\n' "$$" > "$LOCK_DIR/pid"
    return 0
  fi
  local pid="unknown"
  [ -f "$LOCK_DIR/pid" ] && pid="$(cat "$LOCK_DIR/pid" 2>/dev/null || printf unknown)"
  fail "Another forecast sync appears to be running (lock: $LOCK_DIR, pid: $pid)."
  fail "If that process is gone, run: ./sync_forecast_database.sh unlock"
  exit 3
}

unlock_stale() {
  local git_dir lock pid=""
  git_dir="$(repo_git_dir)" || { fail "$SCRIPT_DIR is not a Git repository"; exit 2; }
  [[ "$git_dir" == /* ]] || git_dir="$SCRIPT_DIR/$git_dir"
  lock="$git_dir/forecast-sync.lock.d"
  if [ ! -d "$lock" ]; then
    ok "No synchronization lock exists"
    return 0
  fi
  [ -f "$lock/pid" ] && pid="$(cat "$lock/pid" 2>/dev/null || true)"
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    fail "Refusing to unlock: process $pid is still running"
    exit 3
  fi
  rm -rf "$lock"
  ok "Removed stale synchronization lock"
}

cleanup() {
  local rc=$?
  if [ "$COLLECTION_STARTED" = "true" ] && [ "$COLLECTION_COMMITTED" != "true" ]; then
    restore_candidate_csvs >/dev/null 2>&1 || true
  fi
  if [ -n "$TEMP_DIR" ] && [ -d "$TEMP_DIR" ]; then
    rm -rf "$TEMP_DIR"
  fi
  # Docker build-context data is disposable; the tracked collected_data CSVs are canonical.
  rm -f "$SCRIPT_DIR/forecast_database_ecs/image/data/election_forecasts_2026_national.csv" \
        "$SCRIPT_DIR/forecast_database_ecs/image/data/election_forecasts_2026_national.csv.part" \
        "$SCRIPT_DIR/forecast_database_ecs/image/data/election_forecasts_2026_state.csv" \
        "$SCRIPT_DIR/forecast_database_ecs/image/data/election_forecasts_2026_state.csv.part" 2>/dev/null || true
  if [ -n "$LOCK_DIR" ] && [ -d "$LOCK_DIR" ]; then
    rm -rf "$LOCK_DIR"
  fi
  exit "$rc"
}

assert_repo_root_and_branch() {
  local root branch remote_url
  root="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel 2>/dev/null)" || {
    fail "$SCRIPT_DIR is not inside a Git repository"; exit 2;
  }
  [ "$root" = "$SCRIPT_DIR" ] || {
    fail "f_collector must be the Git repository root. Git reports: $root"; exit 2;
  }
  branch="$(git -C "$SCRIPT_DIR" symbolic-ref --quiet --short HEAD 2>/dev/null || true)"
  [ "$branch" = "$BRANCH" ] || {
    fail "Expected branch '$BRANCH' but current branch is '${branch:-DETACHED}'"; exit 2;
  }
  remote_url="$(git -C "$SCRIPT_DIR" remote get-url "$REMOTE" 2>/dev/null || true)"
  [ -n "$remote_url" ] || { fail "Git remote '$REMOTE' is not configured"; exit 2; }
}

assert_clean() {
  local status
  status="$(git -C "$SCRIPT_DIR" status --porcelain=v1 --untracked-files=all)"
  if [ -n "$status" ]; then
    fail "Unexplained local Git changes detected. Nothing was fetched, collected, committed, or deployed."
    printf '%s\n' "$status" >&2
    exit 4
  fi
}

assert_expected_collection_changes() {
  local line code path bad=0
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    code="${line:0:2}"
    path="${line:3}"
    # Rename/copy porcelain records are intentionally unsupported here.
    case "$path" in
      "$NATIONAL_REL"|"$STATE_REL") ;;
      *)
        fail "Collector produced an unexpected Git change: $code $path"
        bad=1
        ;;
    esac
  done < <(git -C "$SCRIPT_DIR" status --porcelain=v1 --untracked-files=all)
  [ "$bad" -eq 0 ] || exit 4
}

restore_candidate_csvs() {
  git -C "$SCRIPT_DIR" reset --quiet HEAD -- "$NATIONAL_REL" "$STATE_REL" 2>/dev/null || true
  git -C "$SCRIPT_DIR" restore --source=HEAD --worktree -- "$NATIONAL_REL" "$STATE_REL" 2>/dev/null || true
}

fetch_and_fast_forward() {
  log "Fetching $REMOTE/$BRANCH"
  git -C "$SCRIPT_DIR" fetch --prune "$REMOTE" "$BRANCH"
  git -C "$SCRIPT_DIR" show-ref --verify --quiet "refs/remotes/$REMOTE/$BRANCH" || {
    fail "Remote branch does not exist: $REMOTE/$BRANCH"; exit 5;
  }

  local counts ahead behind
  counts="$(git -C "$SCRIPT_DIR" rev-list --left-right --count "HEAD...$REMOTE/$BRANCH")"
  ahead="${counts%%[[:space:]]*}"
  behind="${counts##*[[:space:]]}"
  if [ "$ahead" -gt 0 ]; then
    fail "Local $BRANCH is $ahead commit(s) ahead of $REMOTE/$BRANCH. Refusing to guess which history is canonical."
    exit 5
  fi
  if [ "$behind" -gt 0 ]; then
    log "Fast-forwarding local $BRANCH by $behind commit(s)"
    git -C "$SCRIPT_DIR" merge --ff-only "$REMOTE/$BRANCH"
  fi
  [ "$(git -C "$SCRIPT_DIR" rev-parse HEAD)" = "$(git -C "$SCRIPT_DIR" rev-parse "$REMOTE/$BRANCH")" ] || {
    fail "Local and remote branch are not identical after fast-forward"; exit 5;
  }
  assert_clean
}

require_tracked_csvs() {
  local path
  for path in "$NATIONAL_REL" "$STATE_REL"; do
    git -C "$SCRIPT_DIR" ls-files --error-unmatch "$path" >/dev/null 2>&1 || {
      fail "$path is not tracked by Git. Commit the canonical CSVs to $REMOTE/$BRANCH first."
      exit 6
    }
    [ -s "$SCRIPT_DIR/$path" ] || {
      fail "Tracked CSV is missing or empty: $path"
      exit 6
    }
  done
}

materialize_remote_csvs() {
  mkdir -p "$HOME/.cache/f_collector/tmp"
  TEMP_DIR="$(mktemp -d "$HOME/.cache/f_collector/tmp/forecast-sync.XXXXXX")"

  git -C "$SCRIPT_DIR" show "$REMOTE/$BRANCH:$NATIONAL_REL" \
    > "$TEMP_DIR/remote_national.csv" || {
      fail "Cannot read $NATIONAL_REL from $REMOTE/$BRANCH"
      exit 6
    }

  git -C "$SCRIPT_DIR" show "$REMOTE/$BRANCH:$STATE_REL" \
    > "$TEMP_DIR/remote_state.csv" || {
      fail "Cannot read $STATE_REL from $REMOTE/$BRANCH"
      exit 6
    }
}

run_collector() {
  local -a args
  args=("$COLLECTOR" collect --output-dir "$DATA_DIR")
  if [ "$SAVE_RAW" = "true" ]; then args+=(--save-raw); fi
  local source
  for source in "${COLLECT_SOURCES[@]}"; do args+=(--source "$source"); done
  COLLECTION_STARTED=true
  log "Polling forecast vendors"
  if ! "${args[@]}"; then
    fail "Collector failed; restoring tracked CSVs to the pre-run GitHub state"
    restore_candidate_csvs
    assert_clean
    exit 7
  fi
  assert_expected_collection_changes
  "$DEPLOYER" validate
}

comparison_lines() {
  python3 "$HISTORY_GUARD" \
    --remote-national "$TEMP_DIR/remote_national.csv" \
    --remote-state "$TEMP_DIR/remote_state.csv" \
    --local-national "$NATIONAL_CSV" \
    --local-state "$STATE_CSV" \
    --format lines
}

load_comparison() {
  local line key value comparison_output
  if ! comparison_output="$(comparison_lines)"; then
    fail "GitHub/local history comparison failed; refusing to commit or deploy"
    exit 8
  fi
  CHANGED="False"
  VENDORS=""
  VENDOR_COUNT="0"
  NATIONAL_ADDED_ROWS="0"
  STATE_ADDED_ROWS="0"
  LOCAL_NATIONAL_SHA256=""
  LOCAL_STATE_SHA256=""
  LOCAL_FINGERPRINT=""
  SCHEMA_VERSION=""
  while IFS= read -r line; do
    key="${line%%=*}"
    value="${line#*=}"
    case "$key" in
      changed) CHANGED="$value" ;;
      vendors) VENDORS="$value" ;;
      vendor_count) VENDOR_COUNT="$value" ;;
      national_added_rows) NATIONAL_ADDED_ROWS="$value" ;;
      state_added_rows) STATE_ADDED_ROWS="$value" ;;
      local_national_sha256) LOCAL_NATIONAL_SHA256="$value" ;;
      local_state_sha256) LOCAL_STATE_SHA256="$value" ;;
      local_fingerprint) LOCAL_FINGERPRINT="$value" ;;
      schema_version) SCHEMA_VERSION="$value" ;;
    esac
  done <<< "$comparison_output"
  [ -n "$LOCAL_NATIONAL_SHA256" ] && [ -n "$LOCAL_STATE_SHA256" ] && [ -n "$LOCAL_FINGERPRINT" ] || {
    fail "Unable to calculate canonical CSV fingerprints"; exit 8;
  }
}

commit_and_push() {
  local run_time subject body remote_before remote_after
  run_time="$(date '+%Y-%m-%d %H:%M:%S %Z')"
  subject="run as of $run_time: updates from $VENDOR_COUNT vendors."
  printf -v body 'Vendors: %s\nNational rows appended: %s\nState/district rows appended: %s' \
    "${VENDORS:-none}" "$NATIONAL_ADDED_ROWS" "$STATE_ADDED_ROWS"

  # The pre-collection guard and post-collection allow-list make git add --all safe here.
  git -C "$SCRIPT_DIR" add --all
  local staged bad=0
  staged="$(git -C "$SCRIPT_DIR" diff --cached --name-only)"
  while IFS= read -r path; do
    [ -n "$path" ] || continue
    case "$path" in
      "$NATIONAL_REL"|"$STATE_REL") ;;
      *) fail "Unexpected staged path before commit: $path"; bad=1 ;;
    esac
  done <<< "$staged"
  if [ "$bad" -ne 0 ]; then
    git -C "$SCRIPT_DIR" reset --quiet HEAD -- .
    restore_candidate_csvs
    exit 4
  fi
  [ -n "$staged" ] || { fail "SHA comparison reported changes but Git has nothing to commit"; exit 8; }

  remote_before="$(git -C "$SCRIPT_DIR" rev-parse "$REMOTE/$BRANCH")"
  if ! git -C "$SCRIPT_DIR" commit -m "$subject" -m "$body"; then
    fail "Git commit failed; restoring the worktree"
    git -C "$SCRIPT_DIR" reset --hard "$remote_before" >/dev/null
    exit 9
  fi
  if ! git -C "$SCRIPT_DIR" push "$REMOTE" "HEAD:$BRANCH"; then
    fail "Git push failed. GitHub was not updated; discarding the unpushed local commit."
    git -C "$SCRIPT_DIR" fetch "$REMOTE" "$BRANCH" || true
    git -C "$SCRIPT_DIR" reset --hard "$REMOTE/$BRANCH" >/dev/null
    exit 10
  fi
  git -C "$SCRIPT_DIR" fetch "$REMOTE" "$BRANCH"
  remote_after="$(git -C "$SCRIPT_DIR" rev-parse "$REMOTE/$BRANCH")"
  [ "$(git -C "$SCRIPT_DIR" rev-parse HEAD)" = "$remote_after" ] || {
    fail "Push returned success but local HEAD does not match $REMOTE/$BRANCH"; exit 10;
  }
  COLLECTION_COMMITTED=true
  ok "GitHub canonical history updated at $remote_after"
}

verify_push_path_without_commit() {
  # Test/recovery mode deliberately exercises the remote push path even when
  # the canonical CSVs are unchanged. An empty commit is not created.
  assert_clean
  log "No CSV changes to commit; verifying push path to $REMOTE/$BRANCH"
  if ! git -C "$SCRIPT_DIR" push "$REMOTE" "HEAD:$BRANCH"; then
    fail "Git push verification failed"
    exit 10
  fi
  git -C "$SCRIPT_DIR" fetch "$REMOTE" "$BRANCH"
  [ "$(git -C "$SCRIPT_DIR" rev-parse HEAD)" = "$(git -C "$SCRIPT_DIR" rev-parse "$REMOTE/$BRANCH")" ] || {
    fail "Push verification completed but local HEAD no longer matches $REMOTE/$BRANCH"
    exit 10
  }
  ok "Git push path verified; remote already has the canonical commit"
}

canonical_metadata_from_tracked_files() {
  # Materialize HEAD as the remote baseline and compare it to itself to derive exact SHAs/version.
  rm -rf "$TEMP_DIR"
mkdir -p "$HOME/.cache/f_collector/tmp"
TEMP_DIR="$(mktemp -d "$HOME/.cache/f_collector/tmp/forecast-sync.XXXXXX")"
  git -C "$SCRIPT_DIR" show "HEAD:$NATIONAL_REL" > "$TEMP_DIR/remote_national.csv"
  git -C "$SCRIPT_DIR" show "HEAD:$STATE_REL" > "$TEMP_DIR/remote_state.csv"
  load_comparison
  CANONICAL_GIT_SHA="$(git -C "$SCRIPT_DIR" rev-parse HEAD)"
}

deployed_git_sha() {
  "$DEPLOYER" deployed-git-sha 2>/dev/null || true
}

deployed_fingerprint() {
  "$DEPLOYER" deployed-fingerprint 2>/dev/null || true
}

deployed_version() {
  "$DEPLOYER" deployed-version 2>/dev/null || true
}

deploy_if_needed() {
  local deployed_sha deployed_fp deployed_ver deploy_time
  deployed_sha="$(deployed_git_sha)"
  deployed_fp="$(deployed_fingerprint)"
  deployed_ver="$(deployed_version)"
  if [ "$FORCE_DEPLOY" != "true" ] \
      && [ -n "$deployed_fp" ] \
      && [ "$deployed_fp" = "$LOCAL_FINGERPRINT" ] \
      && [ "$deployed_ver" = "$PIPELINE_VERSION" ]; then
    ok "AWS already serves the current data fingerprint with deployer v$PIPELINE_VERSION; no rebuild required"
    return 0
  fi
  if [ -z "$deployed_fp" ]; then
    log "No data deployment fingerprint is recorded in AWS; deployment is required"
  else
    log "AWS fingerprint ${deployed_fp} (deployer ${deployed_ver:-unknown}); canonical fingerprint ${LOCAL_FINGERPRINT} (deployer $PIPELINE_VERSION)"
  fi

  deploy_time="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  log "Building and deploying exact Git commit $CANONICAL_GIT_SHA"
  FORECAST_DATA_GIT_SHA="$CANONICAL_GIT_SHA" \
  FORECAST_DATA_FINGERPRINT="$LOCAL_FINGERPRINT" \
  FORECAST_SCHEMA_VERSION="$SCHEMA_VERSION" \
  FORECAST_DEPLOYER_VERSION="$PIPELINE_VERSION" \
  FORECAST_DEPLOYED_AT="$deploy_time" \
  FORECAST_NATIONAL_SHA256="$LOCAL_NATIONAL_SHA256" \
  FORECAST_STATE_SHA256="$LOCAL_STATE_SHA256" \
  "$DEPLOYER" rebuild

  "$DEPLOYER" smoke
  deployed_sha="$(deployed_git_sha)"
  deployed_fp="$(deployed_fingerprint)"
  deployed_ver="$(deployed_version)"
  [ "$deployed_sha" = "$CANONICAL_GIT_SHA" ] || {
    fail "Post-deploy Git metadata mismatch: AWS=$deployed_sha GitHub=$CANONICAL_GIT_SHA"; exit 11;
  }
  [ "$deployed_fp" = "$LOCAL_FINGERPRINT" ] || {
    fail "Post-deploy data fingerprint mismatch: AWS=$deployed_fp canonical=$LOCAL_FINGERPRINT"; exit 11;
  }
  [ "$deployed_ver" = "$PIPELINE_VERSION" ] || {
    fail "Post-deploy deployer-version mismatch: AWS=$deployed_ver expected=$PIPELINE_VERSION"; exit 11;
  }
  ok "Database smoke test passed; AWS records Git $CANONICAL_GIT_SHA and the canonical data fingerprint"
}

assert_final_state() {
  git -C "$SCRIPT_DIR" fetch "$REMOTE" "$BRANCH"
  local local_sha remote_sha
  local_sha="$(git -C "$SCRIPT_DIR" rev-parse HEAD)"
  remote_sha="$(git -C "$SCRIPT_DIR" rev-parse "$REMOTE/$BRANCH")"
  [ "$local_sha" = "$remote_sha" ] || {
    fail "Run finished but local HEAD ($local_sha) is not GitHub HEAD ($remote_sha)"; exit 12;
  }
  assert_clean
  ok "Local repository is clean and synchronized with $REMOTE/$BRANCH"
}

status_action() {
  assert_clean
  log "Fetching $REMOTE/$BRANCH for status"
  git -C "$SCRIPT_DIR" fetch --prune "$REMOTE" "$BRANCH"
  local local_sha remote_sha deployed_sha deployed_fp deployed_ver counts ahead behind
  local_sha="$(git -C "$SCRIPT_DIR" rev-parse HEAD)"
  remote_sha="$(git -C "$SCRIPT_DIR" rev-parse "$REMOTE/$BRANCH")"
  counts="$(git -C "$SCRIPT_DIR" rev-list --left-right --count "HEAD...$REMOTE/$BRANCH")"
  ahead="${counts%%[[:space:]]*}"
  behind="${counts##*[[:space:]]}"
  deployed_sha="$(deployed_git_sha)"
  deployed_fp="$(deployed_fingerprint)"
  deployed_ver="$(deployed_version)"
  printf 'Local HEAD:    %s\n' "$local_sha"
  printf 'GitHub HEAD:   %s\n' "$remote_sha"
  printf 'AWS Git SHA:   %s\n' "${deployed_sha:-not recorded / stack absent}"
  printf 'AWS fingerprint: %s\n' "${deployed_fp:-not recorded / stack absent}"
  printf 'AWS deployer:  %s\n' "${deployed_ver:-not recorded / stack absent}"
  printf 'Ahead/behind:  %s/%s\n' "$ahead" "$behind"
  if [ "$ahead" -ne 0 ] || [ "$behind" -ne 0 ]; then
    fail "Local repository is not synchronized with $REMOTE/$BRANCH"
    exit 5
  fi
  # A Git SHA difference alone can be a docs/code-only commit. Data freshness is
  # determined by the CSV fingerprint; deployer-version drift also forces a rebuild.
  require_tracked_csvs
  materialize_remote_csvs
  canonical_metadata_from_tracked_files
  if [ "$deployed_fp" = "$LOCAL_FINGERPRINT" ] && [ "$deployed_ver" = "$PIPELINE_VERSION" ]; then
    ok "AWS serves the current canonical forecast data"
  else
    warn "AWS forecast data/deployer is stale; run './sync_forecast_database.sh deploy'"
  fi
}

main_run() {
  assert_clean
  fetch_and_fast_forward
  require_tracked_csvs
  materialize_remote_csvs
  if [ "$SKIP_COLLECT" = "true" ]; then
    log "Skipping vendor collection; using existing canonical CSVs"
    "$DEPLOYER" validate
  else
    run_collector
  fi
  load_comparison
  if [ "$CHANGED" = "True" ]; then
    log "Validated append-only changes from $VENDOR_COUNT vendor(s): ${VENDORS:-unknown}"
    commit_and_push
  else
    if [ "$SKIP_COLLECT" = "true" ]; then
      ok "Existing CSVs match $REMOTE/$BRANCH; there is no data commit to create"
      verify_push_path_without_commit
    else
      ok "Collector produced no changes relative to $REMOTE/$BRANCH"
      assert_clean
    fi
  fi
  canonical_metadata_from_tracked_files
  deploy_if_needed
  assert_final_state
}

main_deploy() {
  assert_clean
  fetch_and_fast_forward
  require_tracked_csvs
  materialize_remote_csvs
  # Compare HEAD to itself and validate append-only/schema structure without polling vendors.
  "$DEPLOYER" validate
  canonical_metadata_from_tracked_files
  deploy_if_needed
  assert_final_state
}

parse_args "$@"
case "$ACTION" in
  help|-h|--help) usage; exit 0 ;;
  unlock) unlock_stale; exit 0 ;;
esac

need git
need python3
assert_repo_root_and_branch
acquire_lock
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

case "$ACTION" in
  run) main_run ;;
  deploy) main_deploy ;;
  status) status_action ;;
  *) fail "Unknown action: $ACTION"; usage; exit 2 ;;
esac
