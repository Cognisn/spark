"""Generate the 640x400 splash screen image for PyApp first-run."""

import sys
from pathlib import Path

# Ensure project root is importable
sys.path.insert(0, str(Path(__file__).parent.parent))

from assets.generate_installer_assets import (
    ACCENT,
    BG_DEEP,
    BG_PRIMARY,
    BG_SECONDARY,
    TEXT_MUTED,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
    _centered_text,
    _draw_lightning_bolt,
    _try_font,
)
from PIL import Image, ImageDraw

width, height = 640, 400
img = Image.new("RGB", (width, height), BG_DEEP)
draw = ImageDraw.Draw(img)

# Gradient background
for y in range(height):
    t = y / height
    r = int(BG_DEEP[0] + (BG_PRIMARY[0] - BG_DEEP[0]) * t * 0.6)
    g = int(BG_DEEP[1] + (BG_PRIMARY[1] - BG_DEEP[1]) * t * 0.6)
    b = int(BG_DEEP[2] + (BG_PRIMARY[2] - BG_DEEP[2]) * t * 0.6)
    draw.line([(0, y), (width, y)], fill=(r, g, b))

# Subtle accent glow
for i in range(80, 0, -1):
    intensity = (i / 80) * 0.04
    glow_r = int(BG_DEEP[0] + (ACCENT[0] - BG_DEEP[0]) * intensity)
    glow_g = int(BG_DEEP[1] + (ACCENT[1] - BG_DEEP[1]) * intensity)
    glow_b = int(BG_DEEP[2] + (ACCENT[2] - BG_DEEP[2]) * intensity)
    draw.ellipse(
        [width // 2 - i * 3, 80 - i, width // 2 + i * 3, 80 + i],
        fill=(glow_r, glow_g, glow_b),
    )

# Lightning bolt in rounded rect
bolt_size = 70
bolt_cx, bolt_cy = width // 2, 115
margin = bolt_size // 10
radius = bolt_size // 4
draw.rounded_rectangle(
    [
        bolt_cx - bolt_size // 2 - margin,
        bolt_cy - bolt_size // 2 - margin,
        bolt_cx + bolt_size // 2 + margin,
        bolt_cy + bolt_size // 2 + margin,
    ],
    radius=radius,
    fill=BG_SECONDARY,
    outline=(*ACCENT, 50),
    width=1,
)
_draw_lightning_bolt(draw, bolt_cx, bolt_cy, bolt_size)

# Text
font_title = _try_font(38, bold=True)
font_sub = _try_font(14)
font_byline = _try_font(12)
font_status = _try_font(12)
font_hint = _try_font(10)

_centered_text(draw, 170, "Spark", font_title, TEXT_PRIMARY, width)
_centered_text(draw, 220, "Secure Personal AI Research Kit", font_sub, TEXT_SECONDARY, width)
_centered_text(draw, 248, "by Cognisn", font_byline, ACCENT, width)

# Divider
draw.line([(200, 280), (440, 280)], fill=BG_SECONDARY, width=1)

_centered_text(draw, 310, "Setting up environment, please wait...", font_status, TEXT_MUTED, width)
_centered_text(draw, height - 30, "First launch may take a minute", font_hint, TEXT_MUTED, width)

Path("assets/installer").mkdir(parents=True, exist_ok=True)
img.save("assets/installer/splash-screen.png")
print("Generated: assets/installer/splash-screen.png (640x400)")
