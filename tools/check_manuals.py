from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
MANUAL_ROOT = ROOT / "操作手順書_ja"


class ManualParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []
        self.images: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "a" and values.get("href"):
            self.links.append(values["href"] or "")
        if tag == "img" and values.get("src"):
            self.images.append(values["src"] or "")


def local_target(page: Path, reference: str) -> Path | None:
    if reference.startswith(("#", "http://", "https://")):
        return None
    return (page.parent / unquote(reference.split("#", 1)[0])).resolve()


pages = sorted(MANUAL_ROOT.glob("*.html"))
assert len(pages) == 7, f"unexpected manual count: {len(pages)}"

for page in pages:
    text = page.read_text(encoding="utf-8")
    parser = ManualParser()
    parser.feed(text)
    assert parser.images or page.name == "index.html", f"image missing: {page}"
    for reference in [*parser.links, *parser.images]:
        target = local_target(page, reference)
        if target is not None:
            assert target.exists(), f"broken reference: {page} -> {reference}"

    # Each procedure block restarts at 1 after the next heading.
    for block in re.split(r"<h[123][^>]*>", text, flags=re.I)[1:]:
        block = re.split(r"</h[123]>", block, maxsplit=1, flags=re.I)[-1]
        numbers = [int(value) for value in re.findall(r"<b>手順\s+(\d+)</b>", block)]
        if numbers:
            assert numbers == list(range(1, len(numbers) + 1)), (
                f"non-sequential steps in {page}: {numbers}"
            )

for page in sorted(MANUAL_ROOT.glob("0[1-5]_*.html")):
    text = page.read_text(encoding="utf-8")
    assert not re.search(r"<div class=\"legend\">.*[①②③④⑤]", text), (
        f"old numbered callout remains: {page}"
    )
    assert "手順番号ではなく" in text
    assert 'class="success"' in text
    assert 'class="manual-nav"' in text

source_root = MANUAL_ROOT / "images"
expected_sizes = {
    "01_main.png": (1200, 760),
    "02_event_editor.png": (1000, 660),
    "03_schema.png": (720, 560),
    "04_data.png": (1050, 650),
    "05_auth.png": (680, 390),
}
for filename, expected_size in expected_sizes.items():
    source = source_root / filename
    assert Image.open(source).size == expected_size

packaged_manual = ROOT / "dist/WebFlowManager/操作手順書_ja"
if packaged_manual.exists():
    for source in MANUAL_ROOT.rglob("*"):
        if source.is_file():
            relative = source.relative_to(MANUAL_ROOT)
            packaged = packaged_manual / relative
            assert packaged.exists(), f"packaged manual file missing: {relative}"
            assert source.read_bytes() == packaged.read_bytes(), (
                f"packaged manual file differs: {relative}"
            )

print(f"manual checks passed: {len(pages)} HTML files, {len(expected_sizes)} images")
