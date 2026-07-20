"""メイン画面、ダイアログ管理、および PCL バッチ実行の調停を行う。"""
from __future__ import annotations
import ctypes
import os
import sqlite3
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
from core.database import Database
from core.conditions import decode_guard, summarize_guard
from core.executor import WorkflowExecutor, find_variables
from core.settings import SettingsError, load_settings
from i18n import install_tk_translation, set_language, tr
from ui.dialogs import EventDialog, EventGroupDialog, GuardConditionDialog, VariablesDialog, guard_operator_labels
from ui.input_data import InputDataDialog, InputRowSelectDialog
from ui.auth_state import AuthStateDialog, profile_path
from ui.structured_data import DataPathDialog, HierarchicalDataDialog, SchemaDesignerDialog
from ui.ui_helpers import AutoScrollbar

def build_execution_schedule(records: list[dict[str, object]], jobs: list[dict[str, object]]) -> list[tuple[str, int, dict[str, object] | None, dict[str, object]]]:
    """前処理を一度だけ並べ、その後ろに PCL ごとの処理を展開する。"""
    preamble = [job for job in jobs if not job.get('per_pcl')]
    pcl_jobs = [job for job in jobs if job.get('per_pcl')]
    schedule = [('once', 0, None, job) for job in preamble]
    schedule.extend((('pcl', pcl_index, record, job) for pcl_index, record in enumerate(records, 1) for job in pcl_jobs))
    return schedule

