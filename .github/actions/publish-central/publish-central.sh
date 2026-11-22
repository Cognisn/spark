#!/usr/bin/env bash
# Put a published Spark release image into the central repository and sign it there.
#
#   .github/actions/publish-central/publish-central.sh <version>      for example 0.3.0rc38
#
# Run by the publish-central job of .github/workflows/container-publish.yml, after the image is
# on ghcr.io, with the release role's ECR login, a ghcr.io login, crane and cosign v2 on PATH.
#
#   1. Read the digest of ghcr.io/<repository>:<version>.
#   2. Copy it to <central>:<version> with scripts/copy-image.sh, the same script and tool
#      (crane, byte for byte) as the test copy: an existing tag with the same digest is a
#      re-run and nothing is copied; a different digest fails (tags are immutable).
#   3. Read the central tag back: it must name the published digest.
#   4. Sign <central>@<digest> with cosign and the KMS key, without the public transparency
#      log, unless a signature by the key already verifies (a re-run). A signature tag that
#      exists but does not verify stops the job: it cannot be overwritten.
#   5. Verify with scripts/verify-image.sh --kms-key, then write the digest to the step's
#      outputs (digest=...) and the job summary, with the public key in PEM.
#
# Configuration comes from the environment, never from this file (the repository is public):
#   CENTRAL_ECR_REPOSITORY_URI   the central repository, without a tag
#   IMAGE_SIGNING_KEY            cosign's key reference, awskms:///<key ARN>
#   GITHUB_REPOSITORY            the source repository on ghcr.io
#   GITHUB_OUTPUT, GITHUB_STEP_SUMMARY   set by GitHub Actions (optional elsewhere)
#
# The repository address and the key reference are masked first, and no message prints them.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
scripts="$(cd "$here/../../../scripts" && pwd)"

version="${1:-}"
if [[ ! "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+(rc[0-9]+)?$ ]]; then
    echo "::error::usage: publish-central.sh <version> (a final version or a release candidate)"
    exit 2
fi
: "${CENTRAL_ECR_REPOSITORY_URI:?CENTRAL_ECR_REPOSITORY_URI is not set}"
: "${IMAGE_SIGNING_KEY:?IMAGE_SIGNING_KEY is not set}"
: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is not set}"

central="$CENTRAL_ECR_REPOSITORY_URI"
host="${central%%/*}"
account="${host%%.*}"
key_arn="${IMAGE_SIGNING_KEY#awskms:///}"
for value in "$central" "$host" "$account" "$IMAGE_SIGNING_KEY" "$key_arn" "${key_arn##*/}"; do
    [ -z "$value" ] || echo "::add-mask::$value"
done
if [[ "$IMAGE_SIGNING_KEY" != awskms:///* ]]; then
    echo "::error::IMAGE_SIGNING_KEY must be an awskms:/// key reference"
    exit 2
fi

scrub() {
    local text
    while IFS= read -r text; do
        text="${text//"$central"/***}"
        text="${text//"$host"/***}"
        text="${text//"$IMAGE_SIGNING_KEY"/***}"
        printf '%s\n' "$text"
    done | sed -E 's#arn:aws[a-z-]*:[^[:space:]"'"'"',;)]*#arn:***#g'
}

summary() {
    if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
        printf '%s\n' "$*" >>"$GITHUB_STEP_SUMMARY"
    fi
}

fail() {
    echo "::error::$*"
    summary "Central push FAILED for \`$version\`: $*"
    exit 1
}

valid_digest() { [[ "$1" =~ ^sha256:[0-9a-f]{64}$ ]]; }

source="ghcr.io/${GITHUB_REPOSITORY,,}:${version}"

# 1. The published digest.
digest=""
if ! digest="$(crane digest "$source" 2>&1)"; then
    printf '%s\n' "$digest" | scrub
    fail "could not read the digest of the published image for $version from ghcr.io"
fi
valid_digest "$digest" || fail "the published image's digest for $version is not a sha256 digest"

# 2. The copy, shared with the test deploy.
if ! ECR_REPOSITORY_URI="$central" COPY_TARGET_NAME="the central repository" \
    bash "$scripts/copy-image.sh" "$version"; then
    fail "the copy of $version to the central repository did not complete; see the messages above"
fi

# 3. Read it back.
central_digest=""
if ! central_digest="$(crane digest "$central:$version" 2>&1)"; then
    printf '%s\n' "$central_digest" | scrub
    fail "could not read $version back from the central repository"
fi
[ "$central_digest" = "$digest" ] \
    || fail "the central tag $version names $central_digest, not the published $digest; nothing was signed"
echo "the central tag $version names the published digest $digest"

# 4. Sign once.
verify() {
    bash "$scripts/verify-image.sh" --kms-key "$IMAGE_SIGNING_KEY" --digest "$digest" \
        "$central:$version"
}
signature_tag="sha256-${digest#sha256:}.sig"
if verify >/dev/null 2>&1; then
    echo "a signature by the release key already verifies for $digest; not signing again"
    summary_signed="already signed (a re-run)"
else
    if crane digest "$central:$signature_tag" >/dev/null 2>&1; then
        fail "the central repository holds a signature tag for $digest that does not verify with the release key. Tags are immutable, so it cannot be replaced: publish a new version"
    fi
    echo "signing $digest in the central repository (no transparency log upload)"
    sign_out=""
    if ! sign_out="$(cosign sign --yes --key "$IMAGE_SIGNING_KEY" --tlog-upload=false \
        "$central@$digest" 2>&1)"; then
        printf '%s\n' "$sign_out" | scrub
        fail "cosign could not sign $digest; see the message above (the release role needs kms:Sign, kms:GetPublicKey and kms:DescribeKey on the key)"
    fi
    summary_signed="signed"
fi

# 5. Verify as an operator would.
verify_out=""
if ! verify_out="$(verify 2>&1)"; then
    printf '%s\n' "$verify_out" | scrub
    fail "the signed image did not verify; see the message above"
fi
printf '%s\n' "$verify_out" | scrub

public_key=""
if ! public_key="$(cosign public-key --key "$IMAGE_SIGNING_KEY" 2>&1)"; then
    printf '%s\n' "$public_key" | scrub
    public_key="(could not be read)"
fi

if [ -n "${GITHUB_OUTPUT:-}" ]; then
    echo "digest=$digest" >>"$GITHUB_OUTPUT"
fi
summary "## Central repository"
summary ""
summary "- Version: \`$version\`"
summary "- Digest: \`$digest\` (the same as ghcr.io)"
summary "- Signature: $summary_signed, verified with the release key"
summary ""
summary "Public key of the release key (for scripts/verify-image.sh --key):"
summary ""
summary '```'
summary "$public_key"
summary '```'
echo "central push of $version complete: $digest"
