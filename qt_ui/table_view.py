from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any, TypeVar

from PySide6.QtCore import QEvent, QObject, QPointF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView, QHeaderView, QProxyStyle, QStyle,
    QPushButton, QStyledItemDelegate, QTableView, QTreeView, QTreeWidget, QTreeWidgetItem,
)

from i18n import tr

TREE_LEVEL_INDENT = 14
RowKey = TypeVar('RowKey')


def set_row_enabled_appearance(item: QTreeWidgetItem, enabled: bool) -> None:
    """無効な行を共通の淡い文字色で表示する。"""
    if enabled:
        return
    disabled_color = QColor('#929da6')
    for column in range(item.columnCount()):
        item.setForeground(column, disabled_color)


def order_with_inserted_after(
    values: Iterable[RowKey], inserted: Iterable[RowKey], selected: RowKey | None,
) -> list[RowKey]:
    """追加値を選択値の直後、選択なしの場合は末尾へ配置した順序を返す。"""
    inserted_values = list(inserted)
    result = [value for value in values if value not in inserted_values]
    insert_at = result.index(selected) + 1 if selected in result else len(result)
    result[insert_at:insert_at] = inserted_values
    return result


def capture_scroll_position(view: QAbstractItemView) -> tuple[int, int]:
    """表を再構築する前の横・縦スクロール位置を取得する。"""
    return view.horizontalScrollBar().value(), view.verticalScrollBar().value()


def restore_scroll_position(
    view: QAbstractItemView, position: tuple[int, int],
) -> None:
    """選択変更による自動スクロール後に元の表示位置へ戻す。"""
    horizontal, vertical = position
    view.horizontalScrollBar().setValue(horizontal)
    view.verticalScrollBar().setValue(vertical)


def capture_tree_display_state(
    tree: QTreeWidget, key: Callable[[QTreeWidgetItem], Any],
) -> tuple[tuple[int, int], bool, set[Any]]:
    """ツリーのスクロール位置と展開済み項目をまとめて記録する。"""
    expanded: set[Any] = set()
    stack = [tree.topLevelItem(index) for index in range(tree.topLevelItemCount())]
    while stack:
        item = stack.pop()
        if item.isExpanded():
            expanded.add(key(item))
        stack.extend(item.child(index) for index in range(item.childCount()))
    return capture_scroll_position(tree), bool(tree.topLevelItemCount()), expanded


def restore_tree_display_state(
    tree: QTreeWidget, state: tuple[tuple[int, int], bool, set[Any]],
    key: Callable[[QTreeWidgetItem], Any],
) -> None:
    """再構築後のツリーへ展開状態とスクロール位置を復元する。"""
    scroll, had_items, expanded = state
    if had_items:
        stack = [tree.topLevelItem(index) for index in range(tree.topLevelItemCount())]
        while stack:
            item = stack.pop()
            item.setExpanded(key(item) in expanded)
            stack.extend(item.child(index) for index in range(item.childCount()))
    else:
        tree.expandAll()
    restore_scroll_position(tree, scroll)


