#!/usr/bin/env bash
# Lifecycle for the disposable Rhubarb 2026 election-forecast PostgreSQL service.
#
# The accumulated f_collector CSV history remains on the local machine. A
# validated copy is baked into an immutable PostgreSQL image. ECS/Fargate may
# replace the running task at any time; the database is recreated from that
# image and is remotely accessible only through a generated read-only login.
set -Eeuo pipefail
export AWS_PAGER=""

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
DB_PACKAGE_DIR="$SCRIPT_DIR/forecast_database_ecs"
IMAGE_DIR="$DB_PACKAGE_DIR/image"
TEMPLATE_FILE="$DB_PACKAGE_DIR/cloudformation/election-forecasts-postgres.yml"
OUTPUT_DIR="$DB_PACKAGE_DIR/.outputs"
PREPARER="$IMAGE_DIR/prepare_election_forecasts.py"
ACTION="${1:-help}"

# Defaults deliberately match the standalone Stack Overflow survey datasource
# pattern: us-east-2, a dedicated staging stack/cluster, public NLB, and
# linux/amd64 Fargate tasks independent of the Rhubarb app stack.
AWS_REGION="${AWS_REGION:-us-east-2}"
MY_APP="${MY_APP:-rhubarb}"
STAGE="${STAGE:-staging}"
STACK_NAME="${STACK_NAME:-rhubarb-staging-election-forecasts-postgres}"
REPO_NAME="${REPO_NAME:-rhubarb/election-forecasts-postgres}"

DB_NAME="${DB_NAME:-election_forecasts}"
ADMIN_USER="${ADMIN_USER:-forecast_admin}"
READER_USER="${READER_USER:-rhubarb_forecast_reader}"
DB_PORT="${DB_PORT:-5432}"
ALLOWED_CIDR="${ALLOWED_CIDR:-0.0.0.0/0}"

CPU="${CPU:-1024}"
MEMORY="${MEMORY:-2048}"
CLUSTER_NAME="${CLUSTER_NAME:-rhubarb-staging-election-forecast-cluster}"
SERVICE_NAME="${SERVICE_NAME:-rhubarb-staging-election-forecast-service}"
TASK_FAMILY="${TASK_FAMILY:-rhubarb-staging-election-forecast-task}"
CONTAINER_NAME="${CONTAINER_NAME:-rhubarb-staging-election-forecast-db}"
NLB_NAME="${NLB_NAME:-rhubarb-stg-forecast-nlb}"
TG_NAME="${TG_NAME:-rhubarb-stg-forecast-tg}"
LOG_GROUP_NAME="${LOG_GROUP_NAME:-/ecs/rhubarb-staging-election-forecasts}"

DATA_DIR="${DATA_DIR:-$SCRIPT_DIR/collected_data}"
NATIONAL_CSV="${NATIONAL_CSV:-$DATA_DIR/election_forecasts_2026_national.csv}"
STATE_CSV="${STATE_CSV:-$DATA_DIR/election_forecasts_2026_state.csv}"
STAGED_NATIONAL_CSV="$IMAGE_DIR/data/election_forecasts_2026_national.csv"
STAGED_STATE_CSV="$IMAGE_DIR/data/election_forecasts_2026_state.csv"
CONNECTION_FILE="${CONNECTION_FILE:-$OUTPUT_DIR/election_forecasts_connection.env}"
CONNECTION_SECRET_NAME="${CONNECTION_SECRET_NAME:-${MY_APP}/${STAGE}/election-forecasts-postgres/DATABASE_URL}"

# Deployment provenance. The GitHub orchestration runner supplies exact values.
# Direct/manual lifecycle runs remain supported and are labeled accordingly.
FORECAST_DATA_GIT_SHA="${FORECAST_DATA_GIT_SHA:-manual}"
FORECAST_DATA_FINGERPRINT="${FORECAST_DATA_FINGERPRINT:-unknown}"
FORECAST_SCHEMA_VERSION="${FORECAST_SCHEMA_VERSION:-unknown}"
FORECAST_DEPLOYER_VERSION="${FORECAST_DEPLOYER_VERSION:-manual}"
FORECAST_DEPLOYED_AT="${FORECAST_DEPLOYED_AT:-$(date -u '+%Y-%m-%dT%H:%M:%SZ')}"
FORECAST_NATIONAL_SHA256="${FORECAST_NATIONAL_SHA256:-unknown}"
FORECAST_STATE_SHA256="${FORECAST_STATE_SHA256:-unknown}"

# Optional networking overrides. Otherwise use the default VPC and the first
# two public subnets in distinct Availability Zones.
VPC_ID="${VPC_ID:-}"
SUBNET_IDS="${SUBNET_IDS:-}"

# Collection/build controls.
COLLECT_BEFORE_BUILD="${COLLECT_BEFORE_BUILD:-if-missing}"
COLLECT_SAVE_RAW="${COLLECT_SAVE_RAW:-true}"
ALLOW_PARTIAL_COLLECT="${ALLOW_PARTIAL_COLLECT:-false}"
COLLECT_SOURCES="${COLLECT_SOURCES:-}"
NO_CACHE="${NO_CACHE:-false}"
PURGE_ECR="${PURGE_ECR:-true}"
PURGE_STAGED_DATA="${PURGE_STAGED_DATA:-false}"

