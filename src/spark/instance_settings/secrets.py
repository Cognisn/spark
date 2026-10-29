"""Secrets: written only through konfig's secrets store, write-only, never in the database.

The store is whichever konfig backend start-up selected: the OS keychain in
personal mode; in server mode konfig's encrypted store (a shared file with
``KONFIG_MASTER_KEY``) or AWS Secrets Manager (see
:mod:`spark.instance_settings.startup`). A secret is stored under
:func:`spark.instance_settings.registry.secret_name`, the dotted key with its
group (``database.url`` is ``database_url``).

:class:`SecretsWriter` sets and clears secrets and reports whether each is set;
it never hands a value to a caller that could pass it to the browser.
:meth:`SecretsWriter.resolve` exists for server-side consumers (provider
clients, SMTP) only.

SPARK 0.2 named secrets without their group (``database.url`` was ``url``) and
kept some (web search keys) as plain text in ``config.yaml``.
:func:`migrate_config` moves both to the current names.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Protocol

import yaml

from spark.instance_settings import audit, registry
from spark.instance_settings.audit import SettingChange
from spark.instance_settings.registry import InvalidSettingError, SettingsError

__all__ = [
    "SECRET_PREFIX",
    "SecretsLike",
    "SecretsWriter",
    "migrate_config",
    "migrate_config_file",
    "reference",
]

logger = logging.getLogger(__name__)

SECRET_PREFIX = "secret://"


class SecretsLike(Protocol):
    """The part of konfig's ``Secrets`` SPARK uses."""

    def get(self, key: str) -> str | None: ...

    def set(self, key: str, value: str) -> None: ...

    def delete(self, key: str) -> None: ...

    def has(self, key: str) -> bool: ...


class KonfigLike(Protocol):
    def get(self, key: str, default: Any = None) -> Any: ...


def reference(key: str) -> str:
    """The ``secret://`` reference ``config.yaml`` holds for a secret key."""
    return SECRET_PREFIX + registry.secret_name(key)


def _secret_key(key: str) -> str:
    if not registry.spec(key).secret:
        raise SettingsError(f"{key} is not a secret")
    return registry.secret_name(key)


class SecretsWriter:
    """Writes secrets through konfig and reports set or not set.

    ``server`` selects how "set" is judged: in server mode by the store alone; in
    personal mode by the ``config.yaml`` reference first (as 0.2 did), so a value
    kept under another name still counts.
    """

    def __init__(self, secrets: SecretsLike, settings: KonfigLike | None, *, server: bool) -> None:
        self._secrets = secrets
        self._settings = settings
        self._server = server

    def set(self, key: str, value: str, *, actor: str) -> None:
        name = _secret_key(key)
        if not isinstance(value, str) or not value.strip():
            raise InvalidSettingError(key, "a secret must be non-empty text")
        self._secrets.set(name, value)
        audit.record(SettingChange(key, registry.spec(key).scope.value, actor, "set", True))

    def clear(self, key: str, *, actor: str) -> None:
        name = _secret_key(key)
        self._secrets.delete(name)
        audit.record(SettingChange(key, registry.spec(key).scope.value, actor, "clear", True))

    def _configured(self, key: str) -> str:
        if self._server or self._settings is None:
            return ""
        value = self._settings.get(key, "")
        return value if isinstance(value, str) else ""

    def is_set(self, key: str) -> bool:
        name = _secret_key(key)
        configured = self._configured(key)
        if configured.startswith(SECRET_PREFIX):
            ref = configured[len(SECRET_PREFIX) :]
            return bool(ref) and bool(self._secrets.get(ref))
        if configured:
            return True
        return bool(self._secrets.get(name))

    def status(self, keys: list[str] | None = None) -> dict[str, bool]:
        """``{key: set or not}`` for every secret key (or those given); never a value."""
        return {k: self.is_set(k) for k in (keys if keys is not None else registry.secret_keys())}

    def resolve(self, key: str) -> str:
        """The secret's value, for server-side use only. Never send it to the browser."""
        name = _secret_key(key)
        configured = self._configured(key)
        if configured.startswith(SECRET_PREFIX):
            return self._secrets.get(configured[len(SECRET_PREFIX) :]) or ""
        if configured:
            return configured
        return self._secrets.get(name) or ""


# -- Migration of 0.2 names and plain-text secrets ------------------------------------


def _nested_get(data: dict[str, Any], key: str) -> Any:
    node: Any = data
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _nested_set(data: dict[str, Any], key: str, value: Any) -> None:
    parts = key.split(".")
    node = data
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


def migrate_config(raw: dict[str, Any], store: SecretsLike) -> list[str]:
    """Bring secret references in a parsed ``config.yaml`` to the current names, in place.

    * ``secret://<0.2 name>`` becomes ``secret://<current name>``; a stored value is
      moved across (kept where the current name already has one).
    * A plain-text secret value is moved into the store and replaced by its reference.

    Other references (a name the user chose, MCP server environments) are left
    alone. Returns the keys changed; running it twice changes nothing.
    """
    changed: list[str] = []
    for key in registry.secret_keys():
        value = _nested_get(raw, key)
        if not isinstance(value, str) or not value:
            continue
        name = registry.secret_name(key)
        if value.startswith(SECRET_PREFIX):
            ref = value[len(SECRET_PREFIX) :]
            legacy = registry.legacy_secret_name(key)
            if ref != legacy or ref == name:
                continue
            stored = store.get(legacy)
            if stored is not None:
                if store.get(name) is None:
                    store.set(name, stored)
                    if store.get(name) == stored:
                        store.delete(legacy)
                else:
                    logger.warning(
                        "Secret %s already exists; the 0.2 entry %s was left in place",
                        name,
                        legacy,
                    )
        else:
            store.set(name, value)
        _nested_set(raw, key, SECRET_PREFIX + name)
        logger.info("Secret for %s now stored as %s", key, name)
        changed.append(key)
    return changed


def migrate_config_file(path: Path, store: SecretsLike) -> list[str]:
    """:func:`migrate_config` on a config file, rewritten only when something changed."""
    if not path.exists():
        return []
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        return []
    changed = migrate_config(raw, store)
    if changed:
        with path.open("w", encoding="utf-8", newline="\n") as fh:
            yaml.safe_dump(raw, fh, default_flow_style=False, sort_keys=False)
    return changed
