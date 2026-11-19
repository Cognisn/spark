#!/usr/bin/env bash
# Deploy a release candidate to the Spark test environment through CloudFormation.
#
#   scripts/deploy-test.sh <version>      for example 0.3.0rc17
#
# Called by the deploy-test job of .github/workflows/container-publish.yml after the image
# is in ECR. It never registers a task definition, updates a service or updates the stack
# directly: CloudFormation is the only thing that changes the services, through change sets
# that reuse the stack's current template and change one parameter each.
#
#   1. Read the service stack. A re-run with the version already running goes to step 5.
#   2. Change set one: MigrateImageTag=<version>. Only MigrateTaskDefinition may change.
#   3. Run the one-off migrate task (spark migrate && spark bootstrap) on the stack's own
#      outputs and require exit code 0. A failure stops here; the services are untouched.
#   4. Change set two: ImageTag=<version>. Only the two task definitions and the two
#      services may change. A rollback fails the deploy; it is never repaired.
#   5. Check the stack, both services and GET /ready.
#
# Configuration comes from the environment, never from this file (the repository is public,
# so no account, role, stack or network identifier is written here):
#   AWS_REGION             used by the AWS CLI
#   STACK_NAME             the service stack
#   EXECUTION_ROLE_ARN     the CloudFormation execution role the change sets run as
#   GITHUB_RUN_ID, GITHUB_RUN_ATTEMPT   named in the change sets (default 0 and 1)
#   READY_URL              default https://test.spark.cognisn.net/ready
#   POLL_INTERVAL          seconds between polls (default 10)
#   CHANGESET_TIMEOUT, MIGRATE_STACK_TIMEOUT, TASK_TIMEOUT, STACK_TIMEOUT, SERVICE_TIMEOUT,
#   READY_TIMEOUT          bounds in seconds (defaults 300, 900, 1200, 1800, 600, 600)
#
# Every identifier read from AWS is masked (::add-mask::) before it can be printed, and
# nothing prints an identifier on purpose: the Actions log of a public repository is public.
set -euo pipefail

TARGET_TYPES=(AWS::ECS::TaskDefinition AWS::ECS::Service)
MIGRATE_IDS=" MigrateTaskDefinition "
SERVICE_IDS=" WebTaskDefinition WorkerTaskDefinition WebService WorkerService "

version="${1:-}"
if [[ ! "$version" =~ ^[0-9A-Za-z][0-9A-Za-z._-]*$ ]]; then
    echo "::error::usage: deploy-test.sh <version>"
    exit 2
fi
: "${STACK_NAME:?STACK_NAME is not set}"
: "${EXECUTION_ROLE_ARN:?EXECUTION_ROLE_ARN is not set}"
: "${AWS_REGION:?AWS_REGION is not set}"
export AWS_PAGER=""

run_id="${GITHUB_RUN_ID:-0}"
run_attempt="${GITHUB_RUN_ATTEMPT:-1}"
ready_url="${READY_URL:-https://test.spark.cognisn.net/ready}"
poll="${POLL_INTERVAL:-10}"
changeset_timeout="${CHANGESET_TIMEOUT:-300}"
migrate_stack_timeout="${MIGRATE_STACK_TIMEOUT:-900}"
task_timeout="${TASK_TIMEOUT:-1200}"
stack_timeout="${STACK_TIMEOUT:-1800}"
service_timeout="${SERVICE_TIMEOUT:-600}"
ready_timeout="${READY_TIMEOUT:-600}"

summary() {
    if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
        printf '%s\n' "$*" >>"$GITHUB_STEP_SUMMARY"
    fi
}

mask() {
    local value
    for value in "$@"; do
        case "$value" in "" | None | none | null) continue ;; esac
        echo "::add-mask::$value"
    done
}

# scrub: print AWS text with every ARN replaced, whatever else the message holds.
scrub() {
    sed -E 's#arn:aws[a-z-]*:[^[:space:]"'"'"',;)]*#arn:***#g'
}

fail() {
    echo "::error::$*" | scrub
    summary "Deploy FAILED: $(printf '%s' "$*" | scrub)"
    exit 1
}

# Mask the identifiers we know before anything can print them.
mask "$STACK_NAME" "$EXECUTION_ROLE_ARN" "${EXECUTION_ROLE_ARN##*/}"
account="${EXECUTION_ROLE_ARN#arn:*:iam::}"
mask "${account%%:*}"