RED="$(printf '\033[31m')"
GRN="$(printf '\033[32m')"
YLW="$(printf '\033[33m')"
BLU="$(printf '\033[34m')"
CYN="$(printf '\033[36m')"
RST="$(printf '\033[0m')"

ts() { date '+%Y-%m-%d %H:%M:%S'; }
log() { printf '%s[%s]%s %s\n' "$BLU" "$(ts)" "$RST" "$*"; }
ok() { printf '%s✅ %s%s\n' "$GRN" "$*" "$RST"; }
warn() { printf '%s⚠️  %s%s\n' "$YLW" "$*" "$RST"; }
fail() { printf '%s❌ %s%s\n' "$RED" "$*" "$RST" >&2; }

usage() {
  cat <<EOF_USAGE
Rhubarb 2026 election-forecast disposable ECS PostgreSQL service

Usage:
  ./$(basename "$0") collect      Append current vendor forecasts to collected_data
  ./$(basename "$0") validate     Validate the two accumulated CSVs
  ./$(basename "$0") stage        Validate and copy the CSVs into the Docker build context
  ./$(basename "$0") up           Build image and create/update the AWS stack
  ./$(basename "$0") refresh      Collect, validate, rebuild, and redeploy the complete history
  ./$(basename "$0") rebuild      Rebuild/redeploy existing CSVs without polling vendors
  ./$(basename "$0") status       Show stack, ECS service, NLB, and target health
  ./$(basename "$0") connection   Print the read-only PostgreSQL URI
  ./$(basename "$0") deployed-git-sha  Print the Git commit currently recorded by AWS
  ./$(basename "$0") deployed-fingerprint Print the data fingerprint currently recorded by AWS
  ./$(basename "$0") deployed-version  Print the deployer version currently recorded by AWS
  ./$(basename "$0") deployment-metadata Print Git/data provenance recorded on the ECS task
  ./$(basename "$0") credentials  Print connection components and retrieval locations
  ./$(basename "$0") smoke        Query table/view counts with local psql
  ./$(basename "$0") logs         Follow PostgreSQL logs in CloudWatch
  ./$(basename "$0") down         Delete the stack, connection secret, and ECR repository

Defaults:
  Region:             $AWS_REGION
  Stack:              $STACK_NAME
  Public ingress:     $ALLOWED_CIDR -> TCP/$DB_PORT
  Source directory:   $DATA_DIR
  National CSV:       $NATIONAL_CSV
  State CSV:          $STATE_CSV
  Database:           $DB_NAME
  Read-only user:     $READER_USER
  URI secret:         $CONNECTION_SECRET_NAME
  Local URI file:     $CONNECTION_FILE

Useful overrides:
  ALLOWED_CIDR=203.0.113.10/32 ./$(basename "$0") up
  VPC_ID=vpc-... SUBNET_IDS="subnet-a subnet-b" ./$(basename "$0") up
  DATA_DIR="\$HOME/election-model-average" ./$(basename "$0") refresh
  COLLECT_SOURCES="electindex election-statsheet" ./$(basename "$0") refresh
  ALLOW_PARTIAL_COLLECT=true ./$(basename "$0") refresh
  CPU=512 MEMORY=1024 ./$(basename "$0") up
  PURGE_ECR=false ./$(basename "$0") down

The stack is independent of the Rhubarb application stack. The down action never deletes
$DATA_DIR; the accumulated source CSV history remains local.
EOF_USAGE
}

need() {
  command -v "$1" >/dev/null 2>&1 || {
    fail "Missing required command: $1"
    exit 127
  }
}

normalize_path() {
  python3 - "$1" <<'PY'
from pathlib import Path
import sys
print(Path(sys.argv[1]).expanduser().resolve())
PY
}

normalize_paths() {
  DATA_DIR="$(normalize_path "$DATA_DIR")"
  NATIONAL_CSV="$(normalize_path "$NATIONAL_CSV")"
  STATE_CSV="$(normalize_path "$STATE_CSV")"
  CONNECTION_FILE="$(normalize_path "$CONNECTION_FILE")"
}

check_aws_tools() {
  need aws
}

check_build_tools() {
  check_aws_tools
  need docker
  need python3
  docker buildx version >/dev/null 2>&1 || {
    fail "Docker buildx is required"
    exit 127
  }
}

validate_identifier() {
  local value="$1" label="$2"
  if ! [[ "$value" =~ ^[A-Za-z_][A-Za-z0-9_]{0,62}$ ]]; then
    fail "$label is not a valid PostgreSQL identifier: $value"
    exit 2
  fi
}

validate_boolean() {
  local value="$1" label="$2"
  case "$value" in
    true|false) ;;
    *) fail "$label must be true or false: $value"; exit 2 ;;
  esac
}

