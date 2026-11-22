# Release images: the vulnerability gate, the central repository and signing

Every Spark release image follows one path from the build to a running instance:

```
build and check (container.yml)
  -> vulnerability gate (Trivy)                    nothing is pushed if it fails
  -> ghcr.io/cognisn/spark:<version>               the scanned image, by docker push
  -> central repository <central>:<version>        copied byte for byte (publish-central)
  -> signed: <central>@<digest>                    cosign with the KMS key, no public log
  -> verified before deploy (scripts/verify-image.sh)
       test environment: deploy-test, every release candidate
       client instances: the operator, before changing the image tag
```

One digest names the image all the way: the digest built and scanned is the digest on
ghcr.io, in the test account's repository, in the central repository, and the one signed
and verified. An instance only runs a digest the release process built, scanned and signed.

Release candidates (`v0.3.0rcN`) and final versions (`v0.3.0`) take the whole path; alpha
and beta versions are never pushed. A manual run of the "Container publish" workflow pushes
to ghcr.io only.

## The vulnerability gate

`.github/actions/vulnerability-gate` runs in every image build (`container.yml`), after
`deploy/check-image.sh` and before anything is pushed. It scans the image that was just built
and loaded, and the push that follows sends that same image, so what reaches ghcr.io is what
was scanned.

The scan is Trivy (`aquasecurity/trivy-action`, pinned by commit, with a pinned Trivy
version), over the operating system packages and the Python libraries, for critical and high
findings, fixed or not. `evaluate.py`, beside the action, decides:

| Finding | Effect |
|---|---|
| Critical, with a fixed version, not accepted | **Fails the build** |
| Critical, no fixed version | Reported in the job summary |
| High, with or without a fix | Reported in the job summary (up to 200 rows) |
| Medium and lower | Not considered |

The job summary lists the counts, every reported finding with its package, installed version
and fixed version, and every accepted finding.

### Accepting a finding

A finding is accepted only by an entry in `.trivyignore` at the repository root, in Trivy's own
syntax (so a local `trivy image` honours it too), with two comments directly above it:

```
# reason: the vulnerable function is not reachable from Spark (it parses X, which Spark never does)
# accepted-by: A Person
CVE-2026-12345 exp:2026-12-31
```

The gate fails, whether or not the finding is still present, on an entry with no
`# reason:`, no `# accepted-by:`, no `exp:YYYY-MM-DD` or a malformed one, on an entry listed
twice, and on an expired entry (its date has passed; an entry is valid through its date, in
UTC). Keep expiries short, and remove an entry once the finding is fixed. Prefer fixing: a new
base image or dependency version usually clears a finding with a fix.

To run the gate locally against an image you built:

```bash
trivy image --scanners vuln --pkg-types os,library --severity CRITICAL,HIGH \
  --format json --output trivy.json spark:dev
python3 .github/actions/vulnerability-gate/evaluate.py --report trivy.json --ignore-file .trivyignore
```

## The central push

The `publish-central` job of `.github/workflows/container-publish.yml` runs after the image is
on ghcr.io, for every `v*` tag push, in the GitHub environment `spark-release` (deployments
limited to `v*` tags). It signs in to AWS with GitHub's OIDC token as the release role
(`AWS_RELEASE_ROLE_ARN`), so the repository holds no AWS secret, and runs
`.github/actions/publish-central`:

1. Reads the digest of `ghcr.io/cognisn/spark:<version>`.
2. Copies it to `CENTRAL_ECR_REPOSITORY_URI:<version>` with `scripts/copy-image.sh`, the same
   script and the same pinned `crane` as the test copy. `crane copy` writes the source
   manifest itself, byte for byte, so the central tag's digest is the ghcr.io digest. If the
   central tag already names that digest (a re-run), nothing is copied; if it names another
   digest, the job fails: central tags are immutable, so publish a new version instead.
3. Reads the central tag back and requires the ghcr.io digest. Nothing is signed otherwise.
4. Signs (see below), verifies with `scripts/verify-image.sh --kms-key`, and writes the digest
   and the release key's public key (PEM) to the job summary.

