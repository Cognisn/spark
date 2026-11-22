"""A stand-in for ``crane`` and ``cosign`` v2, for testing the image scripts.

Run as ``python registry_stub.py crane <args>`` or ``python registry_stub.py cosign <args>``
(the tests put shims of those names on PATH that do this). It keeps a model of the
registries in the ``registry`` key of the JSON file named by ``STUB_STATE`` and appends every
call to that file's ``calls`` list, the same list ``aws_stub`` writes, so a test sees the
order of every call across the tools. All names are invented.

The model:

* ``tags``: ``repository:tag`` to digest. Every repository has immutable tags: writing a
  tag that holds another digest is refused, as ECR refuses it.
* ``signatures``: ``repository@digest`` to the name of the key that signed it. A signature
  is stored the way cosign v2 stores it, as the tag ``sha256-<hex>.sig`` in the same
  repository (so it is also in ``tags``).
* ``keys``: a key reference (``awskms:///...``) or the text of a public key file, to the
  name of the key it is. ``public_keys``: a key name to its PEM text.
* switches: ``copy_rewrites_to`` (a copy lands as this digest instead, as an image index
  wrapped round the manifest would), ``sign_denied`` (KMS refuses), ``claim_digest`` (cosign
  verify succeeds but its payload names this digest instead).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

MISSING = "MANIFEST_UNKNOWN: Requested image not found"


def empty_registry(**overrides: Any) -> dict[str, Any]:
    registry: dict[str, Any] = {
        "tags": {},
        "signatures": {},
        "keys": {},
        "public_keys": {},
        "copy_rewrites_to": None,
        "sign_denied": False,
        "claim_digest": None,
    }
    registry.update(overrides)
    return registry


def split(reference: str) -> tuple[str, str | None, str | None]:
    """``(repository, tag, digest)`` of ``repo:tag``, ``repo@digest`` or ``repo:tag@digest``."""
    digest = None
    if "@" in reference:
        reference, digest = reference.split("@", 1)
    last = reference.rsplit("/", 1)[-1]
    if ":" in last:
        repository, tag = reference.rsplit(":", 1)
        return repository, tag, digest
    return reference, None, digest


def resolve(registry: dict[str, Any], reference: str) -> str | None:
    repository, tag, digest = split(reference)
    if digest is not None:
        known = set(registry["tags"].values())
        return digest if digest in known else None
    found = registry["tags"].get(f"{repository}:{tag}")
    return str(found) if found else None


def fail(message: str, code: int = 1) -> None:
    print(message, file=sys.stderr)
    sys.exit(code)


def option(args: list[str], name: str) -> str | None:
    for index, item in enumerate(args):
        if item == name and index + 1 < len(args):
            return args[index + 1]
        if item.startswith(name + "="):
            return item.split("=", 1)[1]
    return None


def key_name(registry: dict[str, Any], key: str) -> str | None:
    if key.startswith("awskms://"):
        return registry["keys"].get(key)
    path = Path(key)
    if not path.is_file():
        return None
    return registry["keys"].get(path.read_text(encoding="utf-8").strip())


def crane(registry: dict[str, Any], args: list[str]) -> None:
    command = args[0] if args else ""
    if command == "digest":
        digest = resolve(registry, args[1])
        if digest is None:
            fail(f"GET https://{split(args[1])[0].split('/')[0]}/v2/...: {MISSING}")
        print(digest)
    elif command == "copy":
        source, target = args[1], args[2]
        digest = resolve(registry, source)
        if digest is None:
            fail(f"fetching {source}: {MISSING}")
        assert digest is not None
        landed = registry["copy_rewrites_to"] or digest
        repository, tag, _ = split(target)
        key = f"{repository}:{tag}"
        held = registry["tags"].get(key)
        if held and held != landed:
            fail(
                f"PUT https://{repository.split('/')[0]}/v2/...: TAG_INVALID: The image tag "
                f"'{tag}' already exists in the repository and cannot be overwritten because "
                "the tag is immutable."
            )
        registry["tags"][key] = landed
    else:
        fail(f"stub: crane {command} is not modelled", 2)


def cosign(registry: dict[str, Any], args: list[str]) -> None:
    command = args[0] if args else ""
    if command == "sign":
        key = option(args, "--key") or ""
        reference = args[-1]
        repository, _, digest = split(reference)
        if digest is None:
            fail("stub: cosign sign was given a tag, not a digest", 2)
        if resolve(registry, reference) is None:
            fail(f"signing {reference}: {MISSING}")
        if registry["sign_denied"]:
            fail(
                "error signing: AccessDeniedException: User: arn:aws:sts::111122223333:"
                "assumed-role/example-release/run is not authorized to perform: kms:Sign on "
                "resource: arn:aws:kms:ap-southeast-2:111122223333:key/"
                "0000aaaa-bb11-cc22-dd33-eeeeee444444"
            )
        name = key_name(registry, key)
        if name is None:
            fail("stub: cosign sign could not load the key", 1)
        assert digest is not None
        signature_tag = f"{repository}:sha256-{digest.removeprefix('sha256:')}.sig"
        if signature_tag in registry["tags"]:
            fail(f"PUT {signature_tag}: TAG_INVALID: the tag is immutable")
        registry["tags"][signature_tag] = "sha256:" + "5" * 64
        registry["signatures"][f"{repository}@{digest}"] = name
        print("Pushing signature to: " + repository, file=sys.stderr)
    elif command == "verify":
        key = option(args, "--key") or ""
        reference = args[-1]
        repository, _, _ = split(reference)
        digest = resolve(registry, reference)
        if digest is None:
            fail(f"{reference}: {MISSING}")
        name = key_name(registry, key)
        signer = registry["signatures"].get(f"{repository}@{digest}")
        if name is None or signer != name:
            fail("Error: no matching signatures: invalid signature when validating ASN.1")
        claimed = registry["claim_digest"] or digest
        print(f"\nVerification for {reference} --", file=sys.stderr)
        print(
            json.dumps(
                [
                    {
                        "critical": {
                            "identity": {"docker-reference": repository},
                            "image": {"docker-manifest-digest": claimed},
                            "type": "cosign container image signature",
                        },
                        "optional": None,
                    }
                ],
                separators=(",", ":"),
            )
        )
    elif command == "public-key":
        name = key_name(registry, option(args, "--key") or "")
        if name is None:
            fail("stub: no such key")
        print(registry["public_keys"][name])
    else:
        fail(f"stub: cosign {command} is not modelled", 2)


def main() -> None:
    sys.stdout.reconfigure(newline="\n")  # type: ignore[union-attr]
    sys.stderr.reconfigure(newline="\n")  # type: ignore[union-attr]
    tool, args = sys.argv[1], sys.argv[2:]
    state_path = Path(os.environ["STUB_STATE"])
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["calls"].append([tool, *args])
    registry = state.setdefault("registry", empty_registry())
    try:
        if tool == "crane":
            crane(registry, args)
        elif tool == "cosign":
            cosign(registry, args)
        else:
            fail(f"stub: {tool} is not modelled", 2)
    finally:
        state_path.write_text(json.dumps(state), encoding="utf-8")


if __name__ == "__main__":
    main()