validate_config() {
  need python3
  normalize_paths
  validate_identifier "$DB_NAME" "DB_NAME"
  validate_identifier "$ADMIN_USER" "ADMIN_USER"
  validate_identifier "$READER_USER" "READER_USER"
  if [ "$ADMIN_USER" = "$READER_USER" ]; then
    fail "ADMIN_USER and READER_USER must be different"
    exit 2
  fi
  case "$DB_PORT" in
    ''|*[!0-9]*) fail "DB_PORT must be numeric"; exit 2 ;;
  esac
  if [ "$DB_PORT" -lt 1 ] || [ "$DB_PORT" -gt 65535 ]; then
    fail "DB_PORT must be between 1 and 65535"
    exit 2
  fi
  case "$CPU:$MEMORY" in
    512:1024|512:2048|512:3072|512:4096|\
    1024:2048|1024:3072|1024:4096|1024:5120|1024:6144|1024:7168|1024:8192|\
    2048:4096|2048:5120|2048:6144|2048:7168|2048:8192) ;;
    *)
      fail "Unsupported Fargate CPU/memory pair: CPU=$CPU MEMORY=$MEMORY"
      exit 2
      ;;
  esac
  if [ "${#NLB_NAME}" -gt 32 ] || [ "${#TG_NAME}" -gt 32 ]; then
    fail "NLB_NAME and TG_NAME must be no more than 32 characters"
    exit 2
  fi
  case "$COLLECT_BEFORE_BUILD" in
    true|false|if-missing) ;;
    *) fail "COLLECT_BEFORE_BUILD must be true, false, or if-missing"; exit 2 ;;
  esac
  validate_boolean "$COLLECT_SAVE_RAW" "COLLECT_SAVE_RAW"
  validate_boolean "$ALLOW_PARTIAL_COLLECT" "ALLOW_PARTIAL_COLLECT"
  validate_boolean "$NO_CACHE" "NO_CACHE"
  validate_boolean "$PURGE_ECR" "PURGE_ECR"
  validate_boolean "$PURGE_STAGED_DATA" "PURGE_STAGED_DATA"
  [ -f "$PREPARER" ] || { fail "Missing preparer: $PREPARER"; exit 1; }
  [ -f "$TEMPLATE_FILE" ] || { fail "Missing template: $TEMPLATE_FILE"; exit 1; }
  [ -f "$IMAGE_DIR/Dockerfile" ] || { fail "Missing Dockerfile: $IMAGE_DIR/Dockerfile"; exit 1; }
}

ensure_collector_environment() {
  if [ ! -x "$SCRIPT_DIR/.venv/bin/python" ]; then
    log "Collector virtual environment is absent; running setup.sh"
    "$SCRIPT_DIR/setup.sh"
  fi
}

collect_forecasts() {
  validate_config
  ensure_collector_environment
  mkdir -p "$DATA_DIR"
  local args source
  args=("$SCRIPT_DIR/run.sh" collect --output-dir "$DATA_DIR")
  if [ "$COLLECT_SAVE_RAW" = "true" ]; then
    args+=(--save-raw)
  fi
  if [ "$ALLOW_PARTIAL_COLLECT" = "true" ]; then
    args+=(--allow-partial-success)
  fi
  if [ -n "$COLLECT_SOURCES" ]; then
    for source in $COLLECT_SOURCES; do
      args+=(--source "$source")
    done
  fi
  log "Collecting forecast updates into $DATA_DIR"
  "${args[@]}"
  ok "Forecast collection completed"
}

require_csvs() {
  [ -s "$NATIONAL_CSV" ] || {
    fail "National CSV is missing or empty: $NATIONAL_CSV"
    fail "Run ./$(basename "$0") collect or set DATA_DIR/NATIONAL_CSV."
    exit 1
  }
  [ -s "$STATE_CSV" ] || {
    fail "State CSV is missing or empty: $STATE_CSV"
    fail "Run ./$(basename "$0") collect or set DATA_DIR/STATE_CSV."
    exit 1
  }
}

validate_csvs() {
  validate_config
  require_csvs
  python3 "$PREPARER" \
    --national-input "$NATIONAL_CSV" \
    --state-input "$STATE_CSV" \
    --validate-only
}

stage_csvs() {
  validate_csvs
  mkdir -p "$IMAGE_DIR/data"
  local temp_national temp_state
  temp_national="$STAGED_NATIONAL_CSV.part"
  temp_state="$STAGED_STATE_CSV.part"
  cp "$NATIONAL_CSV" "$temp_national"
  cp "$STATE_CSV" "$temp_state"
  mv -f "$temp_national" "$STAGED_NATIONAL_CSV"
  mv -f "$temp_state" "$STAGED_STATE_CSV"
  ok "Staged national CSV: $STAGED_NATIONAL_CSV"
  ok "Staged state CSV:    $STAGED_STATE_CSV"
}

maybe_collect() {
  case "$COLLECT_BEFORE_BUILD" in
    true)
      collect_forecasts
      ;;
    if-missing)
      if [ ! -s "$NATIONAL_CSV" ] || [ ! -s "$STATE_CSV" ]; then
        warn "One or both CSVs are absent; collecting before build"
        collect_forecasts
      fi
      ;;
    false)
      ;;
  esac
}

aws_text() {
  aws "$@" --output text | tr -d '\r'
}

get_account() {
  ACCOUNT_ID="$(aws_text sts get-caller-identity --query Account)"
  [ -n "$ACCOUNT_ID" ] && [ "$ACCOUNT_ID" != "None" ] || {
    fail "Unable to determine AWS account ID"
    exit 1
  }
  ECR_REGISTRY="$ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com"
  ECR_REPOSITORY_URI="$ECR_REGISTRY/$REPO_NAME"
}

