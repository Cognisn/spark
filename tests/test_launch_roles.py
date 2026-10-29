"""The ``spark`` command's roles: ``spark`` and ``spark web`` serve, ``spark worker`` works."""

from __future__ import annotations

import pytest

from spark import launch, worker
from spark.core import application


class TestRoles:
    def test_plain_spark_still_starts_the_web_interface(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[str] = []
        monkeypatch.setattr(application, "run", lambda: calls.append("web"))
        monkeypatch.setattr(worker, "main", lambda: pytest.fail("not the worker"))
        launch.main([])
        assert calls == ["web"]

    def test_spark_web_starts_the_web_interface(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[str] = []
        monkeypatch.setattr(application, "run", lambda: calls.append("web"))
        monkeypatch.setattr(worker, "main", lambda: pytest.fail("not the worker"))
        launch.main(["web"])
        assert calls == ["web"]

    @pytest.mark.parametrize("code", [0, 2])
    def test_spark_worker_runs_the_worker_and_exits_with_its_code(
        self, monkeypatch: pytest.MonkeyPatch, code: int
    ) -> None:
        monkeypatch.setattr(application, "run", lambda: pytest.fail("not the web interface"))
        monkeypatch.setattr(worker, "main", lambda: code)
        with pytest.raises(SystemExit) as exc:
            launch.main(["worker"])
        assert exc.value.code == code

    def test_usage_names_every_role(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(application, "run", lambda: pytest.fail("must not start"))
        with pytest.raises(SystemExit) as exc:
            launch.main(["web", "extra"])
        assert exc.value.code == 2
        err = capsys.readouterr().err
        for role in ("web", "worker", "migrate"):
            assert role in err
