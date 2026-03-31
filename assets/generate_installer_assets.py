"""Generate branded installer assets for macOS DMG and Windows NSIS."""

import math

from PIL import Image, ImageDraw, ImageFont


# Cognisn palette
BG_DEEP = (8, 14, 30)
BG_PRIMARY = (12, 20, 40)
BG_SECONDARY = (17, 28, 53)
BG_TERTIARY = (22, 34, 64)
ACCENT = (90, 170, 232)
ACCENT_LIGHT = (122, 192, 248)
ACCENT_DARK = (58, 120, 196)
TEXT_PRIMARY = (232, 240, 250)
TEXT_SECONDARY = (162, 178, 200)
TEXT_MUTED = (100, 120, 150)


def _draw_lightning_bolt(draw: ImageDraw.ImageDraw, cx: int, cy: int, size: int) -> None:
    """Draw a lightning bolt centered at (cx, cy) with given size."""
    s = size
    bolt = [
        (cx + s * 0.05, cy - s * 0.38),
        (cx - s * 0.22, cy - s * 0.03),
        (cx - s * 0.03, cy - s * 0.03),
        (cx - s * 0.11, cy + s * 0.38),
        (cx + s * 0.22, cy + s * 0.06),
        (cx + s * 0.03, cy + s * 0.06),
    ]
    bolt = [(int(x), int(y)) for x, y in bolt]
    draw.polygon(bolt, fill=ACCENT)


