"""フロー変数の表形式入力と、実行時の入力行選択を提供する。"""
from __future__ import annotations
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any
from core.database import Database
from i18n import tr
from ui.ui_helpers import AutoScrollbar, scrollable_tree

class InputDataDialog(tk.Toplevel):
    """一つのフローで使用する変数値を表形式で編集する。"""

    def __init__(self, parent: tk.Misc, database: Database, workflow_id: int, workflow_name: str, variables: list[str]) -> None:
        super().__init__(parent)
        self.db = database
        self.workflow_id = workflow_id
        self.variables = variables
        self.editor: ttk.Entry | None = None
        self.title(f'msg.0215{workflow_name}')
        self.geometry('900x520')
        self.minsize(650, 350)
        ttk.Label(self, text='msg.0216', style='Section.TLabel').pack(anchor='w', padx=10, pady=(10, 4))
        if not variables:
            ttk.Label(self, text='msg.0217').pack(anchor='w', padx=10, pady=4)
        frame = ttk.Frame(self)
        frame.pack(fill='both', expand=True, padx=10, pady=6)
        columns = ('row_name', *variables)
        self.tree = ttk.Treeview(frame, columns=columns, show='headings', selectmode='browse')
        self.tree.heading('row_name', text='msg.0218')
        self.tree.column('row_name', width=130, minwidth=90)
        for variable in variables:
            self.tree.heading(variable, text=variable)
            self.tree.column(variable, width=150, minwidth=90)
        yscroll = AutoScrollbar(frame, orient='vertical', command=self.tree.yview)
        xscroll = AutoScrollbar(frame, orient='horizontal', command=self.tree.xview)
        self.tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.tree.grid(row=0, column=0, sticky='nsew')
        yscroll.grid(row=0, column=1, sticky='ns')
        xscroll.grid(row=1, column=0, sticky='ew')
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        self.tree.bind('<Double-1>', self._begin_edit)
        buttons = ttk.Frame(self)
        buttons.pack(fill='x', padx=10, pady=(0, 10))
        ttk.Button(buttons, text='msg.0219', command=self._add_row).pack(side='left', padx=3)
        ttk.Button(buttons, text='msg.0220', command=self._delete_row).pack(side='left', padx=3)
        ttk.Button(buttons, text='msg.0221', command=self.destroy).pack(side='right', padx=3)
        self._refresh()
        self.transient(parent)

    def _refresh(self, select_id: int | None=None) -> None:
        self.tree.delete(*self.tree.get_children())
        for row in self.db.list_input_rows(self.workflow_id):
            values = [row['name'], *(row['values'].get(variable, '') for variable in self.variables)]
            item = self.tree.insert('', 'end', iid=str(row['id']), values=values)
            if row['id'] == select_id:
                self.tree.selection_set(item)
                self.tree.focus(item)

    def _add_row(self) -> None:
        row_id = self.db.add_input_row(self.workflow_id)
        self._refresh(row_id)
        if self.variables:
            self._open_editor(str(row_id), '#2')

    def _delete_row(self) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        if messagebox.askyesno('msg.0046', 'msg.0222', parent=self):
            self.db.delete_input_row(self.workflow_id, int(selection[0]))
            self._refresh()

    def _begin_edit(self, event: tk.Event) -> None:
        if self.tree.identify_region(event.x, event.y) != 'cell':
            return
        row_id = self.tree.identify_row(event.y)
        column = self.tree.identify_column(event.x)
        if row_id and column:
            self._open_editor(row_id, column)

    def _open_editor(self, row_id: str, column: str) -> None:
        # Treeview 自体にはセル編集機能がないため、対象セル上へ Entry を一時配置する。
        if self.editor is not None:
            self.editor.destroy()
        box = self.tree.bbox(row_id, column)
        if not box:
            return
        column_index = int(column[1:]) - 1
        current = self.tree.item(row_id, 'values')[column_index]
        self.editor = ttk.Entry(self.tree)
        self.editor.insert(0, current)
        self.editor.select_range(0, 'end')
        self.editor.place(x=box[0], y=box[1], width=box[2], height=box[3])
        self.editor.focus_set()
        self.editor.bind('<Return>', lambda _event: self._save_edit(row_id, column_index))
        self.editor.bind('<Escape>', lambda _event: self._cancel_edit())
        self.editor.bind('<FocusOut>', lambda _event: self._save_edit(row_id, column_index))

    def _save_edit(self, row_id: str, column_index: int) -> None:
        if self.editor is None:
            return
        value = self.editor.get()
        self.editor.destroy()
        self.editor = None
        if column_index == 0:
            self.db.update_input_row_name(int(row_id), value.strip() or tr('msg.0223'))
        else:
            self.db.update_input_cell(int(row_id), self.variables[column_index - 1], value)
        self._refresh(int(row_id))

    def _cancel_edit(self) -> None:
        if self.editor is not None:
            self.editor.destroy()
            self.editor = None

class InputRowSelectDialog(tk.Toplevel):

    def __init__(self, parent: tk.Misc, rows: list[dict[str, Any]], variables: list[str]) -> None:
        super().__init__(parent)
        self.title('msg.0224')
        self.geometry('760x380')
        self.result: dict[str, str] | str | None = None
        self.rows = {str(row['id']): row for row in rows}
        self.variables = variables
        preview_variables = variables[:4]
        columns = ('name', *preview_variables)
        tree_frame, self.tree = scrollable_tree(self, columns=columns, show='headings')
        self.tree.heading('name', text='msg.0218')
        self.tree.column('name', width=140)
        for variable in preview_variables:
            self.tree.heading(variable, text=variable)
            self.tree.column(variable, width=140)
        for row in rows:
            self.tree.insert('', 'end', iid=str(row['id']), values=(row['name'], *(row['values'].get(name, '') for name in preview_variables)))
        tree_frame.pack(fill='both', expand=True, padx=10, pady=10)
        self.tree.bind('<Double-1>', lambda _event: self._use_selected())
        buttons = ttk.Frame(self)
        buttons.pack(fill='x', padx=10, pady=(0, 10))
        ttk.Button(buttons, text='msg.0225', command=self._use_selected).pack(side='left', padx=3)
        ttk.Button(buttons, text='msg.0226', command=self._manual).pack(side='left', padx=3)
        ttk.Button(buttons, text='msg.0145', command=self.destroy, style='Secondary.TButton').pack(side='right', padx=3)
        self.transient(parent)
        self.grab_set()

    def _use_selected(self) -> None:
        selection = self.tree.selection()
        if not selection:
            messagebox.showinfo('msg.0048', 'msg.0227', parent=self)
            return
        values = self.rows[selection[0]]['values']
        self.result = {name: values.get(name, '') for name in self.variables}
        self.destroy()

    def _manual(self) -> None:
        self.result = 'manual'
        self.destroy()