`docker buildx imagetools create` is not used for either copy: given one manifest it writes a
new image index around it, under a new digest, so the tag would not name the digest that is
signed.

The release role, its trust and its least-privilege permissions are in
[Running on AWS](aws.md#the-central-repository-and-the-release-role). The repository and its
run logs are public: the job masks every AWS identifier before a step can print it, and no
message prints one.

## Signing

The digest in the central repository is signed with cosign v2 and the asymmetric KMS key named
by `IMAGE_SIGNING_KEY` (`awskms:///<key ARN>`); the private key never leaves KMS.

- **No public transparency log.** Images are signed with `--tlog-upload=false` and verified
  with `--insecure-ignore-tlog=true`. Trust rests on the release key alone.
- **Signatures survive lifecycle rules.** cosign v2 stores a signature as a manifest tagged
  `sha256-<digest>.sig` beside the image. A rule that expires untagged images (the central
  repository's 7-day rule) does not touch it. cosign v3 stores signatures as untagged OCI
  referrers by default, which such a rule would remove, so the release stays on cosign v2
  (`.github/actions/install-cosign`) until that is changed deliberately.
- **Signed once.** Tags are immutable, so a signature tag cannot be rewritten. A re-run whose
  signature already verifies does not sign again; a signature tag that exists but does not
  verify with the release key stops the job.

## Verifying an image

`scripts/verify-image.sh` checks that a tag names the digest you expect and that the digest
carries a valid signature by the release key. It changes nothing. It needs `crane` and
cosign v2, logged in to the registry (`aws ecr get-login-password | docker login ...` for ECR).

```bash
# with the public key (from a publish-central job summary, or `cosign public-key --key awskms:///<key ARN>`)
scripts/verify-image.sh --key release-signing.pub \
  123456789012.dkr.ecr.ap-southeast-2.amazonaws.com/spark:0.3.0

# with the public key read from KMS (needs kms:GetPublicKey and kms:DescribeKey on the key)
scripts/verify-image.sh --kms-key awskms:///arn:aws:kms:ap-southeast-2:123456789012:key/CHANGEME \
  123456789012.dkr.ecr.ap-southeast-2.amazonaws.com/spark:0.3.0

# also require the digest you are about to deploy
scripts/verify-image.sh --key release-signing.pub --digest sha256:<digest> \
  123456789012.dkr.ecr.ap-southeast-2.amazonaws.com/spark:0.3.0
```

It exits 0 and prints `verified: tag <tag> is <digest>, signed by the release key` only when
everything matches; 1 when the tag is missing, names another digest, or the digest has no valid
signature by the key; 2 on a usage error.

**Before changing a client instance's image tag, run `verify-image.sh` against the central
reference of the version, and change the tag only if it exits 0.** Pin the instance to the
version tag that was verified (there is no `latest`).

The test environment does this itself: `scripts/deploy-test.sh` requires the test copy's digest
to be the digest `publish-central` signed and runs `verify-image.sh` on the central reference
before any other call, so a release candidate that is not signed is never deployed
([Deploying to the test environment](aws.md#deploying-to-the-test-environment)).

## When it is first used

The first tag after this was added runs the gate, the central push and signing only once the
AWS side is in place (the release role, the signing key, the central repository's policy and
the test deploy role's read access, as [Running on AWS](aws.md#the-central-repository-and-the-release-role)
lists) and the `spark-release` environment exists with its four variables. Until then
`publish-central` fails at its first step, and the release-candidate test deploy, which waits
for it, does not start. The test deploy also needs the `spark-test` variables
`CENTRAL_ECR_REPOSITORY_URI` and `IMAGE_SIGNING_PUBLIC_KEY` (the PEM from the first
`publish-central` job summary). Versions published before signing began have no signature and
cannot be deployed by `deploy-test.sh`.

## How this was checked

The scripts are tested against fakes of `crane`, `cosign` and the AWS CLI (`tests/scripts/`),
including a copy that lands as another digest, a signature by another key, a signature naming
another digest and an expired accepted finding. The gate was run locally with the pinned Trivy
version against an image built from this repository. Nothing here has yet run against the real
registries or the KMS key.