discover_network() {
  if [ -z "$VPC_ID" ]; then
    VPC_ID="$(aws_text ec2 describe-vpcs \
      --region "$AWS_REGION" \
      --filters Name=isDefault,Values=true \
      --query 'Vpcs[0].VpcId')"
  fi
  [ -n "$VPC_ID" ] && [ "$VPC_ID" != "None" ] || {
    fail "No default VPC found. Set VPC_ID and SUBNET_IDS explicitly."
    exit 1
  }

  if [ -n "$SUBNET_IDS" ]; then
    set -- $SUBNET_IDS
    SUBNET1_ID="${1:-}"
    SUBNET2_ID="${2:-}"
  else
    local subnet_lines selected
    subnet_lines="$(aws ec2 describe-subnets \
      --region "$AWS_REGION" \
      --filters "Name=vpc-id,Values=$VPC_ID" \
                "Name=map-public-ip-on-launch,Values=true" \
      --query 'Subnets[].[AvailabilityZone,SubnetId]' \
      --output text | sort)"
    selected="$(printf '%s\n' "$subnet_lines" | awk '!seen[$1]++ {print $2}' | sed -n '1,2p')"
    SUBNET1_ID="$(printf '%s\n' "$selected" | sed -n '1p')"
    SUBNET2_ID="$(printf '%s\n' "$selected" | sed -n '2p')"
  fi

  [ -n "${SUBNET1_ID:-}" ] && [ -n "${SUBNET2_ID:-}" ] || {
    fail "Two public subnets in distinct Availability Zones are required. Set SUBNET_IDS."
    exit 1
  }
  ok "Network: VPC $VPC_ID; subnets $SUBNET1_ID, $SUBNET2_ID"
}

ensure_ecr_and_image() {
  local image_tag local_image digest attempt
  local -a build_cmd
  if ! aws ecr describe-repositories \
      --region "$AWS_REGION" --repository-names "$REPO_NAME" >/dev/null 2>&1; then
    log "Creating ECR repository: $REPO_NAME"
    aws ecr create-repository \
      --region "$AWS_REGION" \
      --repository-name "$REPO_NAME" \
      --image-scanning-configuration scanOnPush=true \
      --encryption-configuration encryptionType=AES256 \
      --tags \
        Key=Application,Value="$MY_APP" \
        Key=Environment,Value="$STAGE" \
        Key=Component,Value=election-forecasts-postgres >/dev/null
  fi

  aws ecr put-lifecycle-policy \
    --region "$AWS_REGION" \
    --repository-name "$REPO_NAME" \
    --lifecycle-policy-text '{"rules":[{"rulePriority":1,"description":"Keep the five newest images","selection":{"tagStatus":"any","countType":"imageCountMoreThan","countNumber":5},"action":{"type":"expire"}}]}' \
    >/dev/null

  aws ecr get-login-password --region "$AWS_REGION" \
    | docker login --username AWS --password-stdin "$ECR_REGISTRY" >/dev/null

  image_tag="${IMAGE_TAG:-$(date -u +%Y%m%d%H%M%S)}"
  local_image="rhubarb-election-forecasts-postgres:$image_tag"
  ECR_TAGGED="$ECR_REPOSITORY_URI:$image_tag"

  log "Building the self-contained election forecast PostgreSQL image"
  build_cmd=(docker buildx build \
    --platform linux/amd64 \
    --load \
    -t "$local_image")
  if [ "$NO_CACHE" = "true" ]; then
    build_cmd+=(--no-cache)
  fi
  build_cmd+=("$IMAGE_DIR")
  "${build_cmd[@]}"

  docker tag "$local_image" "$ECR_TAGGED"
  docker tag "$local_image" "$ECR_REPOSITORY_URI:latest"
  docker push "$ECR_TAGGED"
  docker push "$ECR_REPOSITORY_URI:latest"

  digest=""
  for ((attempt = 1; attempt <= 20; attempt++)); do
    digest="$(aws_text ecr describe-images \
      --region "$AWS_REGION" \
      --repository-name "$REPO_NAME" \
      --image-ids "imageTag=$image_tag" \
      --query 'imageDetails[0].imageDigest' 2>/dev/null || true)"
    if [ -n "$digest" ] && [ "$digest" != "None" ]; then
      break
    fi
    sleep 2
  done
  [ -n "$digest" ] && [ "$digest" != "None" ] || {
    fail "Unable to resolve the pushed ECR image digest"
    exit 1
  }
  IMAGE_REF="$ECR_REPOSITORY_URI@$digest"
  ok "Image pushed: $IMAGE_REF"
}

stack_status() {
  aws_text cloudformation describe-stacks \
    --region "$AWS_REGION" --stack-name "$STACK_NAME" \
    --query 'Stacks[0].StackStatus' 2>/dev/null || true
}

stack_exists() {
  local status
  status="$(stack_status)"
  [ -n "$status" ] && [ "$status" != "None" ]
}

show_stack_failures() {
  aws cloudformation describe-stack-events \
    --region "$AWS_REGION" --stack-name "$STACK_NAME" \
    --query "StackEvents[?contains(ResourceStatus, 'FAILED')].[Timestamp,LogicalResourceId,ResourceStatus,ResourceStatusReason] | [0:12]" \
    --output table 2>/dev/null || true
}

