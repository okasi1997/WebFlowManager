from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


# Letters identify persistent screen areas without being confused with steps.
CALLOUTS_BY_FILE = {
    "01_main.png": (
        ((175, 655), "A"),
        ((720, 78), "B"),
        ((970, 78), "C"),
        ((795, 675), "D"),
    ),
    "02_event_editor.png": (
        ((250, 151), "A"),
        ((250, 386), "B"),
        ((760, 336), "C"),
        ((760, 536), "D"),
        ((805, 626), "E"),
    ),
    "03_schema.png": (
        ((210, 270), "A"),
        ((105, 511), "B"),
        ((385, 511), "C"),
        ((610, 542), "D"),
    ),
    "04_data.png": (
        ((155, 307), "A"),
        ((155, 543), "B"),
        ((660, 315), "C"),
        ((535, 613), "D"),
        ((920, 613), "E"),
    ),
    "05_auth.png": (
        ((325, 70), "A"),
        ((175, 182), "B"),
        ((335, 182), "C"),
        ((570, 355), "D"),
    ),
}


def load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for candidate in (
        Path(r"C:\Windows\Fonts\arialbd.ttf"),
        Path(r"C:\Windows\Fonts\segoeuib.ttf"),
    ):
        if candidate.exists():
            return ImageFont.truetype(str(candidate), size)
    return ImageFont.load_default()


def relabel(path: Path) -> None:
    image = Image.open(path).convert("RGBA")
    draw = ImageDraw.Draw(image)
    font = load_font(15)
    radius = 13
    red = (222, 47, 36, 255)

    for (x, y), label in CALLOUTS_BY_FILE[path.name]:
        draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=red)
        box = draw.textbbox((0, 0), label, font=font)
        width = box[2] - box[0]
        height = box[3] - box[1]
        draw.text(
            (x - width / 2, y - height / 2 - box[1]),
            label,
            fill="white",
            font=font,
        )

    image.convert("RGB").save(path, optimize=True)


for relative_root in (
    Path("操作手順書_ja/images"),
):
    for filename in CALLOUTS_BY_FILE:
        image_path = relative_root / filename
        if image_path.exists():
            relabel(image_path)
