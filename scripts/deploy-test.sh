#!/usr/bin/env bash
# Deploy a release candidate to the Spark test environment through CloudFormation.
#
#   scripts/deploy-test.sh <version>      for example 0.3.0rc17
#
# Called by the deploy-test job of .github/workflows/container-publish.yml after the image
# is in the test account's ECR repository and the publish-central job has pushed and signed it
# in the central repository. It never registers a task definition, updates a service or updates the stack
# directly: CloudFormation is the only thing that changes the services, through change sets
# that reuse the stack's current template and change one parameter each.
#
#   0. Verify, before anything else on every path: the test copy's digest (and EXPECTED_DIGEST,
#      when given) is the digest the central tag names, and that digest is signed by the
#      release key (scripts/verify-image.sh). A failure stops here and nothing is changed, so
#      the test stack only ever runs a signed digest.
#   1. Read the service stack. A re-run with the version already running goes to step 5.
#   1a. The hardening check, read only and on every path: each Spark container of the
#      migrate, web and worker task definitions runs as 10001, with a read-only root, no
#      privilege, every capability dropped, an init process and a writable /tmp task volume,
#      and neither service has ECS Exec on (docs/deployment/container-hardening.md). Enforced,
#      a failure stops here and nothing is changed; otherwise it is a warning (HARDENING_CHECK).
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
#   ECR_REPOSITORY_URI     the test account's ECR repository the stack pulls from
#   CENTRAL_ECR_REPOSITORY_URI   the central repository holding the signed image
#   IMAGE_SIGNING_PUBLIC_KEY     the release key's public key, PEM
#   EXPECTED_DIGEST        optional: the digest publish-central signed (the release job sets it)
#   GITHUB_RUN_ID, GITHUB_RUN_ATTEMPT   named in the change sets (default 0 and 1)
#   READY_URL              default https://test.spark.cognisn.net/ready
#   POLL_INTERVAL          seconds between polls (default 10)
#   CHANGESET_TIMEOUT, MIGRATE_STACK_TIMEOUT, TASK_TIMEOUT, STACK_TIMEOUT, SERVICE_TIMEOUT,
#   READY_TIMEOUT          bounds in seconds (defaults 300, 900, 1200, 1800, 600, 600)
#   HARDENING_CHECK        enforce, warn, or enforce-from:YYYY-MM-DD (warn before that UTC
#                          date, enforce from it); default HARDENING_CHECK_DEFAULT below
#
# Every identifier read from AWS is masked (::add-mask::) before it can be printed, and
# nothing prints an identifier on purpose: the Actions log of a public repository is public.
set -euo pipefail

TARGET_TYPES=(AWS::ECS::TaskDefinition AWS::ECS::Service)
MIGRATE_IDS=" MigrateTaskDefinition "
SERVICE_IDS=" WebTaskDefinition WorkerTaskDefinition WebService WorkerService "
# What the hardening check does when a control is missing (see HARDENING_CHECK above).
# The default is enforce, since the first test deploy whose job summary showed
# "Hardening check: passed" (v0.3.0rc44). HARDENING_CHECK=warn is the override for a one-off
# deploy that must go on despite a missing control.
HARDENING_CHECK_DEFAULT="enforce"

version="${1:-}"
if [[ ! "$version" =~ ^[0-9A-Za-z][0-9A-Za-z._-]*$ ]]; then
    echo "::error::usage: deploy-test.sh <version>"
    exit 2
fi
: "${STACK_NAME:?STACK_NAME is not set}"
: "${EXECUTION_ROLE_ARN:?EXECUTION_ROLE_ARN is not set}"
: "${AWS_REGION:?AWS_REGION is not set}"
# The verification needs these; without them nothing is read or changed.
for name in ECR_REPOSITORY_URI CENTRAL_ECR_REPOSITORY_URI IMAGE_SIGNING_PUBLIC_KEY; do
    if [ -z "${!name:-}" ]; then
        echo "::error::$name is not set; the image cannot be verified, so nothing is deployed"
        exit 2
    fi