remove_unusable_stack() {
  local status
  status="$(stack_status)"
  case "$status" in
    ROLLBACK_COMPLETE|CREATE_FAILED)
      warn "Removing unusable stack state: $status"
      aws cloudformation delete-stack --region "$AWS_REGION" --stack-name "$STACK_NAME"
      aws cloudformation wait stack-delete-complete --region "$AWS_REGION" --stack-name "$STACK_NAME"
      ;;
    DELETE_IN_PROGRESS)
      log "Waiting for the previous stack deletion to finish"
      aws cloudformation wait stack-delete-complete --region "$AWS_REGION" --stack-name "$STACK_NAME"
      ;;
    DELETE_FAILED|UPDATE_ROLLBACK_FAILED)
      fail "Stack is in $status. Review the failed resources before redeploying."
      show_stack_failures
      exit 1
      ;;
  esac
}

deploy_stack() {
  log "Deploying CloudFormation stack: $STACK_NAME"
  if ! aws cloudformation deploy \
      --region "$AWS_REGION" \
      --stack-name "$STACK_NAME" \
      --template-file "$TEMPLATE_FILE" \
      --capabilities CAPABILITY_IAM \
      --no-fail-on-empty-changeset \
      --parameter-overrides \
        "ImageUri=$IMAGE_REF" \
        "VpcId=$VPC_ID" \
        "PublicSubnetIds=$SUBNET1_ID,$SUBNET2_ID" \
        "AllowedCidr=$ALLOWED_CIDR" \
        "DatabaseName=$DB_NAME" \
        "AdminUsername=$ADMIN_USER" \
        "ReaderUsername=$READER_USER" \
        "ContainerPort=$DB_PORT" \
        "TaskCpu=$CPU" \
        "TaskMemory=$MEMORY" \
        "ClusterName=$CLUSTER_NAME" \
        "ServiceName=$SERVICE_NAME" \
        "TaskFamily=$TASK_FAMILY" \
        "ContainerName=$CONTAINER_NAME" \
        "NetworkLoadBalancerName=$NLB_NAME" \
        "TargetGroupName=$TG_NAME" \
        "LogGroupName=$LOG_GROUP_NAME" \
        "ApplicationName=$MY_APP" \
        "EnvironmentName=$STAGE" \
        "ForecastDataGitSha=$FORECAST_DATA_GIT_SHA" \
        "ForecastDataFingerprint=$FORECAST_DATA_FINGERPRINT" \
        "ForecastSchemaVersion=$FORECAST_SCHEMA_VERSION" \
        "ForecastDeployerVersion=$FORECAST_DEPLOYER_VERSION" \
        "ForecastDeployedAt=$FORECAST_DEPLOYED_AT" \
        "ForecastNationalSha256=$FORECAST_NATIONAL_SHA256" \
        "ForecastStateSha256=$FORECAST_STATE_SHA256" \
      --tags \
        "Application=$MY_APP" \
        "Environment=$STAGE" \
        "Component=election-forecasts-postgres" \
        "ManagedBy=election_forecasts_ecs"; then
    fail "CloudFormation deployment failed"
    show_stack_failures
    exit 1
  fi
  ok "CloudFormation stack deployed: $STACK_NAME"
}

get_output() {
  local key="$1"
  aws_text cloudformation describe-stacks \
    --region "$AWS_REGION" --stack-name "$STACK_NAME" \
    --query "Stacks[0].Outputs[?OutputKey=='$key'].OutputValue | [0]"
}

load_outputs() {
  ENDPOINT_HOST="$(get_output EndpointHost)"
  ENDPOINT_PORT="$(get_output EndpointPort)"
  OUTPUT_DB_NAME="$(get_output DatabaseName)"
  OUTPUT_READER_USER="$(get_output ReaderUsername)"
  READER_SECRET_ARN="$(get_output ReaderPasswordSecretArn)"
  ADMIN_SECRET_ARN="$(get_output AdminPasswordSecretArn)"
  OUTPUT_CLUSTER="$(get_output ClusterName)"
  OUTPUT_SERVICE="$(get_output ServiceName)"
  OUTPUT_TASK_DEFINITION="$(get_output TaskDefinitionArn)"
  OUTPUT_TG_ARN="$(get_output TargetGroupArn)"
  OUTPUT_LOG_GROUP="$(get_output LogGroupName)"
  OUTPUT_IMAGE_URI="$(get_output DeployedImageUri)"
  OUTPUT_FORECAST_DATA_GIT_SHA="$(get_output ForecastDataGitSha 2>/dev/null || true)"
  OUTPUT_FORECAST_DATA_FINGERPRINT="$(get_output ForecastDataFingerprint 2>/dev/null || true)"
  OUTPUT_FORECAST_SCHEMA_VERSION="$(get_output ForecastSchemaVersion 2>/dev/null || true)"
  OUTPUT_FORECAST_DEPLOYER_VERSION="$(get_output ForecastDeployerVersion 2>/dev/null || true)"
  OUTPUT_FORECAST_DEPLOYED_AT="$(get_output ForecastDeployedAt 2>/dev/null || true)"
  OUTPUT_FORECAST_NATIONAL_SHA256="$(get_output ForecastNationalSha256 2>/dev/null || true)"
  OUTPUT_FORECAST_STATE_SHA256="$(get_output ForecastStateSha256 2>/dev/null || true)"
}

