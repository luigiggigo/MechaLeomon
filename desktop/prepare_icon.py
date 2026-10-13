"""Resize the validated transparent source into app-ready PNG and ICO files."""

from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "assets" / "mechaleomon_icon_source.png"
PNG = ROOT / "assets" / "mechaleomon_icon.png"
ICO = ROOT / "assets" / "mechaleomon_icon.ico"


def main() -> None:
    transparent = Image.open(SOURCE).convert("RGBA")
    bounds = transparent.getbbox()
    if bounds is None:
        raise RuntimeError("L'icona è completamente trasparente")
    cropped = transparent.crop(bounds)
    canvas = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    cropped.thumbnail((232, 232), Image.Resampling.LANCZOS)
    canvas.alpha_composite(cropped, ((256 - cropped.width) // 2, (256 - cropped.height) // 2))
    canvas.save(PNG, optimize=True)
    canvas.save(ICO, sizes=[(16, 16), (24, 24), (32, 32), (48, 48),
                           (64, 64), (128, 128), (256, 256)])

    corner_alpha = [canvas.getpixel(point)[3] for point in ((0, 0), (255, 0), (0, 255), (255, 255))]
    opaque = sum(1 for alpha in canvas.getchannel("A").get_flattened_data() if alpha > 240)
    if any(corner_alpha) or not 20_000 <= opaque <= 62_000:
        raise RuntimeError(f"Validazione trasparenza fallita: corners={corner_alpha}, opaque={opaque}")
    print(f"Icona pronta: {PNG} e {ICO}")


if __name__ == "__main__":
    main()
