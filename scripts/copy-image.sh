#!/usr/bin/env bash
# Copy a published Spark image from ghcr.io to an ECR repository, byte for byte.
#
#   scripts/copy-image.sh <version>      for example 0.3.0rc22
#
# Called by the deploy-test job of .github/workflows/container-publish.yml before
# scripts/deploy-test.sh (to the test account's repository), and by the publish-central job
# (.github/actions/publish-central/publish-central.sh, to the central repository), so both
# copies are made the same way. The copy is made with crane (go-containerregistry), which
# copies the manifest and every blob as they are, so the digest in ECR equals the digest in
# ghcr.io and is the digest that is signed. (docker buildx imagetools create writes a new
# manifest, or wraps a single manifest in a new image index, so its digest differs.) Both
# registries are already logged in: crane reads the same Docker credential file.
#
#   1. Read the source digest.
#   2. If ECR already holds the tag at the same digest, succeed and copy nothing.
#   3. If ECR holds the tag at another digest, fail: ECR tags are immutable, so a new release
#      candidate is needed.
#   4. Otherwise copy, then require the digest ECR reports to equal the source digest.
#
# Configuration comes from the environment, never from this file (the repository is public):
#   ECR_REPOSITORY_URI   the ECR repository, without a tag
#   GITHUB_REPOSITORY    the source repository on ghcr.io (lower-cased here)
#   COPY_TARGET_NAME     how messages name the repository (default ECR)
#
# The ECR repository address is masked (::add-mask::) before anything can print it, and every
# message from crane has it replaced before it is shown.
set -euo pipefail

version="${1:-}"
if [[ ! "$version" =~ ^[0-9A-Za-z][0-9A-Za-z._-]*$ ]]; then
    echo "::error::usage: copy-image.sh <version>"
    exit 2
fi
: "${ECR_REPOSITORY_URI:?ECR_REPOSITORY_URI is not set}"
: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is not set}"
target_name="${COPY_TARGET_NAME:-ECR}"

ecr_host="${ECR_REPOSITORY_URI%%/*}"
for value in "$ECR_REPOSITORY_URI" "$ecr_host"; do
    [ -z "$value" ] || echo "::add-mask::$value"
done

lower_repository="${GITHUB_REPOSITORY,,}"
source="ghcr.io/${lower_repository}:${version}"
target="${ECR_REPOSITORY_URI}:${version}"

# scrub: print registry text with the ECR address and any ARN replaced.
scrub() {
    local text
    while IFS= read -r text; do
        text="${text//"$ECR_REPOSITORY_URI"/***}"
        text="${text//"$ecr_host"/***}"
        printf '%s\n' "$text"
    done | sed -E 's#arn:aws[a-z-]*:[^[:space:]"'"'"',;)]*#arn:***#g'
}

fail() {
    echo "::error::$*"
    exit 1
}

valid_digest() { [[ "$1" =~ ^sha256:[0-9a-f]{64}$ ]]; }

source_digest=""
if ! source_digest="$(crane digest "$source" 2>&1)"; then
    printf '%s\n' "$source_digest" | scrub
    fail "could not read the digest of the published image for $version from ghcr.io"
fi
valid_digest "$source_digest" || fail "the published image's digest for $version is not a sha256 digest"
echo "published digest: $source_digest"

existing=""
if existing="$(crane digest "$target" 2>&1)"; then
    valid_digest "$existing" || fail "the digest $target_name reports for $version is not a sha256 digest"
    if [ "$existing" = "$source_digest" ]; then
        echo "the image is already in $target_name with the same digest; nothing to copy"
        exit 0
    fi
    fail "$target_name already holds $version with a different digest. An immutable tag cannot be overwritten: publish a new release candidate (the next release candidate number) instead of re-running $version"
fi
case "$existing" in
    *MANIFEST_UNKNOWN* | *NAME_UNKNOWN* | *"not found"* | *"Not Found"* | *404*) ;;
    *)
        printf '%s\n' "$existing" | scrub
        fail "could not read the $target_name tag for $version (not a missing tag); see the message above"
        ;;
esac

echo "copying $version to $target_name"
if ! copy_out="$(crane copy "$source" "$target" 2>&1)"; then
    printf '%s\n' "$copy_out" | scrub
    fail "the copy of $version to $target_name failed; see the message above"
fi

copied=""
if ! copied="$(crane digest "$target" 2>&1)"; then
    printf '%s\n' "$copied" | scrub
    fail "could not read the digest of the copied image for $version from $target_name"
fi
[ "$copied" = "$source_digest" ] || fail "the copied image's digest in $target_name differs from the published one for $version"
echo "copied $version to $target_name with digest $source_digest"