# aws_text ARGS...: run the AWS CLI; on failure print its (scrubbed) message and stop.
aws_text() {
    local out
    if ! out="$(aws "$@" 2>&1)"; then
        echo "AWS refused or failed a call (aws $1 $2):"
        printf '%s\n' "$out" | scrub
        fail "the call aws $1 $2 failed; see the message above"
    fi
    printf '%s' "$out"
}

stack_query() {
    aws_text cloudformation describe-stacks --stack-name "$STACK_NAME" \
        --query "$1" --output text
}

stack_output() {
    stack_query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue | [0]"
}

# Deadline helper: deadline SECONDS prints the epoch second to stop at.
deadline() { echo $((SECONDS + $1)); }

# ---- 1. read the stack ------------------------------------------------------------

status="$(stack_query 'Stacks[0].StackStatus')"
case "$status" in
    CREATE_COMPLETE | UPDATE_COMPLETE | UPDATE_ROLLBACK_COMPLETE) ;;
    *) fail "the service stack is in $status; it must be CREATE_COMPLETE, UPDATE_COMPLETE or UPDATE_ROLLBACK_COMPLETE" ;;
esac

read_outputs() {
    running_tag="$(stack_output RunningImageTag)"
    migrate_tag="$(stack_output MigrateImageTag)"
    cluster="$(stack_output ClusterName)"
    migrate_task_def="$(stack_output MigrateTaskDefinitionArn)"
    migrate_subnets="$(stack_output MigrateSubnetIds)"
    migrate_sg="$(stack_output MigrateSecurityGroupId)"
    web_service="$(stack_output WebServiceName)"
    worker_service="$(stack_output WorkerServiceName)"
    local item
    mask "$cluster" "$migrate_task_def" "$migrate_sg" "$web_service" "$worker_service"
    IFS=',' read -r -a subnet_list <<<"$migrate_subnets"
    for item in "${subnet_list[@]}"; do mask "$item"; done
    mask "$migrate_subnets"
}
read_outputs

summary "## Deploy to the test environment"
summary ""
summary "Version: \`$version\` (running before: \`$running_tag\`)"

# ---- change sets ------------------------------------------------------------------

# parameters_for KEY VALUE: the --parameters list, KEY=VALUE and UsePreviousValue for the rest.
parameters_for() {
    local key found=0
    param_args=()
    for key in $(stack_query 'Stacks[0].Parameters[].ParameterKey'); do
        if [ "$key" = "$1" ]; then
            found=1
            param_args+=("ParameterKey=$key,ParameterValue=$2")
        else
            param_args+=("ParameterKey=$key,UsePreviousValue=true")
        fi
    done
    [ "$found" = 1 ] || fail "the stack has no parameter $1"
}

safe_version="${version//[^A-Za-z0-9]/-}"

# change_set NAME KEY VALUE ALLOWED_IDS: create, wait, vet. Sets cs_state to created or
# nochanges; a vetting failure deletes the change set and fails the deploy.
change_set() {
    local name="$1" key="$2" value="$3" allowed="$4"
    parameters_for "$key" "$value"
    local out
    if ! out="$(aws cloudformation create-change-set --stack-name "$STACK_NAME" \
        --change-set-name "$name" --use-previous-template \
        --parameters "${param_args[@]}" \
        --role-arn "$EXECUTION_ROLE_ARN" \
        --resource-types "${TARGET_TYPES[@]}" 2>&1)"; then
        echo "AWS refused CreateChangeSet:"
        printf '%s\n' "$out" | scrub
        fail "CreateChangeSet was refused; see the message above (the deploy role's policy may need adjusting)"
    fi
    local end cs_status reason
    end="$(deadline "$changeset_timeout")"
    while :; do
        cs_status="$(aws_text cloudformation describe-change-set --stack-name "$STACK_NAME" \
            --change-set-name "$name" --query Status --output text)"
        case "$cs_status" in
            CREATE_COMPLETE) break ;;
            FAILED)
                reason="$(aws_text cloudformation describe-change-set --stack-name "$STACK_NAME" \
                    --change-set-name "$name" --query StatusReason --output text)"
                if [[ "$reason" == *"didn't contain changes"* || "$reason" == *"No updates are to be performed"* ]]; then
                    aws_text cloudformation delete-change-set --stack-name "$STACK_NAME" \
                        --change-set-name "$name" >/dev/null
                    echo "change set $name: no changes"
                    cs_state=nochanges
                    return 0
                fi
                aws_text cloudformation delete-change-set --stack-name "$STACK_NAME" \
                    --change-set-name "$name" >/dev/null
                fail "the change set failed: $reason"
                ;;
            *)
                if [ "$SECONDS" -ge "$end" ]; then
                    fail "the change set stayed in $cs_status past $changeset_timeout seconds"
                fi
                sleep "$poll"
                ;;
        esac
    done

    local changes action id type bad=0
    changes="$(aws_text cloudformation describe-change-set --stack-name "$STACK_NAME" \
        --change-set-name "$name" \
        --query 'Changes[].[ResourceChange.Action, ResourceChange.LogicalResourceId, ResourceChange.ResourceType]' \
        --output text)"
    summary "Change set \`$key=$value\`:"
    if [ -z "$changes" ] || [ "$changes" = None ]; then
        aws_text cloudformation delete-change-set --stack-name "$STACK_NAME" \
            --change-set-name "$name" >/dev/null
        echo "change set $name: no changes"
        cs_state=nochanges
        return 0
    fi
    while IFS=$'\t' read -r action id type; do
        [ -n "$action" ] || continue
        echo "  change: $action $id ($type)"
        summary "- $action $id"
        case "$action" in Add | Modify) ;; *) bad=1 ;; esac
        case "$allowed" in *" $id "*) ;; *) bad=1 ;; esac
        case "$type" in AWS::ECS::TaskDefinition | AWS::ECS::Service) ;; *) bad=1 ;; esac
    done <<<"$changes"
    if [ "$bad" = 1 ]; then
        aws_text cloudformation delete-change-set --stack-name "$STACK_NAME" \
            --change-set-name "$name" >/dev/null
        fail "the change set holds a change that is not allowed here (listed above); it was deleted"
    fi
    cs_state=created
    cs_name="$name"
}

