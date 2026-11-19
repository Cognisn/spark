"""The bootstrap settings, the waits' timing and the command's wording, without PostgreSQL.

Every refusal lists every problem by its dotted and ``SPARK__`` names, and no
message carries a URL, a password or an address.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from spark import launch
from spark.bootstrap import cli
from spark.bootstrap import settings as bs
from spark.bootstrap import wait
from spark.bootstrap.run import Outcome, connect_reason
from spark.instance_settings.startup import StartupConfigError
from tests.test_identity_runtime import Konfig
from tests.test_instance_settings_web import MemorySecrets

OWNER = "postgresql://spark:owner-password-dev-only@db.example.com:5432/spark"
WEB = "postgresql://spark_web:web-password-dev-only@db.example.com:5432/spark"
WORKER = "postgresql://spark_worker:worker-password-dev-only@db.example.com/spark"
PURGER = "postgresql://spark_purger:purger-password-dev-only@db.example.com:5432/spark"
SECRET_VALUES = ("owner-password-dev-only", "web-password-dev-only", "worker-password-dev-only")
ADMIN = "admin@example.com"


def _konfig(**dotted: Any) -> Konfig:
    values: dict[str, Any] = {}
    for key, value in dotted.items():
        node = values
        *parents, leaf = key.split("__")
        for part in parents:
            node = node.setdefault(part, {})
        node[leaf] = value
    return Konfig(values)


def _complete(**extra: Any) -> dict[str, Any]:
    base = {
        "bootstrap__admin__email": ADMIN,
        "bootstrap__admin__first_name": "Ada",
        "bootstrap__admin__last_name": "Example",
        "mail__smtp__host": "smtp.example.com",
        "mail__smtp__sender_address": "noreply@example.com",
    }
    base.update(extra)
    return base


def _collect(
    settings: Konfig,
    secrets: MemorySecrets | None = None,
    *,
    stored: dict[str, Any] | None = None,
    owner: str = OWNER,
    environ: dict[str, str] | None = None,
) -> bs.BootstrapSettings:
    return bs.collect(
        settings,
        secrets or MemorySecrets(),
        owner_url=owner,
        stored_mail=stored or {},
        environ=environ or {},
    )


def _problems(settings: Konfig, secrets: MemorySecrets | None = None, **kw: Any) -> list[str]:
    with pytest.raises(bs.BootstrapSettingsError) as caught:
        _collect(settings, secrets, **kw)
    text = str(caught.value)
    for secret in SECRET_VALUES:
        assert secret not in text
    assert "db.example.com" not in text
    assert ADMIN not in text
    return caught.value.problems


class TestNames:
    def test_env_name(self) -> None:
        assert bs.env_name("bootstrap.admin.email") == "SPARK__BOOTSTRAP__ADMIN__EMAIL"

    def test_named_gives_both_forms(self) -> None:
        assert bs.named("mail.smtp.host") == "mail.smtp.host (SPARK__MAIL__SMTP__HOST)"

    def test_the_error_is_a_start_up_error(self) -> None:
        assert issubclass(bs.BootstrapSettingsError, StartupConfigError)


class TestComplete:
    def test_complete_settings_without_logins(self) -> None:
        found = _collect(_konfig(**_complete()))
        assert found.administrator.display_name == "Ada Example"
        assert found.logins is None
        assert found.mail["mail.smtp.host"] == "smtp.example.com"
        assert found.mail["mail.smtp.port"] == 587
        assert found.mail_seed["mail.smtp.sender_address"] == "noreply@example.com"

    def test_logins_from_secret_references(self) -> None:
        secrets = MemorySecrets(
            {"database_url_web": WEB, "database_url_worker": WORKER, "database_url_purger": PURGER}
        )
        settings = _konfig(
            **_complete(
                bootstrap__database__web_url="secret://database_url_web",
                bootstrap__database__worker_url="secret://database_url_worker",
                worker__audit_purge_database_url="secret://database_url_purger",
            )
        )
        found = _collect(settings, secrets)
        assert found.logins is not None
        assert found.logins.names() == ("spark_web", "spark_worker", "spark_purger")
        assert found.logins.web.password == "web-password-dev-only"
        assert "web-password-dev-only" not in repr(found.logins)

    def test_plain_urls_and_the_default_port(self) -> None:
        settings = _konfig(
            **_complete(bootstrap__database__web_url=WEB, bootstrap__database__worker_url=WORKER)
        )
        found = _collect(settings)
        assert found.logins is not None and found.logins.purger is None

    def test_configured_only_with_an_administrator(self) -> None:
        assert bs.configured(_konfig(**_complete()))
        assert not bs.configured(_konfig())


class TestRefusals:
    def test_everything_missing_is_listed_at_once(self) -> None:
        problems = _problems(_konfig())
        text = "\n".join(problems)
        for env in (
            "SPARK__BOOTSTRAP__ADMIN__EMAIL",
            "SPARK__BOOTSTRAP__ADMIN__FIRST_NAME",
            "SPARK__BOOTSTRAP__ADMIN__LAST_NAME",
            "SPARK__MAIL__SMTP__HOST",
            "SPARK__MAIL__SMTP__SENDER_ADDRESS",
        ):
            assert env in text
        assert len(problems) == 4

    def test_the_message_lists_each_problem_on_its_line(self) -> None:
        with pytest.raises(bs.BootstrapSettingsError) as caught:
            _collect(_konfig())
        assert str(caught.value).startswith("the bootstrap settings are not complete and valid:")
        assert str(caught.value).count("\n  - ") == 4

    def test_an_invalid_address(self) -> None:
        problems = _problems(_konfig(**_complete(bootstrap__admin__email="not an address")))
        assert problems == [f"{bs.named(bs.ADMIN_EMAIL)} is not a valid email address"]

    def test_a_name_with_a_line_break(self) -> None:
        problems = _problems(_konfig(**_complete(bootstrap__admin__last_name="Ex\nample")))
        assert problems == [
            f"{bs.named(bs.ADMIN_LAST_NAME)} must be one line of at most 100 characters"
        ]

    def test_one_login_url_without_the_other(self) -> None:
        problems = _problems(_konfig(**_complete(bootstrap__database__web_url=WEB)))
        assert len(problems) == 1
        assert problems[0].startswith(f"{bs.named(bs.WORKER_URL)} is not set")

    def test_a_missing_secret_is_named_not_its_value(self) -> None:
        settings = _konfig(
            **_complete(
                bootstrap__database__web_url="secret://database_url_web",
                bootstrap__database__worker_url=WORKER,
            )
        )
        problems = _problems(settings)
        assert problems == [
            f"{bs.named(bs.WEB_URL)} refers to the secret 'database_url_web', which is not set "
            "in the secrets store"
        ]

    def test_another_host_or_database_is_refused(self) -> None:
        other = WEB.replace("db.example.com", "other.example.com")
        settings = _konfig(
            **_complete(
                bootstrap__database__web_url=other,
                bootstrap__database__worker_url=WORKER.replace("/spark", "/other"),
            )
        )
        problems = _problems(settings)
        assert len(problems) == 2
        assert all("same host, port and database" in p for p in problems)
        assert "other.example.com" not in "\n".join(problems)

    def test_the_owner_is_not_a_restricted_login(self) -> None:
        settings = _konfig(
            **_complete(bootstrap__database__web_url=OWNER, bootstrap__database__worker_url=WORKER)
        )
        assert _problems(settings) == [
            f"{bs.named(bs.WEB_URL)} must name its own login, not the schema owner's"
        ]

    def test_a_url_without_a_password(self) -> None:
        settings = _konfig(
            **_complete(
                bootstrap__database__web_url="postgresql://spark_web@db.example.com/spark",
                bootstrap__database__worker_url=WORKER,
            )
        )
        assert _problems(settings) == [f"{bs.named(bs.WEB_URL)}: the URL carries no password"]

    def test_a_url_that_is_not_postgresql(self) -> None:
        settings = _konfig(
            **_complete(
                bootstrap__database__web_url="mysql://spark_web:x@db.example.com/spark",
                bootstrap__database__worker_url=WORKER,
            )
        )
        assert _problems(settings) == [f"{bs.named(bs.WEB_URL)} must be a PostgreSQL URL"]

    def test_two_settings_naming_one_login(self) -> None:
        settings = _konfig(
            **_complete(bootstrap__database__web_url=WEB, bootstrap__database__worker_url=WEB)
        )
        problems = _problems(settings)
        assert len(problems) == 1 and "different login" in problems[0]

    def test_a_retention_needs_the_purge_login(self) -> None:
        settings = _konfig(
            **_complete(
                bootstrap__database__web_url=WEB,
                bootstrap__database__worker_url=WORKER,
                warden__audit__retention_days=365,
            )
        )
        problems = _problems(settings)
        assert len(problems) == 1 and problems[0].startswith(f"{bs.named(bs.PURGE_URL)} is not set")

    def test_a_retention_without_restricted_logins_needs_nothing(self) -> None:
        _collect(_konfig(**_complete(warden__audit__retention_days=365)))


class TestTls:
    def test_verify_full_without_a_root_certificate(self, tmp_path: Path) -> None:
        owner = OWNER + "?sslmode=verify-full"
        problems = _problems(_konfig(**_complete()), owner=owner, environ={"HOME": str(tmp_path)})
        assert problems == [
            f"{bs.named('database.url')} asks for sslmode=verify-full, but no readable root "
            "certificate was found (set sslrootcert in the URL or PGSSLROOTCERT)"
        ]

    def test_pgsslrootcert_names_a_readable_file(self, tmp_path: Path) -> None:
        root = tmp_path / "rds-bundle.pem"
        root.write_text("not checked here", newline="\n")
        owner = OWNER + "?sslmode=verify-full"
        _collect(_konfig(**_complete()), owner=owner, environ={"PGSSLROOTCERT": str(root)})

    def test_sslrootcert_in_the_url(self, tmp_path: Path) -> None:
        root = tmp_path / "ca.pem"
        root.write_text("x", newline="\n")
        owner = f"{OWNER}?sslmode=verify-ca&sslrootcert={root}"
        _collect(_konfig(**_complete()), owner=owner)

    def test_the_system_store(self) -> None:
        owner = OWNER + "?sslmode=verify-full&sslrootcert=system"
        _collect(_konfig(**_complete()), owner=owner)

    def test_a_login_url_is_checked_too(self, tmp_path: Path) -> None:
        settings = _konfig(
            **_complete(
                bootstrap__database__web_url=WEB + "?sslmode=verify-full&sslrootcert=/nowhere.pem",
                bootstrap__database__worker_url=WORKER,
            )
        )
        problems = _problems(settings, environ={"HOME": str(tmp_path)})
        assert len(problems) == 1 and problems[0].startswith(
            f"{bs.named(bs.WEB_URL)} asks for sslmode=verify-full"
        )

    def test_require_needs_no_certificate(self) -> None:
        assert bs.tls_problem("database.url", OWNER + "?sslmode=require", {}) is None


class TestMailPrecedence:
    def test_stored_settings_win_and_nothing_is_seeded(self) -> None:
        stored = {
            "mail.smtp.host": "relay.example.org",
            "mail.smtp.sender_address": "spark@example.org",
        }
        found = _collect(_konfig(**_complete()), stored=stored)
        assert found.mail_seed == {}
        assert found.mail["mail.smtp.host"] == "relay.example.org"

    def test_start_up_values_fill_an_empty_database(self) -> None:
        settings = _konfig(**_complete(mail__smtp__port="465", mail__smtp__tls="implicit"))
        found = _collect(settings)
        assert found.mail_seed["mail.smtp.port"] == 465
        assert found.mail["mail.smtp.tls"] == "implicit"

    def test_stored_mail_alone_is_enough(self) -> None:
        settings = _konfig(
            bootstrap__admin__email=ADMIN,
            bootstrap__admin__first_name="Ada",
            bootstrap__admin__last_name="Example",
        )
        stored = {
            "mail.smtp.host": "relay.example.org",
            "mail.smtp.sender_address": "a@example.org",
        }
        assert _collect(settings, stored=stored).mail_seed == {}

    def test_plain_text_to_another_host_is_refused(self) -> None:
        problems = _problems(_konfig(**_complete(mail__smtp__tls="none")))
        assert len(problems) == 1 and problems[0].startswith(bs.named("mail.smtp.tls"))

    def test_plain_text_to_loopback_is_accepted(self) -> None:
        _collect(_konfig(**_complete(mail__smtp__host="127.0.0.1", mail__smtp__tls="none")))

    def test_a_host_without_a_sender(self) -> None:
        settings = _konfig(
            **_complete(mail__smtp__sender_address=""),
        )
        problems = _problems(settings)
        assert len(problems) == 1 and problems[0].startswith(bs.named("mail.smtp.sender_address"))

    def test_an_invalid_port(self) -> None:
        problems = _problems(_konfig(**_complete(mail__smtp__port="seventy")))
        assert f"{bs.named('mail.smtp.port')} is not valid" in problems

    def test_a_username_needs_the_password_secret(self) -> None:
        settings = _konfig(**_complete(mail__smtp__username="mailer@example.com"))
        problems = _problems(settings)
        assert len(problems) == 1 and "mail_smtp_password" in problems[0]
        _collect(settings, MemorySecrets({"mail_smtp_password": "smtp-dev-only"}))

    def test_the_sender_settings_carry_the_password_from_the_store(self) -> None:
        values = {"mail.smtp.host": "smtp.example.com", "mail.smtp.port": 2525}
        smtp = bs.smtp_of(values, MemorySecrets({"mail_smtp_password": "smtp-dev-only"}))
        assert smtp.port == 2525 and smtp.password == "smtp-dev-only"
        assert "smtp-dev-only" not in repr(smtp)


class TestWaitLimits:
    def test_defaults(self) -> None:
        assert bs.wait_limits(_konfig()) == (600.0, 5.0)

    def test_set_values(self) -> None:
        settings = _konfig(bootstrap__wait_seconds="30", bootstrap__wait_interval_seconds=2)
        assert bs.wait_limits(settings) == (30.0, 2.0)

    def test_bad_values_fall_back(self) -> None:
        settings = _konfig(bootstrap__wait_seconds="soon", bootstrap__wait_interval_seconds=-1)
        assert bs.wait_limits(settings) == (600.0, 5.0)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class TestWaitTiming:
    async def test_waits_then_proceeds_logging_once(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        answers = [["the secret spark.csrf-key"], ["the secret spark.csrf-key"], []]

        async def fake_missing(*_: Any, **__: Any) -> list[str]:
            return answers.pop(0)

        monkeypatch.setattr(wait, "missing", fake_missing)
        clock = FakeClock()
        caplog.set_level("INFO", logger="spark.bootstrap.wait")
        await wait.wait_until_ready(
            None,  # type: ignore[arg-type]
            _konfig(),
            None,
            role="worker",
            record=False,
            sleep=clock.sleep,
            clock=clock,
        )
        assert clock.sleeps == [5.0, 5.0]
        waiting = [r for r in caplog.records if "waiting for spark bootstrap" in r.getMessage()]
        assert len(waiting) == 1
        assert any("is starting" in r.getMessage() for r in caplog.records)

    async def test_gives_up_after_the_total_naming_what_is_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_missing(*_: Any, **__: Any) -> list[str]:
            return [wait.RECORD, wait.SIGNING_KEY]

        monkeypatch.setattr(wait, "missing", fake_missing)
        clock = FakeClock()
        settings = _konfig(bootstrap__wait_seconds=12, bootstrap__wait_interval_seconds=5)
        with pytest.raises(wait.BootstrapNotReady) as caught:
            await wait.wait_until_ready(
                None,  # type: ignore[arg-type]
                settings,
                None,
                role="worker",
                record=True,
                sleep=clock.sleep,
                clock=clock,
            )
        assert clock.sleeps == [5.0, 5.0, 2.0]
        assert str(caught.value) == (
            f"spark worker: bootstrap was not complete after 12 s; missing: "
            f"{wait.RECORD}; {wait.SIGNING_KEY}"
        )

    async def test_web_does_not_wait_without_bootstrap_settings(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def boom(*_: Any, **__: Any) -> list[str]:
            raise AssertionError("checked")

        monkeypatch.setattr(wait, "missing", boom)
        await wait.before_web(None, _konfig(), None)  # type: ignore[arg-type]


class TestWording:
    def test_connect_reasons_are_fixed_words(self) -> None:
        refused = Exception('password authentication failed for user "spark" at db.example.com')
        assert connect_reason(refused) == "the login or password was refused"
        tls = Exception("SSL error: certificate verify failed")
        assert connect_reason(tls) == "the TLS connection could not be verified"
        assert connect_reason(ValueError("x")) == "the connection failed (ValueError)"

    def test_describe_names_no_address(self) -> None:
        outcome = Outcome({"spark_web": "created"}, ("mail.smtp.host",), True, False, False)
        text = cli.describe(outcome)
        assert "spark_web created" in text
        assert "invitation sent" in text
        assert "@" not in text

    def test_describe_a_restart(self) -> None:
        text = cli.describe(Outcome({}, (), False, False, True))
        assert "already complete, nothing sent" in text

    def test_launch_routes_bootstrap(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: list[list[str]] = []

        def fake_main(argv: Any) -> int:
            seen.append(list(argv))
            return 0

        monkeypatch.setattr(cli, "main", fake_main)
        with pytest.raises(SystemExit) as caught:
            launch.main(["bootstrap", "--resend-invitation"])
        assert caught.value.code == 0
        assert seen == [["--resend-invitation"]]

    def test_parse(self) -> None:
        assert cli.parse([]).resend_invitation is False
        assert cli.parse(["--resend-invitation"]).resend_invitation is True


class TestWorkerHook:
    async def test_the_worker_refuses_after_the_wait(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from spark import worker
        from spark.mode import DatabaseSettings, Dialect, Mode, RuntimeSettings

        async def not_ready(*_: Any, **__: Any) -> None:
            raise wait.BootstrapNotReady("spark worker: bootstrap was not complete after 1 s")

        monkeypatch.setattr(wait, "before_worker", not_ready)

        class App:
            settings = _konfig()
            secrets = None

        runtime = RuntimeSettings(
            Mode.SERVER, DatabaseSettings(Dialect.POSTGRESQL, url="postgresql://x/y")
        )
        with pytest.raises(worker.WorkerRefused, match="bootstrap was not complete"):
            await worker._wait_for_bootstrap(runtime, App())