done
hardening_policy="${HARDENING_CHECK:-$HARDENING_CHECK_DEFAULT}"
case "$hardening_policy" in
    enforce)
        hardening_enforce=1
        hardening_note="enforced"
        ;;
    warn)
        hardening_enforce=0
        hardening_note="warning only, not enforced"
        ;;
    enforce-from:*)
        hardening_from="${hardening_policy#enforce-from:}"
        if [[ ! "$hardening_from" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]; then
            echo "::error::HARDENING_CHECK must be enforce, warn or enforce-from:YYYY-MM-DD"
            exit 2
        fi
        if [[ "$(date -u +%F)" < "$hardening_from" ]]; then
            hardening_enforce=0
            hardening_note="warning only until $hardening_from, enforced from that date"
        else
            hardening_enforce=1
            hardening_note="enforced since $hardening_from"
        fi
        ;;
    *)
        echo "::error::HARDENING_CHECK must be enforce, warn or enforce-from:YYYY-MM-DD"
        exit 2
        ;;
esac
export AWS_PAGER=""
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

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

# fail MESSAGE: print the error (to stderr, so that it still shows when fail runs inside a
# command substitution, whose standard output is captured), note it in the job summary, stop.
fail() {
    echo "::error::$*" | scrub >&2
    summary "Deploy FAILED: $(printf '%s' "$*" | scrub)"
    exit 1
}

# Mask the identifiers we know before anything can print them.
mask "$STACK_NAME" "$EXECUTION_ROLE_ARN" "${EXECUTION_ROLE_ARN##*/}"
account="${EXECUTION_ROLE_ARN#arn:*:iam::}"
mask "${account%%:*}"
test_host="${ECR_REPOSITORY_URI%%/*}"
central_host="${CENTRAL_ECR_REPOSITORY_URI%%/*}"
mask "$ECR_REPOSITORY_URI" "$test_host" "${test_host%%.*}" \
    "$CENTRAL_ECR_REPOSITORY_URI" "$central_host" "${central_host%%.*}"