# execute_and_wait NAME TIMEOUT: execute, poll the stack, fail unless UPDATE_COMPLETE.
execute_and_wait() {
    local name="$1" limit="$2" end st
    aws_text cloudformation execute-change-set --stack-name "$STACK_NAME" \
        --change-set-name "$name" >/dev/null
    end="$(deadline "$limit")"
    while :; do
        st="$(stack_query 'Stacks[0].StackStatus')"
        case "$st" in
            *_IN_PROGRESS)
                if [ "$SECONDS" -ge "$end" ]; then
                    fail "the stack update did not finish within $limit seconds (still $st)"
                fi
                sleep "$poll"
                ;;
            UPDATE_COMPLETE) return 0 ;;
            *)
                echo "the stack ended in $st; failed events:"
                aws cloudformation describe-stack-events --stack-name "$STACK_NAME" \
                    --max-items 100 \
                    --query "StackEvents[?contains(ResourceStatus, 'FAILED')].[LogicalResourceId, ResourceStatus, ResourceStatusReason]" \
                    --output text 2>&1 | scrub || true
                fail "the stack update ended in $st; it is not repaired or continued here"
                ;;
        esac
    done
}

# ---- 2 to 4 -----------------------------------------------------------------------

if [ "$running_tag" = "$version" ]; then
    echo "RunningImageTag already is the version: re-run, going to the checks"
    summary "The stack already runs this version; checks only."
