"""メイン画面、ダイアログ管理、および PCL バッチ実行の調停を行う。"""
from __future__ import annotations
import ctypes
import json
import ntpath
import os
import re
import sqlite3
import sys
import threading
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from tkinter import font as tkfont
from typing import Callable
from browser.element_picker import DebugBrowserSession
from browser.auth_session import AuthBrowserSession
from browser.profile_runtime import clear_profile, persistent_profile_dir, profile_has_state
from core.database import Database
from core.conditions import decode_guard, summarize_guard
from core.executor import WorkflowExecutor, find_variables
from core.settings import DEFAULT_START_URL, runtime_settings
from i18n import install_tk_translation, set_language, tr
from ui.dialogs import EventDialog, EventGroupDialog, GuardConditionDialog, VariablesDialog, guard_operator_labels
from ui.auth_state import AuthStateDialog, profile_path
from ui.message_dialog import install_app_messageboxes
from ui.structured_data import DataPathDialog, HierarchicalDataDialog, SchemaDesignerDialog
from ui.ui_helpers import AutoScrollbar, toggle_tree_indicator_on_double_click, tree_toggle_all_button

class FlowManagerApp:
    """Tk の画面状態と、バックグラウンドで動く実行処理を接続する。"""

    def __init__(self) -> None:
        # 言語は Tk ウィジェットを作る前に確定する必要がある。
        # 作成後に変更すると、一部の ttk 内部文字列だけ旧言語が残るためである。
        frozen = getattr(sys, 'frozen', False)
        self.project_dir = Path(sys.executable).resolve().parent if frozen else Path(__file__).resolve().parent
        self.resource_dir = Path(getattr(sys, '_MEIPASS', self.project_dir)).resolve() if frozen else self.project_dir
        self.log_dir = self.project_dir / 'log'
        self.log_dir.mkdir(parents=True, exist_ok=True)
        (self.project_dir / 'data').mkdir(parents=True, exist_ok=True)
        self._log_file_lock = threading.Lock()
        self.db = Database(self.project_dir / 'data' / 'flows.db')
        set_language(self.db.get_language())
        install_tk_translation()
        start_url = self.db.get_start_url()
        if start_url is None:
            start_url = self._read_legacy_start_url()
            self.db.set_start_url(start_url)
        self.settings = runtime_settings(start_url)
        self.ui_font_family, self.ui_font_size = self.db.get_ui_font()
        if os.name == 'nt':
            try:
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('WebFlowManager.Desktop')
            except (AttributeError, OSError):
                pass
        self.root = tk.Tk()
        self.root.title('msg.0002')
        self.app_icon_path = self.resource_dir / 'assets' / 'app.ico'
        if self.app_icon_path.is_file():
            try:
                self.root.iconbitmap(default=str(self.app_icon_path))
            except tk.TclError:
                pass
        self.root.geometry('1380x820')
        self.root.minsize(1180, 720)
        self.root.configure(bg='#F6F7F9')
        self.root._flow_show_toplevel = self._show_toplevel
        self._install_deferred_toplevel_display()
        self._configure_styles()
        install_app_messageboxes(self.root)
        self.root.bind_class('TCombobox', '<<ComboboxSelected>>', self._clear_combobox_text_selection, add='+')
        self.current_workflow_id: int | None = None
        self.running = False
        self.execution_starting = False
        self.executing_tasks: dict[str, tuple[dict[str, object], dict[str, object] | None]] = {}
        self.execution_task_states: dict[str, tuple[dict[str, object], dict[str, object] | None, str]] = {}
        self.open_dialogs: dict[str, tk.Toplevel] = {}
        self.browser_visible = tk.BooleanVar(value=self.db.get_browser_visible())
        self._build_settings_state()
        self.debug_browser = DebugBrowserSession(
            self.project_dir, self.settings['picker']['start_url'], self._log,
            lambda: profile_path(self.project_dir, self.db.get_auth_profile()))
        self.auth_browser = AuthBrowserSession(self.project_dir, lambda message: self._log(message, 'AuthBrowserSession'))
        self.drag_source: dict[str, str | None] = {'workflow': None, 'event': None}
        self.drag_start_points: dict[str, tuple[int, int] | None] = {'workflow': None, 'event': None}
        self.drag_active: dict[str, bool] = {'workflow': False, 'event': False}
        self.current_page_name: str | None = None
        try:
            self._build_ui()
        except Exception:
            # 初期画面の構築に失敗した場合も Playwright の子プロセスを確実に終了し、
            # 親プロセス終了後の EPIPE エラーを残さない。
            for browser_session in (self.debug_browser, self.auth_browser):
                try:
                    browser_session.shutdown()
                except Exception:
                    pass
            self.db.close()
            self.root.destroy()
            raise
        self._apply_font_configuration()
        initial_workflows = self.db.list_workflows()
        self._refresh_workflows(initial_workflows[0]['id'] if initial_workflows else None)
        self.root.after_idle(lambda: self._apply_window_chrome(self.root))
        self.root.protocol('WM_DELETE_WINDOW', self._close)

    def run(self) -> None:
        self.root.mainloop()

    def _clear_combobox_text_selection(self, event: tk.Event) -> None:
        """選択値を維持したままフィールド文字列の選択表示を解除する。"""
        widget = event.widget

        def clear_selection() -> None:
            if not widget.winfo_exists():
                return
            try:
                widget.selection_clear()
                widget.icursor('end')
            except tk.TclError:
                pass

        widget.after_idle(clear_selection)

    @staticmethod
    def _install_deferred_toplevel_display() -> None:
        """サブクラスによる構築が完了するまで各 Toplevel を非表示に保つ。"""
        if not hasattr(simpledialog, '_flow_original_place_window'):
            original_place_window = simpledialog._place_window
            simpledialog._flow_original_place_window = original_place_window

            def place_simple_dialog(window: tk.Toplevel, parent: tk.Misc | None = None) -> None:
                callback = getattr(window._root(), '_flow_show_toplevel', None)
                if callback is None:
                    original_place_window(window, parent)
                else:
                # simpledialog はこの関数の直後に wait_visibility() を呼び出す。
                # 最初の表示イベントをここで消費せず、非表示のまま位置を決めてから
                # idle コールバックで表示し、wait_visibility が検出できるようにする。
                    window.configure(bg='#F3F3F3')
                    window.update_idletasks()
                    width = max(window.winfo_reqwidth(), window.winfo_width())
                    height = max(window.winfo_reqheight(), window.winfo_height())
                    anchor = window._root()
                    anchor.update_idletasks()
                    x = anchor.winfo_x() + (anchor.winfo_width() - width) // 2
                    y = anchor.winfo_y() + (anchor.winfo_height() - height) // 2
                    window.geometry(f'{width}x{height}{x:+d}{y:+d}')
                    window._flow_centered = True
                    try:
                        window.attributes('-alpha', 0.0)
                    except tk.TclError:
                        pass

                    def reveal() -> None:
                        if not window.winfo_exists():
                            return
                        window.deiconify()

                        def make_opaque() -> None:
                            if not window.winfo_exists():
                                return
                            try:
                                window.attributes('-alpha', 1.0)
                            except tk.TclError:
                                pass

                        window.after_idle(make_opaque)

                    window.after_idle(reveal)

            simpledialog._place_window = place_simple_dialog

        if hasattr(tk.Toplevel, '_flow_original_init'):
            return
        original_init = tk.Toplevel.__init__
        tk.Toplevel._flow_original_init = original_init

        def hidden_init(window: tk.Toplevel, *args: object, **kwargs: object) -> None:
            original_init(window, *args, **kwargs)
            window.withdraw()

            def queue_display() -> None:
                if not window.winfo_exists():
                    return
                callback = getattr(window._root(), '_flow_show_toplevel', None)
                if callback is None:
                    window.deiconify()
                else:
                    window.after(1, lambda: callback(window) if window.winfo_exists() else None)

            window.after_idle(queue_display)

        tk.Toplevel.__init__ = hidden_init

    def _show_toplevel(self, window: tk.Toplevel) -> None:
        if not window.winfo_exists() or getattr(window, '_flow_centered', False):
            return
        if self.app_icon_path.is_file():
            try:
                window.iconbitmap(default=str(self.app_icon_path))
            except tk.TclError:
                pass
        window.configure(bg='#F3F3F3')
        window._flow_centered = True
        window.update_idletasks()
        width = window.winfo_width()
        height = window.winfo_height()
        try:
            geometry_size = window.geometry().split('+', 1)[0]
            geometry_width, geometry_height = (int(value) for value in geometry_size.split('x', 1))
            if geometry_width > 1 and geometry_height > 1:
                width, height = geometry_width, geometry_height
        except (TypeError, ValueError):
            pass
        if width <= 1:
            width = window.winfo_reqwidth()
        if height <= 1:
            height = window.winfo_reqheight()
        # 全ダイアログをメイン画面の中央へ配置する。直近の master を基準にすると
        # 入れ子のダイアログが親側へずれ、特に複数モニター環境で目立つためである。
        anchor = self.root
        if anchor.winfo_exists() and anchor.winfo_ismapped():
            anchor.update_idletasks()
            x = anchor.winfo_x() + (anchor.winfo_width() - width) // 2
            y = anchor.winfo_y() + (anchor.winfo_height() - height) // 2
        else:
            x = (window.winfo_screenwidth() - width) // 2
            y = (window.winfo_screenheight() - height) // 2
        geometry = f'{x:+d}{y:+d}'
        window.geometry(geometry)
        # Windows は owned/transient ウィンドウの初回位置を置き換える場合がある。
        # 不可視状態で表示して正確な座標を再設定してから可視化し、既定位置や
        # 左上に一瞬表示される残像を防ぐ。
        try:
            window.attributes('-alpha', 0.0)
        except tk.TclError:
            pass
        window.deiconify()
        window.update()
        window.geometry(geometry)
        window.update_idletasks()
        try:
            window.attributes('-alpha', 1.0)
        except tk.TclError:
            pass
        window.after_idle(lambda: self._apply_window_chrome(window))

    @staticmethod
    def _apply_window_chrome(window: tk.Misc) -> None:
        """Windows 11 のタイトルバーも VS Code Light に近い配色へ揃える。"""
        if os.name != 'nt' or not window.winfo_exists():
            return
        try:
            window.update_idletasks()
            child_handle = window.winfo_id()
            handle = ctypes.windll.user32.GetParent(child_handle) or child_handle
            # COLORREF は RGB ではなく BGR 順で各色を格納する。
            colors = {34: 0x00D4D4D4, 35: 0x00F3F3F3, 36: 0x001F1F1F}
            for attribute, color in colors.items():
                value = ctypes.c_int(color)
                ctypes.windll.dwmapi.DwmSetWindowAttribute(handle, attribute, ctypes.byref(value), ctypes.sizeof(value))
        except (AttributeError, OSError, tk.TclError):
            # 古い Windows では属性が利用できないため、通常のタイトルバーへフォールバックする。
            return

    def _register_dialog(self, key: str, factory: Callable[[], tk.Toplevel]) -> tk.Toplevel | None:
        # 同じ用途のウィンドウを二重に開かず、既存画面を前面へ戻す。
        existing = self.open_dialogs.get(key)
        if existing is not None and existing.winfo_exists():
            existing.deiconify()
            existing.lift()
            existing.focus_force()
            return None
        dialog = factory()
        self.open_dialogs[key] = dialog
        dialog.bind('<Destroy>', lambda event: self.open_dialogs.pop(key, None) if event.widget is dialog else None, add='+')
        return dialog

    def _configure_styles(self) -> None:
        style = ttk.Style(self.root)
        if 'clam' in style.theme_names():
            style.theme_use('clam')
        family, size = self.ui_font_family, self.ui_font_size
        font = (family, size)
        small_font = (family, max(8, size - 1))
        bold_font = (family, size, 'bold')
        self.root.option_add('*Font', font)
        style.configure('TFrame', background='#F3F3F3')
        style.configure('TLabel', background='#F3F3F3', foreground='#1F1F1F', font=font)
        style.configure('Section.TLabel', background='#F3F3F3', foreground='#1F1F1F', font=(family, size + 3, 'bold'), padding=(0, 2, 0, 6))
        style.configure('DialogSection.TLabel', background='#F3F3F3', foreground='#3B3B3B', font=bold_font, padding=(0, 2, 0, 4))
        style.configure('DialogCard.TFrame', background='#FFFFFF', relief='solid', borderwidth=1, bordercolor='#E1E1E1')
        style.configure('DialogCardBody.TFrame', background='#FFFFFF')
        style.configure('DialogFooter.TFrame', background='#F6F7F9')
        style.configure('DialogCard.TLabel', background='#FFFFFF', foreground='#1F1F1F', font=font)
        style.configure('MessageBody.TLabel', background='#FFFFFF', foreground='#171717', font=(family, size + 1), padding=(0, 2))
        style.map('DialogCard.TLabel', background=[('disabled', '#FFFFFF')], foreground=[('disabled', '#A0A0A0')])
        style.configure('DialogCardSection.TLabel', background='#FFFFFF', foreground='#1F1F1F', font=bold_font, padding=(0, 1, 0, 5))
        style.configure('DialogCardSubtle.TLabel', background='#FFFFFF', foreground='#616161', font=small_font)
        style.map('DialogCardSubtle.TLabel', background=[('disabled', '#FFFFFF')], foreground=[('disabled', '#A0A0A0')])
        style.configure('DialogCard.TCheckbutton', background='#FFFFFF', foreground='#1F1F1F', font=font)
        style.configure('InfoIcon.TLabel', background='#E5F3FF', foreground='#0078D4', font=(family, size + 5, 'bold'), padding=(8, 6), anchor='center')
        style.configure('WarningIcon.TLabel', background='#FFF4CE', foreground='#9D5D00', font=(family, size + 5, 'bold'), padding=(8, 6), anchor='center')
        style.configure('ErrorIcon.TLabel', background='#FDE7E9', foreground='#C42B1C', font=(family, size + 5, 'bold'), padding=(8, 6), anchor='center')
        style.map('DialogCard.TCheckbutton', background=[('active', '#FFFFFF')], foreground=[('disabled', '#A0A0A0')])
        style.configure('DialogAction.TButton', background='#E9EDF2', foreground='#1F1F1F', bordercolor='#E9EDF2', lightcolor='#E9EDF2', darkcolor='#E9EDF2', font=small_font, padding=(7, 12), relief='flat', borderwidth=1, focusthickness=1, focuscolor='#0078D4')
        style.map('DialogAction.TButton', background=[('active', '#DCE8F1'), ('pressed', '#CCDDE9'), ('disabled', '#F1F1F1')], foreground=[('disabled', '#999999')], bordercolor=[('active', '#DCE8F1'), ('focus', '#0078D4'), ('disabled', '#F1F1F1')], lightcolor=[('active', '#DCE8F1'), ('disabled', '#F1F1F1')], darkcolor=[('active', '#DCE8F1'), ('disabled', '#F1F1F1')])
        style.configure('DialogInline.TButton', background='#E9EDF2', foreground='#1F1F1F', bordercolor='#E9EDF2', lightcolor='#E9EDF2', darkcolor='#E9EDF2', font=small_font, padding=(8, 5), relief='flat', borderwidth=1, focusthickness=1, focuscolor='#0078D4')
        style.map('DialogInline.TButton', background=[('active', '#DCE8F1'), ('pressed', '#CCDDE9'), ('disabled', '#F1F1F1')], foreground=[('disabled', '#999999')], bordercolor=[('active', '#DCE8F1'), ('focus', '#0078D4'), ('disabled', '#F1F1F1')], lightcolor=[('active', '#DCE8F1'), ('disabled', '#F1F1F1')], darkcolor=[('active', '#DCE8F1'), ('disabled', '#F1F1F1')])
        style.configure('Dialog.TEntry', font=font, padding=(6, 4))
        style.configure('Dialog.TCombobox', font=font, padding=(6, 3))
        style.configure('DialogPlaceholder.TLabel', background='#F8F8F8', foreground='#A0A0A0', font=font)
        style.map('DialogPlaceholder.TLabel', background=[('disabled', '#F3F3F3')], foreground=[('disabled', '#A0A0A0')])
        style.configure('DialogCard.TSeparator', background='#E5E5E5', bordercolor='#E5E5E5', lightcolor='#E5E5E5', darkcolor='#E5E5E5')
        style.configure('DialogFooter.TFrame', background='#FAFAFA')
        style.configure('DialogFooter.TSeparator', background='#E1E1E1', bordercolor='#E1E1E1', lightcolor='#E1E1E1', darkcolor='#E1E1E1')
        style.configure('Subtle.TLabel', background='#F3F3F3', foreground='#616161', font=small_font)
        style.configure('TButton', font=small_font, padding=(10, 5), relief='flat', borderwidth=1, background='#FFFFFF', foreground='#1F1F1F', bordercolor='#D4D4D4', focusthickness=1, focuscolor='#0078D4')
        style.map('TButton', background=[('active', '#E8E8E8'), ('pressed', '#DCDCDC'), ('disabled', '#F3F3F3')], foreground=[('disabled', '#A0A0A0')], bordercolor=[('active', '#B8B8B8'), ('focus', '#0078D4')])
        style.configure('Primary.TButton', background='#0078D4', foreground='#FFFFFF', bordercolor='#0078D4', font=(family, max(8, size - 1), 'bold'), relief='flat')
        style.map('Primary.TButton', background=[('active', '#106EBE'), ('pressed', '#005A9E'), ('disabled', '#C8C8C8')], foreground=[('disabled', '#F3F3F3')], bordercolor=[('disabled', '#C8C8C8')])
        style.configure('Secondary.TButton', background='#EDF2F7', foreground='#1F1F1F', bordercolor='#D9E0E7', lightcolor='#EDF2F7', darkcolor='#EDF2F7', font=small_font, padding=(10, 7), relief='flat')
        style.map('Secondary.TButton', background=[('active', '#E1EAF3'), ('pressed', '#D4E1ED'), ('disabled', '#F4F5F6')], foreground=[('disabled', '#A0A0A0')], bordercolor=[('active', '#C8D7E5'), ('pressed', '#B9CCDD')], lightcolor=[('active', '#E1EAF3'), ('pressed', '#D4E1ED')], darkcolor=[('active', '#E1EAF3'), ('pressed', '#D4E1ED')])
        style.configure('MessagePrimary.TButton', background='#0078D4', foreground='#FFFFFF', bordercolor='#0078D4', lightcolor='#0078D4', darkcolor='#0078D4', font=(family, max(8, size - 1), 'bold'), padding=(10, 5), relief='flat', borderwidth=1)
        style.map('MessagePrimary.TButton', background=[('active', '#106EBE'), ('pressed', '#005A9E')], bordercolor=[('active', '#106EBE'), ('pressed', '#005A9E')])
        style.configure('MessageSecondary.TButton', background='#EDF2F7', foreground='#1F1F1F', bordercolor='#D9E0E7', lightcolor='#EDF2F7', darkcolor='#EDF2F7', font=(family, max(8, size - 1), 'bold'), padding=(10, 5), relief='flat', borderwidth=1)
        style.map('MessageSecondary.TButton', background=[('active', '#E1EAF3'), ('pressed', '#D4E1ED')], bordercolor=[('active', '#C8D7E5'), ('pressed', '#B9CCDD')], lightcolor=[('active', '#E1EAF3'), ('pressed', '#D4E1ED')], darkcolor=[('active', '#E1EAF3'), ('pressed', '#D4E1ED')])
        style.configure('Danger.TButton', background='#FDF0F1', foreground='#B42318', bordercolor='#F0C9CD', lightcolor='#FDF0F1', darkcolor='#FDF0F1', padding=(10, 7), relief='flat')
        style.map('Danger.TButton', background=[('disabled', '#F6F6F6'), ('active', '#FBE3E5'), ('pressed', '#F6D3D6')], foreground=[('disabled', '#A6A6A6'), ('active', '#9F1C13')], bordercolor=[('disabled', '#E5E5E5'), ('active', '#E7AEB4')])
        style.configure('Toolbar.TButton', background='#F5F7FA', foreground='#1F1F1F', bordercolor='#D9DEE5', font=small_font, padding=(9, 6), relief='flat')
        style.map('Toolbar.TButton', background=[('active', '#E8EEF4'), ('pressed', '#DDE6EF'), ('disabled', '#F4F5F6')], foreground=[('disabled', '#A0A0A0')], bordercolor=[('active', '#C5D1DD')])
        style.configure('ExecutionAction.TButton', background='#E1E9F1', foreground='#263746', bordercolor='#BBC9D6', lightcolor='#E1E9F1', darkcolor='#E1E9F1', font=small_font, padding=(10, 6), relief='flat', borderwidth=1)
        style.map('ExecutionAction.TButton', background=[('active', '#D2E0EC'), ('pressed', '#C3D4E3'), ('disabled', '#F1F3F5')], foreground=[('disabled', '#9A9A9A')], bordercolor=[('active', '#91ABC1'), ('pressed', '#7898B2'), ('disabled', '#E0E3E6')], lightcolor=[('active', '#D2E0EC'), ('pressed', '#C3D4E3')], darkcolor=[('active', '#D2E0EC'), ('pressed', '#C3D4E3')])
        style.configure('Action.TButton', background='#EAF2F9', foreground='#075A9C', bordercolor='#C9DCEB', lightcolor='#EAF2F9', darkcolor='#EAF2F9', font=small_font, padding=(10, 7), relief='flat')
        style.map('Action.TButton', background=[('active', '#DCEBF7'), ('pressed', '#CFE3F2'), ('disabled', '#F4F5F6')], foreground=[('disabled', '#A0A0A0')], bordercolor=[('active', '#9FC4DF'), ('focus', '#0078D4')])
        style.configure('Toolbar.TMenubutton', background='#FFFFFF', foreground='#1F1F1F', bordercolor='#D4D4D4', arrowcolor='#616161', font=small_font, padding=(9, 5), relief='flat')
        style.map('Toolbar.TMenubutton', background=[('active', '#E8E8E8'), ('pressed', '#DCDCDC')], bordercolor=[('active', '#B8B8B8')])
        style.configure('Treeview', background='#FFFFFF', fieldbackground='#FFFFFF', foreground='#3D3D3D', rowheight=28, font=font, borderwidth=1, bordercolor='#D4D4D4')
        style.map('Treeview', background=[('selected', '#CFE8FF')], foreground=[('selected', '#3D3D3D')])
        style.configure('Status.Treeview', background='#FFFFFF', fieldbackground='#FFFFFF', foreground='#3D3D3D', rowheight=28, font=font, borderwidth=0, relief='flat')
        style.map('Status.Treeview', background=[('selected', '#CFE8FF')], foreground=[('selected', '#3D3D3D')])
        # clam テーマは borderwidth がゼロでも Treeview.field の上側と左側に
        # 独自の立体枠を描くため、その要素を除去して外周の1ピクセル枠だけを表示する。
        style.layout('Status.Treeview', [
            ('Treeview.padding', {'sticky': 'nswe', 'children': [
                ('Treeview.treearea', {'sticky': 'nswe'})
            ]})
        ])
        style.configure('Treeview.Heading', background='#F3F3F3', foreground='#3B3B3B', font=(family, max(8, size - 1), 'bold'), padding=(6, 8), relief='flat', bordercolor='#D4D4D4')
        style.map('Treeview.Heading', background=[('active', '#E8E8E8'), ('pressed', '#DCDCDC')])
        style.configure('ExecutionTab.TButton', background='#E7EBF0', foreground='#3B3B3B', bordercolor='#E7EBF0', lightcolor='#E7EBF0', darkcolor='#E7EBF0', font=small_font, padding=(9, 4), relief='flat', borderwidth=0, focusthickness=0)
        style.map('ExecutionTab.TButton', background=[('active', '#DCE4EC'), ('pressed', '#CEDAE5')], bordercolor=[('active', '#DCE4EC')])
        style.configure('ExecutionTabSelected.TButton', background='#FFFFFF', foreground='#0078D4', bordercolor='#FFFFFF', lightcolor='#FFFFFF', darkcolor='#FFFFFF', font=small_font, padding=(9, 4), relief='flat', borderwidth=0, focusthickness=0)
        style.map('ExecutionTabSelected.TButton', background=[('active', '#FFFFFF'), ('pressed', '#FFFFFF')], foreground=[('active', '#0078D4')])
        style.configure('ExecutionTab.TFrame', background='#FFFFFF', relief='flat', borderwidth=0)
        style.configure('ExecutionTabHeader.TFrame', background='#FFFFFF')
        style.configure('ExecutionTab.TLabel', background='#FFFFFF', foreground='#616161', font=small_font)
        style.configure('AppShell.TFrame', background='#F6F7F9')
        style.configure('Sidebar.TFrame', background='#FFFFFF')
        style.configure('SidebarSection.TLabel', background='#FFFFFF', foreground='#7A7A7A', font=small_font, padding=(18, 12, 8, 4))
        style.configure('SidebarBrand.TLabel', background='#FFFFFF', foreground='#1F2937', font=(family, size + 2, 'bold'))
        style.configure('SidebarBrandSubtle.TLabel', background='#FFFFFF', foreground='#737373', font=(family, max(8, size - 1)))
        style.configure('Sidebar.TButton', background='#FFFFFF', foreground='#303030', bordercolor='#FFFFFF', lightcolor='#FFFFFF', darkcolor='#FFFFFF', font=font, padding=(18, 11), relief='flat', borderwidth=0, anchor='w', focusthickness=0)
        style.map('Sidebar.TButton', background=[('active', '#F0F4F8'), ('pressed', '#E7EEF5')], bordercolor=[('active', '#F0F4F8')])
        style.configure('SidebarSelected.TButton', background='#E8F2FC', foreground='#0067C0', bordercolor='#E8F2FC', lightcolor='#E8F2FC', darkcolor='#E8F2FC', font=bold_font, padding=(18, 11), relief='flat', borderwidth=0, anchor='w', focusthickness=0)
        style.map('SidebarSelected.TButton', background=[('active', '#DCECFB'), ('pressed', '#D1E6F8')], foreground=[('active', '#005A9E')])
        style.configure('PageTitle.TLabel', background='#FFFFFF', foreground='#161616', font=(family, size + 8, 'bold'))
        style.configure('PageSubtitle.TLabel', background='#FFFFFF', foreground='#616161', font=font)
        style.configure('Page.TFrame', background='#F6F7F9')
        style.configure('PageHeader.TFrame', background='#FFFFFF')
        style.configure('Summary.TFrame', background='#FFFFFF')
        style.configure('Summary.TLabel', background='#FFFFFF', foreground='#3B3B3B', font=font, padding=(18, 8))
        style.configure('CompactSummary.TLabel', background='#FFFFFF', foreground='#3B3B3B', font=font, padding=(10, 4))
        style.configure('ExecutionSurface.TFrame', background='#FFFFFF', relief='solid', borderwidth=1, bordercolor='#E1E5EA')
        style.configure(
            'Execution.Horizontal.TProgressbar',
            background='#0078D4',
            troughcolor='#E5EAF0',
            bordercolor='#E5EAF0',
            lightcolor='#0078D4',
            darkcolor='#0078D4',
            thickness=12,
        )
        style.configure('EmbeddedCard.TFrame', background='#FFFFFF', relief='solid', borderwidth=1, bordercolor='#E1E5EA')
        style.configure('EmbeddedCardBody.TFrame', background='#FFFFFF')
        style.configure('EmbeddedCard.TLabel', background='#FFFFFF', foreground='#242424', font=font)
        style.configure('EmbeddedCardSubtle.TLabel', background='#FFFFFF', foreground='#6B6B6B', font=small_font)
        style.configure('EmbeddedCardTitle.TLabel', background='#FFFFFF', foreground='#1F1F1F', font=(family, size + 3, 'bold'), padding=(0, 2, 0, 5))
        style.configure('LogArea.TFrame', background='#FFFFFF', relief='solid', borderwidth=1, bordercolor='#D4D4D4')
        style.configure('TPanedwindow', background='#D4D4D4', sashwidth=3)
        style.configure('TSeparator', background='#D4D4D4')
        style.configure('TEntry', fieldbackground='#FFFFFF', foreground='#1F1F1F', bordercolor='#CECECE', insertcolor='#1F1F1F')
        style.map('TEntry', fieldbackground=[('disabled', '#F3F3F3'), ('readonly', '#F8F8F8')], foreground=[('disabled', '#A0A0A0'), ('readonly', '#616161')], bordercolor=[('focus', '#0078D4'), ('disabled', '#E0E0E0')])
        style.configure('TCombobox', fieldbackground='#FFFFFF', background='#FFFFFF', foreground='#1F1F1F', arrowcolor='#616161', bordercolor='#CECECE')
        style.map('TCombobox', fieldbackground=[('disabled', '#F3F3F3'), ('readonly', '#FFFFFF')], foreground=[('disabled', '#A0A0A0'), ('readonly', '#1F1F1F')], bordercolor=[('focus', '#0078D4'), ('disabled', '#E0E0E0')], arrowcolor=[('disabled', '#A0A0A0')])
        style.configure('TSpinbox', fieldbackground='#FFFFFF', foreground='#1F1F1F', arrowcolor='#616161', bordercolor='#CECECE')
        style.map('TLabel', foreground=[('disabled', '#A0A0A0')])
        style.configure('TCheckbutton', background='#F3F3F3', foreground='#1F1F1F', font=font)
        style.map('TCheckbutton', background=[('active', '#F3F3F3')], foreground=[('disabled', '#A0A0A0')])
        for scrollbar_style in ('Vertical.TScrollbar', 'Horizontal.TScrollbar'):
            style.configure(scrollbar_style, background='#C8C8C8', troughcolor='#F3F3F3', bordercolor='#F3F3F3', arrowcolor='#616161')
            style.map(scrollbar_style, background=[('active', '#A6A6A6'), ('pressed', '#8C8C8C')])
        unchecked = tk.PhotoImage(width=16, height=16)
        checked = tk.PhotoImage(width=16, height=16)
        for image in (unchecked, checked):
            image.put('#F3F3F3', to=(0, 0, 16, 16))
            image.put('#FFFFFF', to=(2, 2, 14, 14))
            image.put('#767676', to=(1, 1, 15, 2))
            image.put('#767676', to=(1, 14, 15, 15))
            image.put('#767676', to=(1, 1, 2, 15))
            image.put('#767676', to=(14, 1, 15, 15))
        for x, y in ((4, 8), (5, 9), (6, 10), (7, 9), (8, 8), (9, 7), (10, 6), (11, 5), (12, 4)):
            checked.put('#0078D4', to=(x, y, x + 2, y + 2))
        self._check_images = (unchecked, checked)
        style.element_create('Tick.indicator', 'image', unchecked, ('selected', checked))
        style.layout('TCheckbutton', [('Checkbutton.padding', {'sticky': 'nswe', 'children': [('Tick.indicator', {'side': 'left', 'sticky': ''}), ('Checkbutton.label', {'side': 'left', 'sticky': 'nswe'})]})])
        self._apply_font_configuration()

    def _apply_font_configuration(self) -> None:
        """選択されたフォントを named font、ttk、直接指定の Tk 部品へ一括反映する。"""
        family, size = self.ui_font_family, self.ui_font_size
        base = (family, size)
        small = (family, max(8, size - 1))
        bold = (family, size, 'bold')
        for name in ('TkDefaultFont', 'TkTextFont', 'TkMenuFont', 'TkFixedFont', 'TkTooltipFont'):
            try:
                tkfont.nametofont(name).configure(family=family, size=size)
            except tk.TclError:
                pass
        try:
            tkfont.nametofont('TkHeadingFont').configure(family=family, size=max(8, size - 1), weight='bold')
        except tk.TclError:
            pass
        self.root.option_add('*Font', base)
        self.root.option_add('*TCombobox*Listbox.font', base)
        style = ttk.Style(self.root)
        style.configure('TLabel', font=base)
        style.configure('Section.TLabel', font=(family, size + 3, 'bold'))
        style.configure('DialogSection.TLabel', font=bold)
        style.configure('Subtle.TLabel', font=small)
        style.configure('DialogCard.TLabel', font=base)
        style.configure('MessageBody.TLabel', font=(family, size + 1))
        style.configure('DialogCardSection.TLabel', font=bold)
        style.configure('DialogCardSubtle.TLabel', font=small)
        style.configure('DialogCard.TCheckbutton', font=base)
        style.configure('InfoIcon.TLabel', font=(family, size + 5, 'bold'))
        style.configure('WarningIcon.TLabel', font=(family, size + 5, 'bold'))
        style.configure('ErrorIcon.TLabel', font=(family, size + 5, 'bold'))
        style.configure('DialogAction.TButton', font=small)
        style.configure('DialogInline.TButton', font=small)
        style.configure('Dialog.TEntry', font=base)
        style.configure('Dialog.TCombobox', font=base)
        style.configure('DialogPlaceholder.TLabel', font=base)
        style.configure('TEntry', font=base)
        style.configure('TCombobox', font=base)
        style.configure('TSpinbox', font=base)
        for button_style in ('TButton', 'Toolbar.TButton', 'ExecutionAction.TButton', 'Action.TButton', 'Secondary.TButton', 'DialogAction.TButton'):
            style.configure(button_style, font=small)
        style.configure('Toolbar.TMenubutton', font=small)
        style.configure('Primary.TButton', font=(family, max(8, size - 1), 'bold'))
        style.configure('MessagePrimary.TButton', font=(family, max(8, size - 1), 'bold'))
        style.configure('MessageSecondary.TButton', font=(family, max(8, size - 1), 'bold'))
        style.configure('Treeview', font=base, rowheight=max(26, size * 2 + 8))
        style.configure('Status.Treeview', font=base, rowheight=max(26, size * 2 + 8))
        style.configure('Treeview.Heading', font=(family, max(8, size - 1), 'bold'))
        style.configure('ExecutionTab.TButton', font=small)
        style.configure('ExecutionTabSelected.TButton', font=small)
        style.configure('ExecutionTab.TLabel', font=small)
        style.configure('SidebarSection.TLabel', font=small)
        style.configure('SidebarBrand.TLabel', font=(family, size + 2, 'bold'))
        style.configure('SidebarBrandSubtle.TLabel', font=(family, max(8, size - 1)))
        style.configure('Sidebar.TButton', font=base)
        style.configure('SidebarSelected.TButton', font=bold)
        style.configure('PageTitle.TLabel', font=(family, size + 8, 'bold'))
        style.configure('PageSubtitle.TLabel', font=base)
        style.configure('Summary.TLabel', font=base)
        style.configure('CompactSummary.TLabel', font=base)
        style.configure('EmbeddedCard.TLabel', font=base)
        style.configure('EmbeddedCardSubtle.TLabel', font=small)
        style.configure('EmbeddedCardTitle.TLabel', font=(family, size + 3, 'bold'))
        style.configure('TCheckbutton', font=base)
        if hasattr(self, 'run_button'):
            self.run_button.configure(font=bold)
            self.log_text.configure(font=small)
            for tag in ('loop_start', 'loop_end', 'retry_start', 'retry_end'):
                self.event_tree.tag_configure(tag, font=small)
            self.event_tree.tag_configure('event_group', font=(family, max(8, size - 1), 'bold'))
        self.root.update_idletasks()

    def _build_settings_state(self) -> None:
        installed = {name.casefold(): name for name in tkfont.families(self.root)}
        candidates = ('Segoe UI', 'Meiryo', 'Yu Gothic UI', 'Microsoft YaHei UI', 'Arial', 'Calibri', 'Tahoma', 'Verdana', 'Noto Sans CJK JP', 'Noto Sans CJK SC', 'Cascadia Code', 'Consolas')
        available = [installed[name.casefold()] for name in candidates if name.casefold() in installed]
        if self.ui_font_family.casefold() not in installed:
            self.ui_font_family = available[0] if available else 'TkDefaultFont'
            self.db.set_ui_font(self.ui_font_family, self.ui_font_size)
            self._apply_font_configuration()
        elif self.ui_font_family not in available:
            available.insert(0, installed[self.ui_font_family.casefold()])
        self.available_font_families = tuple(available)
        self.font_family_choice = tk.StringVar(value=self.ui_font_family)
        self.font_size_choice = tk.IntVar(value=self.ui_font_size)
        self.menu_language_choice = tk.StringVar(value=self.db.get_language())
        self.start_url_choice = tk.StringVar(value=str(self.settings['picker']['start_url']))
        self.default_timeout_choice = tk.StringVar(value=str(self.db.get_default_timeout_ms()))

    def _set_ui_font(self, family: str | None=None, size: int | None=None) -> None:
        self.ui_font_family = family or self.ui_font_family
        self.ui_font_size = size or self.ui_font_size
        self.font_family_choice.set(self.ui_font_family)
        self.font_size_choice.set(self.ui_font_size)
        self.db.set_ui_font(self.ui_font_family, self.ui_font_size)
        self._apply_font_configuration()

    def _reset_ui_font(self) -> None:
        installed = {name.casefold(): name for name in tkfont.families(self.root)}
        family = installed.get('yu gothic ui', next(iter(installed.values()), 'TkDefaultFont'))
        self._set_ui_font(family=family, size=10)

    def _set_language_from_menu(self, language: str) -> None:
        self.db.set_language(language)
        messagebox.showinfo('msg.0089', 'msg.0090')

    def _read_legacy_start_url(self) -> str:
        """旧 settings.json の開始 URL を初回だけ移行する。"""
        candidates = (self.project_dir / 'settings.json', self.resource_dir / 'settings.json')
        for path in dict.fromkeys(candidates):
            if not path.is_file():
                continue
            try:
                raw = json.loads(path.read_text(encoding='utf-8'))
                value = raw.get('picker', {}).get('start_url', '')
            except (OSError, ValueError, AttributeError):
                continue
            if isinstance(value, str) and value.strip().startswith(('http://', 'https://')):
                return value.strip()
        return DEFAULT_START_URL

    def _save_execution_settings(self) -> None:
        """ブラウザーと新規イベントの既定値を保存する。"""
        value = self.start_url_choice.get().strip()
        if not value.startswith(('http://', 'https://')):
            messagebox.showwarning('入力内容の確認', '開始 URL は http:// または https:// で始めてください。', parent=self.root)
            return
        try:
            default_timeout_ms = int(self.default_timeout_choice.get().strip())
            if not 1 <= default_timeout_ms <= 3600000:
                raise ValueError
        except ValueError:
            messagebox.showwarning('msg.0159', 'msg.0509', parent=self.root)
            return
        previous = str(self.settings['picker']['start_url'])
        self.db.set_start_url(value)
        self.db.set_default_timeout_ms(default_timeout_ms)
        self.db.set_browser_visible(self.browser_visible.get())
        self.settings['picker']['start_url'] = value
        self.debug_browser.start_url = value
        if hasattr(self, 'auth_view'):
            self.auth_view.start_url = value
            if not self.auth_view.url.get().strip() or self.auth_view.url.get().strip() == previous:
                self.auth_view.url.set(value)
        messagebox.showinfo('msg.0281', 'msg.0510', parent=self.root)

    def _build_ui(self) -> None:
        shell = ttk.Frame(self.root, style='AppShell.TFrame')
        shell.pack(fill='both', expand=True)
        sidebar = ttk.Frame(shell, width=224, style='Sidebar.TFrame')
        sidebar.pack(side='left', fill='y')
        sidebar.pack_propagate(False)
        ttk.Separator(shell, orient='vertical').pack(side='left', fill='y')
        content = ttk.Frame(shell, style='Page.TFrame')
        content.pack(side='left', fill='both', expand=True)

        self.page_frames: dict[str, ttk.Frame] = {}
        design_page = ttk.Frame(content, style='Page.TFrame')
        schema_page = ttk.Frame(content, style='Page.TFrame')
        data_page = ttk.Frame(content, style='Page.TFrame')
        auth_page = ttk.Frame(content, style='Page.TFrame')
        execution_page = ttk.Frame(content, style='Page.TFrame')
        settings_page = ttk.Frame(content, style='Page.TFrame')
        self.page_frames.update(
            design=design_page,
            schema=schema_page,
            data=data_page,
            auth=auth_page,
            execution=execution_page,
            settings=settings_page,
        )
        for page in self.page_frames.values():
            page.place(relx=0, rely=0, relwidth=1, relheight=1)

        self.sidebar_buttons: dict[str, ttk.Button] = {}
        self.sidebar_icons = self._create_sidebar_icons()
        brand = ttk.Frame(sidebar, style='Sidebar.TFrame')
        brand.pack(fill='x', padx=18, pady=(20, 12))
        ttk.Label(brand, text='msg.0002', style='SidebarBrand.TLabel').pack(anchor='w')
        ttk.Label(brand, text='msg.0525', style='SidebarBrandSubtle.TLabel').pack(anchor='w', pady=(2, 0))
        ttk.Separator(sidebar).pack(fill='x', padx=14, pady=(0, 4))
        ttk.Label(sidebar, text='msg.0526', style='SidebarSection.TLabel').pack(fill='x', pady=(4, 0))
        self._add_sidebar_button(sidebar, 'auth', 'msg.0460', lambda: self._show_page('auth'))
        ttk.Label(sidebar, text='msg.0527', style='SidebarSection.TLabel').pack(fill='x', pady=(12, 0))
        self._add_sidebar_button(sidebar, 'design', 'msg.0528', lambda: self._show_page('design'))
        ttk.Label(sidebar, text='msg.0529', style='SidebarSection.TLabel').pack(fill='x', pady=(12, 0))
        self._add_sidebar_button(sidebar, 'schema', 'msg.0530', lambda: self._show_page('schema'))
        self._add_sidebar_button(sidebar, 'data', 'msg.0019', lambda: self._show_page('data'))
        ttk.Label(sidebar, text='msg.0531', style='SidebarSection.TLabel').pack(fill='x', pady=(12, 0))
        self._add_sidebar_button(sidebar, 'execution', 'msg.0532', lambda: self._show_page('execution'))
        sidebar_spacer = ttk.Frame(sidebar, style='Sidebar.TFrame')
        sidebar_spacer.pack(fill='both', expand=True)
        ttk.Separator(sidebar).pack(fill='x', padx=14, pady=(0, 8))
        self._add_sidebar_button(sidebar, 'settings', 'msg.0533', lambda: self._show_page('settings'))

        self._build_embedded_page_header(
            design_page,
            'msg.0528',
            'msg.0534',
        )
        design_body = ttk.Frame(design_page, padding=(18, 12, 18, 18), style='Page.TFrame')
        design_body.pack(fill='both', expand=True)
        outer = ttk.Panedwindow(design_body, orient='horizontal')
        self.main_pane = outer
        outer.pack(fill='both', expand=True)
        left_shell = ttk.Frame(outer, style='EmbeddedCard.TFrame')
        right_shell = ttk.Frame(outer, style='EmbeddedCard.TFrame')
        left = ttk.Frame(left_shell, padding=(16, 14), style='EmbeddedCardBody.TFrame')
        right = ttk.Frame(right_shell, padding=(16, 14), style='EmbeddedCardBody.TFrame')
        left.pack(fill='both', expand=True, padx=1, pady=1)
        right.pack(fill='both', expand=True, padx=1, pady=1)
        outer.add(left_shell, weight=1)
        outer.add(right_shell, weight=4)
        self.root.after_idle(self._set_initial_pane_ratio)
        ttk.Label(left, text='msg.0003', style='EmbeddedCardTitle.TLabel').pack(anchor='w')
        ttk.Label(left, text='msg.0004', style='EmbeddedCardSubtle.TLabel').pack(anchor='w')
        # Treeview 自体の選択背景や自動スクロールバーに左右されず、
        # 表の四辺が常に連続して見える専用の外枠を使用する。
        workflow_table = tk.Frame(
            left,
            background='#D4D4D4',
            highlightbackground='#D4D4D4',
            highlightcolor='#D4D4D4',
            highlightthickness=1,
            bd=0,
        )
        workflow_table.pack(fill='both', expand=True, pady=6)
        self.workflow_tree = ttk.Treeview(workflow_table, columns=('position', 'name', 'enabled', 'pcl_start', 'guard'), show='headings', height=8)
        self.workflow_tree.heading('position', text='msg.0005')
        self.workflow_tree.heading('name', text='msg.0003')
        self.workflow_tree.heading('enabled', text='msg.0006')
        self.workflow_tree.heading('pcl_start', text='msg.0007')
        self.workflow_tree.heading('guard', text='msg.0405')
        self.workflow_tree.column('position', width=42, anchor='center', stretch=False)
        self.workflow_tree.column('name', width=150)
        self.workflow_tree.column('enabled', width=48, anchor='center', stretch=False)
        self.workflow_tree.column('pcl_start', width=65, anchor='center', stretch=False)
        self.workflow_tree.column('guard', width=130)
        workflow_y = AutoScrollbar(workflow_table, orient='vertical', command=self.workflow_tree.yview)
        workflow_x = AutoScrollbar(workflow_table, orient='horizontal', command=self.workflow_tree.xview)
        self.workflow_tree.configure(yscrollcommand=workflow_y.set, xscrollcommand=workflow_x.set)
        self.workflow_tree.grid(row=0, column=0, sticky='nsew')
        workflow_y.grid(row=0, column=1, sticky='ns')
        workflow_x.grid(row=1, column=0, sticky='ew')
        workflow_table.rowconfigure(0, weight=1)
        workflow_table.columnconfigure(0, weight=1)
        self.workflow_tree.tag_configure('odd', background='#FAFAFA')
        self.workflow_tree.tag_configure('disabled', foreground='#A0A0A0')
        self.workflow_tree.bind('<<TreeviewSelect>>', self._select_workflow)
        self.workflow_tree.bind('<Double-1>', self._workflow_double_click)
        self._bind_drag_sort(self.workflow_tree, 'workflow')
        workflow_buttons = ttk.Frame(left, style='EmbeddedCardBody.TFrame')
        workflow_buttons.pack(fill='x')
        ttk.Button(
            workflow_buttons, text='msg.0008', command=self._add_workflow,
            style='Action.TButton',
        ).grid(row=0, column=0, padx=3, pady=3, sticky='ew')
        ttk.Button(
            workflow_buttons, text='msg.0013', command=self._toggle_workflow,
            style='Secondary.TButton',
        ).grid(row=0, column=1, padx=3, pady=3, sticky='ew')
        ttk.Button(
            workflow_buttons, text='msg.0403', command=self._edit_workflow_guard,
            style='Secondary.TButton',
        ).grid(row=1, column=0, padx=3, pady=3, sticky='ew')
        ttk.Button(
            workflow_buttons, text='msg.0010', command=self._delete_workflow,
            style='Danger.TButton',
        ).grid(row=1, column=1, padx=3, pady=3, sticky='ew')
        for column in range(2):
            workflow_buttons.columnconfigure(column, weight=1, uniform='workflow_action')
        collection_buttons = ttk.Frame(left, style='EmbeddedCardBody.TFrame')
        collection_buttons.pack(fill='x', pady=(12, 0))
        ttk.Separator(collection_buttons).pack(fill='x', pady=(0, 8))
        ttk.Label(collection_buttons, text='msg.0015', style='EmbeddedCardSubtle.TLabel').pack(anchor='w', pady=(0, 5))
        ttk.Button(collection_buttons, text='msg.0016', command=self._import, style='Toolbar.TButton').pack(fill='x', pady=2)
        ttk.Button(collection_buttons, text='msg.0017', command=self._export, style='Toolbar.TButton').pack(fill='x', pady=2)
        header = ttk.Frame(right, style='EmbeddedCardBody.TFrame')
        header.pack(fill='x')
        self.title_label = ttk.Label(header, text='msg.0018', style='PageTitle.TLabel')
        self.title_label.pack(side='left')
        ttk.Separator(right).pack(fill='x', pady=(8, 4))
        event_tab = ttk.Frame(right, style='EmbeddedCardBody.TFrame')
        event_tab.pack(fill='both', expand=True)

        columns = ('action', 'value', 'guard', 'enabled', 'position')
        self.event_table = ttk.Frame(event_tab, style='EmbeddedCardBody.TFrame')
        self.event_table.pack(fill='both', expand=True, pady=(8, 4))
        event_tree_style = ttk.Style(self.root)
        event_tree_style.layout('Event.Treeview.Item', [
            ('Treeitem.padding', {'sticky': 'nswe', 'children': [
                ('Treeitem.indicator', {'side': 'left', 'sticky': ''}),
                ('Treeitem.image', {'side': 'left', 'sticky': ''}),
                ('Treeitem.text', {'sticky': 'nswe'})
            ]})
        ])
        self.event_tree = ttk.Treeview(self.event_table, columns=columns, show='tree headings', height=8, style='Event.Treeview')
        self.event_tree.heading('#0', text='msg.0028')
        self.event_tree.column('#0', width=170, minwidth=90, stretch=False)
        headings = {'position': 'msg.0027', 'action': 'msg.0029', 'value': 'msg.0032', 'guard': 'msg.0405', 'enabled': 'msg.0006'}
        widths = {'position': 48, 'action': 78, 'value': 130, 'guard': 240, 'enabled': 58}
        for column in columns:
            self.event_tree.heading(column, text=headings[column])
            self.event_tree.column(column, width=widths[column], minwidth=35, stretch=False)
        self.event_tree.column('guard', stretch=True)
        event_y = ttk.Scrollbar(self.event_table, orient='vertical', command=self.event_tree.yview)
        event_x = AutoScrollbar(self.event_table, orient='horizontal', command=self.event_tree.xview)
        self.event_tree.configure(yscrollcommand=event_y.set, xscrollcommand=event_x.set)
        self.event_tree.grid(row=0, column=0, sticky='nsew')
        event_y.grid(row=0, column=1, sticky='ns')
        event_x.grid(row=1, column=0, sticky='ew')
        self.event_table.rowconfigure(0, weight=1)
        self.event_table.columnconfigure(0, weight=1)
        self.event_tree.after_idle(lambda: event_x.set(*self.event_tree.xview()))
        self.event_tree.bind('<Configure>', self._resize_event_columns)
        self.event_tree.tag_configure('odd', background='#FAFAFA')
        self.event_tree.tag_configure('disabled', foreground='#A0A0A0')
        small_font = (self.ui_font_family, max(8, self.ui_font_size - 1))
        small_bold_font = (self.ui_font_family, max(8, self.ui_font_size - 1), 'bold')
        self.event_tree.tag_configure('loop_start', background='#F0F0F0', foreground='#525252', font=small_font)
        self.event_tree.tag_configure('loop_end', background='#FAFAFA', foreground='#737373', font=small_font)
        self.event_tree.tag_configure('retry_start', background='#EDEDED', foreground='#525252', font=small_font)
        self.event_tree.tag_configure('retry_end', background='#F7F7F7', foreground='#737373', font=small_font)
        self.event_tree.tag_configure('event_group', background='#F3F3F3', foreground='#3D3D3D', font=small_bold_font)
        self.event_tree.bind('<Double-1>', self._event_double_click)
        self._bind_drag_sort(self.event_tree, 'event')
        event_buttons = ttk.Frame(event_tab, style='EmbeddedCardBody.TFrame')
        event_buttons.pack(fill='x')
        event_actions = (
            ('msg.0034', self._add_event, 'Action.TButton'),
            ('msg.0413', self._add_event_group, 'Action.TButton'),
            ('msg.0035', self._edit_event, 'TButton'),
            ('msg.0011', lambda: self._move_event(-1), 'TButton'),
            ('msg.0012', lambda: self._move_event(1), 'TButton'),
            ('msg.0010', self._delete_event, 'Danger.TButton'),
        )
        for index, (text, command, button_style) in enumerate(event_actions):
            if button_style == 'TButton':
                button_style = 'Secondary.TButton'
            ttk.Button(event_buttons, text=text, command=command, style=button_style).grid(
                row=0, column=index, padx=3, pady=2, sticky='ew'
            )
            event_buttons.columnconfigure(index, weight=1, uniform='event_action')
        toggle_column = len(event_actions)
        tree_toggle_all_button(
            event_buttons, self.event_tree, style='Secondary.TButton',
        ).grid(row=0, column=toggle_column, padx=3, pady=2, sticky='ew')
        event_buttons.columnconfigure(toggle_column, weight=1, uniform='event_action')

        execution_header = ttk.Frame(execution_page, padding=(30, 14, 30, 12), style='PageHeader.TFrame')
        execution_header.pack(fill='x')
        execution_titles = ttk.Frame(execution_header, style='PageHeader.TFrame')
        execution_titles.pack(side='left', fill='x', expand=True)
        ttk.Label(execution_titles, text='msg.0532', style='PageTitle.TLabel').pack(anchor='w')
        ttk.Label(
            execution_titles,
            text='msg.0535',
            style='PageSubtitle.TLabel',
        ).pack(anchor='w', pady=(4, 0))
        self.run_button = tk.Button(
            execution_header,
            text='msg.0021',
            command=self._run_workflow,
            bg='#0078D4',
            fg='white',
            activebackground='#106EBE',
            activeforeground='white',
            disabledforeground='#F3F3F3',
            relief='flat',
            bd=0,
            font=(self.ui_font_family, self.ui_font_size, 'bold'),
            padx=18,
            pady=8,
            cursor='hand2',
        )
        self.run_button.pack(side='right')
        ttk.Separator(execution_page).pack(fill='x')

        summary = ttk.Frame(execution_page, padding=(22, 4), style='Summary.TFrame')
        summary.pack(fill='x')
        self.execution_status = ttk.Label(summary, text='msg.0026', style='CompactSummary.TLabel')
        self.execution_status.pack(side='left', fill='x', expand=True)
        ttk.Separator(summary, orient='vertical').pack(side='left', fill='y', pady=4)
        self.execution_target_summary = ttk.Label(summary, text='msg.0536', style='CompactSummary.TLabel')
        self.execution_target_summary.pack(side='left', fill='x', expand=True)
        ttk.Separator(summary, orient='vertical').pack(side='left', fill='y', pady=4)
        session_settings = ttk.Frame(summary, style='Summary.TFrame')
        session_settings.pack(side='left', fill='x', expand=True)
        ttk.Label(session_settings, text='msg.0287', style='CompactSummary.TLabel').pack(side='left')
        self.execution_session_limit = tk.StringVar(value=str(self.db.get_pcl_session_limit()))
        execution_session_spinbox = ttk.Spinbox(
            session_settings, from_=1, to=20, width=4,
            textvariable=self.execution_session_limit,
            command=self._save_execution_session_limit,
        )
        execution_session_spinbox.pack(side='left', padx=(6, 0))
        execution_session_spinbox.bind('<Return>', self._save_execution_session_limit)
        execution_session_spinbox.bind('<FocusOut>', self._save_execution_session_limit)
        ttk.Separator(execution_page).pack(fill='x')

        self.execution_panel = ttk.Frame(execution_page, padding=(22, 16, 22, 16), style='Page.TFrame')
        self.execution_panel.pack(fill='both', expand=True)
        execution_toolbar = ttk.Frame(self.execution_panel, style='Page.TFrame')
        execution_toolbar.pack(fill='x')
        execution_tab_bar = ttk.Frame(execution_toolbar, style='Page.TFrame')
        execution_tab_bar.pack(side='left')
        self.execution_tab_buttons: dict[str, ttk.Button] = {}
        for index, (name, text) in enumerate((('status', 'msg.0444'), ('log', 'msg.0036'))):
            button = ttk.Button(
                execution_tab_bar,
                text=text,
                command=lambda tab=name: self._select_execution_tab(tab),
                style='ExecutionTabSelected.TButton' if name == 'status' else 'ExecutionTab.TButton',
            )
            button.pack(side='left', padx=(2 if index else 0, 0))
            self.execution_tab_buttons[name] = button
        ttk.Button(
            execution_toolbar,
            text='msg.0537',
            command=self._clear_execution_results,
            style='ExecutionAction.TButton',
        ).pack(side='right')
        self.stop_button = ttk.Button(
            execution_toolbar,
            text='msg.0538',
            style='ExecutionAction.TButton',
            state='disabled',
        )
        self.stop_button.pack(side='right', padx=(0, 8))
        ttk.Button(
            execution_toolbar, text='msg.0292',
            command=self._set_execution_record_group, style='ExecutionAction.TButton',
        ).pack(side='right', padx=(0, 6))
        ttk.Button(
            execution_toolbar, text='msg.0291',
            command=self._toggle_execution_record, style='ExecutionAction.TButton',
        ).pack(side='right', padx=(0, 6))
        execution_content = ttk.Frame(self.execution_panel, style='ExecutionSurface.TFrame')
        execution_content.pack(fill='both', expand=True)
        execution_footer = ttk.Frame(self.execution_panel, padding=(14, 10), style='ExecutionSurface.TFrame')
        execution_footer.pack(fill='x', pady=(10, 0))
        self.execution_progress_text = ttk.Label(
            execution_footer,
            text='msg.0539',
            style='ExecutionTab.TLabel',
        )
        self.execution_progress_text.pack(side='left')
        self.execution_progress = ttk.Progressbar(
            execution_footer,
            mode='determinate',
            maximum=1,
            value=0,
            style='Execution.Horizontal.TProgressbar',
        )
        self.execution_progress.pack(side='left', fill='x', expand=True, padx=18)
        self.execution_progress_count = ttk.Label(
            execution_footer,
            text='0 / 0',
            style='ExecutionTab.TLabel',
        )
        self.execution_progress_count.pack(side='right')

        self.parallel_frame = ttk.Frame(execution_content, padding=(10, 8), style='ExecutionTab.TFrame')
        self.parallel_summary = ttk.Label(self.parallel_frame, text='msg.0451', style='ExecutionTab.TLabel')
        self.parallel_summary.grid(row=0, column=0, columnspan=2, sticky='w', pady=(0, 6))
        parallel_table_frame = tk.Frame(
            self.parallel_frame,
            bg='#D4D4D4',
            bd=0,
            highlightthickness=0,
        )
        parallel_table_frame.grid(row=1, column=0, columnspan=2, sticky='nsew')
        parallel_table_inner = tk.Frame(parallel_table_frame, bg='#FFFFFF', bd=0, highlightthickness=0)
        parallel_table_inner.grid(row=0, column=0, sticky='nsew', padx=1, pady=1)
        parallel_columns = ('group', 'enabled', 'data', 'summary', 'workflow', 'event', 'status')
        self.parallel_tree = ttk.Treeview(parallel_table_inner, columns=parallel_columns, show='headings', height=4, style='Status.Treeview')
        for column, heading, width, minimum in (
            ('group', 'msg.0522', 120, 105), ('enabled', 'msg.0523', 130, 115),
            ('data', 'msg.0446', 160, 130), ('summary', 'msg.0517', 150, 110),
            ('workflow', 'msg.0447', 170, 130), ('event', 'msg.0448', 220, 160),
            ('status', 'msg.0288', 120, 100),
        ):
            self.parallel_tree.heading(column, text=heading)
            self.parallel_tree.column(column, width=width, minwidth=minimum, stretch=False)
        self.parallel_scrollbar = AutoScrollbar(parallel_table_inner, orient='vertical', command=self.parallel_tree.yview)
        self.parallel_xscrollbar = AutoScrollbar(parallel_table_inner, orient='horizontal', command=self.parallel_tree.xview)
        self.parallel_tree.configure(
            yscrollcommand=self.parallel_scrollbar.set,
            xscrollcommand=self.parallel_xscrollbar.set,
        )
        self.parallel_tree.grid(row=0, column=0, sticky='nsew')
        self.parallel_scrollbar.grid(row=0, column=1, sticky='ns')
        self.parallel_xscrollbar.grid(row=1, column=0, sticky='ew')
        self.parallel_scrollbar.grid_remove()
        self.parallel_xscrollbar.grid_remove()
        self.parallel_tree.bind('<Configure>', self._resize_execution_status_columns)
        self.parallel_tree.bind('<Double-1>', self._execution_record_double_click)
        self.parallel_tree.bind('<Motion>', self._execution_record_motion)
        self.parallel_tree.bind('<Leave>', lambda _event: self.parallel_tree.configure(cursor=''))
        parallel_table_inner.columnconfigure(0, weight=1)
        parallel_table_inner.rowconfigure(0, weight=1)
        parallel_table_frame.columnconfigure(0, weight=1)
        parallel_table_frame.rowconfigure(0, weight=1)
        self.parallel_frame.columnconfigure(0, weight=1)
        self.parallel_frame.rowconfigure(1, weight=1)

        log_tab = ttk.Frame(execution_content, padding=(10, 8), style='ExecutionTab.TFrame')
        log_header = ttk.Frame(log_tab, style='ExecutionTabHeader.TFrame')
        log_header.pack(fill='x', pady=(0, 6))
        ttk.Button(log_header, text='msg.0435', command=self._copy_log, style='Toolbar.TButton', width=8).pack(side='right', padx=(4, 0))
        ttk.Button(log_header, text='msg.0434', command=self._clear_log, style='Toolbar.TButton', width=8).pack(side='right')
        self.log_frame = tk.Frame(
            log_tab,
            bg='#D4D4D4',
            bd=0,
            highlightthickness=0,
        )
        self.log_frame.pack(fill='both', expand=True)
        log_inner = tk.Frame(self.log_frame, bg='#FFFFFF', bd=0, highlightthickness=0)
        log_inner.grid(row=0, column=0, sticky='nsew', padx=1, pady=1)
        self.log_text = tk.Text(log_inner, height=5, state='disabled', wrap='none', bg='#FFFFFF', fg='#1F1F1F', insertbackground='#1F1F1F', selectbackground='#ADD6FF', relief='flat', bd=0, padx=10, pady=8, font=(self.ui_font_family, max(8, self.ui_font_size - 1)))
        log_y = AutoScrollbar(log_inner, orient='vertical', command=self.log_text.yview)
        log_x = AutoScrollbar(log_inner, orient='horizontal', command=self.log_text.xview)
        self.log_text.configure(yscrollcommand=log_y.set, xscrollcommand=log_x.set)
        self.log_text.grid(row=0, column=0, sticky='nsew')
        log_y.grid(row=0, column=1, sticky='ns')
        log_x.grid(row=1, column=0, sticky='ew')
        log_inner.columnconfigure(0, weight=1)
        log_inner.rowconfigure(0, weight=1)
        self.log_frame.columnconfigure(0, weight=1)
        self.log_frame.rowconfigure(0, weight=1)
        self.execution_tabs = {'status': self.parallel_frame, 'log': log_tab}

        self._build_embedded_page_header(
            schema_page,
            'msg.0530',
            'msg.0540',
        )
        schema_host = ttk.Frame(schema_page, padding=(22, 8, 22, 20), style='Page.TFrame')
        schema_host.pack(fill='both', expand=True)
        self.schema_view = SchemaDesignerDialog(
            schema_host,
            self.db,
            0,
            'msg.0088',
            embedded=True,
        )
        ttk.Frame.pack(self.schema_view, fill='both', expand=True)

        self._build_embedded_page_header(
            data_page,
            'msg.0019',
            'msg.0541',
        )
        data_host = ttk.Frame(data_page, padding=(22, 8, 22, 20), style='Page.TFrame')
        data_host.pack(fill='both', expand=True)
        self.data_view = HierarchicalDataDialog(
            data_host,
            self.db,
            0,
            'msg.0088',
            embedded=True,
        )
        ttk.Frame.pack(self.data_view, fill='both', expand=True)

        self._build_embedded_page_header(
            auth_page,
            'msg.0460',
            'msg.0542',
        )
        auth_host = ttk.Frame(auth_page, padding=(22, 8, 22, 20), style='Page.TFrame')
        auth_host.pack(fill='both', expand=True)
        self.auth_view = AuthStateDialog(
            auth_host,
            self.project_dir,
            self.auth_browser,
            self.db.get_auth_profile(),
            self._set_auth_profile,
            self.settings['picker']['start_url'],
            embedded=True,
        )
        ttk.Frame.pack(self.auth_view, fill='both', expand=True)

        self._build_settings_page(settings_page)
        self._select_execution_tab('status')
        self._refresh_execution_overview()
        self._refresh_execution_plan_preview()
        self._show_page('design')

    def _add_sidebar_button(
            self,
            parent: ttk.Frame,
            key: str,
            text: str,
            command: Callable[[], object],
    ) -> None:
        """サイドバー項目を同じ見た目と余白で追加する。"""
        def invoke() -> None:
            command()
            self.root.after_idle(self.root.focus_set)

        button = ttk.Button(
            parent,
            text=text,
            image=self.sidebar_icons[key]['normal'],
            compound='left',
            command=invoke,
            style='Sidebar.TButton',
            takefocus=False,
        )
        button.pack(fill='x', padx=12, pady=1)
        self.sidebar_buttons[key] = button

    def _create_sidebar_icons(self) -> dict[str, dict[str, tk.PhotoImage]]:
        """サイドバー用の軽量な線画アイコンを通常色と選択色で生成する。"""
        icon_builders = {
            'auth': self._draw_auth_icon,
            'design': self._draw_flow_icon,
            'schema': self._draw_database_icon,
            'data': self._draw_table_icon,
            'execution': self._draw_execution_icon,
            'settings': self._draw_settings_icon,
        }
        colors = {'normal': '#4B5563', 'selected': '#0067C0'}
        icons: dict[str, dict[str, tk.PhotoImage]] = {}
        for key, builder in icon_builders.items():
            icons[key] = {}
            for state, color in colors.items():
                image = tk.PhotoImage(master=self.root, width=20, height=20)
                builder(image, color)
                icons[key][state] = image
        return icons

    @staticmethod
    def _icon_pixel(image: tk.PhotoImage, color: str, x: int, y: int, size: int = 1) -> None:
        """画像範囲内にだけアイコンの画素を描画する。"""
        if 0 <= x < 20 and 0 <= y < 20:
            image.put(color, to=(x, y, min(20, x + size), min(20, y + size)))

    @classmethod
    def _icon_line(
            cls,
            image: tk.PhotoImage,
            color: str,
            start: tuple[int, int],
            end: tuple[int, int],
            width: int = 2,
    ) -> None:
        """整数座標の線分を描画し、外部画像ライブラリへの依存を避ける。"""
        x1, y1 = start
        x2, y2 = end
        dx = abs(x2 - x1)
        sx = 1 if x1 < x2 else -1
        dy = -abs(y2 - y1)
        sy = 1 if y1 < y2 else -1
        error = dx + dy
        while True:
            cls._icon_pixel(image, color, x1, y1, width)
            if x1 == x2 and y1 == y2:
                break
            doubled = error * 2
            if doubled >= dy:
                error += dy
                x1 += sx
            if doubled <= dx:
                error += dx
                y1 += sy

    @classmethod
    def _icon_rectangle(
            cls,
            image: tk.PhotoImage,
            color: str,
            left: int,
            top: int,
            right: int,
            bottom: int,
    ) -> None:
        cls._icon_line(image, color, (left, top), (right, top))
        cls._icon_line(image, color, (right, top), (right, bottom))
        cls._icon_line(image, color, (right, bottom), (left, bottom))
        cls._icon_line(image, color, (left, bottom), (left, top))

    @classmethod
    def _draw_auth_icon(cls, image: tk.PhotoImage, color: str) -> None:
        for x, y in ((8, 3), (9, 2), (10, 2), (11, 3), (12, 4), (12, 5),
                     (11, 6), (10, 7), (9, 7), (8, 6), (7, 5), (7, 4)):
            cls._icon_pixel(image, color, x, y, 2)
        cls._icon_line(image, color, (5, 16), (6, 12))
        cls._icon_line(image, color, (6, 12), (9, 10))
        cls._icon_line(image, color, (9, 10), (12, 10))
        cls._icon_line(image, color, (12, 10), (15, 12))
        cls._icon_line(image, color, (15, 12), (16, 16))
        cls._icon_line(image, color, (5, 16), (16, 16))

    @classmethod
    def _draw_flow_icon(cls, image: tk.PhotoImage, color: str) -> None:
        for left, top, right, bottom in ((7, 2, 12, 6), (2, 13, 7, 17), (13, 13, 18, 17)):
            cls._icon_rectangle(image, color, left, top, right, bottom)
        cls._icon_line(image, color, (10, 7), (10, 10))
        cls._icon_line(image, color, (5, 10), (15, 10))
        cls._icon_line(image, color, (5, 10), (5, 12))
        cls._icon_line(image, color, (15, 10), (15, 12))

    @classmethod
    def _draw_database_icon(cls, image: tk.PhotoImage, color: str) -> None:
        cls._icon_line(image, color, (4, 4), (6, 2))
        cls._icon_line(image, color, (6, 2), (14, 2))
        cls._icon_line(image, color, (14, 2), (16, 4))
        cls._icon_line(image, color, (16, 4), (14, 6))
        cls._icon_line(image, color, (14, 6), (6, 6))
        cls._icon_line(image, color, (6, 6), (4, 4))
        cls._icon_line(image, color, (4, 4), (4, 15))
        cls._icon_line(image, color, (16, 4), (16, 15))
        cls._icon_line(image, color, (4, 10), (6, 12))
        cls._icon_line(image, color, (6, 12), (14, 12))
        cls._icon_line(image, color, (14, 12), (16, 10))
        cls._icon_line(image, color, (4, 15), (6, 17))
        cls._icon_line(image, color, (6, 17), (14, 17))
        cls._icon_line(image, color, (14, 17), (16, 15))

    @classmethod
    def _draw_table_icon(cls, image: tk.PhotoImage, color: str) -> None:
        cls._icon_rectangle(image, color, 2, 3, 18, 17)
        cls._icon_line(image, color, (2, 8), (18, 8))
        cls._icon_line(image, color, (2, 13), (18, 13))
        cls._icon_line(image, color, (8, 3), (8, 17))
        cls._icon_line(image, color, (13, 3), (13, 17))

    @classmethod
    def _draw_execution_icon(cls, image: tk.PhotoImage, color: str) -> None:
        cls._icon_rectangle(image, color, 2, 2, 18, 18)
        for left, right, y in ((7, 8, 6), (7, 11, 7), (7, 13, 8), (7, 15, 9),
                               (7, 15, 10), (7, 13, 11), (7, 11, 12), (7, 8, 13)):
            image.put(color, to=(left, y, right, y + 1))

    @classmethod
    def _draw_settings_icon(cls, image: tk.PhotoImage, color: str) -> None:
        # 小さい歯車は中心円と八方向のスポークで明瞭に見せる。
        for x, y in ((8, 7), (9, 6), (10, 6), (11, 7), (12, 8), (12, 10),
                     (11, 12), (9, 12), (7, 11), (6, 9), (7, 8)):
            cls._icon_pixel(image, color, x, y, 2)
        for start, end in (
            ((10, 1), (10, 5)), ((10, 14), (10, 18)),
            ((1, 10), (5, 10)), ((14, 10), (18, 10)),
            ((3, 3), (6, 6)), ((14, 14), (17, 17)),
            ((3, 17), (6, 14)), ((14, 6), (17, 3)),
        ):
            cls._icon_line(image, color, start, end)
        image.put('#FFFFFF', to=(9, 9, 12, 12))

    @staticmethod
    def _build_embedded_page_header(parent: ttk.Frame, title: str, subtitle: str) -> None:
        """埋め込み画面に共通のタイトル領域を追加する。"""
        header = ttk.Frame(parent, padding=(30, 24, 30, 16), style='PageHeader.TFrame')
        header.pack(fill='x')
        ttk.Label(header, text=title, style='PageTitle.TLabel').pack(anchor='w')
        ttk.Label(header, text=subtitle, style='PageSubtitle.TLabel').pack(anchor='w', pady=(4, 0))
        ttk.Separator(parent).pack(fill='x')

    def _show_page(self, page_name: str) -> None:
        """指定した主画面を表示し、選択中のメニューを強調する。"""
        page = self.page_frames.get(page_name)
        if page is None:
            return
        if page_name == self.current_page_name:
            return
        if self.current_page_name == 'schema' and not self.schema_view.confirm_pending_changes():
            return
        if self.current_page_name == 'data' and not self.data_view.confirm_pending_changes():
            return
        page.lift()
        self.current_page_name = page_name
        selected_key = page_name
        for key, button in self.sidebar_buttons.items():
            selected = key == selected_key
            button.configure(
                style='SidebarSelected.TButton' if selected else 'Sidebar.TButton',
                image=self.sidebar_icons[key]['selected' if selected else 'normal'],
            )
        if page_name == 'execution':
            self._refresh_execution_overview()
            self._refresh_execution_plan_preview()
            self.root.after_idle(self._update_execution_status_scrollbar)
        elif page_name == 'data':
            self.data_view.reload_from_database()
        elif page_name == 'auth':
            try:
                self.debug_browser.close_browser()
            except Exception:
                pass
            self.auth_view._refresh_profiles(self.db.get_auth_profile())

    def _build_settings_page(self, page: ttk.Frame) -> None:
        """外観、言語、実行時ブラウザーの設定画面を構築する。"""
        self._build_embedded_page_header(
            page,
            'msg.0533',
            'msg.0543',
        )
        body = ttk.Frame(page, padding=(30, 22), style='Page.TFrame')
        body.pack(fill='both', expand=True)
        body.columnconfigure(0, weight=1, uniform='settings_card')
        body.columnconfigure(1, weight=1, uniform='settings_card')

        display_card = ttk.Frame(body, padding=(24, 20), style='ExecutionSurface.TFrame')
        display_card.grid(row=0, column=0, sticky='nsew', padx=(0, 10))
        display_card.columnconfigure(1, weight=1)
        ttk.Label(display_card, text='msg.0544', style='DialogCardSection.TLabel').grid(
            row=0,
            column=0,
            columnspan=2,
            sticky='w',
        )
        ttk.Label(
            display_card,
            text='msg.0545',
            style='DialogCardSubtle.TLabel',
        ).grid(
            row=1,
            column=0,
            columnspan=2,
            sticky='w',
            pady=(2, 16),
        )
        ttk.Label(display_card, text='msg.0546', width=14, anchor='w', style='DialogCard.TLabel').grid(row=2, column=0, sticky='w', pady=7)
        font_box = ttk.Combobox(
            display_card,
            textvariable=self.font_family_choice,
            values=self.available_font_families,
            state='readonly',
            width=30,
        )
        font_box.grid(row=2, column=1, sticky='w', pady=7)
        font_box.bind(
            '<<ComboboxSelected>>',
            lambda _event: self._set_ui_font(family=self.font_family_choice.get()),
        )
        ttk.Label(display_card, text='msg.0547', width=14, anchor='w', style='DialogCard.TLabel').grid(row=3, column=0, sticky='w', pady=7)
        size_box = ttk.Combobox(
            display_card,
            textvariable=self.font_size_choice,
            values=(8, 9, 10, 11, 12, 14, 16, 18),
            state='readonly',
            width=10,
        )
        size_box.grid(row=3, column=1, sticky='w', pady=7)
        size_box.bind(
            '<<ComboboxSelected>>',
            lambda _event: self._set_ui_font(size=int(self.font_size_choice.get())),
        )
        ttk.Label(display_card, text='msg.0548', width=14, anchor='w', style='DialogCard.TLabel').grid(row=4, column=0, sticky='w', pady=7)
        language_box = ttk.Combobox(
            display_card,
            textvariable=self.menu_language_choice,
            values=('ja', 'zh'),
            state='readonly',
            width=10,
        )
        language_box.grid(row=4, column=1, sticky='w', pady=7)
        language_box.bind(
            '<<ComboboxSelected>>',
            lambda _event: self._set_language_from_menu(self.menu_language_choice.get()),
        )
        display_actions = ttk.Frame(display_card, style='DialogCardBody.TFrame')
        display_actions.grid(row=5, column=0, columnspan=2, sticky='w', pady=(18, 0))
        ttk.Button(
            display_actions,
            text='msg.0549',
            command=self._reset_ui_font,
            style='Secondary.TButton',
        ).pack(side='left')
        ttk.Button(
            display_actions,
            text='msg.0550',
            command=lambda: self.root.geometry('1380x820'),
            style='Secondary.TButton',
        ).pack(side='left', padx=(8, 0))

        browser_card = ttk.Frame(body, padding=(24, 20), style='ExecutionSurface.TFrame')
        browser_card.grid(row=0, column=1, sticky='nsew', padx=(10, 0))
        browser_card.columnconfigure(1, weight=1)
        ttk.Label(browser_card, text='msg.0551', style='DialogCardSection.TLabel').grid(
            row=0, column=0, columnspan=3, sticky='w'
        )
        ttk.Label(
            browser_card,
            text='msg.0552',
            style='DialogCardSubtle.TLabel',
            wraplength=520,
            justify='left',
        ).grid(row=1, column=0, columnspan=3, sticky='w', pady=(2, 16))
        ttk.Label(browser_card, text='msg.0553', width=18, anchor='w', style='DialogCard.TLabel').grid(
            row=2, column=0, sticky='w', pady=7
        )
        ttk.Entry(browser_card, textvariable=self.start_url_choice).grid(
            row=2, column=1, columnspan=2, sticky='ew', pady=7
        )
        ttk.Label(browser_card, text='msg.0506', width=18, anchor='w', style='DialogCard.TLabel').grid(
            row=3, column=0, sticky='w', pady=7
        )
        ttk.Spinbox(
            browser_card,
            textvariable=self.default_timeout_choice,
            from_=1,
            to=3600000,
            increment=1000,
            width=14,
        ).grid(row=3, column=1, sticky='w', pady=7)
        ttk.Label(browser_card, text='msg.0507', style='DialogCardSubtle.TLabel').grid(
            row=3, column=2, sticky='w', padx=(10, 0), pady=7
        )
        ttk.Checkbutton(
            browser_card,
            text='msg.0554',
            variable=self.browser_visible,
            style='DialogCard.TCheckbutton',
        ).grid(row=4, column=0, columnspan=3, sticky='w', pady=(12, 4))
        ttk.Label(
            browser_card,
            text='msg.0555',
            style='DialogCardSubtle.TLabel',
            wraplength=520,
            justify='left',
        ).grid(row=5, column=0, columnspan=3, sticky='w', pady=(0, 14))
        ttk.Button(
            browser_card,
            text='msg.0508',
            command=self._save_execution_settings,
            style='Primary.TButton',
        ).grid(row=6, column=0, columnspan=3, sticky='e')

    def _refresh_execution_overview(self) -> None:
        """実行画面上部の対象件数と Session 数を最新化する。"""
        records = [row for row in self.db.list_data_records() if row['enabled']]
        self.execution_target_summary.configure(text=f'msg.0556{len(records)}msg.0557')
        self.execution_session_limit.set(str(self.db.get_pcl_session_limit()))

    def _save_execution_session_limit(self, _event: object=None) -> None:
        try:
            limit = int(self.execution_session_limit.get())
            self.db.set_pcl_session_limit(limit)
        except (TypeError, ValueError):
            self.execution_session_limit.set(str(self.db.get_pcl_session_limit()))

    @staticmethod
    def _execution_group_sort_key(value: object) -> tuple[tuple[int, object], ...]:
        """実行グループを数値部分優先の自然順で比較できる形にする。"""
        return tuple(
            (0, int(part)) if part.isdigit() else (1, part.casefold())
            for part in re.split(r'(\d+)', str(value))
            if part
        )

    def _refresh_execution_plan_preview(self) -> None:
        """未実行時はデータ管理の現在の実行設定を一覧表示する。"""
        if self.running or self.execution_task_states:
            return
        records = sorted(
            (dict(row) for row in self.db.list_data_records()),
            key=lambda record: (
                self._execution_group_sort_key(record['execution_group']),
                int(record['id']),
            ),
        )
        enabled_count = sum(bool(record['enabled']) for record in records)
        skipped_count = len(records) - enabled_count
        group_totals: dict[str, int] = {}
        for record in records:
            group = str(record['execution_group'])
            group_totals[group] = group_totals.get(group, 0) + 1
        group_positions: dict[str, int] = {}
        self.parallel_tree.delete(*self.parallel_tree.get_children())
        self.parallel_tree.tag_configure('preview_skipped', foreground='#999999')
        for record in records:
            enabled = bool(record['enabled'])
            group = str(record['execution_group'])
            group_positions[group] = group_positions.get(group, 0) + 1
            self.parallel_tree.insert(
                '',
                'end',
                iid=f'preview:{record["id"]}',
                values=(
                    group,
                    tr('msg.0248') if enabled else tr('msg.0309'),
                    f'({group_positions[group]}/{group_totals[group]})  {record["name"]}',
                    record['summary'],
                    '-',
                    '-',
                    tr('msg.0513') if enabled else tr('msg.0309'),
                ),
                tags=() if enabled else ('preview_skipped',),
            )
        self.parallel_summary.configure(
            text=f'{tr("msg.0513")}: {enabled_count} / {tr("msg.0309")}: {skipped_count}'
        )
        self.execution_progress.configure(maximum=max(1, enabled_count), value=0)
        self.execution_progress_text.configure(text='msg.0539')
        self.execution_progress_count.configure(text=f'0 / {enabled_count}')
        if getattr(self, 'selected_execution_tab', None) == 'status':
            self.root.after_idle(self._update_execution_status_scrollbar)

    def _selected_execution_record_id(self) -> int | None:
        selection = self.parallel_tree.selection()
        if self.running or not selection or not selection[0].startswith('preview:'):
            return None
        return int(selection[0].split(':', 1)[1])

    def _toggle_execution_record(self) -> None:
        record_id = self._selected_execution_record_id()
        if record_id is None:
            messagebox.showinfo('msg.0310', 'msg.0311', parent=self.root)
            return
        record = next(row for row in self.db.list_data_records() if row['id'] == record_id)
        self.db.set_data_record_enabled(record_id, not record['enabled'])
        self._refresh_execution_overview()
        self._refresh_execution_plan_preview()
        self.parallel_tree.selection_set(f'preview:{record_id}')

    def _set_execution_record_group(self) -> None:
        record_id = self._selected_execution_record_id()
        if record_id is None:
            messagebox.showinfo('msg.0310', 'msg.0312', parent=self.root)
            return
        record = next(row for row in self.db.list_data_records() if row['id'] == record_id)
        group = simpledialog.askstring(
            'msg.0313', 'msg.0314', initialvalue=record['execution_group'], parent=self.root,
        )
        if group is None:
            return
        try:
            self.db.set_data_record_group(record_id, group)
        except ValueError as error:
            messagebox.showerror('msg.0315', str(error), parent=self.root)
            return
        self._refresh_execution_plan_preview()
        self.parallel_tree.selection_set(f'preview:{record_id}')

    def _execution_record_double_click(self, event: tk.Event) -> str | None:
        if self.parallel_tree.identify_region(event.x, event.y) != 'cell':
            return None
        item = self.parallel_tree.identify_row(event.y)
        if not item.startswith('preview:'):
            return None
        self.parallel_tree.selection_set(item)
        self.parallel_tree.focus(item)
        column = self.parallel_tree.identify_column(event.x)
        if column == '#1':
            self._set_execution_record_group()
        elif column == '#2':
            self._toggle_execution_record()
        return 'break'

    def _execution_record_motion(self, event: tk.Event) -> None:
        """プレビューのグループ／実行列をダブルクリックで編集できることを示す。"""
        editable = (
            not self.running
            and self.parallel_tree.identify_region(event.x, event.y) == 'cell'
            and self.parallel_tree.identify_row(event.y).startswith('preview:')
            and self.parallel_tree.identify_column(event.x) in {'#1', '#2'}
        )
        self.parallel_tree.configure(cursor='hand2' if editable else '')

    def _clear_execution_results(self) -> None:
        """画面上の実行結果とログを初期状態へ戻す。"""
        if self.running or self.execution_starting:
            return
        self.executing_tasks.clear()
        self.execution_task_states.clear()
        self._clear_log()
        self._refresh_execution_indicators()
        self.execution_status.configure(text='msg.0026')
        self._refresh_execution_overview()
        self._refresh_execution_plan_preview()

    def _set_initial_pane_ratio(self) -> None:
        if self.main_pane.winfo_exists() and self.main_pane.winfo_width() > 1:
            self.main_pane.sashpos(0, int(self.main_pane.winfo_width() * 0.31))

    def _resize_event_columns(self, event: tk.Event) -> None:
        # Treeview の実クライアント幅を使用する。スクロールバー領域を固定確保すると、
        # AutoScrollbar が非表示になった際に空白が残るためである。
        # Treeview 内部の境界分を残し、列幅の丸めだけで横方向が溢れないようにする。
        available = max(420, event.width - 10)
        fixed = {'position': 52, 'action': 90, 'enabled': 62}
        flexible = available - sum(fixed.values())
        widths = {
            **fixed,
            'name': int(flexible * 0.34),
            'value': int(flexible * 0.18),
            'guard': int(flexible * 0.48),
        }
        for column, width in widths.items():
            target = '#0' if column == 'name' else column
            minimum = 180 if target == '#0' else 45
            self.event_tree.column(target, width=max(minimum, width))

    def _resize_execution_status_columns(self, event: tk.Event) -> None:
        available = max(980, event.width - 3)
        ratios = {
            'group': 0.10, 'enabled': 0.11, 'data': 0.15, 'summary': 0.14,
            'workflow': 0.16, 'event': 0.23, 'status': 0.11,
        }
        minimums = {
            'group': 105, 'enabled': 115, 'data': 130, 'summary': 110,
            'workflow': 130, 'event': 160, 'status': 100,
        }
        for column, ratio in ratios.items():
            self.parallel_tree.column(
                column,
                width=max(minimums[column], int(available * ratio)),
                stretch=False,
            )

    def _select_execution_tab(self, selected: str) -> None:
        self.selected_execution_tab = selected
        for name, frame in self.execution_tabs.items():
            frame.pack_forget()
            self.execution_tab_buttons[name].configure(
                style='ExecutionTabSelected.TButton' if name == selected else 'ExecutionTab.TButton'
            )
        self.execution_tabs[selected].pack(fill='both', expand=True)
        if selected == 'status':
            self.root.after_idle(self._update_execution_status_scrollbar)

    def _update_execution_status_scrollbar(self) -> None:
        if getattr(self, 'selected_execution_tab', None) != 'status' or not self.parallel_tree.winfo_ismapped():
            return
        self.parallel_tree.update_idletasks()
        first, last = self.parallel_tree.yview()
        if first <= 0.0 and last >= 0.999:
            self.parallel_scrollbar.grid_remove()
        else:
            self.parallel_scrollbar.grid()

    def _clear_log(self) -> None:
        self.log_text.configure(state='normal')
        self.log_text.delete('1.0', 'end')
        self.log_text.configure(state='disabled')

    def _copy_log(self) -> None:
        content = self.log_text.get('1.0', 'end-1c')
        if content:
            self.root.clipboard_clear()
            self.root.clipboard_append(content)

    def _refresh_workflows(self, select_id: int | None=None) -> None:
        self.workflow_tree.delete(*self.workflow_tree.get_children())
        for row_index, row in enumerate(self.db.list_workflows()):
            tags = []
            if row_index % 2:
                tags.append('odd')
            if not row['enabled']:
                tags.append('disabled')
            guard_summary = summarize_guard(decode_guard(row['guard_json']), guard_operator_labels()) or tr('msg.0404')
            item = self.workflow_tree.insert('', 'end', iid=str(row['id']), values=(row['position'], row['name'], 'msg.0037' if row['enabled'] else 'msg.0038', 'msg.0039' if row['pcl_loop_start'] else '', guard_summary), tags=tuple(tags))
            if select_id == row['id']:
                self.workflow_tree.selection_set(item)
                self.workflow_tree.focus(item)
        if select_id:
            self._load_events(select_id)

    def _select_workflow(self, _event: object=None) -> None:
        selection = self.workflow_tree.selection()
        if selection:
            self._load_events(int(selection[0]))

    def _load_events(self, workflow_id: int) -> None:
        self.current_workflow_id = workflow_id
        name = self.workflow_tree.item(str(workflow_id), 'values')[1]
        self.title_label.config(text=name)
        open_states: dict[str, bool] = {}
        existing = list(self.event_tree.get_children())
        while existing:
            item = existing.pop()
            if 'event_group' in self.event_tree.item(item, 'tags'):
                open_states[item] = bool(self.event_tree.item(item, 'open'))
            existing.extend(self.event_tree.get_children(item))
        self.event_tree.delete(*self.event_tree.get_children())
        parents: list[str] = []
        for row_index, row in enumerate(self.db.list_events(workflow_id)):
            if row['action'] in ('loop_end', 'retry_end', 'group_end'):
                if parents:
                    parents.pop()
                continue
            is_group = row['action'] in ('loop_start', 'retry_start', 'group_start')
            if row['action'] == 'group_start':
                modes = ('↻' if row['data_path'] else '') + ('⟳' if str(row['value']).strip() else '')
                marker = f'{modes} ' if modes else ''
            else:
                marker = '↻ ' if row['action'] == 'loop_start' else '⟳ ' if row['action'] == 'retry_start' else ''
            clean_name = str(row['name']).lstrip('○◯●⟳↻ ')
            group_open = open_states.get(str(row['id']), True)
            display_name = f'{marker}{clean_name}'
            guard_summary = summarize_guard(decode_guard(row['guard_json']), guard_operator_labels()) or tr('msg.0404')
            parent = parents[-1] if parents else ''
            display_action = 'group' if row['action'] == 'group_start' else 'loop' if row['action'] == 'loop_start' else 'retry' if row['action'] == 'retry_start' else row['action']
            self.event_tree.insert(parent, 'end', iid=str(row['id']), text=display_name, open=group_open if is_group else False, values=(display_action, row['value'], guard_summary, 'msg.0037' if row['enabled'] else 'msg.0038', row['position']), tags=tuple((tag for tag, applies in (('odd', row_index % 2 == 1), ('disabled', not row['enabled']), ('event_group', is_group)) if applies)))
            if is_group:
                parents.append(str(row['id']))
        self._append_group_counts()
        self._refresh_execution_indicators()

    def _append_group_counts(self) -> None:
        def descendant_event_count(item: str) -> int:
            total = 0
            for child in self.event_tree.get_children(item):
                total += descendant_event_count(child) if 'event_group' in self.event_tree.item(child, 'tags') else 1
            return total

        pending = list(self.event_tree.get_children())
        while pending:
            item = pending.pop()
            children = list(self.event_tree.get_children(item))
            pending.extend(children)
            if 'event_group' not in self.event_tree.item(item, 'tags'):
                continue
            name = str(self.event_tree.item(item, 'text'))
            self.event_tree.item(
                item,
                text=f'{name} （{descendant_event_count(item)}{tr("msg.0438")}）',
            )

    def _add_workflow(self) -> None:
        selected_id = self.current_workflow_id
        name = simpledialog.askstring('msg.0040', 'msg.0041', parent=self.root)
        if not name:
            return
        try:
            workflow_id = self.db.add_workflow(name)
        except sqlite3.IntegrityError:
            messagebox.showerror('msg.0042', 'msg.0043')
            return
        workflow_ids = [row['id'] for row in self.db.list_workflows()]
        workflow_ids.remove(workflow_id)
        insert_at = workflow_ids.index(selected_id) + 1 if selected_id in workflow_ids else len(workflow_ids)
        workflow_ids.insert(insert_at, workflow_id)
        self.db.reorder_workflows(workflow_ids)
        self._refresh_workflows(workflow_id)

    def _edit_workflow(self) -> None:
        if not self.current_workflow_id:
            return
        old_name = self.workflow_tree.item(str(self.current_workflow_id), 'values')[1]
        name = simpledialog.askstring('msg.0044', 'msg.0041', initialvalue=old_name, parent=self.root)
        if name:
            try:
                self.db.update_workflow(self.current_workflow_id, name, '')
                self._refresh_workflows(self.current_workflow_id)
            except sqlite3.IntegrityError:
                messagebox.showerror('msg.0045', 'msg.0043')

    def _delete_workflow(self) -> None:
        if not self.current_workflow_id:
            return
        if messagebox.askyesno('msg.0046', 'msg.0047'):
            self.db.delete_workflow(self.current_workflow_id)
            self.current_workflow_id = None
            self.title_label.config(text='msg.0018')
            self.event_tree.delete(*self.event_tree.get_children())
            self._refresh_workflows()

    def _toggle_workflow(self) -> None:
        if self.current_workflow_id is None:
            return
        row = next((row for row in self.db.list_workflows() if row['id'] == self.current_workflow_id))
        self.db.set_workflow_enabled(self.current_workflow_id, not bool(row['enabled']))
        self._refresh_workflows(self.current_workflow_id)

    def _workflow_double_click(self, event: tk.Event) -> str | None:
        if self.workflow_tree.identify_region(event.x, event.y) == 'heading':
            return 'break'
        if self.workflow_tree.identify_column(event.x) == '#2':
            self._edit_workflow()
        elif self.workflow_tree.identify_column(event.x) == '#3':
            self._toggle_workflow()
        elif self.workflow_tree.identify_column(event.x) == '#4':
            self._toggle_pcl_start()
        elif self.workflow_tree.identify_column(event.x) == '#5':
            self._edit_workflow_guard()

    def _edit_workflow_guard(self) -> None:
        if self.current_workflow_id is None:
            return
        row = next(row for row in self.db.list_workflows() if row['id'] == self.current_workflow_id)
        dialog = self._register_dialog('workflow_guard', lambda: GuardConditionDialog(self.root, row['guard_json'], lambda: self._choose_data_path('condition')))
        if dialog is None:
            return
        self.root.wait_window(dialog)
        if dialog.result is not None:
            self.db.set_workflow_guard(self.current_workflow_id, dialog.result)
            self._refresh_workflows(self.current_workflow_id)

    def _toggle_pcl_start(self) -> None:
        if self.current_workflow_id is None:
            return
        row = next((row for row in self.db.list_workflows() if row['id'] == self.current_workflow_id))
        self.db.set_pcl_loop_start(None if row['pcl_loop_start'] else self.current_workflow_id)
        self._refresh_workflows(self.current_workflow_id)

    def _selected_event(self) -> int | None:
        selection = self.event_tree.selection()
        return int(selection[0]) if selection else None

    @classmethod
    def _event_insert_before_id(
            cls, rows: list[dict[str, object]], selected_id: int | None) -> int | None:
        """選択行の直後、コンテナ選択時はその内部末尾となる挿入基準を返す。"""
        if selected_id is None:
            return None
        selected = next((row for row in rows if row['id'] == selected_id), None)
        if selected is None:
            return None
        if selected['action'] in {'loop_start', 'retry_start', 'group_start'}:
            return cls._paired_boundary_event_id(rows, selected_id)
        row_ids = [int(row['id']) for row in rows]
        selected_index = row_ids.index(selected_id)
        next_index = selected_index + 1
        return row_ids[next_index] if next_index < len(row_ids) else None

    def _event_double_click(self, event: tk.Event) -> str | None:
        if self.event_tree.identify_region(event.x, event.y) == 'heading':
        # 行の編集と切替だけを抑止する。並べ替えなどの見出しクリック処理は
        # 既に実行済みであり、そのまま利用できる。
            return 'break'
        item = self.event_tree.identify_row(event.y)
        if not item:
            return
        self.event_tree.selection_set(item)
        self.event_tree.focus(item)
        if self.event_tree.identify_column(event.x) == '#0':
            if self.event_tree.get_children(item):
                # 矢印の連続クリックで2回目がダブルクリック通知になった場合も、
                # その1回分だけ展開状態を反転する。グループ名のダブルクリックは無効。
                toggle_tree_indicator_on_double_click(self.event_tree, event, item)
                return 'break'
            self._edit_event()
            return 'break'
        if self.event_tree.identify_column(event.x) == '#4':
            self._toggle_event()
        elif self.event_tree.get_children(item):
            self._edit_event()
            return 'break'
        else:
            self._edit_event()
        # Treeview のクラスバインドへ伝播させると、任意列のダブルクリックで
        # open 状態が自動反転するため、アプリ側で処理した時点で必ず停止する。
        return 'break'

    def _toggle_event(self) -> None:
        event_id = self._selected_event()
        if event_id is None or self.current_workflow_id is None:
            return
        rows = [dict(row) for row in self.db.list_events(self.current_workflow_id)]
        row = next(row for row in rows if row['id'] == event_id)
        self.db.set_event_enabled(event_id, not bool(row['enabled']))
        pair_id = self._paired_boundary_event_id(rows, event_id)
        if pair_id is not None:
            self.db.set_event_enabled(pair_id, not bool(row['enabled']))
        self._load_events(self.current_workflow_id)
        self.event_tree.selection_set(str(event_id))
        self.event_tree.focus(str(event_id))

    def _add_event(self) -> None:
        if not self.current_workflow_id:
            messagebox.showinfo('msg.0048', 'msg.0049')
            return
        existing_rows = [dict(row) for row in self.db.list_events(self.current_workflow_id)]
        insert_before_id = self._event_insert_before_id(existing_rows, self._selected_event())
        actions = tuple(action for action in self.settings['actions'] if action not in {'loop_start', 'loop_end', 'retry_start', 'retry_end', 'group_start', 'group_end'})
        dialog = self._register_dialog('event_editor', lambda: EventDialog(self.root, actions, self.settings['selector_types'], self._pick_element, self._test_element, self._verify_event, self._close_debug_browser, None, self._choose_data_path, self.settings['picker']['start_url'], default_timeout_ms=self.db.get_default_timeout_ms()))
        if dialog is None:
            return
        self.root.wait_window(dialog)
        if dialog.result:
            event_id = self.db.add_event(self.current_workflow_id, dialog.result)
            event_ids = [row['id'] for row in self.db.list_events(self.current_workflow_id)]
            inserted_ids = [event_id]
            for inserted_id in inserted_ids:
                event_ids.remove(inserted_id)
            target_index = event_ids.index(insert_before_id) if insert_before_id is not None else len(event_ids)
            event_ids[target_index:target_index] = inserted_ids
            self.db.reorder_events(self.current_workflow_id, event_ids)
            self._load_events(self.current_workflow_id)
            self.event_tree.selection_set(str(event_id))
            self.event_tree.focus(str(event_id))

    def _add_event_group(self) -> None:
        if not self.current_workflow_id:
            messagebox.showinfo('msg.0048', 'msg.0049')
            return
        existing_rows = [dict(row) for row in self.db.list_events(self.current_workflow_id)]
        insert_before_id = self._event_insert_before_id(existing_rows, self._selected_event())
        dialog = self._register_dialog('event_group_editor', lambda: EventGroupDialog(self.root, self._choose_data_path))
        if dialog is None:
            return
        self.root.wait_window(dialog)
        if not dialog.result:
            return
        start_id = self.db.add_event(self.current_workflow_id, dialog.result)
        end_data = dict(dialog.result)
        end_data.update(name=dialog.result['name'], action='group_end', value='', data_path='', guard={'logic': 'all', 'rules': []})
        end_id = self.db.add_event(self.current_workflow_id, end_data)
        event_ids = [row['id'] for row in self.db.list_events(self.current_workflow_id)]
        event_ids.remove(start_id)
        event_ids.remove(end_id)
        target_index = event_ids.index(insert_before_id) if insert_before_id in event_ids else len(event_ids)
        event_ids[target_index:target_index] = [start_id, end_id]
        self.db.reorder_events(self.current_workflow_id, event_ids)
        self._load_events(self.current_workflow_id)
        self.event_tree.selection_set(str(start_id))

    def _edit_event(self) -> None:
        event_id = self._selected_event()
        if event_id is None or self.current_workflow_id is None:
            return
        rows_before = [dict(row) for row in self.db.list_events(self.current_workflow_id)]
        event = next(row for row in rows_before if row['id'] == event_id)
        pair_id = self._paired_boundary_event_id(rows_before, event_id)
        if event['action'] in {'loop_start', 'retry_start', 'group_start'}:
            dialog = self._register_dialog('event_group_editor', lambda: EventGroupDialog(self.root, self._choose_data_path, event))
        else:
            execute_to_event = lambda target_url, completed: self._execute_to_event(self.current_workflow_id, event_id, target_url, completed)
            actions = tuple(action for action in self.settings['actions'] if action not in {'loop_start', 'loop_end', 'retry_start', 'retry_end', 'group_start', 'group_end'})
            dialog = self._register_dialog('event_editor', lambda: EventDialog(self.root, actions, self.settings['selector_types'], self._pick_element, self._test_element, self._verify_event, self._close_debug_browser, execute_to_event, self._choose_data_path, self.settings['picker']['start_url'], event))
        if dialog is None:
            return
        self.root.wait_window(dialog)
        if dialog.result:
            self.db.update_event(event_id, dialog.result)
            if pair_id is not None and dialog.result['action'] in {'loop_start', 'retry_start', 'group_start'}:
                end_data = dict(dialog.result)
                end_data.update(name=dialog.result['name'], action='group_end', value='', data_path='', guard={'logic': 'all', 'rules': []})
                self.db.update_event(pair_id, end_data)
            self._load_events(self.current_workflow_id)

    def _delete_event(self) -> None:
        event_id = self._selected_event()
        if event_id is None or self.current_workflow_id is None:
            return
        rows = [dict(row) for row in self.db.list_events(self.current_workflow_id)]
        pair_id = self._paired_boundary_event_id(rows, event_id)
        prompt = 'msg.0051' if pair_id is not None else 'msg.0052'
        if messagebox.askyesno('msg.0046', prompt):
            delete_ids = [event_id]
            if pair_id is not None:
                start = next(index for index, row in enumerate(rows) if row['id'] == event_id)
                end = next(index for index, row in enumerate(rows) if row['id'] == pair_id)
                delete_ids = [int(row['id']) for row in rows[min(start, end):max(start, end) + 1]]
            self.db.delete_events(delete_ids, self.current_workflow_id)
            self._load_events(self.current_workflow_id)

    @staticmethod
    def _paired_boundary_event_id(rows: list[dict[str, object]], event_id: int) -> int | None:
        selected_index = next((index for index, row in enumerate(rows) if row['id'] == event_id), None)
        if selected_index is None:
            return None
        action = str(rows[selected_index]['action'])
        pairs = {'loop_start': ('loop_start', 'loop_end', 1), 'loop_end': ('loop_start', 'loop_end', -1), 'retry_start': ('retry_start', 'retry_end', 1), 'retry_end': ('retry_start', 'retry_end', -1), 'group_start': ('group_start', 'group_end', 1), 'group_end': ('group_start', 'group_end', -1)}
        if action not in pairs:
            return None
        start_action, end_action, direction = pairs[action]
        depth = 0
        indexes = range(selected_index + 1, len(rows)) if direction == 1 else range(selected_index - 1, -1, -1)
        for index in indexes:
            candidate = str(rows[index]['action'])
            if direction == 1:
                if candidate == start_action:
                    depth += 1
                elif candidate == end_action:
                    if depth == 0:
                        return int(rows[index]['id'])
                    depth -= 1
            elif candidate == end_action:
                depth += 1
            elif candidate == start_action:
                if depth == 0:
                    return int(rows[index]['id'])
                depth -= 1
        return None

    def _move_event(self, direction: int) -> None:
        event_id = self._selected_event()
        if event_id is None or self.current_workflow_id is None:
            return
        parent = self.event_tree.parent(str(event_id))
        siblings = list(self.event_tree.get_children(parent))
        if str(event_id) not in siblings:
            return
        old = siblings.index(str(event_id))
        new = old + direction
        rows = [dict(row) for row in self.db.list_events(self.current_workflow_id)]
        ids = [int(row['id']) for row in rows]

        def block(item_id: str) -> list[int]:
            current_id = int(item_id)
            pair = self._paired_boundary_event_id(rows, current_id)
            if pair is None:
                return [current_id]
            a, b = ids.index(current_id), ids.index(pair)
            return ids[min(a, b):max(a, b) + 1]

        if 0 <= new < len(siblings):
            first, second = (block(siblings[new]), block(siblings[old])) if direction < 0 else (block(siblings[old]), block(siblings[new]))
            start = ids.index(first[0])
            end = ids.index(second[-1]) + 1
            ids[start:end] = second + first
        elif parent:
            source_block = block(str(event_id))
            remaining = [item_id for item_id in ids if item_id not in source_block]
            parent_id = int(parent)
            parent_end = self._paired_boundary_event_id(rows, parent_id)
            if parent_end is None:
                return
            insert_at = remaining.index(parent_id) if direction < 0 else remaining.index(parent_end) + 1
            remaining[insert_at:insert_at] = source_block
            ids = remaining
        else:
            return
        self.db.reorder_events(self.current_workflow_id, ids)
        self._load_events(self.current_workflow_id)
        self.event_tree.selection_set(str(event_id))

    def _bind_drag_sort(self, tree: ttk.Treeview, kind: str) -> None:
        tree.bind('<ButtonPress-1>', lambda event: self._drag_start(event, tree, kind), add='+')
        tree.bind('<B1-Motion>', lambda event: self._drag_motion(event, tree, kind), add='+')
        tree.bind('<ButtonRelease-1>', lambda event: self._drag_end(event, tree, kind), add='+')

    def _drag_start(self, event: tk.Event, tree: ttk.Treeview, kind: str) -> None:
        self.drag_source[kind] = None
        self.drag_start_points[kind] = None
        self.drag_active[kind] = False
        if tree.identify_region(event.x, event.y) not in ('tree', 'cell'):
            return
        if tree.identify_element(event.x, event.y) == 'Treeitem.indicator':
            return
        source = tree.identify_row(event.y) or None
        self.drag_source[kind] = source
        if source:
            self.drag_start_points[kind] = (event.x, event.y)

    def _drag_motion(self, event: tk.Event, tree: ttk.Treeview, kind: str) -> None:
        start = self.drag_start_points[kind]
        if not self.drag_source[kind] or start is None:
            return
        if not self.drag_active[kind]:
            if max(abs(event.x - start[0]), abs(event.y - start[1])) < 6:
                return
            self.drag_active[kind] = True
        target = tree.identify_row(event.y)
        if target:
            tree.configure(cursor='hand2')

    def _drag_end(self, event: tk.Event, tree: ttk.Treeview, kind: str) -> None:
        tree.configure(cursor='')
        source = self.drag_source[kind]
        self.drag_source[kind] = None
        was_active = self.drag_active[kind]
        self.drag_start_points[kind] = None
        self.drag_active[kind] = False
        if not was_active:
            return
        target = tree.identify_row(event.y)
        if not source:
            return
        if kind == 'event' and not target:
            self._drop_event_to_root_end(source)
            return
        if not target or source == target:
            return
        if kind == 'event':
            self._drop_event(source, target, event.y)
            return
        children = list(tree.get_children())
        if source not in children or target not in children:
            return
        children.remove(source)
        target_index = children.index(target)
        box = tree.bbox(target)
        if box and event.y > box[1] + box[3] // 2:
            target_index += 1
        children.insert(target_index, source)
        if kind == 'workflow':
            self.db.reorder_workflows([int(item) for item in children])
            selected_workflow = int(source)
            self._refresh_workflows(selected_workflow)

    def _drop_event(self, source: str, target: str, pointer_y: int) -> None:
        """イベントまたはグループ全体を、ドロップ先の階層へ移動する。"""
        if self.current_workflow_id is None:
            return
        rows = [dict(row) for row in self.db.list_events(self.current_workflow_id)]
        ids = [int(row['id']) for row in rows]

        def block(item_id: int) -> list[int]:
            pair = self._paired_boundary_event_id(rows, item_id)
            if pair is None:
                return [item_id]
            start, end = ids.index(item_id), ids.index(pair)
            return ids[min(start, end):max(start, end) + 1]

        source_id, target_id = int(source), int(target)
        source_block = block(source_id)
        if target_id in source_block:
            return
        target_row = next(row for row in rows if row['id'] == target_id)
        target_is_group = target_row['action'] in {'group_start', 'loop_start', 'retry_start'}
        remaining = [item_id for item_id in ids if item_id not in source_block]
        if target_is_group:
            target_end = self._paired_boundary_event_id(rows, target_id)
            if target_end is None or target_end not in remaining:
                return
            insert_at = remaining.index(target_end)
        else:
            insert_at = remaining.index(target_id)
            box = self.event_tree.bbox(target)
            if box and pointer_y > box[1] + box[3] // 2:
                insert_at += 1
        remaining[insert_at:insert_at] = source_block
        self.db.reorder_events(self.current_workflow_id, remaining)
        self._load_events(self.current_workflow_id)
        self.event_tree.selection_set(source)
        self.event_tree.focus(source)

    def _drop_event_to_root_end(self, source: str) -> None:
        """一覧の空白へドロップされた項目を最外層の末尾へ移動する。"""
        if self.current_workflow_id is None:
            return
        rows = [dict(row) for row in self.db.list_events(self.current_workflow_id)]
        ids = [int(row['id']) for row in rows]
        source_id = int(source)
        pair_id = self._paired_boundary_event_id(rows, source_id)
        source_end = pair_id if pair_id is not None else source_id
        start_index, end_index = ids.index(source_id), ids.index(source_end)
        source_block = ids[min(start_index, end_index):max(start_index, end_index) + 1]
        remaining = [item_id for item_id in ids if item_id not in source_block]
        remaining.extend(source_block)
        self.db.reorder_events(self.current_workflow_id, remaining)
        self._load_events(self.current_workflow_id)
        self.event_tree.selection_set(source)
        self.event_tree.focus(source)

    def _export(self) -> None:
        path = filedialog.asksaveasfilename(defaultextension='.json', filetypes=(('Json', '*.json'),))
        if path:
            self.db.export_workflow_collection(Path(path))
            self._log(f'msg.0053{path}')

    def _import(self) -> None:
        path = filedialog.askopenfilename(filetypes=(('Json', '*.json'), ('msg.0054', '*.*')))
        if not path:
            return
        if not messagebox.askyesno('msg.0055', 'msg.0056'):
            return
        try:
            count = self.db.import_workflow_collection(Path(path), self.settings['actions'], self.settings['selector_types'])
        except (OSError, ValueError, sqlite3.Error) as error:
            messagebox.showerror('msg.0057', str(error))
            return
        self.current_workflow_id = None
        self.event_tree.delete(*self.event_tree.get_children())
        self.title_label.config(text='msg.0018')
        rows = self.db.list_workflows()
        first_id = rows[0]['id'] if rows else None
        self._refresh_workflows(first_id)
        self.browser_visible.set(self.db.get_browser_visible())
        self._log(f'msg.0058{count}msg.0059{path}')

    def _run_workflow(self) -> None:
        # DB の内容はワーカースレッドへ渡す前にスナップショット化する。
        # 実行中に画面側で編集されても、今回の実行計画は変化させない。
        if self.running or self.execution_starting:
            return
        self._show_page('execution')
        self._select_execution_tab('status')
        all_workflows = self.db.list_workflows()
        enabled_workflows = [row for row in all_workflows if row['enabled']]
        if not enabled_workflows:
            messagebox.showinfo('msg.0048', 'msg.0060')
            return
        jobs: list[dict[str, object]] = []
        for workflow in enabled_workflows:
            events = [dict(row) for row in self.db.list_events(workflow['id'])]
            if events:
                # 旧仕様の「データ開始」境界は現在の画面には存在しない。
                # 有効な各業務フローを、有効なデータ行ごとに一回実行する。
                jobs.append({'id': workflow['id'], 'name': workflow['name'], 'position': workflow['position'], 'events': events, 'guard': decode_guard(workflow['guard_json']), 'per_pcl': True})
            else:
                self._log(f"msg.0061{workflow['name']}")
        if not jobs:
            messagebox.showinfo('msg.0048', 'msg.0062')
            return
        names = sorted({name for job in jobs for name in find_variables(job['events'])})
        variables: dict[str, str] = {}
        if names:
            dialog = self._register_dialog('run_variables', lambda: VariablesDialog(self.root, names))
            if dialog is None:
                return
            self.root.wait_window(dialog)
            if dialog.result is None:
                return
            variables = dialog.result
        # 実行中もレコード ID と固定プレビュー行を対応付けられるように、
        # ワーカー／画面間の状態には通常の辞書を使用する。
        all_structured_records = [dict(row) for row in self.db.list_data_records()]
        structured_records = [row for row in all_structured_records if row['enabled']]
        skipped_record_count = len(all_structured_records) - len(structured_records)
        pcl_jobs = [job for job in jobs if job['per_pcl']]
        preamble_jobs = [job for job in jobs if not job['per_pcl']]
        if any((event.get('data_path') for job in preamble_jobs for event in job['events'])):
            messagebox.showerror('msg.0063', 'msg.0064')
            return
        if any(job['guard']['rules'] for job in preamble_jobs) or any(decode_guard(event.get('guard_json'))['rules'] for job in preamble_jobs for event in job['events']):
            messagebox.showerror('msg.0063', 'msg.0407')
            return
        if pcl_jobs and (not all_structured_records):
            messagebox.showerror('msg.0065', 'msg.0066')
            return
        # 永続 Chrome プロファイルは一つのブラウザープロセスだけが使用できる。
        # 実行処理へ渡す前に対話操作用セッションを閉じる。
        self.execution_starting = True
        close_errors: list[Exception] = []
        try:
            for browser_session in (self.debug_browser, self.auth_browser):
                try:
                    browser_session.close_browser()
                except Exception as error:
                    close_errors.append(error)
        finally:
            self.execution_starting = False
        if close_errors:
            messagebox.showerror(
                'msg.0561',
                '\n'.join(tr(str(error)) for error in close_errors),
            )
            return
        self.running = True
        self.run_button.config(state='disabled')
        self.execution_status.configure(text='msg.0558')
        self.executing_tasks.clear()
        self.execution_task_states.clear()
        self.db.prepare_data_record_statuses()
        execution_state_path = profile_path(self.project_dir, self.db.get_auth_profile())
        groups: dict[str, list[dict[str, object]]] = {}
        for record in structured_records:
            groups.setdefault(str(record['execution_group']), []).append(record)
        session_limit = self.db.get_pcl_session_limit()
        self._log(f'msg.0067{len(preamble_jobs)}msg.0068{len(structured_records)}msg.0069{len(pcl_jobs)}msg.0070{len(groups)}msg.0071{session_limit}msg.0072{skipped_record_count}msg.0073')
        preamble_steps = [{'phase': 'once', 'pcl_index': 0, 'record': None, 'pcl_total': len(structured_records), 'id': job['id'], 'name': job['name'], 'position': job['position'], 'events': job['events'], 'guard': job['guard']} for job in preamble_jobs]
        pcl_indexes = {record['id']: index for index, record in enumerate(structured_records, 1)}

        def group_steps(group: str, records: list[dict[str, object]]) -> list[dict[str, object]]:
            return [{'phase': 'pcl', 'group': group, 'pcl_index': pcl_indexes[record['id']], 'record': record, 'pcl_total': len(structured_records), 'id': job['id'], 'name': job['name'], 'position': job['position'], 'events': job['events'], 'guard': job['guard']} for record in records for job in pcl_jobs]

        if preamble_steps:
            first_step = dict(preamble_steps[0])
            self.execution_task_states[self._execution_task_key(first_step)] = (first_step, None, 'waiting')
        if pcl_jobs:
            for group, records in groups.items():
                for record in records:
                    first_step = dict(group_steps(group, [record])[0])
                    self.execution_task_states[self._execution_task_key(first_step)] = (first_step, None, 'waiting')
        self._refresh_execution_indicators()

        def worker() -> None:
            # 前処理は同期実行し、ログイン状態と取得変数を確定してから各組を開始する。
            try:
                def step_start(step: dict[str, object]) -> int:
                    self._show_executing_step(step, None)
                    if step['phase'] == 'pcl' and step.get('record'):
                        self.db.set_data_record_status(step['record']['id'], 'running')
                    if step['phase'] == 'once':
                        self._log(f"msg.0074{step['name']}")
                    return self.db.create_run(step['id'])

                def step_success(_step: dict[str, object], run_id: int) -> None:
                    self.db.finish_run(run_id, 'success', '')
                    self._clear_executing_step(_step, 'success')
                    if _step['phase'] == 'pcl' and _step.get('record'):
                        record = _step['record']
                        self.db.update_data_record(record['id'], record['name'], record['data'])
                    if _step['phase'] == 'pcl' and _step.get('record') and pcl_jobs and (_step['id'] == pcl_jobs[-1]['id']):
                        self.db.set_data_record_status(_step['record']['id'], 'success')

                def step_failure(_step: dict[str, object], run_id: int, error: Exception) -> None:
                    self.db.finish_run(run_id, 'failed', str(error))
                    self._clear_executing_step(_step, 'failed')
                    if _step['phase'] == 'pcl' and _step.get('record'):
                        record = _step['record']
                        self.db.update_data_record(record['id'], record['name'], record['data'])
                        self.db.set_data_record_status(_step['record']['id'], 'failed')

                def event_start(step: dict[str, object], event: dict[str, object]) -> None:
                    self._show_executing_step(step, event)
                executor = WorkflowExecutor(self.project_dir, lambda message: self._log(message, 'WorkflowExecutor'))
                if preamble_steps:
                    executor.run_batch(preamble_steps, variables, step_start, step_success, step_failure, event_start, self.browser_visible.get(), 'preamble', execution_state_path)
                if groups and pcl_jobs:
                    # 異なる実行グループは設定済みのセッション上限まで並列実行できる。
                    # 同一グループ内のレコードは一つの run_batch を使うため直列実行する。
                    worker_count = max(1, min(session_limit, len(groups)))
                    self._log(f'msg.0078{len(groups)}msg.0079{worker_count}msg.0080')
                    failures: list[str] = []
                    with ThreadPoolExecutor(
                        max_workers=worker_count,
                        thread_name_prefix='flow-group',
                    ) as pool:
                        # 組ごとにブラウザーコンテキストと変数辞書を分離する。
                        # 同一組の PCL は一つの run_batch 内で順番に処理される。
                        futures = {pool.submit(WorkflowExecutor(self.project_dir, lambda message: self._log(message, 'WorkflowExecutor')).run_batch, group_steps(group, records), dict(variables), step_start, step_success, step_failure, event_start, self.browser_visible.get(), f'group_{group}', execution_state_path): group for group, records in groups.items()}
                        for future in as_completed(futures):
                            group = futures[future]
                            try:
                                future.result()
                                self._log(f'msg.0081{group}msg.0082')
                            except Exception:
                                failures.append(str(group))
                    if failures:
                        raise RuntimeError(', '.join(failures))
                self._log('msg.0085')
            except Exception as error:
                self._log(f'msg.0086{error}')
            finally:
                self.running = False
                self.root.after(0, self._finish_execution_ui)
        threading.Thread(
            target=worker,
            name='workflow-runner',
            daemon=True,
        ).start()

    def _set_auth_profile(self, profile: str) -> None:
        if self.db.get_auth_profile() == profile:
            return
        self.db.set_auth_profile(profile)
        # 既に開いている要素選択ブラウザーは以前の Cookie を保持するため、
        # 状態切替時に閉じて次回の起動で新しいプロファイルを読み込ませる。
        try:
            self.debug_browser.close_browser()
        except Exception:
            pass

    def _choose_data_path(self, action: str) -> str | None:
        schema = self.db.get_data_schema()
        dialog = self._register_dialog('data_path', lambda: DataPathDialog(self.root, schema, lists_only=action == 'loop_start'))
        if dialog is None:
            return None
        self.root.wait_window(dialog)
        return dialog.result

    def _pick_element(self, target_url: str, completed: object) -> None:

        def worker() -> None:
            try:
                self.auth_browser.close_browser()
                result = self.debug_browser.pick(target_url)
                self.root.after(0, lambda: completed(result, None))
            except Exception as error:
                message = str(error)
                self.root.after(0, lambda: completed(None, message))
        threading.Thread(target=worker, daemon=True).start()

    def _test_element(self, target_url: str, selector_type: str, selector: str, completed: object) -> None:

        def worker() -> None:
            try:
                self.auth_browser.close_browser()
                count = self.debug_browser.test(selector_type, selector, target_url)
                self.root.after(0, lambda: completed(count, None))
            except Exception as error:
                message = str(error)
                self.root.after(0, lambda: completed(None, message))
        threading.Thread(target=worker, daemon=True).start()

    def _verify_event(self, target_url: str, event: dict[str, object], completed: object) -> None:

        def worker() -> None:
            try:
                self.auth_browser.close_browser()
                self.debug_browser.execute_event(event, target_url)
                self.root.after(0, lambda: completed(None))
            except Exception as error:
                message = str(error)
                self.root.after(0, lambda: completed(message))
        threading.Thread(target=worker, daemon=True).start()

    def _close_debug_browser(self, completed: object) -> None:

        def worker() -> None:
            try:
                self.debug_browser.close_browser()
                self.root.after(0, lambda: completed(None))
            except Exception as error:
                message = str(error)
                self.root.after(0, lambda: completed(message))
        threading.Thread(target=worker, daemon=True).start()

    def _execute_to_event(self, workflow_id: int | None, event_id: int, target_url: str, completed: object) -> None:
        if workflow_id is None:
            completed(tr('msg.0364'))
            return
        workflows = self.db.list_workflows()
        marker = next((row for row in workflows if row['pcl_loop_start']), None)
        marker_position = marker['position'] if marker else None
        records = [row for row in self.db.list_data_records() if row['enabled']]
        debug_record = records[0] if records else None
        jobs: list[dict[str, object]] = []
        for workflow in workflows:
            if workflow['id'] != workflow_id and not workflow['enabled']:
                continue
            events = [dict(row) for row in self.db.list_events(workflow['id'])]
            if workflow['id'] == workflow_id:
                # 無効な対象でも停止位置として認識できるよう、対象イベントだけ一時的に有効化する。
                for event in events:
                    if event['id'] == event_id:
                        event['enabled'] = 1
                jobs.append({'events': events, 'guard': decode_guard(workflow['guard_json']), 'data': debug_record['data'] if debug_record and marker_position is not None and workflow['position'] >= marker_position else None})
                break
            jobs.append({'events': events, 'guard': decode_guard(workflow['guard_json']), 'data': debug_record['data'] if debug_record and marker_position is not None and workflow['position'] >= marker_position else None})
        names = sorted({name for job in jobs for name in find_variables(job['events'])})
        variables: dict[str, str] = {}
        if names:
            dialog = VariablesDialog(self.root, names)
            self.root.wait_window(dialog)
            if dialog.result is None:
                completed(tr('msg.0365'))
                return
            variables = dialog.result

        def worker() -> None:
            try:
                self.auth_browser.close_browser()
                self.debug_browser.execute_until(jobs, event_id, variables, target_url)
                self.root.after(0, lambda: completed(None))
            except Exception as error:
                message = str(error)
                self.root.after(0, lambda: completed(message))
        threading.Thread(target=worker, daemon=True).start()

    @staticmethod
    def _format_file_log(
        message: str,
        now: datetime,
        thread_name: str = 'MainThread',
    ) -> str:
        timestamp = now.strftime('%Y-%m-%d %H:%M:%S')
        lines = message.splitlines() or ['']
        return '\n'.join(
            f'{timestamp} [{thread_name}] {line}' for line in lines
        ) + '\n'

    def _log(self, message: str, _source: str='FlowManagerApp') -> None:
        translated = tr(message)
        now = datetime.now()
        file_text = self._format_file_log(
            translated,
            now,
            threading.current_thread().name,
        )
        log_path = self.log_dir / f'{now:%Y-%m-%d}.log'
        try:
            with self._log_file_lock:
                with log_path.open('a', encoding='utf-8', newline='') as log_file:
                    log_file.write(file_text)
                try:
                    print(file_text.rstrip(), flush=True)
                except (OSError, RuntimeError):
                    pass
        except OSError:
            # ファイルログの失敗でワークフロー実行や画面ログを中断させない。
            pass

        display_text = self._compact_display_log(message, translated)
        if display_text is None:
            return

        def append() -> None:
            self.log_text.config(state='normal')
            self.log_text.insert('end', display_text + '\n')
            self.log_text.see('end')
            self.log_text.config(state='disabled')
        self.root.after(0, append)

    @staticmethod
    def _compact_display_log(raw_message: str, translated: str) -> str | None:
        """ファイルログの完全性を保ちながら、画面ログを簡潔に表示する。"""
        # 中間経過または重複情報は日次ログファイルに残すが、
        # 実行状態表と並べて画面表示する必要はない。
        if any(token in raw_message for token in ('msg.0076', 'msg.0078', 'msg.0212')):
            return None
        # バッチ全体のエラーは、グループごとに報告済みの失敗と重複する。
        if 'msg.0086' in raw_message and 'msg.0083' in raw_message:
            return None
        text = ' '.join(translated.splitlines()).strip()
        if 'msg.0193' in raw_message:
            # 画面には成果物名だけを表示し、完全なパスはファイルログに残す。
            prefix, separator, path = text.partition(':')
            if separator and path.strip():
                text = f'{prefix}: {ntpath.basename(path.strip())}'
        maximum = 180
        return text if len(text) <= maximum else f'{text[:maximum - 1]}…'

    @staticmethod
    def _execution_task_key(step: dict[str, object]) -> str:
        if step.get('phase') == 'once':
            return 'preamble'
        record = step.get('record')
        record_id = record.get('id', step.get('pcl_index', '?')) if isinstance(record, dict) else step.get('pcl_index', '?')
        return f'group:{step.get("group", "1")}:data:{record_id}'

    def _refresh_execution_indicators(self) -> None:
        status_labels = {'waiting': tr('msg.0304'), 'running': tr('msg.0305'), 'success': tr('msg.0306'), 'failed': tr('msg.0307')}
        status_counts = {'waiting': 0, 'running': 0, 'success': 0, 'failed': 0}
        # 実行計画のプレビュー行と先頭四列をそのまま維持する。
        # ここで Treeview を再構築するとスキップ対象が消え、実行開始時に
        # 表の構成が変わったように見えていた。
        records_by_id = {int(row['id']): row for row in self.db.list_data_records()}
        for item in self.parallel_tree.get_children():
            if not item.startswith('preview:'):
                continue
            record_id = int(item.split(':', 1)[1])
            record = records_by_id.get(record_id)
            values = list(self.parallel_tree.item(item, 'values'))
            if len(values) < 7:
                continue
            values[4] = '-'
            values[5] = '-'
            values[6] = (
                tr('msg.0513')
                if record is not None and bool(record['enabled'])
                else tr('msg.0309')
            )
            self.parallel_tree.item(item, values=values)
        ordered_tasks = sorted(
            self.execution_task_states.items(),
            key=lambda item: (
                0 if item[1][0].get('phase') == 'once' else 1,
                self._execution_group_sort_key(item[1][0].get('group', '')),
                int(item[1][0].get('pcl_index', 0)),
            ),
        )
        for _task_key, (step, event, status) in ordered_tasks:
            record = step.get('record')
            workflow_text = '-' if status == 'waiting' else f'{step.get("position", "")}. {step.get("name", "")}'.strip('. ')
            event_text = '-' if event is None or status == 'waiting' else f'{event.get("position", "")}. {event.get("name", "")}'.strip('. ')
            status_counts[status] = status_counts.get(status, 0) + 1
            if not isinstance(record, dict):
                continue
            item = f'preview:{record.get("id")}'
            if not self.parallel_tree.exists(item):
                continue
            values = list(self.parallel_tree.item(item, 'values'))
            if len(values) < 7:
                continue
            values[4] = workflow_text
            values[5] = event_text
            values[6] = status_labels.get(status, status)
            self.parallel_tree.item(item, values=values)
        # 前処理フローはデータ別フローより先に一回だけ実行され、レコード ID を持たない。
        # 状態表が停止して見えないよう、有効な全プレビュー行へ実行中のフロー／イベントを
        # 表示する。データ別処理の開始後は、レコード固有の状態表示へ切り替える。
        record_tasks_exist = any(
            isinstance(step.get('record'), dict)
            for step, _event, _status in self.execution_task_states.values()
        )
        preamble_display = next(
            (
                (step, event, status)
                for step, event, status in self.execution_task_states.values()
                if step.get('phase') == 'once'
                and (status in {'running', 'failed'} or not record_tasks_exist)
            ),
            None,
        )
        if preamble_display is not None:
            step, event, status = preamble_display
            workflow_text = f'{step.get("position", "")}. {step.get("name", "")}'.strip('. ')
            event_text = '-' if event is None else f'{event.get("position", "")}. {event.get("name", "")}'.strip('. ')
            for item in self.parallel_tree.get_children():
                if not item.startswith('preview:'):
                    continue
                record_id = int(item.split(':', 1)[1])
                record = records_by_id.get(record_id)
                if record is None or not bool(record['enabled']):
                    continue
                values = list(self.parallel_tree.item(item, 'values'))
                if len(values) < 7:
                    continue
                values[4] = workflow_text
                values[5] = event_text
                values[6] = status_labels.get(status, status)
                self.parallel_tree.item(item, values=values)
        if getattr(self, 'selected_execution_tab', None) == 'status':
            self.root.after_idle(self._update_execution_status_scrollbar)
        if self.execution_task_states:
            self.parallel_summary.configure(text=' / '.join(
                f'{status_labels[key]}: {status_counts[key]}' for key in ('waiting', 'running', 'success', 'failed')
            ))
        else:
            self.parallel_summary.configure(text='msg.0505')
        if self.executing_tasks:
            self.execution_status.configure(text='msg.0558')
        total = len(self.execution_task_states)
        completed = status_counts['success'] + status_counts['failed']
        self.execution_progress.configure(maximum=max(1, total), value=completed)
        self.execution_progress_text.configure(text=f'msg.0450：{status_counts["running"]}')
        self.execution_progress_count.configure(text=f'{completed} / {total}')

    def _show_executing_step(self, step: dict[str, object], event: dict[str, object] | None) -> None:
        def update() -> None:
            task_key = self._execution_task_key(step)
            step_copy = dict(step)
            event_copy = dict(event) if event is not None else None
            self.executing_tasks[task_key] = (step_copy, event_copy)
            self.execution_task_states[task_key] = (step_copy, event_copy, 'running')
            self._refresh_execution_indicators()
        self.root.after(0, update)

    def _clear_executing_step(self, step: dict[str, object], status: str) -> None:
        def update() -> None:
            task_key = self._execution_task_key(step)
            active = self.executing_tasks.pop(task_key, None)
            previous = self.execution_task_states.get(task_key)
            event = active[1] if active is not None else (previous[1] if previous is not None else None)
            self.execution_task_states[task_key] = (dict(step), event, status)
            self._refresh_execution_indicators()
        self.root.after(0, update)

    def _finish_execution_ui(self) -> None:
        self.run_button.config(state='normal')
        for task_key, (step, event) in tuple(self.executing_tasks.items()):
            self.execution_task_states[task_key] = (step, event, 'success')
        self.executing_tasks.clear()
        self._refresh_execution_indicators()
        self.execution_status.config(text='msg.0559')

    def _close(self) -> None:
        if not self.schema_view.confirm_pending_changes():
            return
        if not self.data_view.confirm_pending_changes():
            return
        if self.running and (not messagebox.askyesno('msg.0094', 'msg.0095')):
            return
        self.debug_browser.shutdown()
        self.auth_browser.shutdown()
        state_path = profile_path(self.project_dir, self.db.get_auth_profile())
        profile_dir = persistent_profile_dir(self.project_dir, state_path)
        if profile_has_state(profile_dir) and messagebox.askyesno(
                'msg.0485',
                'msg.0486',
                parent=self.root,
        ):
            try:
                assert profile_dir is not None
                clear_profile(self.project_dir, profile_dir)
            except OSError as error:
                messagebox.showerror('msg.0057', str(error))
        self.db.close()
        self.root.destroy()
