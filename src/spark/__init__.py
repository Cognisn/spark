"""Spark - Secure Personal AI Research Kit."""

from pathlib import Path

__version__ = (Path(__file__).parent / "_version.txt").read_text().strip()
