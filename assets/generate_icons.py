"""Generate Spark application icons for all platforms."""

from PIL import Image, ImageDraw


def create_spark_icon(size: int = 512) -> Image.Image:
    """Create the Spark lightning bolt icon."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Background rounded square
    margin = int(size * 0.03)
    radius = int(size * 0.18)
    draw.rounded_rectangle(
        [margin, margin, size - margin, size - margin],
        radius=radius,
        fill=(8, 14, 30, 255),  # #080e1e
    )

    # Lightning bolt
    s = size
    bolt = [
        (s * 0.55, s * 0.12),
        (s * 0.28, s * 0.47),
        (s * 0.47, s * 0.47),
        (s * 0.39, s * 0.88),
        (s * 0.72, s * 0.44),
        (s * 0.53, s * 0.44),
    ]
    bolt = [(int(x), int(y)) for x, y in bolt]
    draw.polygon(bolt, fill=(90, 170, 232, 255))  # #5aaae8

    return img


def main() -> None:
    icon = create_spark_icon(512)

    # PNG at various sizes
    for size in [16, 32, 48, 64, 128, 256, 512]:
        resized = icon.resize((size, size), Image.LANCZOS)
        resized.save(f"assets/icons/spark-{size}.png")

    # ICO for Windows (multi-size)
    sizes = [(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    icon.save("assets/icons/spark.ico", format="ICO", sizes=sizes)

    # ICNS is complex — macOS uses iconutil, so save the 512px and 1024px PNGs
    icon_1024 = create_spark_icon(1024)
    icon_1024.save("assets/icons/spark-1024.png")

    print("Icons generated in assets/icons/")


if __name__ == "__main__":
    main()
