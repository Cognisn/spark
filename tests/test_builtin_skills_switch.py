"""The administrator's switch for each built-in skill (PC21).

``skills.disabled_builtin`` is an instance setting (administrator scope, audited in
server mode, ``config.yaml`` in personal mode) naming the built-in skills switched off for
the whole instance. A skill in it is not offered to any user's model, whatever the user's
own choice, and user skills are never affected by it.

Guards under test (each mutation-checked): the filter in ``resolve_enabled``, its limit to
bundled skills, the administrator-only write, and the audit event.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

import spark
from spark import mode
from spark.database import skills as skills_db
from spark.instance_settings import access, accessor, audit, registry
from spark.instance_settings.accessor import ServerSettings
from spark.mode import RuntimeSettings
from spark.skills.manager import SkillsManager, set_skills_manager
from spark.web.server import create_app
from tests.db_helpers import app_client
from tests.test_instance_settings_web import Ctx, MemorySecrets
from tests.web_helpers import sign_in

KEY = "skills.disabled_builtin"
BUNDLED = Path(spark.__file__).parent / "resources" / "skills"
NAMES = {"charts", "mermaid-diagrams", "projects"}


class Source:
    """A settings source holding the disabled list."""

    def __init__(self, disabled: list[str]) -> None:
        self.disabled = disabled

    def get(self, key: str) -> Any:
        return self.disabled if key == KEY else registry.spec(key).default


@pytest.fixture(autouse=True)
def _clean() -> Iterator[None]:
    yield
    accessor.reset()
    access.reset()
    audit.set_audit_hook(None)
    mode.reset()
    set_skills_manager(None)


@pytest.fixture()
def manager(tmp_path: Path) -> SkillsManager:
    user = tmp_path / "user"
    folder = user / "pdf-filler"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(
        "---\nname: pdf-filler\ndescription: Fill PDF forms.\n---\n\nFill it.\n",
        encoding="utf-8",
        newline="\n",
    )
    return SkillsManager(user, bundled_dir=BUNDLED)


class TestTheSetting:
    def test_it_is_an_administrator_list_that_defaults_to_nothing_switched_off(self) -> None:
        spec = registry.spec(KEY)
        assert spec.scope is registry.Scope.INSTANCE
        assert spec.kind is registry.Kind.STR_LIST
        assert spec.default == []
        assert registry.coerce(KEY, "charts, projects") == ["charts", "projects"]
        assert KEY not in registry.keys(registry.Scope.USER)


def _turn(manager: SkillsManager, enabled: list[str]) -> tuple[tuple[str, ...], tuple[str, bool]]:
    """What a turn's model is offered, and what loading a switched-off skill answers."""
    from spark.skills.session import TurnSkills

    turn = TurnSkills(manager.source(enabled=enabled))
    try:
        return turn.listed(), turn.use("projects")
    finally:
        turn.close()


class TestResolveEnabled:
    async def test_every_built_in_is_offered_by_default(
        self, spark_db: Any, manager: SkillsManager
    ) -> None:
        accessor.configure(Source([]))
        names = {s["name"] for s in await skills_db.resolve_enabled(spark_db, manager, "u1")}
        assert NAMES <= names

    async def test_a_skill_switched_off_is_offered_to_no_user(
        self, spark_db: Any, manager: SkillsManager
    ) -> None:
        accessor.configure(Source(["charts"]))
        for user in ("u1", "u2"):
            await skills_db.set_skill_enabled(spark_db, "charts", True, user)  # their own choice
            names = {s["name"] for s in await skills_db.resolve_enabled(spark_db, manager, user)}
            assert "charts" not in names
            assert {"mermaid-diagrams", "projects", "skill-creator"} <= names

    async def test_the_switch_reaches_the_model_through_the_turn_source(
        self, spark_db: Any, manager: SkillsManager
    ) -> None:
        accessor.configure(Source(["projects"]))
        enabled = await skills_db.resolve_enabled(spark_db, manager, "u1")
        listed, outcome = await asyncio.to_thread(_turn, manager, [s["name"] for s in enabled])
        assert "projects" not in listed and "charts" in listed
        assert outcome[1], outcome[0]

    async def test_the_list_never_switches_off_a_user_skill(
        self, spark_db: Any, manager: SkillsManager
    ) -> None:
        accessor.configure(Source(["pdf-filler", "charts"]))
        names = {s["name"] for s in await skills_db.resolve_enabled(spark_db, manager, "u1")}
        assert "pdf-filler" in names and "charts" not in names

    async def test_a_conversation_override_cannot_bring_back_a_switched_off_skill(
        self, spark_db: Any, manager: SkillsManager
    ) -> None:
        from spark.database import conversations

        accessor.configure(Source(["charts"]))
        cid = await conversations.create_conversation(spark_db, "c", "m", "u1")
        await skills_db.set_conversation_skill_enabled(spark_db, "u1", cid, "charts", True)
        names = {s["name"] for s in await skills_db.resolve_enabled(spark_db, manager, "u1", cid)}
        assert "charts" not in names

    async def test_without_a_settings_source_nothing_is_switched_off(
        self, spark_db: Any, manager: SkillsManager
    ) -> None:
        names = {s["name"] for s in await skills_db.resolve_enabled(spark_db, manager, "u1")}
        assert NAMES <= names