build_connection_uri() {
  stack_exists || { fail "Stack does not exist: $STACK_NAME"; exit 1; }
  load_outputs
  READER_PASSWORD="$(aws_text secretsmanager get-secret-value \
    --region "$AWS_REGION" --secret-id "$READER_SECRET_ARN" \
    --query SecretString)"
  CONNECTION_URI="postgresql://${OUTPUT_READER_USER}:${READER_PASSWORD}@${ENDPOINT_HOST}:${ENDPOINT_PORT}/${OUTPUT_DB_NAME}?sslmode=require&application_name=rhubarb_forecast_reader"
}

upsert_connection_secret() {
  local deleted
  if aws secretsmanager describe-secret \
      --region "$AWS_REGION" --secret-id "$CONNECTION_SECRET_NAME" >/dev/null 2>&1; then
    deleted="$(aws_text secretsmanager describe-secret \
      --region "$AWS_REGION" --secret-id "$CONNECTION_SECRET_NAME" \
      --query DeletedDate 2>/dev/null || true)"
    if [ -n "$deleted" ] && [ "$deleted" != "None" ]; then
      aws secretsmanager restore-secret \
        --region "$AWS_REGION" --secret-id "$CONNECTION_SECRET_NAME" >/dev/null
    fi
    aws secretsmanager put-secret-value \
      --region "$AWS_REGION" \
      --secret-id "$CONNECTION_SECRET_NAME" \
      --secret-string "$CONNECTION_URI" >/dev/null
  else
    aws secretsmanager create-secret \
      --region "$AWS_REGION" \
      --name "$CONNECTION_SECRET_NAME" \
      --description "Read-only 2026 election forecast PostgreSQL URI for Rhubarb staging" \
      --secret-string "$CONNECTION_URI" \
      --tags \
        Key=Application,Value="$MY_APP" \
        Key=Environment,Value="$STAGE" \
        Key=Component,Value=election-forecasts-postgres >/dev/null
  fi
  ok "Connection URI stored in Secrets Manager: $CONNECTION_SECRET_NAME"
}

write_connection_file() {
  build_connection_uri
  mkdir -p "$(dirname -- "$CONNECTION_FILE")"
  umask 077
  cat > "$CONNECTION_FILE" <<EOF_CONNECTION
ELECTION_FORECASTS_DATABASE_URL='$CONNECTION_URI'
ELECTION_FORECASTS_DB_HOST='$ENDPOINT_HOST'
ELECTION_FORECASTS_DB_PORT='$ENDPOINT_PORT'
ELECTION_FORECASTS_DB_NAME='$OUTPUT_DB_NAME'
ELECTION_FORECASTS_DB_USER='$OUTPUT_READER_USER'
ELECTION_FORECASTS_DB_PASSWORD='$READER_PASSWORD'
ELECTION_FORECASTS_DB_SSLMODE='require'
ELECTION_FORECASTS_CONNECTION_SECRET='$CONNECTION_SECRET_NAME'
EOF_CONNECTION
  chmod 600 "$CONNECTION_FILE"
  ok "Connection details saved: $CONNECTION_FILE"
  upsert_connection_secret
}

print_connection() {
  check_aws_tools
  export AWS_DEFAULT_REGION="$AWS_REGION"
  build_connection_uri
  printf '%s\n' "$CONNECTION_URI"
}

print_credentials() {
  check_aws_tools
  export AWS_DEFAULT_REGION="$AWS_REGION"
  build_connection_uri
  cat <<EOF_CREDENTIALS
Database:          $OUTPUT_DB_NAME
Read-only user:    $OUTPUT_READER_USER
Host:              $ENDPOINT_HOST
Port:              $ENDPOINT_PORT
SSL mode:          require
Connection URI:    $CONNECTION_URI
Fixed URI secret:  $CONNECTION_SECRET_NAME
Local env file:    $CONNECTION_FILE

Retrieve URI later:
aws secretsmanager get-secret-value --region '$AWS_REGION' --secret-id '$CONNECTION_SECRET_NAME' --query SecretString --output text
EOF_CREDENTIALS
}

up() {
  check_build_tools
  validate_config
  export AWS_DEFAULT_REGION="$AWS_REGION"

  case "$ACTION" in
    refresh)
      COLLECT_BEFORE_BUILD=true
      NO_CACHE=true
      ;;
    rebuild)
      COLLECT_BEFORE_BUILD=false
      NO_CACHE=true
      ;;
  esac

  maybe_collect
  stage_csvs
  get_account

  log "AWS account $ACCOUNT_ID; region $AWS_REGION"
  log "Stack $STACK_NAME; ECR repository $REPO_NAME"
  if [ "$ALLOWED_CIDR" = "0.0.0.0/0" ]; then
    warn "The read-only PostgreSQL endpoint will be reachable from the entire IPv4 internet."
  fi

  discover_network
  remove_unusable_stack
  ensure_ecr_and_image
  deploy_stack
  write_connection_file
  load_outputs

  echo
  ok "2026 election forecast PostgreSQL is online"
  printf 'Endpoint: %s%s:%s%s\n' "$CYN" "$ENDPOINT_HOST" "$ENDPOINT_PORT" "$RST"
  printf 'Database: %s\n' "$OUTPUT_DB_NAME"
  printf 'Reader:   %s\n' "$OUTPUT_READER_USER"
  printf 'SSL mode: require\n'
  printf 'Tables:   public.election_forecasts_2026_national\n'
  printf '          public.election_forecasts_2026_state\n'
  printf 'URI:      ./%s connection\n' "$(basename "$0")"
  echo
}