class _PreservePointerScrollFilter(QObject):
    """セルのクリックで Qt がビューを自動スクロールする動作を打ち消す。"""

    def __init__(self, view: QAbstractItemView) -> None:
        super().__init__(view)
        self.view = view
        self._position: tuple[int, int] | None = None
        self._restoring = False
        self._press_point = None
        view.horizontalScrollBar().valueChanged.connect(self._restore_while_active)
        view.verticalScrollBar().valueChanged.connect(self._restore_while_active)
        view.horizontalScrollBar().sliderPressed.connect(self._clear)
        view.verticalScrollBar().sliderPressed.connect(self._clear)

    def _begin(self, event) -> None:
        if self._position is None:
            self._position = capture_scroll_position(self.view)
        if hasattr(event, 'position'):
            self._press_point = event.position().toPoint()

    def _clear(self) -> None:
        self._position = None
        self._press_point = None

    def _restore_while_active(self, _value: int = 0) -> None:
        if self._position is None or self._restoring:
            return
        self._restoring = True
        try:
            restore_scroll_position(self.view, self._position)
        except (AttributeError, RuntimeError):
            # 終了処理でビューの C++ オブジェクトが先に破棄された場合は何もしない。
            self._position = None
        finally:
            self._restoring = False

    def _finish_pointer_event(self) -> None:
        # Qt の自動スクロールは約 1 秒後にも発生するため、ここでは解除しない。
        # 次の明示的なスクロール操作まで位置を保持する。
        self._restore_while_active()

    def eventFilter(self, watched, event) -> bool:
        try:
            viewport = self.view.viewport()
        except (AttributeError, RuntimeError):
            return False
        if watched is not viewport:
            if event.type() in (QEvent.Type.MouseButtonPress, QEvent.Type.Wheel):
                self._clear()
            return super().eventFilter(watched, event)
        if event.type() in (
            QEvent.Type.MouseButtonPress,
            QEvent.Type.MouseButtonDblClick,
        ):
            self._begin(event)
        elif event.type() == QEvent.Type.MouseButtonRelease:
            self._finish_pointer_event()
        elif event.type() == QEvent.Type.MouseMove and self._press_point is not None:
            point = event.position().toPoint()
            if (point - self._press_point).manhattanLength() >= 6:
                # 行ドラッグ中の自動スクロールは利用者の意図した操作として許可する。
                self._clear()
        elif event.type() in (
            QEvent.Type.Wheel, QEvent.Type.KeyPress, QEvent.Type.Hide,
        ):
            self._clear()
        return super().eventFilter(watched, event)


def configure_row_move_tooltips(
    up_button: QPushButton, down_button: QPushButton, target_name: str,
) -> None:
    """行移動ボタンの方向と対象が分かるツールチップを共通設定する。"""
    up_button.setToolTip(f'{tr("選択した")}{tr(target_name)}{tr("を上へ移動")}')
    down_button.setToolTip(f'{tr("選択した")}{tr(target_name)}{tr("を下へ移動")}')


class HierarchicalReorderTreeWidget(QTreeWidget):
    """ドラッグと上下ボタンで同じ階層移動規則を使うツリー。"""

    orderChanged = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._drag_source: QTreeWidgetItem | None = None
        self._container_test: Callable[[QTreeWidgetItem], bool] = lambda item: bool(item.childCount())
        self._move_test: Callable[[QTreeWidgetItem, QTreeWidgetItem], bool] = lambda _source, _parent: True

    def setContainerTest(self, test: Callable[[QTreeWidgetItem], bool]) -> None:
        """子要素を格納できる行の判定を画面固有のデータに合わせる。"""
        self._container_test = test

    def setMoveTest(self, test: Callable[[QTreeWidgetItem, QTreeWidgetItem], bool]) -> None:
        """画面固有の重複条件など、移動先の妥当性判定を設定する。"""
        self._move_test = test

    def startDrag(self, supported_actions) -> None:
        self._drag_source = self.currentItem()
        # Qt に元行を削除させず、項目一式の移動をこのクラスだけで処理する。
        super().startDrag(Qt.DropAction.CopyAction)

    @staticmethod
    def _is_ancestor(item: QTreeWidgetItem, candidate: QTreeWidgetItem | None) -> bool:
        while candidate is not None:
            if candidate is item:
                return True
            candidate = candidate.parent()
        return False

    def _parent_item(self, item: QTreeWidgetItem) -> QTreeWidgetItem:
        return item.parent() or self.invisibleRootItem()

    def _move_item(
        self, source: QTreeWidgetItem, parent: QTreeWidgetItem, index: int,
    ) -> bool:
        """サブツリーを保ったまま指定親の挿入位置へ移す。"""
        if parent is source or self._is_ancestor(source, parent) or not self._move_test(source, parent):
            return False
        source_parent = self._parent_item(source)
        source_index = source_parent.indexOfChild(source)
        if source_index < 0:
            return False
        if source_parent is parent and source_index < index:
            index -= 1
        if source_parent is parent and source_index == index:
            return False
        moved = source_parent.takeChild(source_index)
        parent.insertChild(max(0, min(index, parent.childCount())), moved)
        self.setCurrentItem(moved)
        moved.setSelected(True)
        self.orderChanged.emit()
        return True

    def dropEvent(self, event) -> None:
        source = self._drag_source
        self._drag_source = None
        if source is None:
            event.ignore()
            return
        target = self.itemAt(event.position().toPoint())
        if target is None:
            parent, index = self.invisibleRootItem(), self.topLevelItemCount()
        else:
            rect = self.visualItemRect(target)
            relative_y = event.position().y() - rect.top()
            # 中央 1/3 は構造体の中、上下 1/3 は対象行の前後として扱う。
            if self._container_test(target) and rect.height() / 3 <= relative_y <= rect.height() * 2 / 3:
                parent, index = target, target.childCount()
            else:
                parent = self._parent_item(target)
                index = parent.indexOfChild(target) + (relative_y > rect.height() / 2)
        if parent is source or self._is_ancestor(source, parent) or not self._move_test(source, parent):
            event.ignore()
            return
        if self._move_item(source, parent, index):
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
        else:
            event.ignore()

    def moveCurrent(self, direction: int) -> bool:
        """表示上の隣接位置へ動かし、構造体の出入りも同じ規則で行う。"""
        source = self.currentItem()
        if source is None or direction not in (-1, 1):
            return False
        parent = self._parent_item(source)
        index = parent.indexOfChild(source)
        adjacent_index = index + direction
        if 0 <= adjacent_index < parent.childCount():
            adjacent = parent.child(adjacent_index)
            if self._container_test(adjacent):
                # 上方向では構造体の末尾、下方向では先頭へ入り、視覚的な距離を最小にする。
                child_index = adjacent.childCount() if direction < 0 else 0
                return self._move_item(source, adjacent, child_index)
            return self._move_item(source, parent, adjacent_index + (direction > 0))

        if parent is self.invisibleRootItem():
            return False
        grandparent = self._parent_item(parent)
        parent_index = grandparent.indexOfChild(parent)
        return self._move_item(source, grandparent, parent_index + (direction > 0))