class FlowManagerApp:
    """Tk の画面状態と、バックグラウンドで動く実行処理を接続する。"""

    def __init__(self) -> None:
        # 言語は Tk ウィジェットを作る前に確定する必要がある。
        # 作成後に変更すると、一部の ttk 内部文字列だけ旧言語が残るためである。
        self.project_dir = Path(__file__).resolve().parent
        self.log_dir = self.project_dir / 'log'
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._log_file_lock = threading.Lock()
        self.db = Database(self.project_dir / 'data' / 'flows.db')
        set_language(self.db.get_language())
        install_tk_translation()
        try:
            self.settings = load_settings(self.project_dir / 'settings.json')
        except SettingsError as error:
            self.db.close()
            raise RuntimeError(tr(f'msg.0001{error}')) from error
        self.ui_font_family, self.ui_font_size = self.db.get_ui_font()
        self.root = tk.Tk()
        self.root.title('msg.0002')
        self.app_icon_path = self.project_dir / 'assets' / 'app.ico'
        if self.app_icon_path.is_file():
            try:
                self.root.iconbitmap(default=str(self.app_icon_path))
            except tk.TclError:
                pass
        self.root.geometry('1180x720')
        self.root.minsize(900, 600)
        self.root.configure(bg='#F3F3F3')
        self.root._flow_show_toplevel = self._show_toplevel
        self._install_deferred_toplevel_display()
        self._configure_styles()
        self.root.bind_class('TCombobox', '<<ComboboxSelected>>', self._clear_combobox_text_selection, add='+')
        self.current_workflow_id: int | None = None
        self.running = False
        self.executing_tasks: dict[str, tuple[dict[str, object], dict[str, object] | None]] = {}
        self.execution_task_states: dict[str, tuple[dict[str, object], dict[str, object] | None, str]] = {}
        self.open_dialogs: dict[str, tk.Toplevel] = {}
        self.browser_visible = tk.BooleanVar(value=self.db.get_browser_visible())
        self._build_settings_menu()
        self.debug_browser = DebugBrowserSession(
            self.project_dir, self.settings['picker']['start_url'], self._log,
            lambda: profile_path(self.project_dir, self.db.get_auth_profile()))
        self.auth_browser = AuthBrowserSession(lambda message: self._log(message, 'AuthBrowserSession'))
        self.drag_source: dict[str, str | None] = {'workflow': None, 'event': None}
        self._build_ui()
        self._apply_font_configuration()
        self._refresh_workflows()
        self.root.after_idle(lambda: self._apply_window_chrome(self.root))
        self.root.protocol('WM_DELETE_WINDOW', self._close)

    def run(self) -> None:
        self.root.mainloop()

    def _clear_combobox_text_selection(self, event: tk.Event) -> None:
        """Keep a chosen value without leaving its field text highlighted."""
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
        """Keep every Toplevel hidden until its subclass has finished building it."""
        if not hasattr(simpledialog, '_flow_original_place_window'):
            original_place_window = simpledialog._place_window
            simpledialog._flow_original_place_window = original_place_window

            def place_simple_dialog(window: tk.Toplevel, parent: tk.Misc | None = None) -> None:
                callback = getattr(window._root(), '_flow_show_toplevel', None)
                if callback is None:
                    original_place_window(window, parent)
                else:
                    # simpledialog calls wait_visibility() immediately after
                    # this function.  Do not consume the first visibility
                    # event here; position the withdrawn window, then map it
                    # from an idle callback so wait_visibility can observe it.
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
        # Every application dialog is centered on the main window.  Using the
        # immediate master here makes nested dialogs drift toward their parent
        # dialog, which is especially noticeable across two monitors.
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
        # Windows may replace the first position of an owned/transient window.
        # Map it invisibly, re-apply the exact coordinates, then reveal it so
        # neither the system default position nor a top-left ghost is visible.
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
        style.configure('DialogCard.TLabel', background='#FFFFFF', foreground='#1F1F1F', font=font)
        style.map('DialogCard.TLabel', background=[('disabled', '#FFFFFF')], foreground=[('disabled', '#A0A0A0')])
        style.configure('DialogCardSection.TLabel', background='#FFFFFF', foreground='#1F1F1F', font=bold_font, padding=(0, 1, 0, 5))
        style.configure('DialogCardSubtle.TLabel', background='#FFFFFF', foreground='#616161', font=small_font)
        style.map('DialogCardSubtle.TLabel', background=[('disabled', '#FFFFFF')], foreground=[('disabled', '#A0A0A0')])
        style.configure('DialogCard.TCheckbutton', background='#FFFFFF', foreground='#1F1F1F', font=font)
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
        style.configure('Secondary.TButton', background='#E7EBF0', foreground='#1F1F1F', bordercolor='#E7EBF0', lightcolor='#E7EBF0', darkcolor='#E7EBF0', font=small_font, relief='flat')
        style.map('Secondary.TButton', background=[('active', '#DCE4EC'), ('pressed', '#CEDAE5'), ('disabled', '#F1F1F1')], foreground=[('disabled', '#A0A0A0')], bordercolor=[('active', '#DCE4EC'), ('pressed', '#CEDAE5')], lightcolor=[('active', '#DCE4EC'), ('pressed', '#CEDAE5')], darkcolor=[('active', '#DCE4EC'), ('pressed', '#CEDAE5')])
        style.configure('Danger.TButton', background='#FFFFFF', foreground='#C42B1C', bordercolor='#D4D4D4')
        style.map('Danger.TButton', background=[('active', '#FDE7E9'), ('pressed', '#F8D7DA')], bordercolor=[('active', '#C42B1C')])
        style.configure('Toolbar.TButton', background='#FFFFFF', foreground='#1F1F1F', bordercolor='#D4D4D4', font=small_font, relief='flat')
        style.map('Toolbar.TButton', background=[('active', '#E8E8E8'), ('pressed', '#DCDCDC'), ('disabled', '#F3F3F3')], foreground=[('disabled', '#A0A0A0')], bordercolor=[('active', '#B8B8B8')])
        style.configure('Action.TButton', background='#FFFFFF', foreground='#1F1F1F', bordercolor='#D4D4D4', font=small_font, relief='flat')
        style.map('Action.TButton', background=[('active', '#E8E8E8'), ('pressed', '#DCDCDC'), ('disabled', '#F3F3F3')], foreground=[('disabled', '#A0A0A0')], bordercolor=[('active', '#0078D4')])
        style.configure('Toolbar.TMenubutton', background='#FFFFFF', foreground='#1F1F1F', bordercolor='#D4D4D4', arrowcolor='#616161', font=small_font, padding=(9, 5), relief='flat')
        style.map('Toolbar.TMenubutton', background=[('active', '#E8E8E8'), ('pressed', '#DCDCDC')], bordercolor=[('active', '#B8B8B8')])
        style.configure('Treeview', background='#FFFFFF', fieldbackground='#FFFFFF', foreground='#3D3D3D', rowheight=28, font=font, borderwidth=1, bordercolor='#D4D4D4')
        style.map('Treeview', background=[('selected', '#CFE8FF')], foreground=[('selected', '#3D3D3D')])
        style.configure('Status.Treeview', background='#FFFFFF', fieldbackground='#FFFFFF', foreground='#3D3D3D', rowheight=28, font=font, borderwidth=0, relief='flat')
        style.map('Status.Treeview', background=[('selected', '#CFE8FF')], foreground=[('selected', '#3D3D3D')])
        # The clam theme draws Treeview.field with its own top/left bevel even
        # when borderwidth is zero.  Remove that element so the surrounding
        # one-pixel frame is the only visible border on every side.
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
        style.configure('DialogCardSection.TLabel', font=bold)
        style.configure('DialogCardSubtle.TLabel', font=small)
        style.configure('DialogCard.TCheckbutton', font=base)
        style.configure('DialogAction.TButton', font=small)
        style.configure('DialogInline.TButton', font=small)
        style.configure('Dialog.TEntry', font=base)
        style.configure('Dialog.TCombobox', font=base)
        style.configure('DialogPlaceholder.TLabel', font=base)
        style.configure('TEntry', font=base)
        style.configure('TCombobox', font=base)
        style.configure('TSpinbox', font=base)
        for button_style in ('TButton', 'Toolbar.TButton', 'Action.TButton', 'Secondary.TButton'):
            style.configure(button_style, font=small)
        style.configure('Toolbar.TMenubutton', font=small)
        style.configure('Primary.TButton', font=(family, max(8, size - 1), 'bold'))
        style.configure('Treeview', font=base, rowheight=max(26, size * 2 + 8))
        style.configure('Status.Treeview', font=base, rowheight=max(26, size * 2 + 8))
        style.configure('Treeview.Heading', font=(family, max(8, size - 1), 'bold'))
        style.configure('ExecutionTab.TButton', font=small)
        style.configure('ExecutionTabSelected.TButton', font=small)
        style.configure('ExecutionTab.TLabel', font=small)
        style.configure('TCheckbutton', font=base)
        if hasattr(self, 'run_button'):
            self.run_button.configure(font=bold)
            self.log_text.configure(font=small)
            for tag in ('loop_start', 'loop_end', 'retry_start', 'retry_end'):
                self.event_tree.tag_configure(tag, font=small)
            self.event_tree.tag_configure('event_group', font=(family, max(8, size - 1), 'bold'))
        self.root.update_idletasks()

    def _build_settings_menu(self) -> None:
        installed = {name.casefold(): name for name in tkfont.families(self.root)}
        candidates = ('Segoe UI', 'Meiryo', 'Yu Gothic UI', 'Microsoft YaHei UI', 'Arial', 'Calibri', 'Tahoma', 'Verdana', 'Noto Sans CJK JP', 'Noto Sans CJK SC', 'Cascadia Code', 'Consolas')
        available = [installed[name.casefold()] for name in candidates if name.casefold() in installed]
        if self.ui_font_family.casefold() not in installed:
            self.ui_font_family = available[0] if available else 'TkDefaultFont'
            self.db.set_ui_font(self.ui_font_family, self.ui_font_size)
            self._apply_font_configuration()
        elif self.ui_font_family not in available:
            available.insert(0, installed[self.ui_font_family.casefold()])
        self.font_family_choice = tk.StringVar(value=self.ui_font_family)
        self.font_size_choice = tk.IntVar(value=self.ui_font_size)
        self.menu_language_choice = tk.StringVar(value=self.db.get_language())
        menu_options = {'tearoff': False, 'bg': '#F3F3F3', 'fg': '#1F1F1F', 'activebackground': '#ADD6FF', 'activeforeground': '#1F1F1F', 'bd': 0}
        settings_menu = tk.Menu(self.root, **menu_options)
        appearance_menu = tk.Menu(settings_menu, **menu_options)
        font_menu = tk.Menu(appearance_menu, **menu_options)
        for family in available:
            font_menu.add_radiobutton(label=family, variable=self.font_family_choice, value=family, command=lambda value=family: self._set_ui_font(family=value))
        size_menu = tk.Menu(appearance_menu, **menu_options)
        for size in (8, 9, 10, 11, 12, 14, 16, 18):
            size_menu.add_radiobutton(label=str(size), variable=self.font_size_choice, value=size, command=lambda value=size: self._set_ui_font(size=value))
        appearance_menu.add_cascade(label=tr('msg.0373'), menu=font_menu)
        appearance_menu.add_cascade(label=tr('msg.0374'), menu=size_menu)
        appearance_menu.add_separator()
        appearance_menu.add_command(label=tr('msg.0375'), command=self._reset_ui_font)
        language_menu = tk.Menu(settings_menu, **menu_options)
        language_menu.add_radiobutton(label=tr('msg.0022'), variable=self.menu_language_choice, value='ja', command=lambda: self._set_language_from_menu('ja'))
        language_menu.add_radiobutton(label=tr('msg.0023'), variable=self.menu_language_choice, value='zh', command=lambda: self._set_language_from_menu('zh'))
        settings_menu.add_cascade(label=tr('msg.0372'), menu=appearance_menu)
        settings_menu.add_cascade(label=tr('msg.0376'), menu=language_menu)
        settings_menu.add_separator()
        settings_menu.add_checkbutton(label=tr('msg.0377'), variable=self.browser_visible, command=lambda: self.db.set_browser_visible(self.browser_visible.get()))
        settings_menu.add_command(label=tr('msg.0378'), command=lambda: self.root.geometry('1180x720'))
        self.settings_menu = settings_menu

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

    def _build_ui(self) -> None:
        outer = ttk.Panedwindow(self.root, orient='horizontal')
        self.main_pane = outer
        outer.pack(fill='both', expand=True, padx=14, pady=12)
        left = ttk.Frame(outer, padding=(8, 4, 12, 4))
        right = ttk.Frame(outer, padding=(14, 4, 4, 4))
        outer.add(left, weight=1)
        outer.add(right, weight=4)
        self.root.after_idle(self._set_initial_pane_ratio)
        ttk.Label(left, text='msg.0003', style='Section.TLabel').pack(anchor='w')
        ttk.Label(left, text='msg.0004', style='Subtle.TLabel').pack(anchor='w')
        workflow_table = ttk.Frame(left)
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
        workflow_buttons = ttk.Frame(left)
        workflow_buttons.pack(fill='x')
        controls = (
            ('msg.0008', self._add_workflow, 'Action.TButton'),
            ('msg.0009', self._edit_workflow, 'TButton'),
            ('msg.0013', self._toggle_workflow, 'TButton'),
            ('msg.0010', self._delete_workflow, 'Danger.TButton'),
            ('msg.0011', lambda: self._move_workflow(-1), 'TButton'),
            ('msg.0012', lambda: self._move_workflow(1), 'TButton'),
            ('msg.0014', self._toggle_pcl_start, 'TButton'),
            ('msg.0403', self._edit_workflow_guard, 'TButton'),
        )
        for index, (text, command, button_style) in enumerate(controls):
            ttk.Button(workflow_buttons, text=text, command=command, style=button_style).grid(
                row=index // 4, column=index % 4, padx=2, pady=2, sticky='ew'
            )
        for column in range(4):
            workflow_buttons.columnconfigure(column, weight=1, uniform='workflow_action')
        collection_buttons = ttk.Frame(left)
        collection_buttons.pack(fill='x', pady=(12, 0))
        ttk.Separator(collection_buttons).pack(fill='x', pady=(0, 8))
        ttk.Label(collection_buttons, text='msg.0015', style='Subtle.TLabel').pack(anchor='w', pady=(0, 5))
        ttk.Button(collection_buttons, text='msg.0016', command=self._import, style='Toolbar.TButton').pack(fill='x', pady=2)
        ttk.Button(collection_buttons, text='msg.0017', command=self._export, style='Toolbar.TButton').pack(fill='x', pady=2)
        header = ttk.Frame(right)
        header.pack(fill='x')
        self.title_label = ttk.Label(header, text='msg.0018', style='Section.TLabel')
        self.title_label.pack(side='left')
        ttk.Menubutton(header, text='⚙ msg.0371', menu=self.settings_menu, style='Toolbar.TMenubutton').pack(side='right', padx=(6, 0))
        ttk.Button(header, text='msg.0019', command=self._manage_structured_data, style='Toolbar.TButton').pack(side='right', padx=3)
        ttk.Button(header, text='msg.0020', command=self._design_schema, style='Toolbar.TButton').pack(side='right', padx=3)
        ttk.Button(header, text='msg.0460', command=self._manage_auth_state, style='Toolbar.TButton').pack(side='right', padx=3)
        self.run_button = tk.Button(header, text='msg.0021', command=self._run_workflow, bg='#0078D4', fg='white', activebackground='#106EBE', activeforeground='white', disabledforeground='#F3F3F3', relief='flat', bd=0, font=(self.ui_font_family, self.ui_font_size, 'bold'), padx=12, pady=3, cursor='hand2')
        self.run_button.pack(side='right', padx=3)
        ttk.Separator(right).pack(fill='x', pady=(8, 4))
        self.execution_status = ttk.Label(right, text='msg.0026', style='Subtle.TLabel')
        self.execution_status.pack(fill='x', pady=(3, 0))
        self.execution_panel = ttk.Frame(right)
        self.execution_panel.pack(fill='both', expand=True, pady=(8, 0))
        execution_tab_bar = ttk.Frame(self.execution_panel)
        execution_tab_bar.pack(fill='x')
        self.execution_tab_buttons: dict[str, ttk.Button] = {}
        for index, (name, text) in enumerate((('events', 'msg.0452'), ('status', 'msg.0444'), ('log', 'msg.0036'))):
            button = ttk.Button(execution_tab_bar, text=text, command=lambda tab=name: self._select_execution_tab(tab), style='ExecutionTabSelected.TButton' if name == 'events' else 'ExecutionTab.TButton')
            button.pack(side='left', padx=(2 if index else 0, 0))
            self.execution_tab_buttons[name] = button
        execution_content = ttk.Frame(self.execution_panel, style='ExecutionTab.TFrame')
        execution_content.pack(fill='both', expand=True)
        event_tab = ttk.Frame(execution_content, style='ExecutionTab.TFrame')

        columns = ('position', 'name', 'action', 'value', 'data_path', 'guard', 'enabled')
        self.event_table = ttk.Frame(event_tab)
        self.event_table.pack(fill='both', expand=True, pady=(8, 4))
        event_tree_style = ttk.Style(self.root)
        event_tree_style.layout('Event.Treeview.Item', [
            ('Treeitem.padding', {'sticky': 'nswe', 'children': [
                ('Treeitem.image', {'side': 'left', 'sticky': ''}),
                ('Treeitem.text', {'sticky': 'nswe'})
            ]})
        ])
        self.event_tree = ttk.Treeview(self.event_table, columns=columns, show='tree headings', height=8, style='Event.Treeview')
        self.event_tree.heading('#0', text='')
        self.event_tree.column('#0', width=1, minwidth=1, stretch=False)
        headings = {'position': 'msg.0027', 'name': 'msg.0028', 'action': 'msg.0029', 'value': 'msg.0032', 'data_path': 'msg.0033', 'guard': 'msg.0405', 'enabled': 'msg.0006'}
        widths = {'position': 48, 'name': 170, 'action': 78, 'value': 110, 'data_path': 130, 'guard': 190, 'enabled': 58}
        for column in columns:
            self.event_tree.heading(column, text=headings[column])
            self.event_tree.column(column, width=widths[column], minwidth=35, stretch=False)
        self.event_tree.column('guard', stretch=True)
        event_y = AutoScrollbar(self.event_table, orient='vertical', command=self.event_tree.yview)
        event_x = AutoScrollbar(self.event_table, orient='horizontal', command=self.event_tree.xview)
        self.event_tree.configure(yscrollcommand=event_y.set, xscrollcommand=event_x.set)
        self.event_tree.grid(row=0, column=0, sticky='nsew')
        event_y.grid(row=0, column=1, sticky='ns')
        event_x.grid(row=1, column=0, sticky='ew')
        self.event_table.rowconfigure(0, weight=1)
        self.event_table.columnconfigure(0, weight=1)
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
        self.event_tree.bind('<Button-1>', self._event_group_click, add='+')
        self._bind_drag_sort(self.event_tree, 'event')
        event_buttons = ttk.Frame(event_tab)
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
            ttk.Button(event_buttons, text=text, command=command, style=button_style).grid(
                row=0, column=index, padx=3, pady=2, sticky='ew'
            )
            event_buttons.columnconfigure(index, weight=1, uniform='event_action')
        self.parallel_frame = ttk.Frame(execution_content, padding=(10, 8), style='ExecutionTab.TFrame')
        self.parallel_summary = ttk.Label(self.parallel_frame, text='msg.0451', style='ExecutionTab.TLabel')
        self.parallel_summary.grid(row=0, column=0, columnspan=2, sticky='w', pady=(0, 6))
        parallel_table_frame = tk.Frame(
            self.parallel_frame,
            bg='#D4D4D4',
            bd=0,
            highlightthickness=0,
        )
        parallel_table_frame.grid(row=1, column=0, sticky='nsew')
        parallel_table_inner = tk.Frame(parallel_table_frame, bg='#FFFFFF', bd=0, highlightthickness=0)
        parallel_table_inner.grid(row=0, column=0, sticky='nsew', padx=1, pady=1)
        parallel_columns = ('group', 'data', 'workflow', 'event', 'status')
        self.parallel_tree = ttk.Treeview(parallel_table_inner, columns=parallel_columns, show='headings', height=4, style='Status.Treeview')
        for column, heading, width in (
            ('group', 'msg.0445', 65), ('data', 'msg.0446', 130),
            ('workflow', 'msg.0447', 165), ('event', 'msg.0448', 220),
            ('status', 'msg.0288', 85),
        ):
            self.parallel_tree.heading(column, text=heading)
            self.parallel_tree.column(column, width=width, minwidth=55, stretch=column in {'data', 'workflow', 'event'})
        self.parallel_scrollbar = ttk.Scrollbar(parallel_table_inner, orient='vertical', command=self.parallel_tree.yview)
        self.parallel_tree.configure(yscrollcommand=self.parallel_scrollbar.set)
        self.parallel_tree.grid(row=0, column=0, sticky='nsew')
        self.parallel_scrollbar.grid(row=0, column=1, sticky='ns')
        self.parallel_scrollbar.grid_remove()
        self.parallel_tree.bind('<Configure>', self._resize_execution_status_columns)
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
        self.execution_tabs = {'events': event_tab, 'status': self.parallel_frame, 'log': log_tab}
        self._select_execution_tab('events')

    def _set_initial_pane_ratio(self) -> None:
        if self.main_pane.winfo_exists() and self.main_pane.winfo_width() > 1:
            self.main_pane.sashpos(0, int(self.main_pane.winfo_width() * 0.31))

    def _resize_event_columns(self, event: tk.Event) -> None:
        # Use the Treeview's real client width.  Reserving a fixed scrollbar
        # gutter leaves an empty strip whenever AutoScrollbar hides itself.
        available = max(620, event.width - 3)
        fixed = {'position': 52, 'action': 82, 'enabled': 62}
        flexible = available - sum(fixed.values())
        widths = {
            **fixed,
            'name': int(flexible * 0.24),
            'value': int(flexible * 0.15),
            'data_path': int(flexible * 0.18),
            'guard': int(flexible * 0.43),
        }
        for column, width in widths.items():
            self.event_tree.column(column, width=max(45, width))

    def _resize_execution_status_columns(self, event: tk.Event) -> None:
        available = max(500, event.width - 3)
        ratios = {'group': 0.08, 'data': 0.18, 'workflow': 0.24, 'event': 0.36, 'status': 0.14}
        for column, ratio in ratios.items():
            self.parallel_tree.column(column, width=max(55, int(available * ratio)), stretch=False)

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

    def _toggle_log(self) -> None:
        self.log_expanded = not self.log_expanded
        if self.log_expanded:
            self.log_frame.pack(fill='x')
            self.log_toggle_button.configure(text='msg.0436')
        else:
            self.log_frame.pack_forget()
            self.log_toggle_button.configure(text='msg.0437')

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
            indentation = '    ' * len(parents)
            clean_name = str(row['name']).lstrip('○◯●⟳↻ ')
            group_open = open_states.get(str(row['id']), True)
            display_name = f'{indentation}{("▼ " if group_open else "▶ ") if is_group else ""}{marker}{clean_name}'
            guard_summary = summarize_guard(decode_guard(row['guard_json']), guard_operator_labels()) or tr('msg.0404')
            parent = parents[-1] if parents else ''
            display_action = 'group' if row['action'] == 'group_start' else 'loop' if row['action'] == 'loop_start' else 'retry' if row['action'] == 'retry_start' else row['action']
            self.event_tree.insert(parent, 'end', iid=str(row['id']), open=group_open if is_group else False, values=(row['position'], display_name, display_action, row['value'], row['data_path'], guard_summary, 'msg.0037' if row['enabled'] else 'msg.0038'), tags=tuple((tag for tag, applies in (('odd', row_index % 2 == 1), ('disabled', not row['enabled']), ('event_group', is_group)) if applies)))
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
            values = list(self.event_tree.item(item, 'values'))
            values[1] = f'{values[1]} （{descendant_event_count(item)}{tr("msg.0438")}）'
            self.event_tree.item(item, values=values)

    def _event_group_click(self, event: tk.Event) -> None:
        """グループ名のクリックで展開状態を切り替える。"""
        item = self.event_tree.identify_row(event.y)
        if not item or not self._event_name_hit(item, event.x):
            return
        if not self.event_tree.get_children(item):
            return
        self.event_tree.selection_set(item)
        self.event_tree.focus(item)
        if getattr(self, '_pending_group_toggle', None) is None:
            after_id = self.root.after(350, lambda current=item: self._finish_group_toggle(current))
            self._pending_group_toggle = (item, after_id)

    def _event_name_hit(self, item: str, pointer_x: int) -> bool:
        """イベント名セル内の実際の文字描画範囲だけをクリック対象にする。"""
        if self.event_tree.identify_column(pointer_x) != '#2':
            return False
        box = self.event_tree.bbox(item, 'name')
        if not box:
            return False
        text = str(self.event_tree.item(item, 'values')[1])
        font = tkfont.Font(root=self.root, family=self.ui_font_family, size=max(8, self.ui_font_size - 1), weight='bold')
        text_start = box[0] + 6
        return text_start <= pointer_x <= text_start + font.measure(text) + 8

    def _cancel_pending_group_toggle(self) -> None:
        pending = getattr(self, '_pending_group_toggle', None)
        if pending is not None:
            self.root.after_cancel(pending[1])
            self._pending_group_toggle = None

    def _finish_group_toggle(self, item: str) -> None:
        self._pending_group_toggle = None
        if self.event_tree.exists(item):
            self._toggle_event_group(item)

    def _toggle_event_group(self, item: str) -> None:
        """表示中の矢印と Treeview の open 状態を常に同時に更新する。"""
        opened = bool(self.event_tree.item(item, 'open'))
        self.event_tree.item(item, open=not opened)
        values = list(self.event_tree.item(item, 'values'))
        name = str(values[1])
        values[1] = name.replace('▼ ', '▶ ', 1) if opened else name.replace('▶ ', '▼ ', 1)
        self.event_tree.item(item, values=values)

    def _add_workflow(self) -> None:
        name = simpledialog.askstring('msg.0040', 'msg.0041', parent=self.root)
        if not name:
            return
        try:
            workflow_id = self.db.add_workflow(name)
        except sqlite3.IntegrityError:
            messagebox.showerror('msg.0042', 'msg.0043')
            return
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

    def _move_workflow(self, direction: int) -> None:
        if self.current_workflow_id is None:
            return
        workflow_id = self.current_workflow_id
        self.db.move_workflow(workflow_id, direction)
        self._refresh_workflows(workflow_id)

    def _toggle_workflow(self) -> None:
        if self.current_workflow_id is None:
            return
        row = next((row for row in self.db.list_workflows() if row['id'] == self.current_workflow_id))
        self.db.set_workflow_enabled(self.current_workflow_id, not bool(row['enabled']))
        self._refresh_workflows(self.current_workflow_id)

    def _workflow_double_click(self, event: tk.Event) -> str | None:
        if self.workflow_tree.identify_region(event.x, event.y) == 'heading':
            return 'break'
        if self.workflow_tree.identify_column(event.x) == '#3':
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

    def _event_double_click(self, event: tk.Event) -> str | None:
        if self.event_tree.identify_region(event.x, event.y) == 'heading':
            # Suppress row editing/toggling only. Heading click commands (such
            # as sorting) have already run and remain available.
            return 'break'
        item = self.event_tree.identify_row(event.y)
        if not item:
            return
        self.event_tree.selection_set(item)
        self.event_tree.focus(item)
        if self.event_tree.identify_column(event.x) == '#7':
            self._cancel_pending_group_toggle()
            self._toggle_event()
        elif self.event_tree.get_children(item):
            # シングルクリック予約を取り消し、グループ編集を開く。
            self._cancel_pending_group_toggle()
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
        insert_before_id = self._selected_event()
        existing_rows = [dict(row) for row in self.db.list_events(self.current_workflow_id)]
        selected_row = next((row for row in existing_rows if row['id'] == insert_before_id), None)
        if selected_row and selected_row['action'] in {'loop_start', 'retry_start', 'group_start'}:
            insert_before_id = self._paired_boundary_event_id(existing_rows, insert_before_id)
        actions = tuple(action for action in self.settings['actions'] if action not in {'loop_start', 'loop_end', 'retry_start', 'retry_end', 'group_start', 'group_end'})
        dialog = self._register_dialog('event_editor', lambda: EventDialog(self.root, actions, self.settings['selector_types'], self._pick_element, self._test_element, self._open_debug_browser, self._close_debug_browser, None, self._choose_data_path, self.settings['picker']['start_url']))
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
        insert_before_id = self._selected_event()
        existing_rows = [dict(row) for row in self.db.list_events(self.current_workflow_id)]
        selected_row = next((row for row in existing_rows if row['id'] == insert_before_id), None)
        if selected_row and selected_row['action'] in {'loop_start', 'retry_start', 'group_start'}:
            pair_id = self._paired_boundary_event_id(existing_rows, insert_before_id)
            row_ids = [int(row['id']) for row in existing_rows]
            if pair_id in row_ids:
                next_index = row_ids.index(pair_id) + 1
                insert_before_id = row_ids[next_index] if next_index < len(row_ids) else None
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
            dialog = self._register_dialog('event_editor', lambda: EventDialog(self.root, actions, self.settings['selector_types'], self._pick_element, self._test_element, self._open_debug_browser, self._close_debug_browser, execute_to_event, self._choose_data_path, self.settings['picker']['start_url'], event))
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
        if tree.identify_region(event.x, event.y) not in ('tree', 'cell'):
            self.drag_source[kind] = None
            return
        self.drag_source[kind] = tree.identify_row(event.y) or None

    def _drag_motion(self, event: tk.Event, tree: ttk.Treeview, kind: str) -> None:
        if not self.drag_source[kind]:
            return
        if kind == 'event':
            self._cancel_pending_group_toggle()
        target = tree.identify_row(event.y)
        if target:
            tree.configure(cursor='hand2')

    def _drag_end(self, event: tk.Event, tree: ttk.Treeview, kind: str) -> None:
        tree.configure(cursor='')
        source = self.drag_source[kind]
        self.drag_source[kind] = None
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
        if self.running:
            return
        all_workflows = self.db.list_workflows()
        enabled_workflows = [row for row in all_workflows if row['enabled']]
        if not enabled_workflows:
            messagebox.showinfo('msg.0048', 'msg.0060')
            return
        marker = next((row for row in all_workflows if row['pcl_loop_start']), None)
        marker_position = marker['position'] if marker else None
        jobs: list[dict[str, object]] = []
        for workflow in enabled_workflows:
            events = [dict(row) for row in self.db.list_events(workflow['id'])]
            if events:
                jobs.append({'id': workflow['id'], 'name': workflow['name'], 'position': workflow['position'], 'events': events, 'guard': decode_guard(workflow['guard_json']), 'per_pcl': marker_position is not None and workflow['position'] >= marker_position})
            else:
                self._log(f'msg.0061{workflow['name']}')
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
        all_structured_records = self.db.list_data_records()
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
        self.running = True
        self.run_button.config(state='disabled')
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
                state: dict[str, int] = {}

                def step_start(step: dict[str, object]) -> int:
                    self._show_executing_step(step, None)
                    if step['phase'] == 'pcl' and step.get('record'):
                        self.db.set_data_record_status(step['record']['id'], 'running')
                    if step['phase'] == 'once':
                        self._log(f'msg.0074{step['name']} ==========')
                    else:
                        group = str(step.get('group', '1'))
                        if step['pcl_index'] != state.get(group, -1):
                            pcl_name = step['record']['name']
                            self._log(f'msg.0075{group} | Data [{step['pcl_index']}/{len(structured_records)}] {pcl_name} ################')
                            state[group] = step['pcl_index']
                        self._log(f'msg.0076{step['name']} ==========')
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
                    self._log('msg.0077')
                    executor.run_batch(preamble_steps, variables, step_start, step_success, step_failure, event_start, self.browser_visible.get(), 'preamble', execution_state_path)
                if groups and pcl_jobs:
                    worker_count = min(session_limit, len(groups))
                    self._log(f'msg.0078{len(groups)}msg.0079{worker_count}msg.0080')
                    failures: list[str] = []
                    with ThreadPoolExecutor(max_workers=worker_count) as pool:
                        # 組ごとにブラウザーコンテキストと変数辞書を分離する。
                        # 同一組の PCL は一つの run_batch 内で順番に処理される。
                        futures = {pool.submit(WorkflowExecutor(self.project_dir, lambda message: self._log(message, 'WorkflowExecutor')).run_batch, group_steps(group, records), dict(variables), step_start, step_success, step_failure, event_start, self.browser_visible.get(), f'group_{group}', execution_state_path): group for group, records in groups.items()}
                        for future in as_completed(futures):
                            group = futures[future]
                            try:
                                future.result()
                                self._log(f'msg.0081{group}msg.0082')
                            except Exception as error:
                                failures.append(f'msg.0083{group}: {error}')
                                self._log(f'msg.0081{group}msg.0084{error}')
                    if failures:
                        raise RuntimeError('；'.join(failures))
                self._log('msg.0085')
            except Exception as error:
                self._log(f'msg.0086{error}')
            finally:
                self.running = False
                self.root.after(0, self._finish_execution_ui)
        threading.Thread(target=worker, daemon=True).start()

    def _manage_inputs(self) -> None:
        if self.current_workflow_id is None:
            messagebox.showinfo('msg.0048', 'msg.0087')
            return
        events = [dict(row) for row in self.db.list_events(self.current_workflow_id)]
        variables = find_variables(events)
        dialog = self._register_dialog(f'inputs:{self.current_workflow_id}', lambda: InputDataDialog(self.root, self.db, self.current_workflow_id, str(self.title_label.cget('text')), variables))
        if dialog is None:
            return
        self.root.wait_window(dialog)

    def _design_schema(self) -> None:
        dialog = self._register_dialog('schema', lambda: SchemaDesignerDialog(self.root, self.db, 0, 'msg.0088'))
        if dialog is None:
            return
        self.root.wait_window(dialog)

    def _manage_auth_state(self) -> None:
        dialog = self._register_dialog('auth_state', lambda: AuthStateDialog(
            self.root, self.project_dir, self.auth_browser, self.db.get_auth_profile(),
            self._set_auth_profile, self.settings['picker']['start_url']))
        if dialog is not None:
            self.root.wait_window(dialog)

    def _set_auth_profile(self, profile: str) -> None:
        self.db.set_auth_profile(profile)
        # 既に開いている要素選択ブラウザーは以前の Cookie を保持するため、
        # 状態切替時に閉じて次回の起動で新しいプロファイルを読み込ませる。
        try:
            self.debug_browser.close_browser()
        except Exception:
            pass

    def _manage_structured_data(self) -> None:
        dialog = self._register_dialog('pcl_data', lambda: HierarchicalDataDialog(self.root, self.db, 0, 'msg.0088'))
        if dialog is None:
            return
        self.root.wait_window(dialog)

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
                result = self.debug_browser.pick(target_url)
                self.root.after(0, lambda: completed(result, None))
            except Exception as error:
                message = str(error)
                self.root.after(0, lambda: completed(None, message))
        threading.Thread(target=worker, daemon=True).start()

    def _test_element(self, target_url: str, selector_type: str, selector: str, completed: object) -> None:

        def worker() -> None:
            try:
                count = self.debug_browser.test(selector_type, selector, target_url)
                self.root.after(0, lambda: completed(count, None))
            except Exception as error:
                message = str(error)
                self.root.after(0, lambda: completed(None, message))
        threading.Thread(target=worker, daemon=True).start()

    def _open_debug_browser(self, target_url: str, completed: object) -> None:

        def worker() -> None:
            try:
                self.debug_browser.open(target_url)
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
                self.debug_browser.execute_until(jobs, event_id, variables, target_url)
                self.root.after(0, lambda: completed(None))
            except Exception as error:
                message = str(error)
                self.root.after(0, lambda: completed(message))
        threading.Thread(target=worker, daemon=True).start()

    def _log(self, message: str, class_name: str='FlowManagerApp') -> None:
        translated = tr(message)
        now = datetime.now()
        timestamp = now.strftime('%Y-%m-%d %H%M%S')
        prefix = f'{timestamp} {class_name}: '
        lines = translated.splitlines() or ['']
        file_text = '\n'.join(f'{prefix}{line}' for line in lines) + '\n'
        log_path = self.log_dir / f'{now:%Y-%m-%d}.log'
        try:
            with self._log_file_lock:
                with log_path.open('a', encoding='utf-8', newline='') as log_file:
                    log_file.write(file_text)
        except OSError:
            # File logging must never interrupt workflow execution or UI logs.
            pass

        def append() -> None:
            self.log_text.config(state='normal')
            self.log_text.insert('end', translated + '\n')
            self.log_text.see('end')
            self.log_text.config(state='disabled')
        self.root.after(0, append)

    @staticmethod
    def _execution_task_key(step: dict[str, object]) -> str:
        if step.get('phase') == 'once':
            return 'preamble'
        record = step.get('record')
        record_id = record.get('id', step.get('pcl_index', '?')) if isinstance(record, dict) else step.get('pcl_index', '?')
        return f'group:{step.get("group", "1")}:data:{record_id}'

    def _all_event_items(self) -> list[str]:
        result: list[str] = []
        pending = list(self.event_tree.get_children())
        while pending:
            item = pending.pop()
            result.append(item)
            pending.extend(self.event_tree.get_children(item))
        return result

    def _refresh_execution_indicators(self) -> None:
        self.parallel_tree.delete(*self.parallel_tree.get_children())
        status_labels = {'waiting': tr('msg.0304'), 'running': tr('msg.0305'), 'success': tr('msg.0306'), 'failed': tr('msg.0307')}
        status_counts = {'waiting': 0, 'running': 0, 'success': 0, 'failed': 0}
        for task_key, (step, event, status) in self.execution_task_states.items():
            record = step.get('record')
            record_name = str(record.get('name', '')) if isinstance(record, dict) else ''
            data_text = '-' if step.get('phase') == 'once' else f'{step.get("pcl_index")}/{step.get("pcl_total")} {record_name}'
            group_text = '-' if step.get('phase') == 'once' else str(step.get('group', '1'))
            workflow_text = f'{step.get("position", "")}. {step.get("name", "")}'.strip('. ')
            event_text = '-' if event is None else f'{event.get("position", "")}. {event.get("name", "")}'.strip('. ')
            status_counts[status] = status_counts.get(status, 0) + 1
            self.parallel_tree.insert('', 'end', iid=task_key, values=(group_text, data_text, workflow_text, event_text, status_labels.get(status, status)))
        if getattr(self, 'selected_execution_tab', None) == 'status':
            self.root.after_idle(self._update_execution_status_scrollbar)
        if self.execution_task_states:
            self.parallel_summary.configure(text=' / '.join(
                f'{status_labels[key]}: {status_counts[key]}' for key in ('waiting', 'running', 'success', 'failed')
            ))
        else:
            self.parallel_summary.configure(text='msg.0451')
        if self.executing_tasks:
            self.execution_status.configure(text=f'{tr("msg.0450")}: {len(self.executing_tasks)}')

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
        self.execution_status.config(text='msg.0093')

    def _close(self) -> None:
        if self.running and (not messagebox.askyesno('msg.0094', 'msg.0095')):
            return
        self.debug_browser.shutdown()
        self.auth_browser.shutdown()
        self.db.close()
        self.root.destroy()