print_deployed_git_sha() {
  check_aws_tools
  export AWS_DEFAULT_REGION="$AWS_REGION"
  if ! stack_exists; then
    return 0
  fi
  local value
  value="$(get_output ForecastDataGitSha 2>/dev/null || true)"
  if [ "$value" = "None" ]; then value=""; fi
  printf '%s\n' "$value"
}

print_deployed_fingerprint() {
  check_aws_tools
  export AWS_DEFAULT_REGION="$AWS_REGION"
  if ! stack_exists; then return 0; fi
  local value
  value="$(get_output ForecastDataFingerprint 2>/dev/null || true)"
  if [ "$value" = "None" ]; then value=""; fi
  printf '%s\n' "$value"
}

print_deployed_version() {
  check_aws_tools
  export AWS_DEFAULT_REGION="$AWS_REGION"
  if ! stack_exists; then return 0; fi
  local value
  value="$(get_output ForecastDeployerVersion 2>/dev/null || true)"
  if [ "$value" = "None" ]; then value=""; fi
  printf '%s\n' "$value"
}

print_deployment_metadata() {
  check_aws_tools
  export AWS_DEFAULT_REGION="$AWS_REGION"
  if ! stack_exists; then
    echo "Stack present: no"
    return 0
  fi
  load_outputs
  cat <<EOF_METADATA
Stack present:        yes
Git SHA:              ${OUTPUT_FORECAST_DATA_GIT_SHA:-unknown}
Data fingerprint:     ${OUTPUT_FORECAST_DATA_FINGERPRINT:-unknown}
Schema version:       ${OUTPUT_FORECAST_SCHEMA_VERSION:-unknown}
Deployer version:     ${OUTPUT_FORECAST_DEPLOYER_VERSION:-unknown}
Deployed at:          ${OUTPUT_FORECAST_DEPLOYED_AT:-unknown}
National CSV SHA256:  ${OUTPUT_FORECAST_NATIONAL_SHA256:-unknown}
State CSV SHA256:     ${OUTPUT_FORECAST_STATE_SHA256:-unknown}
Image:                $OUTPUT_IMAGE_URI
Task definition:      $OUTPUT_TASK_DEFINITION
EOF_METADATA
}

status() {
  check_aws_tools
  export AWS_DEFAULT_REGION="$AWS_REGION"
  local status
  status="$(stack_status)"
  if [ -z "$status" ] || [ "$status" = "None" ]; then
    echo "Stack absent: $STACK_NAME"
    exit 0
  fi
  echo "Stack:  $STACK_NAME"
  echo "Status: $status"
  echo
  load_outputs
  printf 'Endpoint: %s:%s\n' "$ENDPOINT_HOST" "$ENDPOINT_PORT"
  printf 'Database: %s\n' "$OUTPUT_DB_NAME"
  printf 'Reader: %s\n' "$OUTPUT_READER_USER"
  printf 'Image: %s\n' "$OUTPUT_IMAGE_URI"
  printf 'Task definition: %s\n' "$OUTPUT_TASK_DEFINITION"
  printf 'Git SHA: %s\n' "${OUTPUT_FORECAST_DATA_GIT_SHA:-unknown}"
  printf 'Data fingerprint: %s\n' "${OUTPUT_FORECAST_DATA_FINGERPRINT:-unknown}"
  printf 'Schema version: %s\n' "${OUTPUT_FORECAST_SCHEMA_VERSION:-unknown}"
  printf 'Deployer version: %s\n' "${OUTPUT_FORECAST_DEPLOYER_VERSION:-unknown}"
  printf 'Deployed at: %s\n' "${OUTPUT_FORECAST_DEPLOYED_AT:-unknown}"
  printf 'Connection secret: %s\n\n' "$CONNECTION_SECRET_NAME"

  aws ecs describe-services \
    --region "$AWS_REGION" \
    --cluster "$OUTPUT_CLUSTER" \
    --services "$OUTPUT_SERVICE" \
    --query 'services[0].[status,desiredCount,runningCount,pendingCount,taskDefinition]' \
    --output table 2>/dev/null || true

  aws elbv2 describe-target-health \
    --region "$AWS_REGION" \
    --target-group-arn "$OUTPUT_TG_ARN" \
    --query 'TargetHealthDescriptions[].{Target:Target.Id,Port:Target.Port,State:TargetHealth.State,Reason:TargetHealth.Reason,Description:TargetHealth.Description}' \
    --output table 2>/dev/null || true

  aws ecs describe-services \
    --region "$AWS_REGION" \
    --cluster "$OUTPUT_CLUSTER" \
    --services "$OUTPUT_SERVICE" \
    --query 'services[0].events[0:8].[createdAt,message]' \
    --output table 2>/dev/null || true
}

logs() {
  check_aws_tools
  export AWS_DEFAULT_REGION="$AWS_REGION"
  stack_exists || { fail "Stack does not exist: $STACK_NAME"; exit 1; }
  load_outputs
  aws logs tail "$OUTPUT_LOG_GROUP" \
    --region "$AWS_REGION" \
    --since 1h \
    --follow
}

