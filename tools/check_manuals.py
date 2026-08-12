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
assert len(pages) == 9, f"unexpected manual count: {len(pages)}"

referenced_images: set[Path] = set()
for page in pages:
    source = page.read_text(encoding="utf-8")
    parser = ManualParser()
    parser.feed(source)
    assert parser.images or page.name == "index.html", f"image missing: {page}"
    for reference in [*parser.links, *parser.images]:
        target = local_target(page, reference)
        if target is not None:
            assert target.exists(), f"broken reference: {page} -> {reference}"
            if reference.lower().endswith(".png"):
                referenced_images.add(target)

    # Each section starts its procedural numbering at 1 and remains sequential.
    for block in re.split(r"<h[123][^>]*>", source, flags=re.I)[1:]:
        block = re.split(r"</h[123]>", block, maxsplit=1, flags=re.I)[-1]
        numbers = [int(value) for value in re.findall(r"<b>手順\s+(\d+)</b>", block)]
        if numbers:
            assert numbers == list(range(1, len(numbers) + 1)), (
                f"non-sequential steps in {page}: {numbers}"
            )

    assert "20260805.png" not in source, f"legacy Tk screenshot remains: {page}"

combined_source = "\n".join(page.read_text(encoding="utf-8") for page in pages)
for stale_text in (
    "並列 Session 数", "変数入力画面が表示", "JSON は追加",
    "現在のデータを保存", "全構造を同期", "画面から選択",
    "現在ページで検証", "このイベントまで実行",
    "共通データ", "正式実行", "本実行", "再試行",
):
    assert stale_text not in combined_source, f"stale manual wording remains: {stale_text}"

readme_and_manuals = ROOT.joinpath("README.md").read_text(encoding="utf-8") + combined_source
assert "Webフロー管理" not in readme_and_manuals, "product name must match the UI: Web フロー管理"

# 操作選択欄は日本語表示のため、内部キーだけを利用者向け名称として記載しない。
for internal_action in (
    "goto", "click", "fill", "select", "wait", "press",
    "get_text", "screenshot", "pause", "upload_file",
):
    assert f"<code>{internal_action}</code>" not in combined_source, (
        f"internal action key used as a visible manual label: {internal_action}"
    )

for image in referenced_images:
    width, height = Image.open(image).size
    assert width >= 900 and height >= 600, f"manual image is too small: {image}"

packaged_manual = ROOT / "dist" / "WebFlowManager" / "操作手順書_ja"
if packaged_manual.exists():
    for source in MANUAL_ROOT.rglob("*"):
        if source.is_file():
            packaged = packaged_manual / source.relative_to(MANUAL_ROOT)
            assert packaged.exists(), f"packaged manual file missing: {packaged}"
            assert source.read_bytes() == packaged.read_bytes(), (
                f"packaged manual file differs: {packaged}"
            )

print(f"manual checks passed: {len(pages)} HTML files, {len(referenced_images)} current images")