class BranchNeutralStyle(QProxyStyle):
    """展開矢印を残し、階層余白だけが選択色になる描画を抑止する。"""

    def drawPrimitive(self, element, option, painter, widget=None) -> None:
        if element == QStyle.PrimitiveElement.PE_PanelItemViewRow:
            # ツリーの字下げ領域は delegate ではなく行パネルとして描画される。
            # セル側と同様に hover 状態を除き、左端だけ青くなる表示を防ぐ。
            original_state = option.state
            option.state &= ~QStyle.StateFlag.State_MouseOver
            try:
                super().drawPrimitive(element, option, painter, widget)
            finally:
                option.state = original_state
            return
        if element == QStyle.PrimitiveElement.PE_IndicatorBranch:
            # Windows の標準スタイルは葉ノードの分岐余白にも選択背景を描くため、
            # 分岐領域を全面的に自前描画し、子を持つ項目にだけ矢印を表示する。
            if not option.state & QStyle.StateFlag.State_Children:
                return
            center = option.rect.center()
            half_size = max(3.0, min(option.rect.width(), option.rect.height()) * 0.18)
            if option.state & QStyle.StateFlag.State_Open:
                points = (
                    QPointF(center.x() - half_size, center.y() - half_size / 2),
                    QPointF(center.x(), center.y() + half_size / 2),
                    QPointF(center.x() + half_size, center.y() - half_size / 2),
                )
            else:
                points = (
                    QPointF(center.x() - half_size / 2, center.y() - half_size),
                    QPointF(center.x() + half_size / 2, center.y()),
                    QPointF(center.x() - half_size / 2, center.y() + half_size),
                )
            painter.save()
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setPen(QPen(
                QColor('#526979'), 1.5, Qt.PenStyle.SolidLine,
                Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin,
            ))
            painter.drawLine(points[0], points[1])
            painter.drawLine(points[1], points[2])
            painter.restore()
            return
        super().drawPrimitive(element, option, painter, widget)