def _draw_cognisn_logo(draw: ImageDraw.ImageDraw, cx: int, cy: int, radius: int) -> None:
    """Draw the Cognisn logo mark: arc with endpoint dots and center dot.

    The logo is a ~270 degree arc (open at upper-right) with dots at the
    endpoints and a central dot — matching the SVG in the web UI.
    """
    # Arc — draw from roughly 50 degrees to 310 degrees (opening at upper-right)
    # PIL arc uses 0=3-o'clock going clockwise
    arc_bbox = [cx - radius, cy - radius, cx + radius, cy + radius]
    # The SVG path: M32 -48 A48 48 0 1 0 32 48 means arc from (32,-48) to (32,48)
    # going the long way (large arc). In angle terms that's about 315 to 45 degrees
    # but the opening is at the right side.
    # Let's draw from 50 to 310 degrees (opening at right, towards upper-right)
    draw.arc(arc_bbox, start=310, end=230, fill=ACCENT, width=max(2, radius // 12))

    # Endpoint dots — at the arc endpoints
    # Upper-right endpoint (~310 degrees = about 50 deg from 3-o'clock)
    angle1 = math.radians(310)
    dot1_x = cx + int(radius * math.cos(angle1))
    dot1_y = cy + int(radius * math.sin(angle1))
    dot_r = max(2, radius // 10)
    draw.ellipse(
        [dot1_x - dot_r, dot1_y - dot_r, dot1_x + dot_r, dot1_y + dot_r],
        fill=ACCENT_LIGHT,
    )

    # Lower-right endpoint (~230 degrees)
    angle2 = math.radians(230)
    dot2_x = cx + int(radius * math.cos(angle2))
    dot2_y = cy + int(radius * math.sin(angle2))
    draw.ellipse(
        [dot2_x - dot_r, dot2_y - dot_r, dot2_x + dot_r, dot2_y + dot_r],
        fill=ACCENT,
    )

    # Center dot (slightly offset right, like the SVG's cx="12")
    center_dot_r = max(3, radius // 7)
    offset = radius // 5
    draw.ellipse(
        [cx + offset - center_dot_r, cy - center_dot_r,
         cx + offset + center_dot_r, cy + center_dot_r],
        fill=ACCENT_LIGHT,
    )


def _try_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Try to load a nice font, fall back to default."""
    if bold:
        names = [
            "/System/Library/Fonts/SFNSDisplay-Bold.otf",
            "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
            "/System/Library/Fonts/Helvetica.ttc",
            "C:/Windows/Fonts/segoeuib.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        ]
    else:
        names = [
            "/System/Library/Fonts/SFNSDisplay.ttf",
            "/System/Library/Fonts/Supplemental/Arial.ttf",
            "/System/Library/Fonts/Helvetica.ttc",
            "C:/Windows/Fonts/segoeui.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ]

    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except (OSError, IOError):
            continue
    return ImageFont.load_default()


def _centered_text(
    draw: ImageDraw.ImageDraw, y: int, text: str, font: ImageFont.ImageFont,
    fill: tuple, width: int,
) -> None:
    """Draw text horizontally centered at the given y position."""
    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    draw.text(((width - tw) // 2, y), text, fill=fill, font=font)


def generate_dmg_background(width: int = 660, height: int = 400) -> Image.Image:
    """Generate a branded DMG background image at 1x (660x400).

    Finder maps background pixels 1:1 to window points, so this must match
    the create-dmg --window-size exactly.

    Layout:
    - Top band (~120px): Cognisn logo, "Spark" title, tagline
    - Middle/bottom: clean zone where Finder draws the app + Applications icons
      Icon positions (set in create-dmg): Spark.app at (180,260), Applications at (480,260)
    - Subtle arrow hint between icon positions
    """
    img = Image.new("RGB", (width, height), BG_DEEP)
    draw = ImageDraw.Draw(img)

    # Gradient background
    for y in range(height):
        t = y / height
        r = int(BG_DEEP[0] + (BG_PRIMARY[0] - BG_DEEP[0]) * t * 0.6)
        g = int(BG_DEEP[1] + (BG_PRIMARY[1] - BG_DEEP[1]) * t * 0.6)
        b = int(BG_DEEP[2] + (BG_PRIMARY[2] - BG_DEEP[2]) * t * 0.6)
        draw.line([(0, y), (width, y)], fill=(r, g, b))

    # Subtle accent glow behind title area
    for i in range(60, 0, -1):
        intensity = (i / 60) * 0.05
        glow_r = int(BG_DEEP[0] + (ACCENT[0] - BG_DEEP[0]) * intensity)
        glow_g = int(BG_DEEP[1] + (ACCENT[1] - BG_DEEP[1]) * intensity)
        glow_b = int(BG_DEEP[2] + (ACCENT[2] - BG_DEEP[2]) * intensity)
        draw.ellipse(
            [width // 2 - i * 4, 30 - i, width // 2 + i * 4, 30 + i],
            fill=(glow_r, glow_g, glow_b),
        )

    # === Top branding band ===

    # Spark app icon — centered, larger
    icon_cx = width // 2
    icon_cy = 42
    icon_size = 44
    _draw_rounded_rect_bg(draw, icon_cx, icon_cy, icon_size)
    _draw_lightning_bolt(draw, icon_cx, icon_cy, icon_size)

    # "Spark" title — centered below icon
    font_title = _try_font(28, bold=True)
    _centered_text(draw, 74, "Spark", font_title, TEXT_PRIMARY, width)

    # Tagline
    font_sub = _try_font(11)
    _centered_text(draw, 108, "Secure Personal AI Research Kit", font_sub, TEXT_SECONDARY, width)

    # "by Cognisn"
    font_byline = _try_font(10)
    _centered_text(draw, 126, "by Cognisn", font_byline, ACCENT, width)

    # Thin divider line
    draw.line([(180, 148), (480, 148)], fill=BG_TERTIARY, width=1)

    # === Icon zone (y ~160-340) — kept clean ===
    # Icon positions: Spark.app at x=180, Applications at x=480, both at y=260

    # Subtle arrow hint between the two icon positions
    arrow_y = 245
    arrow_left = 255
    arrow_right = 405
    for x in range(arrow_left, arrow_right, 6):
        draw.line([(x, arrow_y), (x + 3, arrow_y)], fill=ACCENT_DARK, width=1)
    # Arrowhead
    draw.polygon(
        [
            (arrow_right + 2, arrow_y),
            (arrow_right - 4, arrow_y - 3),
            (arrow_right - 4, arrow_y + 3),
        ],
        fill=ACCENT_DARK,
    )

    return img


def _draw_rounded_rect_bg(draw: ImageDraw.ImageDraw, cx: int, cy: int, size: int) -> None:
    """Draw a subtle rounded rect background for the bolt icon."""
    margin = size // 10
    radius = size // 4
    draw.rounded_rectangle(
        [cx - size // 2 - margin, cy - size // 2 - margin,
         cx + size // 2 + margin, cy + size // 2 + margin],
        radius=radius,
        fill=BG_SECONDARY,
        outline=(*ACCENT, 40),
        width=1,
    )


def generate_nsis_header(width: int = 150, height: int = 57) -> Image.Image:
    """Generate NSIS MUI header image (150x57 bitmap)."""
    img = Image.new("RGB", (width, height), BG_DEEP)
    draw = ImageDraw.Draw(img)

    # Gradient
    for y in range(height):
        t = y / height
        r = int(BG_SECONDARY[0] * (1 - t) + BG_DEEP[0] * t)
        g = int(BG_SECONDARY[1] * (1 - t) + BG_DEEP[1] * t)
        b = int(BG_SECONDARY[2] * (1 - t) + BG_DEEP[2] * t)
        draw.line([(0, y), (width, y)], fill=(r, g, b))

    # Small lightning bolt on the right
    _draw_lightning_bolt(draw, width - 30, height // 2, 22)

    # "Spark" text
    font = _try_font(16, bold=True)
    draw.text((10, 8), "Spark", fill=TEXT_PRIMARY, font=font)

    font_small = _try_font(9)
    draw.text((10, 30), "by Cognisn", fill=ACCENT, font=font_small)

    return img


def generate_nsis_welcome(width: int = 164, height: int = 314) -> Image.Image:
    """Generate NSIS MUI welcome/finish side image (164x314 bitmap)."""
    img = Image.new("RGB", (width, height), BG_DEEP)
    draw = ImageDraw.Draw(img)

    # Gradient
    for y in range(height):
        t = y / height
        r = int(BG_DEEP[0] * (1 - t) + BG_PRIMARY[0] * t)
        g = int(BG_DEEP[1] * (1 - t) + BG_PRIMARY[1] * t)
        b = int(BG_DEEP[2] * (1 - t) + BG_PRIMARY[2] * t)
        draw.line([(0, y), (width, y)], fill=(r, g, b))

    # Cognisn logo mark
    _draw_cognisn_logo(draw, width // 2, 70, radius=30)

    # Lightning bolt centered below logo
    _draw_lightning_bolt(draw, width // 2, 140, 50)

    # "Spark" below bolt
    font_title = _try_font(22, bold=True)
    font_sub = _try_font(10)

    title = "Spark"
    bbox = draw.textbbox((0, 0), title, font=font_title)
    tw = bbox[2] - bbox[0]
    draw.text(((width - tw) // 2, 175), title, fill=TEXT_PRIMARY, font=font_title)

    sub = "Secure Personal"
    bbox = draw.textbbox((0, 0), sub, font=font_sub)
    sw = bbox[2] - bbox[0]
    draw.text(((width - sw) // 2, 205), sub, fill=TEXT_SECONDARY, font=font_sub)

    sub2 = "AI Research Kit"
    bbox = draw.textbbox((0, 0), sub2, font=font_sub)
    s2w = bbox[2] - bbox[0]
    draw.text(((width - s2w) // 2, 220), sub2, fill=TEXT_SECONDARY, font=font_sub)

    # Accent line
    draw.line([(30, 250), (width - 30, 250)], fill=ACCENT, width=1)

    # "by Cognisn" near bottom
    by_text = "by Cognisn"
    bbox = draw.textbbox((0, 0), by_text, font=font_sub)
    bw = bbox[2] - bbox[0]
    draw.text(((width - bw) // 2, height - 40), by_text, fill=ACCENT, font=font_sub)

    return img


def main() -> None:
    import os

    os.makedirs("assets/installer", exist_ok=True)

    # DMG background (1x — Finder maps pixels to points 1:1)
    dmg_bg = generate_dmg_background()
    dmg_bg.save("assets/installer/dmg-background.png")
    dmg_bg.save("assets/installer/dmg-background.tiff", format="TIFF")
    print("Generated: assets/installer/dmg-background.png (660x400)")

    # NSIS header image (BMP required)
    header = generate_nsis_header()
    header.save("assets/installer/nsis-header.bmp", format="BMP")
    print("Generated: assets/installer/nsis-header.bmp")

    # NSIS welcome side image (BMP required)
    welcome = generate_nsis_welcome()
    welcome.save("assets/installer/nsis-welcome.bmp", format="BMP")
    print("Generated: assets/installer/nsis-welcome.bmp")


if __name__ == "__main__":
    main()
