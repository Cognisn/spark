"""Entry point for the Spark application."""

from __future__ import annotations


def main() -> None:
    """Launch the Spark application."""
    from spark.core.application import run

    run()


if __name__ == "__main__":
    main()
