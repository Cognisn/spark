#!/usr/bin/env bash
# Verify a Spark release image before it is deployed: the tag resolves to the expected
# digest, and that digest carries a valid signature by the release signing key.
#
#   scripts/verify-image.sh --key <public key file> [--digest sha256:...] <repository>:<tag>[@sha256:...]
#   scripts/verify-image.sh --kms-key awskms:///<key ARN> [--digest sha256:...] <repository>:<tag>[@sha256:...]
#
#   --key FILE       the release key's public key (PEM), for example saved from
#                    `cosign public-key --key awskms:///<key ARN>` or from a publish-central
#                    job summary
#   --kms-key REF    read the public key from AWS KMS instead (needs kms:GetPublicKey and
#                    kms:DescribeKey on the key, and AWS credentials)
#   --digest D       the digest the tag must resolve to (for example the digest you are
#                    about to deploy); a digest in the reference (@sha256:...) is checked too
#
# Exit status: 0 verified; 1 not verified (the tag is missing, names another digest, or the
# digest has no valid signature by the key); 2 a usage error. Nothing is changed anywhere.
#
# Run it before you change the image tag of any Spark instance; scripts/deploy-test.sh runs it
# before it changes the test stack. The signature is checked with the key alone: release images
# are signed without the public transparency log (--tlog-upload=false), so the log is not
# consulted (--insecure-ignore-tlog=true). docs/deployment/release-images.md describes it.
#
# Needs crane and cosign v2 on PATH, logged in to the registry (cosign and crane read the Docker
# credential file). In GitHub Actions (GITHUB_ACTIONS=true) the registry address and the key
# reference are masked first, and no message prints them; ARNs in tool messages are replaced.
set -euo pipefail

usage() {
    echo "usage: verify-image.sh (--key FILE | --kms-key awskms:///ARN) [--digest sha256:...] REPOSITORY:TAG[@sha256:...]" >&2
    exit 2
}

key_file=""
kms_key=""
expected=""
reference=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        --key)
            [ "$#" -ge 2 ] || usage
            key_file="$2"
            shift 2
            ;;
        --kms-key)
            [ "$#" -ge 2 ] || usage
            kms_key="$2"
            shift 2
            ;;
        --digest)
            [ "$#" -ge 2 ] || usage
            expected="$2"
            shift 2
            ;;
        -*) usage ;;
        *)
            [ -z "$reference" ] || usage
            reference="$1"
            shift
            ;;
    esac
done

[ -n "$reference" ] || usage
if [ -n "$key_file" ] && [ -n "$kms_key" ]; then usage; fi
if [ -z "$key_file" ] && [ -z "$kms_key" ]; then usage; fi
if [ -n "$kms_key" ] && [[ "$kms_key" != awskms://* ]]; then
    echo "verify-image: --kms-key takes an awskms:/// reference" >&2
    exit 2
fi

valid_digest() { [[ "$1" =~ ^sha256:[0-9a-f]{64}$ ]]; }

# Split REPOSITORY:TAG[@DIGEST].
pinned=""
name="$reference"
if [[ "$name" == *@* ]]; then
    pinned="${name##*@}"
    name="${name%@*}"
    valid_digest "$pinned" || { echo "verify-image: $pinned is not a sha256 digest" >&2; exit 2; }
fi
last="${name##*/}"
if [[ "$last" != *:* ]] || [[ "$name" != */* ]]; then
    echo "verify-image: the reference needs a repository and a tag (repository:tag)" >&2
    exit 2
fi
tag="${last##*:}"
repository="${name%:*}"
[[ "$tag" =~ ^[A-Za-z0-9_][A-Za-z0-9._-]{0,127}$ ]] || { echo "verify-image: the tag is not valid" >&2; exit 2; }
if [ -n "$expected" ]; then
    valid_digest "$expected" || { echo "verify-image: --digest takes a sha256 digest" >&2; exit 2; }
fi
if [ -n "$key_file" ] && [ ! -s "$key_file" ]; then
    echo "verify-image: the public key file is missing or empty" >&2
    exit 2
fi
key="${key_file:-$kms_key}"

host="${repository%%/*}"
if [ "${GITHUB_ACTIONS:-}" = true ]; then
    for value in "$repository" "$host" "$kms_key" "${kms_key#awskms:///}" "${kms_key##*/}"; do
        [ -z "$value" ] || echo "::add-mask::$value"
    done
fi

# scrub: print tool text with the registry address, the key reference and any ARN replaced.
scrub() {
    local text
    while IFS= read -r text; do
        text="${text//"$repository"/***}"
        text="${text//"$host"/***}"
        if [ -n "$kms_key" ]; then text="${text//"$kms_key"/***}"; fi
        printf '%s\n' "$text"
    done | sed -E 's#arn:aws[a-z-]*:[^[:space:]"'"'"',;)]*#arn:***#g'
}

fail() {
    echo "verify-image: NOT VERIFIED: $*" >&2
    exit 1
}

# 1. The digest the tag names now.
resolved=""
if ! resolved="$(crane digest "$repository:$tag" 2>&1)"; then
    printf '%s\n' "$resolved" | scrub >&2
    fail "could not read tag $tag (see the message above)"
fi
valid_digest "$resolved" || fail "the registry did not return a sha256 digest for tag $tag"

# 2. It must be the digest asked for.
if [ -n "$pinned" ] && [ "$resolved" != "$pinned" ]; then
    fail "tag $tag names $resolved, not $pinned"
fi
if [ -n "$expected" ] && [ "$resolved" != "$expected" ]; then
    fail "tag $tag names $resolved, not the expected $expected"
fi

# 3. That digest must carry a signature by the key. cosign checks the signed payload names
#    this digest; the check below repeats it on cosign's output.
verified=""
if ! verified="$(cosign verify --key "$key" --insecure-ignore-tlog=true \
    --output json "$repository@$resolved" 2>&1)"; then
    printf '%s\n' "$verified" | scrub >&2
    fail "$resolved (tag $tag) has no valid signature by the release key"
fi
if ! grep -qF "\"docker-manifest-digest\":\"$resolved\"" <<<"$verified"; then
    fail "the signature cosign accepted does not name $resolved"
fi

echo "verified: tag $tag is $resolved, signed by the release key"