else
    # 2. change set one: the migrate task definition
    if [ "$migrate_tag" = "$version" ]; then
        echo "MigrateImageTag already is the version: skipping the migrate change set"
        summary "MigrateImageTag already set; no first change set."
    else
        change_set "spark-deploy-migrate-$safe_version-$run_id-$run_attempt" \
            MigrateImageTag "$version" "$MIGRATE_IDS"
        if [ "$cs_state" = created ]; then
            execute_and_wait "$cs_name" "$migrate_stack_timeout"
            read_outputs
        fi
    fi

    # 3. the one-off migrate task, from the stack's own outputs only
    echo "running the migrate task"
    run_out="$(aws_text ecs run-task --cluster "$cluster" --task-definition "$migrate_task_def" \
        --launch-type FARGATE --count 1 \
        --network-configuration "awsvpcConfiguration={subnets=[$migrate_subnets],securityGroups=[$migrate_sg],assignPublicIp=DISABLED}" \
        --query '[tasks[0].taskArn, failures[0].reason]' --output text)"
    IFS=$'\t' read -r task_arn run_failure <<<"$run_out"
    if [ -z "$task_arn" ] || [ "$task_arn" = None ]; then
        echo "RunTask started no task: ${run_failure:-no reason given}" | scrub
        fail "the migrate task did not start; the services are untouched"
    fi
    mask "$task_arn" "${task_arn##*/}"
    end="$(deadline "$task_timeout")"
    while :; do
        task_status="$(aws_text ecs describe-tasks --cluster "$cluster" --tasks "$task_arn" \
            --query 'tasks[0].lastStatus' --output text)"
        [ "$task_status" = STOPPED ] && break
        if [ "$task_status" = None ]; then
            fail "the migrate task could not be described; the services are untouched"
        fi
        if [ "$SECONDS" -ge "$end" ]; then
            fail "the migrate task was still $task_status after $task_timeout seconds; the services are untouched"
        fi
        sleep "$poll"
    done
    exit_code="$(aws_text ecs describe-tasks --cluster "$cluster" --tasks "$task_arn" \
        --query "tasks[0].containers[?name=='spark-migrate'].exitCode | [0]" --output text)"
    stop_reason="$(aws_text ecs describe-tasks --cluster "$cluster" --tasks "$task_arn" \
        --query 'tasks[0].stoppedReason' --output text)"
    if [ "$exit_code" != 0 ]; then
        echo "migrate task stopped: reason: $(printf '%s' "$stop_reason" | scrub); exit code: $exit_code"
        echo "(the task's log is not readable by this role: read the migrate log group in the console)"
        fail "the migrate task did not finish with exit code 0; the services are untouched"
    fi
    echo "migrate task finished with exit code 0"
    summary "Migrate and bootstrap task: exit code 0."

    # 4. change set two: the image the services run
    change_set "spark-deploy-$safe_version-$run_id-$run_attempt" \
        ImageTag "$version" "$SERVICE_IDS"
    if [ "$cs_state" = created ]; then
        execute_and_wait "$cs_name" "$stack_timeout"
    fi
    read_outputs
fi

# ---- 5. checks --------------------------------------------------------------------

[ "$running_tag" = "$version" ] || fail "RunningImageTag is $running_tag, not $version"
echo "check: the stack reports the version"
summary "- Stack reports the version: yes"

# check_service LABEL SERVICE TASK_DEFINITION_LOGICAL_ID
check_service() {
    local label="$1" service="$2" logical="$3" expected end primary rollout running desired
    expected="$(aws_text cloudformation describe-stack-resources --stack-name "$STACK_NAME" \
        --logical-resource-id "$logical" --query 'StackResources[0].PhysicalResourceId' --output text)"
    mask "$expected"
    end="$(deadline "$service_timeout")"
    while :; do
        primary="$(aws_text ecs describe-services --cluster "$cluster" --services "$service" \
            --query "services[0].deployments[?status=='PRIMARY'] | [0].taskDefinition" --output text)"
        rollout="$(aws_text ecs describe-services --cluster "$cluster" --services "$service" \
            --query "services[0].deployments[?status=='PRIMARY'] | [0].rolloutState" --output text)"
        running="$(aws_text ecs describe-services --cluster "$cluster" --services "$service" \
            --query 'services[0].runningCount' --output text)"
        desired="$(aws_text ecs describe-services --cluster "$cluster" --services "$service" \
            --query 'services[0].desiredCount' --output text)"
        mask "$primary"
        if [ "$primary" != "$expected" ]; then
            fail "the $label service's primary deployment is not on the stack's task definition"
        fi
        if [ "$rollout" = COMPLETED ] && [ "$running" = "$desired" ] && [ "$desired" -ge 1 ] 2>/dev/null; then
            echo "check: the $label service is on the stack's task definition, rollout COMPLETED, $running of $desired running"
            summary "- $label service: stack's task definition, rollout COMPLETED, $running of $desired running"
            return 0
        fi
        if [ "$SECONDS" -ge "$end" ]; then
            fail "the $label service is not settled: rollout $rollout, $running running of $desired desired"
        fi
        sleep "$poll"
    done
}
check_service web "$web_service" WebTaskDefinition
check_service worker "$worker_service" WorkerTaskDefinition

end="$(deadline "$ready_timeout")"
body_file="$(mktemp)"
trap 'rm -f "$body_file"' EXIT
while :; do
    code="$(curl -sS -o "$body_file" -w '%{http_code}' --max-time 20 "$ready_url" 2>/dev/null || true)"
    if [ "$code" = 200 ] && grep -qF "\"$version\"" "$body_file"; then
        echo "check: /ready answered 200 and reports version $version"
        summary "- /ready: 200, version $version"
        break
    fi
    if [ "$SECONDS" -ge "$end" ]; then
        fail "/ready did not answer 200 with version $version within $ready_timeout seconds (last status ${code:-none})"
    fi
    sleep "$poll"
done

summary ""
summary "Deploy of \`$version\` succeeded."
echo "deploy of $version succeeded"
