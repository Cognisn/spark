"""Basic smoke tests."""

import spark


def test_version_is_set() -> None:
    assert spark.__version__
    assert isinstance(spark.__version__, str)