smoke() {
  check_aws_tools
  export AWS_DEFAULT_REGION="$AWS_REGION"
  build_connection_uri
  local -a commands
  commands=(
    --set ON_ERROR_STOP=1
    --command "SELECT current_database() AS database, current_user AS reader, current_setting('transaction_read_only') AS transaction_read_only;"
    --command "SELECT 'national' AS dataset, count(*) AS rows, count(DISTINCT vendor) AS vendors, max(rhubarb_pull_time) AS latest_pull FROM public.election_forecasts_2026_national UNION ALL SELECT 'state', count(*), count(DISTINCT vendor), max(rhubarb_pull_time) FROM public.election_forecasts_2026_state;"
    --command "SELECT * FROM public.election_forecasts_2026_load_metadata;"
  )
  if command -v psql >/dev/null 2>&1; then
    PGCONNECT_TIMEOUT="${PGCONNECT_TIMEOUT:-15}" psql "$CONNECTION_URI" "${commands[@]}"
    return
  fi
  need docker
  warn "Local psql not found; running the smoke test with the official PostgreSQL Docker client"
  docker run --rm \
    -e "PGCONNECT_TIMEOUT=${PGCONNECT_TIMEOUT:-15}" \
    postgres:16-bookworm \
    psql "$CONNECTION_URI" "${commands[@]}"
}

force_delete_secret() {
  local secret_arn="$1" deleted
  [ -n "$secret_arn" ] && [ "$secret_arn" != "None" ] || return 0
  if ! aws secretsmanager describe-secret \
      --region "$AWS_REGION" --secret-id "$secret_arn" >/dev/null 2>&1; then
    return 0
  fi
  deleted="$(aws_text secretsmanager describe-secret \
    --region "$AWS_REGION" --secret-id "$secret_arn" \
    --query DeletedDate 2>/dev/null || true)"
  if [ -n "$deleted" ] && [ "$deleted" != "None" ]; then
    aws secretsmanager restore-secret \
      --region "$AWS_REGION" --secret-id "$secret_arn" >/dev/null 2>&1 || true
  fi
  aws secretsmanager delete-secret \
    --region "$AWS_REGION" --secret-id "$secret_arn" \
    --force-delete-without-recovery >/dev/null 2>&1 || true
}

down() {
  check_aws_tools
  validate_config
  export AWS_DEFAULT_REGION="$AWS_REGION"
  get_account

  local reader_secret="" admin_secret="" status
  status="$(stack_status)"
  if [ -n "$status" ] && [ "$status" != "None" ]; then
    reader_secret="$(get_output ReaderPasswordSecretArn 2>/dev/null || true)"
    admin_secret="$(get_output AdminPasswordSecretArn 2>/dev/null || true)"
    log "Deleting CloudFormation stack: $STACK_NAME"
    aws cloudformation delete-stack \
      --region "$AWS_REGION" --stack-name "$STACK_NAME"
    if ! aws cloudformation wait stack-delete-complete \
        --region "$AWS_REGION" --stack-name "$STACK_NAME"; then
      fail "CloudFormation stack deletion failed"
      show_stack_failures
      exit 1
    fi
    ok "CloudFormation stack deleted"
  else
    ok "CloudFormation stack already absent"
  fi

  force_delete_secret "$reader_secret"
  force_delete_secret "$admin_secret"
  force_delete_secret "$CONNECTION_SECRET_NAME"
  ok "Connection secret removed: $CONNECTION_SECRET_NAME"

  if [ "$PURGE_ECR" = "true" ]; then
    aws ecr delete-repository \
      --region "$AWS_REGION" \
      --repository-name "$REPO_NAME" \
      --force >/dev/null 2>&1 || true
    ok "ECR repository deleted: $REPO_NAME"
  else
    warn "Keeping ECR repository: $REPO_NAME"
  fi

  if [ "$PURGE_STAGED_DATA" = "true" ]; then
    rm -f "$STAGED_NATIONAL_CSV" "$STAGED_NATIONAL_CSV.part" \
          "$STAGED_STATE_CSV" "$STAGED_STATE_CSV.part"
    ok "Docker build-context copies removed"
  else
    warn "Keeping staged build copies for faster redeploys"
  fi

  rm -f "$CONNECTION_FILE"
  rm -rf "$OUTPUT_DIR"
  mkdir -p "$OUTPUT_DIR"
  : > "$OUTPUT_DIR/.gitkeep"
  echo
  ok "Disposable election forecast ECS database is fully down"
  warn "Collected source history was preserved: $DATA_DIR"
}

case "$ACTION" in
  collect) collect_forecasts ;;
  validate) validate_csvs ;;
  stage) stage_csvs ;;
  up|refresh|rebuild) up ;;
  down|destroy) down ;;
  status) status ;;
  connection|uri) print_connection ;;
  deployed-git-sha) print_deployed_git_sha ;;
  deployed-fingerprint) print_deployed_fingerprint ;;
  deployed-version) print_deployed_version ;;
  deployment-metadata|metadata) print_deployment_metadata ;;
  credentials|connection-info) print_credentials ;;
  smoke) smoke ;;
  logs) logs ;;
  help|-h|--help) usage ;;
  *) fail "Unknown action: $ACTION"; usage; exit 2 ;;
esac