class _RowOnlyItemDelegate(QStyledItemDelegate):
    """セル単位のホバー色とフォーカス枠を描画せず、行選択だけを表示する。"""

    def initStyleOption(self, option, index) -> None:
        super().initStyleOption(option, index)
        # 設定画面で変更された現在の表フォントを、委譲描画にも必ず使用する。
        option.font = self.parent().font()
        # 表ではセル単位の反応が行選択に見えるため、Qt の描画状態から除外する。
        option.state &= ~QStyle.StateFlag.State_MouseOver
        option.state &= ~QStyle.StateFlag.State_HasFocus


def configure_table_view(view: QAbstractItemView, *, reorder: bool = False) -> None:
    """表形式ビューに共通の表示・選択・列操作設定を適用する。"""
    view.setAlternatingRowColors(True)
    view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    view.setItemDelegate(_RowOnlyItemDelegate(view))
    # 行や右端セルを選択しても、ビューの横・縦位置を勝手に変えない。
    pointer_scroll_filter = _PreservePointerScrollFilter(view)
    view.viewport().installEventFilter(pointer_scroll_filter)
    view.horizontalScrollBar().installEventFilter(pointer_scroll_filter)
    view.verticalScrollBar().installEventFilter(pointer_scroll_filter)
    view._pointer_scroll_filter = pointer_scroll_filter
    if isinstance(view, QTreeView):
        # Designer のプレースホルダーを実行時に置換するツリーにも同じ操作規則を適用する。
        view.setExpandsOnDoubleClick(False)
        # すべてのツリー表で階層余白の青い矩形を表示しない。
        branch_style = BranchNeutralStyle()
        branch_style.setParent(view)
        view.setStyle(branch_style)
        # 深い階層でも第 1 列の本文幅を確保できるよう、標準より字下げを狭くする。
        view.setIndentation(TREE_LEVEL_INDENT)
    view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    # 幅の広い列でも小刻みに滑らかに移動できるよう、画素単位でスクロールする。
    view.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
    view.horizontalScrollBar().setSingleStep(24)

    # 列順と列幅は、すべて利用者が調整できる状態に統一する。
    header = view.header() if hasattr(view, 'header') else view.horizontalHeader()
    header.setSectionsMovable(True)
    header.setFirstSectionMovable(True)
    header.setStretchLastSection(False)
    for column in range(header.count()):
        header.setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)

    # QTreeWidget には元から格子線がなく、QTableWidget のみ明示的に消す。
    if isinstance(view, QTableView):
        view.setShowGrid(False)

    if reorder:
        # セルの上書きではなく、行全体を挿入位置へ移動する。
        view.setDragEnabled(True)
        view.setAcceptDrops(True)
        view.viewport().setAcceptDrops(True)
        view.setDropIndicatorShown(True)
        view.setDragDropOverwriteMode(False)
        view.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)


def update_preserving_scroll(
        views: Iterable[QAbstractItemView], update: Callable[[], None],
) -> None:
    """表を再構築しても、各ビューの縦横スクロール位置を維持する。"""
    positions = [
        (view, view.horizontalScrollBar().value(), view.verticalScrollBar().value())
        for view in views
    ]
    update()
    for view, horizontal, vertical in positions:
        view.horizontalScrollBar().setValue(horizontal)
        view.verticalScrollBar().setValue(vertical)


def set_column_layout(
        view: QAbstractItemView, logical_order: tuple[int, ...], widths: tuple[int, ...],
) -> None:
    """列名とデータの論理番号を変えず、初期表示順と幅だけを設定する。"""
    header = view.header() if hasattr(view, 'header') else view.horizontalHeader()
    if len(logical_order) != header.count() or len(widths) != header.count():
        raise ValueError('列数とレイアウト設定数が一致していません')
    for logical_column, width in enumerate(widths):
        header.resizeSection(logical_column, width)
    # moveSection は表示位置だけを変更するため、列別の操作判定には影響しない。
    for visual_column, logical_column in enumerate(logical_order):
        current_visual = header.visualIndex(logical_column)
        if current_visual != visual_column:
            header.moveSection(current_visual, visual_column)