# aws_text ARGS...: run the AWS CLI and print its output; on failure print its (scrubbed)
# message and stop. The messages go to stderr: aws_text is mostly called inside $( ), where
# standard output is the value being captured and a message printed there is lost.
aws_text() {
    local out
    if ! out="$(aws "$@" 2>&1)"; then
        echo "AWS refused or failed a call (aws $1 $2):" >&2
        printf '%s\n' "$out" | scrub >&2
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

# ---- 0. verify the image ----------------------------------------------------------

# scrub_registry: print registry text with both repository addresses replaced, and any ARN.
scrub_registry() {
    local text
    while IFS= read -r text; do
        text="${text//"$ECR_REPOSITORY_URI"/***}"
        text="${text//"$test_host"/***}"
        text="${text//"$CENTRAL_ECR_REPOSITORY_URI"/***}"
        text="${text//"$central_host"/***}"
        printf '%s\n' "$text"
    done | scrub
}

# The digest the stack will run is the one the test copy's tag names; it must be the digest
# the central tag names and the release key signed.
test_digest=""
if ! test_digest="$(crane digest "$ECR_REPOSITORY_URI:$version" 2>&1)"; then
    printf '%s\n' "$test_digest" | scrub_registry
    fail "could not read the test copy of $version; nothing was changed"
fi
[[ "$test_digest" =~ ^sha256:[0-9a-f]{64}$ ]] \
    || fail "the test copy of $version has no sha256 digest; nothing was changed"
if [ -n "${EXPECTED_DIGEST:-}" ] && [ "$test_digest" != "$EXPECTED_DIGEST" ]; then
    fail "the test copy of $version is $test_digest, not the digest publish-central signed ($EXPECTED_DIGEST); nothing was changed"
fi
key_file="$(mktemp)"
printf '%s\n' "$IMAGE_SIGNING_PUBLIC_KEY" >"$key_file"
verify_status=0
verify_out="$(bash "$here/verify-image.sh" --key "$key_file" --digest "$test_digest" \
    "$CENTRAL_ECR_REPOSITORY_URI:$version" 2>&1)" || verify_status=$?
rm -f "$key_file"
printf '%s\n' "$verify_out" | scrub_registry
if [ "$verify_status" != 0 ]; then
    fail "$version ($test_digest) is not verified as signed by the release key in the central repository; nothing was changed"
fi

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
summary "Image: \`$test_digest\`, verified as signed by the release key."

# ---- 1a. the hardening check ------------------------------------------------------

# The change sets reuse the stack's current template, so the task definitions they register
# carry the same container settings as the ones the stack holds now. Checking those, and the
# services' ECS Exec setting, before any change shows whether the deploy keeps the controls
# of docs/deployment/container-hardening.md. Every call here only reads.

# One tab-separated item per container, its fields joined by '|' so that an empty field keeps
# its place.
# shellcheck disable=SC2016 # the backticks are JMESPath literals, not command substitution
CONTAINER_QUERY='taskDefinition.containerDefinitions[].join(`"|"`, [name, image, to_string(user), to_string(readonlyRootFilesystem), to_string(privileged), join(`","`, linuxParameters.capabilities.drop || `[]`), join(`","`, linuxParameters.capabilities.add || `[]`), to_string(linuxParameters.initProcessEnabled), join(`","`, (mountPoints || `[]`)[?containerPath==`"/tmp"`].sourceVolume), to_string(length((mountPoints || `[]`)[?containerPath==`"/tmp"` && readOnly]))])'
# One item per task volume: its name, whether it names a host path, whether it is EFS, Docker
# or FSx storage. No identifier (a file system ID) is read.
# shellcheck disable=SC2016 # as above
VOLUME_QUERY='taskDefinition.volumes[].join(`"|"`, [name, to_string(host.sourcePath != `null`), to_string(efsVolumeConfiguration != `null` || dockerVolumeConfiguration != `null` || fsxWindowsFileServerVolumeConfiguration != `null`)])'

hardening_problems=()
problem() { hardening_problems+=("$*"); }

# hardening_read ARGS...: run a read-only AWS call into $reply; on failure $reply holds the
# scrubbed message and the call returns 1 (a hardening problem, not the end of the deploy).
hardening_read() {
    if ! reply="$(aws "$@" 2>&1)"; then
        reply="$(printf '%s\n' "$reply" | scrub_registry | tr '\n' ' ')"
        return 1
    fi
}

# check_task_definition LABEL ARN
check_task_definition() {
    local label="$1" arn="$2" containers volumes row found=0 where
    local name image user ro priv drop add init tmp_source tmp_read_only
    local vname vhost vother tmp_kind
    mask "$arn" "${arn##*/}" "$(basename "${arn%:*}")"
    if ! hardening_read ecs describe-task-definition --task-definition "$arn" \
        --query "$CONTAINER_QUERY" --output text; then
        problem "the $label task definition could not be read ($reply)"
        return 0
    fi
    containers="$reply"
    if ! hardening_read ecs describe-task-definition --task-definition "$arn" \
        --query "$VOLUME_QUERY" --output text; then
        problem "the $label task definition's volumes could not be read ($reply)"
        return 0
    fi
    volumes="$reply"
    local -a rows=()
    IFS=$'\t' read -r -a rows <<<"$containers" || true
    for row in "${rows[@]}"; do
        IFS='|' read -r name image user ro priv drop add init tmp_source tmp_read_only <<<"$row"
        case "$image" in
            "$ECR_REPOSITORY_URI:"* | "$ECR_REPOSITORY_URI@"*) ;;
            *) continue ;; # not a Spark container
        esac
        found=1
        where="the $label task's container $name"
        case "$user" in
            10001 | 10001:10001) ;;
            *) problem "$where does not run as user 10001:10001" ;;
        esac
        [ "$ro" = true ] || problem "$where does not have readonlyRootFilesystem true"
        [ "$priv" != true ] || problem "$where is privileged"
        case ",$drop," in
            *,ALL,*) ;;
            *) problem "$where does not drop ALL capabilities (linuxParameters.capabilities.drop)" ;;
        esac
        [ -z "$add" ] || problem "$where adds capabilities ($add)"
        [ "$init" = true ] || problem "$where does not have linuxParameters.initProcessEnabled true"
        if [ -z "$tmp_source" ]; then
            problem "$where mounts no task volume at /tmp"
            continue
        fi
        if [ "$tmp_read_only" != 0 ]; then
            problem "$where mounts /tmp read-only"
            continue
        fi
        tmp_kind=missing
        while IFS='|' read -r vname vhost vother; do
            [ "$vname" = "$tmp_source" ] || continue
            if [ "$vhost" != false ]; then
                tmp_kind=host
            elif [ "$vother" != false ]; then
                tmp_kind=shared
            else
                tmp_kind=task
            fi
        done < <(printf '%s\n' "$volumes" | tr '\t' '\n')
        case "$tmp_kind" in
            task) ;;
            host) problem "$where mounts /tmp from a host path, not a task volume" ;;
            shared) problem "$where mounts /tmp from EFS, Docker or FSx storage, not a task volume on the task's ephemeral storage" ;;
            *) problem "$where mounts /tmp from a volume the task definition does not declare" ;;
        esac
    done
    [ "$found" = 1 ] || problem "the $label task definition has no container running the Spark image"
}