@pytest.mark.postgres
class TestServerSetting:
    """Server mode: the instance setting in the database, written and audited by SPARK."""

    async def test_the_administrators_write_is_read_back_and_audited(
        self, spark_db: Any, manager: SkillsManager
    ) -> None:
        seen: list[Any] = []
        audit.set_audit_hook(seen.append)
        source = ServerSettings(spark_db, Source([]))  # type: ignore[arg-type]
        await source.reload()
        accessor.configure(source)
        await source.set(KEY, ["mermaid-diagrams"], actor="admin-guid")
        assert [(c.key, c.actor, c.action) for c in seen] == [(KEY, "admin-guid", "set")]
        names = {s["name"] for s in await skills_db.resolve_enabled(spark_db, manager, "u1")}
        assert "mermaid-diagrams" not in names and "charts" in names
        await source.clear(KEY, actor="admin-guid")
        names = {s["name"] for s in await skills_db.resolve_enabled(spark_db, manager, "u1")}
        assert "mermaid-diagrams" in names


# -- The skills page --------------------------------------------------------------------


class Deny:
    def is_administrator(self, request: Any) -> bool:
        return False

    def user_guid(self, request: Any) -> str:
        return "user-not-admin"


@pytest.fixture()
def config_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text("{}\n", encoding="utf-8", newline="\n")
    import spark.core.application as app_mod

    monkeypatch.setattr(app_mod, "_get_config_path", lambda: path)
    return path


@pytest.fixture()
def client(config_file: Path, manager: SkillsManager, sqlite_settings: Any) -> Iterator[TestClient]:
    from konfig.settings.settings import Settings  # noqa: F401  (Ctx builds one)

    mode.configure(RuntimeSettings(mode.Mode.PERSONAL, sqlite_settings))
    ctx = Ctx(config_file, MemorySecrets())
    accessor.configure(accessor.FileSettings(ctx.settings))
    app = create_app(ctx)  # type: ignore[arg-type]
    with app_client(app, sqlite_settings) as test_client:
        app.state.skills_manager = manager
        sign_in(test_client)
        yield test_client


def _by_name(client: TestClient) -> dict[str, dict[str, Any]]:
    return {s["name"]: s for s in client.get("/skills/api/list").json()["skills"]}


class TestSkillsPage:
    def test_the_list_says_which_skills_the_instance_offers_and_who_may_switch(
        self, client: TestClient
    ) -> None:
        body = client.get("/skills/api/list").json()
        assert body["can_switch_builtin"] is True and body["server"] is False
        skills = _by_name(client)
        for name in NAMES:
            assert skills[name]["source"] == "bundled"
            assert skills[name]["instance_enabled"] is True
        assert skills["pdf-filler"]["instance_enabled"] is True

    def test_a_skill_switched_off_in_settings_shows_as_off_and_leaves_the_user_choice(
        self, client: TestClient, config_file: Path
    ) -> None:
        resp = client.post("/settings/api/save", json={KEY: ["charts"]})
        assert resp.status_code == 200
        assert yaml.safe_load(config_file.read_text(encoding="utf-8"))["skills"] == {
            "disabled_builtin": ["charts"]
        }
        skills = _by_name(client)
        assert skills["charts"]["instance_enabled"] is False
        assert skills["charts"]["enabled"] is True  # the user's own switch is untouched
        assert skills["projects"]["instance_enabled"] is True

    def test_someone_who_is_not_an_administrator_cannot_switch(self, client: TestClient) -> None:
        access.set_access_check(Deny())
        assert client.get("/skills/api/list").json()["can_switch_builtin"] is False

    def test_the_view_of_a_built_in_skill_shows_its_references(self, client: TestClient) -> None:
        data = client.get("/skills/api/view?name=charts").json()
        assert "references/bar.md" in data["resources"]
        assert data["source"] == "bundled"

    def test_the_page_offers_the_switch_to_an_administrator(self, client: TestClient) -> None:
        html = client.get("/skills").text
        assert "can_switch_builtin" in html and "instance_enabled" in html
        assert KEY in html
