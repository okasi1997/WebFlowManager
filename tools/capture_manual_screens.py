"""現在の PySide6 画面を日本語操作手順書用に撮影する。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from qt_ui.application import QtFlowManagerApplication  # noqa: E402
from qt_ui.pages.flow_design import EventEditorDialog, EventGroupEditorDialog  # noqa: E402
from qt_ui.ui_loader import apply_application_font  # noqa: E402


OUTPUT = ROOT / "操作手順書_ja" / "images"


parser = argparse.ArgumentParser()
parser.add_argument(
    "--flow-only", action="store_true",
    help="フロー設計画面だけを現在のサンプルデータで撮影する",
)
parser.add_argument(
    "--data-only", action="store_true",
    help="データ管理画面だけを現在のサンプルデータで撮影する",
)
options = parser.parse_args()
if options.flow_only and options.data_only:
    parser.error("--flow-only と --data-only は同時に指定できません")


app = QtFlowManagerApplication()
apply_application_font("Meiryo UI", 10)
app.window.resize(1380, 820)
app.window.show()
app.qt.processEvents()


def save(widget, filename: str) -> None:
    widget.show()
    app.qt.processEvents()
    path = OUTPUT / filename
    if not widget.grab().save(str(path)):
        raise RuntimeError(f"画像を保存できませんでした: {path}")


design = app.window.pages["design"]
captured = 0
if options.data_only:
    app.window.show_page("data")
    app.qt.processEvents()
    save(app.window, "04_data_management_pyside6.png")
    captured = 1
else:
    first_workflow = next(iter(app.db.list_workflows()), None)
    if first_workflow is not None:
        design.reload(first_workflow["id"])
    app.window.show_page("design")
    app.qt.processEvents()
    save(app.window, "01_flow_design_pyside6.png")
    captured = 1

if not options.flow_only and not options.data_only:
    for page, filename in (
        ("schema", "03_data_schema_pyside6.png"),
        ("data", "04_data_management_pyside6.png"),
        ("auth", "05_auth_state_pyside6.png"),
        ("execution", "06_execution_pyside6.png"),
        ("settings", "07_settings_pyside6.png"),
    ):
        app.window.show_page(page)
        app.qt.processEvents()
        save(app.window, filename)
        captured += 1

    event = next(
        (
            dict(row)
            for workflow in app.db.list_workflows()
            for row in app.db.list_events(workflow["id"])
            if not str(row["action"]).endswith(("_start", "_end"))
        ),
        None,
    )
    event_dialog = EventEditorDialog(design, event or {})
    save(event_dialog, "02_event_editor_pyside6.png")
    event_dialog.close()

    group_dialog = EventGroupEditorDialog(design)
    save(group_dialog, "08_event_group_pyside6.png")
    group_dialog.close()
    captured += 2

app.window._force_close = True
app.window.close()
app.db.close()
print(f"{captured} 枚の画面画像を保存しました: {OUTPUT}")