# check_exec LABEL SERVICE: the service must have ECS Exec off.
check_exec() {
    local label="$1" service="$2"
    if ! hardening_read ecs describe-services --cluster "$cluster" --services "$service" \
        --query 'services[0].enableExecuteCommand' --output text; then
        problem "the $label service could not be read ($reply)"
        return 0
    fi
    [ "$reply" = False ] || problem "the $label service does not have ECS Exec off (enableExecuteCommand is $reply)"
}

# check_stack_task_definition LABEL LOGICAL_ID: check one of the stack's task definitions.
check_stack_task_definition() {
    local label="$1" logical="$2"
    if ! hardening_read cloudformation describe-stack-resources --stack-name "$STACK_NAME" \
        --logical-resource-id "$logical" --query 'StackResources[0].PhysicalResourceId' \
        --output text; then
        problem "the stack's $label task definition could not be found ($reply)"
        return 0
    fi
    case "$reply" in
        "" | None) problem "the stack's $label task definition could not be found" ;;
        *) check_task_definition "$label" "$reply" ;;
    esac
}

check_task_definition migrate "$migrate_task_def"
check_stack_task_definition web WebTaskDefinition
check_stack_task_definition worker WorkerTaskDefinition
check_exec web "$web_service"
check_exec worker "$worker_service"

hardening_doc="docs/deployment/container-hardening.md (ECS on Fargate)"
if [ "${#hardening_problems[@]}" = 0 ]; then
    echo "check: hardening: every Spark container of the migrate, web and worker task definitions carries the controls, and ECS Exec is off on both services"
    summary "Hardening check: passed ($hardening_note). Every Spark container in the migrate, web and worker task definitions runs as 10001:10001 with a read-only root, no privilege, every capability dropped, an init process and a writable /tmp task volume; ECS Exec is off on both services."
else
    if [ "$hardening_enforce" = 1 ]; then
        level=error
        summary "Hardening check: FAILED ($hardening_note), nothing was changed:"
    else
        level=warning
        summary "Hardening check: NOT MET ($hardening_note); the deploy continues:"
    fi
    for item in "${hardening_problems[@]}"; do
        echo "::$level::hardening: $item" | scrub_registry
        summary "- $(printf '%s\n' "$item" | scrub_registry)"
    done
    summary "The settings to apply are in $hardening_doc."
    if [ "$hardening_enforce" = 1 ]; then
        fail "the test stack does not carry the container hardening controls (${#hardening_problems[@]} problems, listed above; see $hardening_doc); nothing was changed"
    fi
    echo "the hardening check is not enforced ($hardening_note); continuing"
fi

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

    # 3. the one-off migrate task, from the stack's own outputs only, with ECS Exec off
    echo "running the migrate task"
    run_out="$(aws_text ecs run-task --cluster "$cluster" --task-definition "$migrate_task_def" \
        --launch-type FARGATE --count 1 --disable-execute-command \
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
