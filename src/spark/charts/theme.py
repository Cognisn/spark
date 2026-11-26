"""The palette a chart is drawn in: SPARK's light and dark tokens, and nothing else.

Surfaces, text and grid lines are SPARK's own theme tokens (``static/css/cognisn.css``:
``--app-bg-primary``, ``--app-text-primary``, ``--app-text-muted``,
``--app-chart-grid`` and the card border). The series colours are the Okabe and Ito
palette, which stays distinguishable for the common forms of colour blindness, ordered
for each theme so the first colours have the most contrast with its background. A
chart's option takes every colour from here; the builder writes none of its own.

A saved image is on white by default (:data:`EXPORT`); "Match theme" saves it in the
viewer's theme.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

__all__ = ["DARK", "EXPORT", "FONT", "LIGHT", "THEMES", "Theme", "theme_named"]

FONT: Final = "'DM Sans', 'Inter', -apple-system, 'Segoe UI', Roboto, sans-serif"


@dataclass(frozen=True)
class Theme:
    """The colours of one theme."""

    name: str
    background: str
    surface: str
    text: str
    muted: str
    grid: str
    border: str
    series: tuple[str, ...]
    ramp: tuple[str, str]

    def colours(self) -> frozenset[str]:
        """Every colour this theme may put in an option."""
        return frozenset(
            (self.background, self.surface, self.text, self.muted, self.grid, self.border)
            + self.series
            + self.ramp
        )


LIGHT: Final = Theme(
    name="light",
    background="#ffffff",
    surface="#ffffff",
    text="#0c1428",
    muted="rgba(12, 20, 40, 0.6)",
    grid="rgba(0, 0, 0, 0.06)",
    border="rgba(26, 95, 180, 0.15)",
    series=(
        "#0072b2",
        "#e69f00",
        "#009e73",
        "#d55e00",
        "#cc79a7",
        "#56b4e9",
        "#7f6a00",
        "#4d4d4d",
    ),
    ramp=("#e8eef8", "#0d3a7a"),
)

DARK: Final = Theme(
    name="dark",
    background="#0c1428",
    surface="#111c35",
    text="#e8f0fa",
    muted="rgba(232, 240, 250, 0.6)",
    grid="rgba(90, 170, 232, 0.08)",
    border="rgba(90, 170, 232, 0.2)",
    series=(
        "#56b4e9",
        "#e69f00",
        "#009e73",
        "#f0e442",
        "#cc79a7",
        "#d55e00",
        "#8fb8de",
        "#bbbbbb",
    ),
    ramp=("#162240", "#7ac0f8"),
)

# A saved image without "Match theme": the light palette on white.
EXPORT: Final = LIGHT
THEMES: Final = {"light": LIGHT, "dark": DARK}


def theme_named(name: str | None) -> Theme:
    """The theme called ``name``; anything else is the light theme."""
    return THEMES.get(name or "", LIGHT)
