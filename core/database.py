"""SQLite のスキーマ、移行処理、および永続化 API を提供する。"""
from __future__ import annotations
import json
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any
from core.conditions import decode_guard
from i18n import tr

class Database:
    """画面と実行スレッドから共有される SQLite アクセス層。"""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._create_schema()

    def _create_schema(self) -> None:
        # CREATE TABLE だけでは既存 DB に列が追加されない。
        # 下段の PRAGMA 検査で、過去バージョンを段階的に更新する。
        self.connection.executescript("\n            CREATE TABLE IF NOT EXISTS workflows (\n                id INTEGER PRIMARY KEY AUTOINCREMENT,\n                name TEXT NOT NULL UNIQUE,\n                description TEXT NOT NULL DEFAULT '',\n                position INTEGER NOT NULL DEFAULT 0,\n                enabled INTEGER NOT NULL DEFAULT 1,\n                pcl_loop_start INTEGER NOT NULL DEFAULT 0,\n                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,\n                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP\n            );\n            CREATE TABLE IF NOT EXISTS events (\n                id INTEGER PRIMARY KEY AUTOINCREMENT,\n                workflow_id INTEGER NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,\n                position INTEGER NOT NULL,\n                name TEXT NOT NULL,\n                action TEXT NOT NULL,\n                selector_type TEXT NOT NULL DEFAULT 'none',\n                selector TEXT NOT NULL DEFAULT '',\n                fallback_selector_type TEXT NOT NULL DEFAULT 'none',\n                fallback_selector TEXT NOT NULL DEFAULT '',\n                value TEXT NOT NULL DEFAULT '',\n                timeout_ms INTEGER NOT NULL DEFAULT 10000,\n                enabled INTEGER NOT NULL DEFAULT 1,\n                continue_on_error INTEGER NOT NULL DEFAULT 0,\n                refresh_on_retry INTEGER NOT NULL DEFAULT 0,\n                data_path TEXT NOT NULL DEFAULT '',\n                UNIQUE(workflow_id, position)\n            );\n            CREATE TABLE IF NOT EXISTS runs (\n                id INTEGER PRIMARY KEY AUTOINCREMENT,\n                workflow_id INTEGER NOT NULL,\n                started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,\n                finished_at TEXT,\n                status TEXT NOT NULL,\n                message TEXT NOT NULL DEFAULT ''\n            );\n            CREATE TABLE IF NOT EXISTS input_rows (\n                id INTEGER PRIMARY KEY AUTOINCREMENT,\n                workflow_id INTEGER NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,\n                position INTEGER NOT NULL,\n                name TEXT NOT NULL,\n                UNIQUE(workflow_id, position)\n            );\n            CREATE TABLE IF NOT EXISTS input_cells (\n                row_id INTEGER NOT NULL REFERENCES input_rows(id) ON DELETE CASCADE,\n                variable_name TEXT NOT NULL,\n                value TEXT NOT NULL DEFAULT '',\n                PRIMARY KEY(row_id, variable_name)\n            );\n            CREATE TABLE IF NOT EXISTS data_schemas (\n                workflow_id INTEGER PRIMARY KEY REFERENCES workflows(id) ON DELETE CASCADE,\n                schema_json TEXT NOT NULL\n            );\n            CREATE TABLE IF NOT EXISTS data_records (\n                id INTEGER PRIMARY KEY AUTOINCREMENT,\n                workflow_id INTEGER NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,\n                position INTEGER NOT NULL,\n                name TEXT NOT NULL,\n                data_json TEXT NOT NULL,\n                UNIQUE(workflow_id, position)\n            );\n            CREATE TABLE IF NOT EXISTS global_data_schema (\n                id INTEGER PRIMARY KEY CHECK(id = 1),\n                schema_json TEXT NOT NULL\n            );\n            CREATE TABLE IF NOT EXISTS global_data_records (\n                id INTEGER PRIMARY KEY AUTOINCREMENT,\n                position INTEGER NOT NULL UNIQUE,\n                name TEXT NOT NULL,\n                enabled INTEGER NOT NULL DEFAULT 1,\n                execution_group TEXT NOT NULL DEFAULT '1',\n                execution_status TEXT NOT NULL DEFAULT 'not_run',\n                data_json TEXT NOT NULL\n            );\n            CREATE TABLE IF NOT EXISTS app_meta (\n                key TEXT PRIMARY KEY,\n                value TEXT NOT NULL\n            );\n            ")
        self.connection.execute("""
            CREATE TABLE IF NOT EXISTS workflow_outline (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL CHECK(kind IN ('flow', 'group')),
                workflow_id INTEGER UNIQUE,
                parent_id INTEGER,
                position INTEGER NOT NULL,
                name TEXT NOT NULL DEFAULT '',
                guard_json TEXT NOT NULL DEFAULT ''
            )
        """)
        outline_columns = {
            row['name'] for row in self.connection.execute('PRAGMA table_info(workflow_outline)')
        }
        if 'guard_json' not in outline_columns:
            self.connection.execute(
                "ALTER TABLE workflow_outline ADD COLUMN guard_json TEXT NOT NULL DEFAULT ''"
            )
        columns = {row['name'] for row in self.connection.execute('PRAGMA table_info(workflows)').fetchall()}
        if 'position' not in columns:
            self.connection.execute('ALTER TABLE workflows ADD COLUMN position INTEGER NOT NULL DEFAULT 0')
        if 'enabled' not in columns:
            self.connection.execute('ALTER TABLE workflows ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1')
        if 'pcl_loop_start' not in columns:
            self.connection.execute('ALTER TABLE workflows ADD COLUMN pcl_loop_start INTEGER NOT NULL DEFAULT 0')
        if 'guard_json' not in columns:
            self.connection.execute("ALTER TABLE workflows ADD COLUMN guard_json TEXT NOT NULL DEFAULT ''")
        self._migrate_workflow_name_uniqueness()
        event_columns = {row['name'] for row in self.connection.execute('PRAGMA table_info(events)').fetchall()}
        if 'data_path' not in event_columns:
            self.connection.execute("ALTER TABLE events ADD COLUMN data_path TEXT NOT NULL DEFAULT ''")
        if 'refresh_on_retry' not in event_columns:
            self.connection.execute('ALTER TABLE events ADD COLUMN refresh_on_retry INTEGER NOT NULL DEFAULT 0')
        if 'failure_action' not in event_columns:
            self.connection.execute("ALTER TABLE events ADD COLUMN failure_action TEXT NOT NULL DEFAULT 'none'")
        if 'failure_target' not in event_columns:
            self.connection.execute("ALTER TABLE events ADD COLUMN failure_target TEXT NOT NULL DEFAULT ''")
        self.connection.execute('UPDATE events SET refresh_on_retry=0 WHERE refresh_on_retry<>0')
        if 'fallback_selector_type' not in event_columns:
            self.connection.execute("ALTER TABLE events ADD COLUMN fallback_selector_type TEXT NOT NULL DEFAULT 'none'")
        if 'fallback_selector' not in event_columns:
            self.connection.execute("ALTER TABLE events ADD COLUMN fallback_selector TEXT NOT NULL DEFAULT ''")
        if 'iframe_path' not in event_columns:
            self.connection.execute("ALTER TABLE events ADD COLUMN iframe_path TEXT NOT NULL DEFAULT ''")
        if 'guard_json' not in event_columns:
            self.connection.execute("ALTER TABLE events ADD COLUMN guard_json TEXT NOT NULL DEFAULT ''")
        if 'retry_count' not in event_columns:
            self.connection.execute('ALTER TABLE events ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0')
        if 'retry_interval_ms' not in event_columns:
            self.connection.execute('ALTER TABLE events ADD COLUMN retry_interval_ms INTEGER NOT NULL DEFAULT 0')
        if 'success_json' not in event_columns:
            self.connection.execute("ALTER TABLE events ADD COLUMN success_json TEXT NOT NULL DEFAULT ''")
            # 旧 click の value に保存されていた成功確認を独立列へ移行する。
            for row in self.connection.execute("SELECT id, value FROM events WHERE action='click' AND value<>''"):
                try:
                    success = json.loads(row['value'])
                except (TypeError, json.JSONDecodeError):
                    continue
                if isinstance(success, dict) and success.get('condition'):
                    self.connection.execute(
                        "UPDATE events SET success_json=?, value='' WHERE id=?",
                        (json.dumps(success, ensure_ascii=False), row['id']),
                    )
        if 'scroll_json' not in event_columns:
            self.connection.execute("ALTER TABLE events ADD COLUMN scroll_json TEXT NOT NULL DEFAULT ''")
        # 旧待機操作を統合後の形式へ移行し、画面と保存形式を一つにそろえる。
        self.connection.execute("UPDATE events SET action='wait', value='hidden' WHERE action='wait_hidden'")
        self.connection.execute(
            "UPDATE events SET value='operable' "
            "WHERE action='wait' AND value IN ('clickable', 'editable', 'selectable')"
        )
        global_record_columns = {row['name'] for row in self.connection.execute('PRAGMA table_info(global_data_records)').fetchall()}
        if 'enabled' not in global_record_columns:
            self.connection.execute('ALTER TABLE global_data_records ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1')
        if 'execution_status' not in global_record_columns:
            self.connection.execute("ALTER TABLE global_data_records ADD COLUMN execution_status TEXT NOT NULL DEFAULT 'not_run'")
        if 'execution_group' not in global_record_columns:
            self.connection.execute("ALTER TABLE global_data_records ADD COLUMN execution_group TEXT NOT NULL DEFAULT '1'")
        if 'summary' not in global_record_columns:
            self.connection.execute("ALTER TABLE global_data_records ADD COLUMN summary TEXT NOT NULL DEFAULT ''")
        # Persist the documented first-run default instead of relying on a missing
        # metadata row being interpreted as true by each caller.
        self.connection.execute(
            "INSERT OR IGNORE INTO app_meta(key, value) VALUES ('browser_visible', '1')"
        )
        self._initialize_workflow_positions()
        self._initialize_workflow_outline()
        self._migrate_global_data()
        self._migrate_combined_event_groups()
        self.connection.commit()

    def _migrate_workflow_name_uniqueness(self) -> None:
        """旧DBのFlow名グローバルUNIQUE制約を除去し、階層単位の検証へ移行する。"""
        unique_name_index = any(
            bool(index['unique']) and [
                column['name'] for column in self.connection.execute(
                    f'PRAGMA index_info("{index["name"]}")'
                )
            ] == ['name']
            for index in self.connection.execute('PRAGMA index_list(workflows)').fetchall()
        )
        if not unique_name_index:
            return
        # SQLiteの自動UNIQUE索引は削除できないため、データを維持したまま表を再構築する。
        self.connection.executescript("""
            CREATE TABLE workflows_without_global_name_unique (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                position INTEGER NOT NULL DEFAULT 0,
                enabled INTEGER NOT NULL DEFAULT 1,
                pcl_loop_start INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                guard_json TEXT NOT NULL DEFAULT ''
            );
            INSERT INTO workflows_without_global_name_unique
                (id, name, description, position, enabled, pcl_loop_start,
                 created_at, updated_at, guard_json)
            SELECT id, name, description, position, enabled, pcl_loop_start,
                   created_at, updated_at, guard_json
            FROM workflows;
            DROP TABLE workflows;
            ALTER TABLE workflows_without_global_name_unique RENAME TO workflows;
        """)

    def _migrate_combined_event_groups(self) -> None:
        """旧形式で完全に重なるループと再試行を一つのグループへ統合する。"""
        for workflow in self.list_workflows():
            changed = True
            while changed:
                changed = False
                rows = [dict(row) for row in self.list_events(workflow['id'])]
                for outer_index, outer in enumerate(rows):
                    if outer['action'] not in {'loop_start', 'retry_start'}:
                        continue
                    outer_end_action = 'loop_end' if outer['action'] == 'loop_start' else 'retry_end'
                    depth = 0
                    outer_end = None
                    for index in range(outer_index + 1, len(rows)):
                        if rows[index]['action'] == outer['action']:
                            depth += 1
                        elif rows[index]['action'] == outer_end_action:
                            if depth == 0:
                                outer_end = index
                                break
                            depth -= 1
                    if outer_end is None or outer_end - outer_index < 3:
                        continue
                    inner = rows[outer_index + 1]
                    expected_inner = 'retry_start' if outer['action'] == 'loop_start' else 'loop_start'
                    expected_inner_end = 'retry_end' if expected_inner == 'retry_start' else 'loop_end'
                    if inner['action'] != expected_inner or rows[outer_end - 1]['action'] != expected_inner_end:
                        continue
                    loop = outer if outer['action'] == 'loop_start' else inner
                    retry = inner if inner['action'] == 'retry_start' else outer
                    self.connection.execute('UPDATE events SET action=?, data_path=?, value=?, refresh_on_retry=0, enabled=? WHERE id=?',
                                            ('group_start', loop['data_path'], retry['value'], int(bool(loop['enabled']) and bool(retry['enabled'])), outer['id']))
                    self.connection.execute('UPDATE events SET action=?, name=? WHERE id=?', ('group_end', outer['name'], rows[outer_end]['id']))
                    self.connection.execute('DELETE FROM events WHERE id IN (?, ?)', (inner['id'], rows[outer_end - 1]['id']))
                    self._normalize_positions(workflow['id'])
                    changed = True
                    break

    @classmethod
    def _merge_schemas(cls, base: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
        """同名で互換性のあるフィールドを統合し、型競合時は先の型を維持する。"""
        result = json.loads(json.dumps(base, ensure_ascii=False))
        if result.get('type') not in ('object', 'list') or incoming.get('type') not in ('object', 'list'):
            return result
        children = result.setdefault('children', [])
        by_name = {child.get('name'): child for child in children}
        for child in incoming.get('children', []):
            existing = by_name.get(child.get('name'))
            if existing is None:
                copied = json.loads(json.dumps(child, ensure_ascii=False))
                children.append(copied)
                by_name[copied.get('name')] = copied
            elif existing.get('type') == child.get('type') and child.get('type') in ('object', 'list'):
                merged = cls._merge_schemas(existing, child)
                existing.clear()
                existing.update(merged)
        return result

    @classmethod
    def _normalize_for_schema(cls, schema: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
        result = dict(data)
        for child in schema.get('children', []):
            name, kind = (child['name'], child['type'])
            if kind == 'list':
                current = result.get(name)
                result[name] = [cls._normalize_for_schema(child, item) if isinstance(item, dict) else {} for item in current] if isinstance(current, list) else []
            elif kind == 'object':
                current = result.get(name)
                result[name] = cls._normalize_for_schema(child, current) if isinstance(current, dict) else cls._normalize_for_schema(child, {})
            elif name not in result:
                result[name] = 0 if kind == 'number' else False if kind == 'boolean' else ''
        return result

    def _migrate_global_data(self) -> None:
        migrated = self.connection.execute("SELECT value FROM app_meta WHERE key='global_data_migrated_v1'").fetchone()
        if migrated is not None:
            return
        schema_rows = self.connection.execute('SELECT schema_json FROM data_schemas ORDER BY workflow_id').fetchall()
        schema: dict[str, Any] | None = None
        if schema_rows:
            schema = json.loads(schema_rows[0]['schema_json'])
            for row in schema_rows[1:]:
                schema = self._merge_schemas(schema, json.loads(row['schema_json']))
            self.connection.execute('INSERT OR REPLACE INTO global_data_schema(id, schema_json) VALUES (1, ?)', (json.dumps(schema, ensure_ascii=False),))
        record_rows = self.connection.execute('SELECT name, data_json FROM data_records ORDER BY workflow_id, position, id').fetchall()
        existing_names: set[str] = set()
        for position, row in enumerate(record_rows, 1):
            name = row['name']
            candidate = name
            suffix = 2
            while candidate in existing_names:
                candidate = f'{name} ({suffix})'
                suffix += 1
            existing_names.add(candidate)
            data = json.loads(row['data_json'])
            if schema is not None and isinstance(data, dict):
                data = self._normalize_for_schema(schema, data)
            self.connection.execute('INSERT INTO global_data_records(position, name, data_json) VALUES (?, ?, ?)', (position, candidate, json.dumps(data, ensure_ascii=False)))
        self.connection.execute("INSERT INTO app_meta(key, value) VALUES ('global_data_migrated_v1', '1')")

    def _initialize_workflow_positions(self) -> None:
        rows = self.connection.execute('SELECT id, position FROM workflows ORDER BY position, id').fetchall()
        if not rows:
            return
        positions = [row['position'] for row in rows]
        if any((position <= 0 for position in positions)) or len(set(positions)) != len(positions):
            for index, row in enumerate(rows, 1):
                self.connection.execute('UPDATE workflows SET position=? WHERE id=?', (index, row['id']))

    def _initialize_workflow_outline(self) -> None:
        """既存 Flow を保持したまま、管理用ツリーへ未登録行だけを追加する。"""
        registered = {
            int(row['workflow_id']) for row in self.connection.execute(
                "SELECT workflow_id FROM workflow_outline WHERE kind='flow' AND workflow_id IS NOT NULL"
            )
        }
        next_position = int(self.connection.execute(
            'SELECT COALESCE(MAX(position), 0) + 1 FROM workflow_outline WHERE parent_id IS NULL'
        ).fetchone()[0])
        for workflow in self.list_workflows():
            if int(workflow['id']) in registered:
                continue
            self.connection.execute(
                "INSERT INTO workflow_outline(kind, workflow_id, parent_id, position) VALUES ('flow', ?, NULL, ?)",
                (workflow['id'], next_position),
            )
            next_position += 1

    def list_workflow_outline(self) -> list[sqlite3.Row]:
        return self.connection.execute(
            'SELECT * FROM workflow_outline ORDER BY parent_id, position, id'
        ).fetchall()

    def workflow_names_at_level(
        self, parent_id: int | None, *, exclude_workflow_id: int | None = None,
    ) -> list[str]:
        """指定階層に直接配置されたFlow名を返す。"""
        query = (
            "SELECT w.name FROM workflow_outline o "
            "JOIN workflows w ON w.id=o.workflow_id "
            "WHERE o.kind='flow' AND o.parent_id IS ?"
        )
        params: list[Any] = [parent_id]
        if exclude_workflow_id is not None:
            query += ' AND w.id<>?'
            params.append(exclude_workflow_id)
        return [str(row['name']) for row in self.connection.execute(query, params)]

    def workflow_parent_id(self, workflow_id: int) -> int | None:
        row = self.connection.execute(
            "SELECT parent_id FROM workflow_outline WHERE kind='flow' AND workflow_id=?",
            (workflow_id,),
        ).fetchone()
        return row['parent_id'] if row is not None else None

    def _validate_workflow_name_at_level(
        self, name: str, parent_id: int | None, *, exclude_workflow_id: int | None = None,
    ) -> None:
        if name.strip() in self.workflow_names_at_level(
            parent_id, exclude_workflow_id=exclude_workflow_id,
        ):
            raise ValueError('flow.name_duplicate')

    def add_workflow_group(self, name: str, guard: dict[str, Any] | None=None) -> int:
        position = self.connection.execute(
            'SELECT COALESCE(MAX(position), 0) + 1 FROM workflow_outline WHERE parent_id IS NULL'
        ).fetchone()[0]
        cursor = self.connection.execute(
            "INSERT INTO workflow_outline(kind, parent_id, position, name, guard_json) "
            "VALUES ('group', NULL, ?, ?, ?)",
            (position, name.strip(), json.dumps(decode_guard(guard), ensure_ascii=False)),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def update_workflow_group(
        self, group_id: int, name: str, guard: dict[str, Any] | None=None,
    ) -> None:
        self.connection.execute(
            "UPDATE workflow_outline SET name=?, guard_json=? WHERE id=? AND kind='group'",
            (name.strip(), json.dumps(decode_guard(guard), ensure_ascii=False), group_id),
        )
        self.connection.commit()

    def get_workflow_guards(self, workflow_id: int) -> list[dict[str, Any]]:
        """外側のグループから Flow 自身まで、適用する条件を順番に返す。"""
        node = self.connection.execute(
            "SELECT parent_id FROM workflow_outline WHERE kind='flow' AND workflow_id=?",
            (workflow_id,),
        ).fetchone()
        group_guards: list[dict[str, Any]] = []
        parent_id = node['parent_id'] if node is not None else None
        while parent_id is not None:
            group = self.connection.execute(
                "SELECT parent_id, guard_json FROM workflow_outline WHERE id=? AND kind='group'",
                (parent_id,),
            ).fetchone()
            if group is None:
                break
            group_guards.append(decode_guard(group['guard_json']))
            parent_id = group['parent_id']
        group_guards.reverse()
        workflow = self.connection.execute(
            'SELECT guard_json FROM workflows WHERE id=?', (workflow_id,)
        ).fetchone()
        if workflow is not None:
            group_guards.append(decode_guard(workflow['guard_json']))
        return group_guards

    def reorder_workflow_outline(self, nodes: list[tuple[int, int | None, int]]) -> None:
        """管理ツリーと実行用 Flow 順序を同時に保存する。"""
        existing = {int(row['id']) for row in self.list_workflow_outline()}
        if {node_id for node_id, _parent_id, _position in nodes} != existing:
            raise ValueError('flow.order_mismatch')
        outline = {int(row['id']): dict(row) for row in self.list_workflow_outline()}
        workflow_names = {
            int(row['id']): str(row['name']) for row in self.list_workflows()
        }
        sibling_names: set[tuple[int | None, str]] = set()
        for node_id, parent_id, _position in nodes:
            row = outline[node_id]
            if row['kind'] != 'flow':
                continue
            key = (parent_id, workflow_names[int(row['workflow_id'])])
            if key in sibling_names:
                raise ValueError('flow.name_duplicate')
            sibling_names.add(key)
        with self.connection:
            for node_id, parent_id, position in nodes:
                self.connection.execute(
                    'UPDATE workflow_outline SET parent_id=?, position=? WHERE id=?',
                    (parent_id, position, node_id),
                )
            flow_ids = [
                int(row['workflow_id']) for node_id, _parent_id, _position in nodes
                for row in self.connection.execute(
                    "SELECT workflow_id FROM workflow_outline WHERE id=? AND kind='flow'", (node_id,)
                ).fetchall()
            ]
            for position, workflow_id in enumerate(flow_ids, 1):
                self.connection.execute('UPDATE workflows SET position=? WHERE id=?', (position, workflow_id))

    def delete_workflow_group(self, group_id: int) -> None:
        """グループ配下の Flow と子グループをまとめて削除する。"""
        rows = [dict(row) for row in self.list_workflow_outline()]
        by_id = {int(row['id']): row for row in rows}
        children: dict[int | None, list[dict[str, Any]]] = {}
        for row in rows:
            children.setdefault(row['parent_id'], []).append(row)
        node_ids: list[int] = []
        workflow_ids: list[int] = []

        def collect(node_id: int) -> None:
            node = by_id.get(node_id)
            if node is None:
                return
            node_ids.append(node_id)
            if node['kind'] == 'flow' and node['workflow_id'] is not None:
                workflow_ids.append(int(node['workflow_id']))
            for child in children.get(node_id, []):
                collect(int(child['id']))

        collect(group_id)
        for workflow_id in workflow_ids:
            self.delete_workflow(workflow_id)
        with self.connection:
            self.connection.executemany(
                'DELETE FROM workflow_outline WHERE id=?', ((node_id,) for node_id in reversed(node_ids))
            )
            self._normalize_workflow_positions()

    def list_workflows(self) -> list[sqlite3.Row]:
        return self.connection.execute('SELECT * FROM workflows ORDER BY position, id').fetchall()

    def add_workflow(
        self, name: str, description: str='', parent_id: int | None=None,
    ) -> int:
        self._validate_workflow_name_at_level(name, parent_id)
        position = self.connection.execute('SELECT COALESCE(MAX(position), 0) + 1 FROM workflows').fetchone()[0]
        cursor = self.connection.execute('INSERT INTO workflows(name, description, position) VALUES (?, ?, ?)', (name.strip(), description.strip(), position))
        outline_position = self.connection.execute(
            'SELECT COALESCE(MAX(position), 0) + 1 FROM workflow_outline WHERE parent_id IS ?',
            (parent_id,),
        ).fetchone()[0]
        self.connection.execute(
            "INSERT INTO workflow_outline(kind, workflow_id, parent_id, position) VALUES ('flow', ?, ?, ?)",
            (cursor.lastrowid, parent_id, outline_position),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def update_workflow(self, workflow_id: int, name: str, description: str) -> None:
        self._validate_workflow_name_at_level(
            name, self.workflow_parent_id(workflow_id), exclude_workflow_id=workflow_id,
        )
        self.connection.execute('UPDATE workflows SET name=?, description=?, updated_at=CURRENT_TIMESTAMP WHERE id=?', (name.strip(), description.strip(), workflow_id))
        self.connection.commit()

    def set_workflow_guard(self, workflow_id: int, guard: dict[str, Any]) -> None:
        payload = json.dumps(decode_guard(guard), ensure_ascii=False)
        self.connection.execute('UPDATE workflows SET guard_json=?, updated_at=CURRENT_TIMESTAMP WHERE id=?', (payload, workflow_id))
        self.connection.commit()

    def set_workflow_enabled(self, workflow_id: int, enabled: bool) -> None:
        self.connection.execute('UPDATE workflows SET enabled=?, updated_at=CURRENT_TIMESTAMP WHERE id=?', (int(enabled), workflow_id))
        self.connection.commit()

    def set_pcl_loop_start(self, workflow_id: int | None) -> None:
        with self.connection:
            self.connection.execute('UPDATE workflows SET pcl_loop_start=0')
            if workflow_id is not None:
                self.connection.execute('UPDATE workflows SET pcl_loop_start=1 WHERE id=?', (workflow_id,))

    def delete_workflow(self, workflow_id: int) -> None:
        with self.connection:
            row_ids = [row['id'] for row in self.connection.execute('SELECT id FROM input_rows WHERE workflow_id=?', (workflow_id,)).fetchall()]
            for row_id in row_ids:
                self.connection.execute('DELETE FROM input_cells WHERE row_id=?', (row_id,))
            self.connection.execute('DELETE FROM input_rows WHERE workflow_id=?', (workflow_id,))
            self.connection.execute('DELETE FROM data_records WHERE workflow_id=?', (workflow_id,))
            self.connection.execute('DELETE FROM data_schemas WHERE workflow_id=?', (workflow_id,))
            self.connection.execute('DELETE FROM events WHERE workflow_id=?', (workflow_id,))
            self.connection.execute('DELETE FROM workflows WHERE id=?', (workflow_id,))
            self.connection.execute('DELETE FROM workflow_outline WHERE workflow_id=?', (workflow_id,))
            self._normalize_workflow_positions()

    def reorder_workflows(self, workflow_ids: list[int]) -> None:
        existing = {row['id'] for row in self.list_workflows()}
        if set(workflow_ids) != existing or len(workflow_ids) != len(existing):
            raise ValueError('flow.order_mismatch')
        with self.connection:
            for position, workflow_id in enumerate(workflow_ids, 1):
                self.connection.execute('UPDATE workflows SET position=? WHERE id=?', (position, workflow_id))

    def _normalize_workflow_positions(self) -> None:
        for position, row in enumerate(self.list_workflows(), 1):
            self.connection.execute('UPDATE workflows SET position=? WHERE id=?', (position, row['id']))

    def list_events(self, workflow_id: int) -> list[sqlite3.Row]:
        return self.connection.execute('SELECT * FROM events WHERE workflow_id=? ORDER BY position', (workflow_id,)).fetchall()

    def add_event(self, workflow_id: int, data: dict[str, Any]) -> int:
        position = self.connection.execute('SELECT COALESCE(MAX(position), 0) + 1 FROM events WHERE workflow_id=?', (workflow_id,)).fetchone()[0]
        guard_json = json.dumps(decode_guard(data.get('guard', data.get('guard_json', ''))), ensure_ascii=False)
        cursor = self.connection.execute('INSERT INTO events\n               (workflow_id, position, name, action, selector_type, selector,\n                fallback_selector_type, fallback_selector, iframe_path, value,\n                timeout_ms, enabled, continue_on_error, refresh_on_retry, failure_action, failure_target, data_path, guard_json, retry_count, retry_interval_ms, success_json, scroll_json)\n               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', (workflow_id, position, data['name'], data['action'], data['selector_type'], data['selector'], data.get('fallback_selector_type', 'none'), data.get('fallback_selector', ''), data.get('iframe_path', ''), data['value'], data['timeout_ms'], data['enabled'], data['continue_on_error'], 0, data.get('failure_action', 'none'), data.get('failure_target', ''), data.get('data_path', ''), guard_json, data.get('retry_count', 0), data.get('retry_interval_ms', 0), data.get('success_json', ''), data.get('scroll_json', '')))
        self.connection.commit()
        return int(cursor.lastrowid)

    def update_event(self, event_id: int, data: dict[str, Any]) -> None:
        guard_json = json.dumps(decode_guard(data.get('guard', data.get('guard_json', ''))), ensure_ascii=False)
        self.connection.execute('UPDATE events SET name=?, action=?, selector_type=?, selector=?,\n               fallback_selector_type=?, fallback_selector=?, iframe_path=?, value=?,\n               timeout_ms=?, enabled=?, continue_on_error=?, refresh_on_retry=0, failure_action=?, failure_target=?, data_path=?, guard_json=?, retry_count=?, retry_interval_ms=?, success_json=?, scroll_json=? WHERE id=?', (data['name'], data['action'], data['selector_type'], data['selector'], data.get('fallback_selector_type', 'none'), data.get('fallback_selector', ''), data.get('iframe_path', ''), data['value'], data['timeout_ms'], data['enabled'], data['continue_on_error'], data.get('failure_action', 'none'), data.get('failure_target', ''), data.get('data_path', ''), guard_json, data.get('retry_count', 0), data.get('retry_interval_ms', 0), data.get('success_json', ''), data.get('scroll_json', ''), event_id))
        self.connection.commit()

    def set_event_enabled(self, event_id: int, enabled: bool) -> None:
        self.connection.execute('UPDATE events SET enabled=? WHERE id=?', (int(enabled), event_id))
        self.connection.commit()

    def delete_events(self, event_ids: list[int], workflow_id: int) -> None:
        unique_ids = list(dict.fromkeys(event_ids))
        with self.connection:
            self.connection.executemany('DELETE FROM events WHERE id=? AND workflow_id=?', ((event_id, workflow_id) for event_id in unique_ids))
            self._normalize_positions(workflow_id)

    def reorder_events(self, workflow_id: int, event_ids: list[int]) -> None:
        existing = {row['id'] for row in self.list_events(workflow_id)}
        if set(event_ids) != existing or len(event_ids) != len(existing):
            raise ValueError('event.order_mismatch')
        with self.connection:
            for index, current_id in enumerate(event_ids, 1):
                self.connection.execute('UPDATE events SET position=? WHERE id=?', (-index, current_id))
            for index, current_id in enumerate(event_ids, 1):
                self.connection.execute('UPDATE events SET position=? WHERE id=?', (index, current_id))

    def _normalize_positions(self, workflow_id: int) -> None:
        ids = [row['id'] for row in self.list_events(workflow_id)]
        for index, event_id in enumerate(ids, 1):
            self.connection.execute('UPDATE events SET position=? WHERE id=?', (index, event_id))

    @classmethod
    def _events_to_group_items(cls, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """境界イベントの配列を、JSON 用の入れ子グループへ変換する。"""
        items: list[dict[str, Any]] = []
        index = 0
        while index < len(events):
            event = events[index]
            action = event.get('action')
            if action not in {'loop_start', 'retry_start', 'group_start'}:
                if action not in {'loop_end', 'retry_end', 'group_end'}:
                    items.append(event)
                index += 1
                continue
            end_action = 'loop_end' if action == 'loop_start' else 'retry_end' if action == 'retry_start' else 'group_end'
            depth = 0
            end = None
            for candidate in range(index + 1, len(events)):
                candidate_action = events[candidate].get('action')
                if candidate_action == action:
                    depth += 1
                elif candidate_action == end_action:
                    if depth == 0:
                        end = candidate
                        break
                    depth -= 1
            if end is None:
                items.append(event)
                index += 1
                continue
            group = {'type': 'group', 'name': event.get('name', ''),
                     'loop_enabled': action == 'loop_start' or (action == 'group_start' and bool(str(event.get('data_path', '')).strip())),
                     'retry_enabled': action == 'retry_start' or (action == 'group_start' and bool(str(event.get('value', '')).strip())),
                     'enabled': event.get('enabled', 1), 'guard': event.get('guard', {}),
                     'data_path': event.get('data_path', ''),
                     'retry_count': event.get('value', '') if action in {'retry_start', 'group_start'} else '',
                     'retry_interval_ms': event.get('retry_interval_ms', 0),
                     'timeout_ms': event.get('timeout_ms', 600000),
                     'events': cls._events_to_group_items(events[index + 1:end])}
            items.append(group)
            index = end + 1
        return items

    @classmethod
    def _group_items_to_events(cls, items: list[Any]) -> list[Any]:
        """新版 JSON のグループを従来の実行可能な境界配列へ展開する。"""
        events: list[Any] = []
        for item in items:
            if not isinstance(item, dict) or item.get('type') != 'group':
                events.append(item)
                continue
            group_type = item.get('group_type')
            if not isinstance(item.get('events', []), list):
                events.append(item)
                continue
            loop_enabled = bool(item.get('loop_enabled', group_type == 'loop'))
            retry_enabled = bool(item.get('retry_enabled', group_type == 'retry'))
            start_action = 'group_start'
            base = {'name': str(item.get('name', '')), 'action': start_action,
                    'selector_type': 'none', 'selector': '', 'fallback_selector_type': 'none',
                    'fallback_selector': '', 'value': str(item.get('retry_count', '')) if retry_enabled else '',
                    'timeout_ms': int(item.get('timeout_ms', 600000)),
                    'retry_count': int(item.get('retry_count', 0)) if retry_enabled else 0,
                    'retry_interval_ms': int(item.get('retry_interval_ms', 0)) if retry_enabled else 0,
                    'enabled': int(bool(item.get('enabled', 1))),
                    'continue_on_error': 0, 'refresh_on_retry': 0,
                    'data_path': str(item.get('data_path', '')) if loop_enabled else '',
                    'guard': item.get('guard')}
            events.append(base)
            events.extend(cls._group_items_to_events(item.get('events', [])))
            end = dict(base)
            end.update(action='group_end', value='', data_path='', refresh_on_retry=0, guard={})
            events.append(end)
        return events

    def export_workflow_collection(self, path: Path) -> None:
        workflows: list[dict[str, Any]] = []
        for workflow in self.list_workflows():
            events = [dict(row) for row in self.list_events(workflow['id'])]
            for event in events:
                event.pop('id', None)
                event.pop('workflow_id', None)
                event.pop('refresh_on_retry', None)
                event['guard'] = decode_guard(event.pop('guard_json', ''))
            workflows.append({'key': f'workflow:{workflow["id"]}', 'name': workflow['name'], 'description': workflow['description'], 'position': workflow['position'], 'enabled': int(workflow['enabled']), 'pcl_loop_start': int(workflow['pcl_loop_start']), 'guard': decode_guard(workflow['guard_json']), 'events': self._events_to_group_items(events)})
        workflow_keys = {
            int(row['id']): f'workflow:{row["id"]}' for row in self.list_workflows()
        }
        outline_rows = [dict(row) for row in self.list_workflow_outline()]
        outline = [
            {
                'key': f'node:{row["id"]}', 'kind': row['kind'],
                'workflow': workflow_keys.get(int(row['workflow_id'])) if row['workflow_id'] is not None else None,
                'name': row['name'],
                'guard': decode_guard(row.get('guard_json', '')),
                'parent': f'node:{row["parent_id"]}' if row['parent_id'] is not None else None,
                'position': int(row['position']),
            }
            for row in outline_rows
        ]
        payload = {
            'version': 3, 'type': 'web-flow-collection',
            'browser_visible': self.get_browser_visible(),
            'workflows': workflows, 'workflow_outline': outline,
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')

    def import_workflow_collection(self, path: Path, allowed_actions: tuple[str, ...], allowed_selector_types: tuple[str, ...]) -> int:
        # 検証がすべて完了するまで既存データを変更しない。
        # 不正なファイルで現在のフローが消えることを防ぐためである。
        try:
            payload = json.loads(path.read_text(encoding='utf-8-sig'))
        except json.JSONDecodeError as error:
            raise ValueError(f'{tr("flow.json_format_error_prefix")}{error}') from error
        if not isinstance(payload, dict) or payload.get('version') not in {1, 2, 3}:
            raise ValueError('flow.json_version_unsupported')
        version = int(payload['version'])
        workflows = payload.get('workflows')
        outline_payload = payload.get('workflow_outline')
        if payload.get('type') not in {'web-flow-collection', 'salesforce-flow-collection'} or not isinstance(workflows, list):
            raise ValueError('flow.json_type_invalid')
        browser_visible = payload.get('browser_visible', True)
        if not isinstance(browser_visible, bool):
            raise ValueError('flow.browser_visible_invalid')
        normalized: list[dict[str, Any]] = []
        names: set[str] = set()
        workflow_keys: set[str] = set()
        for workflow_index, workflow in enumerate(workflows, 1):
            if not isinstance(workflow, dict) or not isinstance(workflow.get('name'), str) or (not workflow['name'].strip()):
                raise ValueError(f'{tr("validation.ordinal_prefix")}{workflow_index}{tr("flow.invalid_name_suffix")}')
            name = workflow['name'].strip()
            if version < 3 and name in names:
                raise ValueError(f'{tr("flow.duplicate_name_prefix")}{name}')
            names.add(name)
            workflow_key = (
                str(workflow.get('key', '')).strip() if version >= 3 else name
            )
            if not workflow_key or workflow_key in workflow_keys:
                raise ValueError(f'{tr("flow.duplicate_name_prefix")}{name}')
            workflow_keys.add(workflow_key)
            events = workflow.get('events')
            if not isinstance(events, list):
                raise ValueError(f'{tr("flow.name_quote_prefix")}{name}{tr("flow.events_array_suffix")}')
            events = self._group_items_to_events(events)
            checked_events: list[dict[str, Any]] = []
            required = ('name', 'action', 'selector_type', 'selector', 'value')
            for event_index, event in enumerate(events, 1):
                if not isinstance(event, dict) or any((not isinstance(event.get(key), str) for key in required)):
                    raise ValueError(
                        f'{tr("flow.name_quote_prefix")}{name}{tr("validation.item_ordinal_infix")}'
                        f'{event_index}{tr("flow.event_format_invalid_suffix")}'
                    )
                if event['action'] == 'wait_hidden':
                    event = dict(event)
                    event['action'] = 'wait'
                    event['value'] = 'hidden'
                elif event['action'] == 'wait' and event['value'] in {'clickable', 'editable', 'selectable'}:
                    event = dict(event)
                    event['value'] = 'operable'
                if event['action'] not in allowed_actions:
                    raise ValueError(
                        f'{tr("flow.name_quote_prefix")}{name}{tr("flow.unsupported_action_infix")}{event["action"]}'
                    )
                if event['selector_type'] not in allowed_selector_types:
                    raise ValueError(
                        f'{tr("flow.name_quote_prefix")}{name}{tr("flow.unsupported_selector_infix")}'
                        f'{event["selector_type"]}'
                    )
                try:
                    timeout = int(event.get('timeout_ms', 10000))
                except (TypeError, ValueError) as error:
                    raise ValueError(
                        f'{tr("flow.name_quote_prefix")}{name}{tr("validation.item_ordinal_infix")}'
                        f'{event_index}{tr("flow.event_timeout_invalid_suffix")}'
                    ) from error
                if timeout <= 0:
                    raise ValueError(
                        f'{tr("flow.name_quote_prefix")}{name}{tr("validation.item_ordinal_infix")}'
                        f'{event_index}{tr("flow.event_timeout_nonpositive_suffix")}'
                    )
                failure_action = str(event.get('failure_action', 'none'))
                if failure_action not in {'none', 'refresh', 'goto'}:
                    failure_action = 'none'
                try:
                    retry_count = int(event.get('retry_count', 0))
                    retry_interval_ms = int(event.get('retry_interval_ms', 0))
                    if retry_count < 0 or retry_interval_ms < 0:
                        raise ValueError
                except (TypeError, ValueError) as error:
                    raise ValueError(
                        f'{tr("flow.name_quote_prefix")}{name}{tr("validation.item_ordinal_infix")}'
                        f'{event_index}{tr("event.retry_values_invalid")}'
                    ) from error
                checked_events.append({'name': event['name'], 'action': event['action'], 'selector_type': event['selector_type'], 'selector': event['selector'], 'fallback_selector_type': str(event.get('fallback_selector_type', 'none')), 'fallback_selector': str(event.get('fallback_selector', '')), 'iframe_path': str(event.get('iframe_path', '')), 'value': event['value'], 'success_json': str(event.get('success_json', '')), 'scroll_json': str(event.get('scroll_json', '')), 'timeout_ms': timeout, 'enabled': int(bool(event.get('enabled', 1))), 'continue_on_error': int(bool(event.get('continue_on_error', 0))), 'refresh_on_retry': 0, 'failure_action': failure_action, 'failure_target': str(event.get('failure_target', '')), 'data_path': str(event.get('data_path', '')), 'retry_count': retry_count, 'retry_interval_ms': retry_interval_ms, 'guard': decode_guard(event.get('guard'))})
            normalized.append({'key': workflow_key, 'name': name, 'description': str(workflow.get('description', '')), 'enabled': int(bool(workflow.get('enabled', 1))), 'events': checked_events, 'pcl_loop_start': int(bool(workflow.get('pcl_loop_start', 0))), 'guard': decode_guard(workflow.get('guard'))})
        # Flow名は同じ親ノードに直接配置されるものだけを重複不可とする。
        parent_by_workflow = {
            str(item.get('workflow', '')): (
                str(item.get('parent')) if item.get('parent') is not None else None
            )
            for item in outline_payload or []
            if isinstance(item, dict) and item.get('kind') == 'flow'
        } if isinstance(outline_payload, list) else {}
        level_names: set[tuple[str | None, str]] = set()
        for workflow in normalized:
            level_key = (parent_by_workflow.get(workflow['key']), workflow['name'])
            if level_key in level_names:
                raise ValueError(f'{tr("flow.duplicate_name_prefix")}{workflow["name"]}')
            level_names.add(level_key)
        if sum((workflow['pcl_loop_start'] for workflow in normalized)) > 1:
            raise ValueError('flow.multiple_data_loop_starts')
        with self.connection:
            self.connection.execute('DELETE FROM runs')
            self.connection.execute('DELETE FROM events')
            self.connection.execute('DELETE FROM workflow_outline')
            self.connection.execute('DELETE FROM workflows')
            self.connection.execute("INSERT INTO app_meta(key, value) VALUES ('browser_visible', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", ('1' if browser_visible else '0',))
            imported_workflow_ids: dict[str, int] = {}
            for workflow_position, workflow in enumerate(normalized, 1):
                cursor = self.connection.execute('INSERT INTO workflows\n                       (name, description, position, enabled, pcl_loop_start, guard_json)\n                       VALUES (?, ?, ?, ?, ?, ?)', (workflow['name'], workflow['description'], workflow_position, workflow['enabled'], workflow['pcl_loop_start'], json.dumps(workflow['guard'], ensure_ascii=False)))
                workflow_id = int(cursor.lastrowid)
                imported_workflow_ids[str(workflow['key'])] = workflow_id
                for event_position, event in enumerate(workflow['events'], 1):
                    event_cursor = self.connection.execute('INSERT INTO events\n                           (workflow_id, position, name, action, selector_type, selector,\n                            fallback_selector_type, fallback_selector, iframe_path, value,\n                            timeout_ms, enabled, continue_on_error, refresh_on_retry, data_path, guard_json)\n                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', (workflow_id, event_position, event['name'], event['action'], event['selector_type'], event['selector'], event['fallback_selector_type'], event['fallback_selector'], event['iframe_path'], event['value'], event['timeout_ms'], event['enabled'], event['continue_on_error'], 0, event['data_path'], json.dumps(event['guard'], ensure_ascii=False)))
                    self.connection.execute('UPDATE events SET failure_action=?, failure_target=?, retry_count=?, retry_interval_ms=?, success_json=?, scroll_json=? WHERE id=?', (event['failure_action'], event['failure_target'], event['retry_count'], event['retry_interval_ms'], event['success_json'], event['scroll_json'], event_cursor.lastrowid))
            # 新形式の JSON では管理用グループも復元する。旧形式は従来どおり平坦表示にする。
            if isinstance(outline_payload, list):
                pending = [item for item in outline_payload if isinstance(item, dict)]
                restored: dict[str, int] = {}
                while pending:
                    progressed = False
                    for item in pending[:]:
                        key = str(item.get('key', ''))
                        parent_key = item.get('parent')
                        if not key or (parent_key is not None and str(parent_key) not in restored):
                            continue
                        kind = str(item.get('kind', ''))
                        workflow_id = imported_workflow_ids.get(str(item.get('workflow', '')))
                        if kind not in {'flow', 'group'} or (kind == 'flow' and workflow_id is None):
                            pending.remove(item)
                            progressed = True
                            continue
                        cursor = self.connection.execute(
                            'INSERT INTO workflow_outline(kind, workflow_id, parent_id, position, name, guard_json) '
                            'VALUES (?, ?, ?, ?, ?, ?)',
                            (
                                kind, workflow_id,
                                restored.get(str(parent_key)) if parent_key is not None else None,
                                max(1, int(item.get('position', 1))),
                                str(item.get('name', '')) if kind == 'group' else '',
                                json.dumps(decode_guard(item.get('guard')), ensure_ascii=False),
                            ),
                        )
                        restored[key] = int(cursor.lastrowid)
                        pending.remove(item)
                        progressed = True
                    if not progressed:
                        break
        self._initialize_workflow_outline()
        self._migrate_combined_event_groups()
        self.connection.commit()

    @staticmethod
    def _remap_text_data_references(text: str, path_map: dict[str, str]) -> str:
        """文字列内の ${data:...} を正規化後の構造パスへ置換する。"""
        def replace(match: re.Match[str]) -> str:
            raw_path = match.group(1)
            path = path_map.get(raw_path, path_map.get(raw_path.strip(), raw_path.strip()))
            return '${data:' + path + '}'
        return re.sub(
            r'\$\{data:([^{}]+)\}',
            replace,
            text,
        )

    def remap_data_paths(self, path_map: dict[str, str]) -> None:
        """イベントとガードに保存された Data パスを一括で移行する。"""
        if not path_map:
            return

        def remap_guard(raw: Any) -> str:
            guard = decode_guard(raw)
            for rule in guard['rules']:
                rule['path'] = path_map.get(rule['path'], rule['path'])
            return json.dumps(guard, ensure_ascii=False)

        with self.connection:
            workflows = self.connection.execute('SELECT id, guard_json FROM workflows').fetchall()
            for workflow in workflows:
                self.connection.execute(
                    'UPDATE workflows SET guard_json=? WHERE id=?',
                    (remap_guard(workflow['guard_json']), workflow['id']),
                )
            events = self.connection.execute(
                'SELECT id, selector, fallback_selector, value, failure_target, success_json, data_path, guard_json FROM events'
            ).fetchall()
            for event in events:
                fields = [
                    self._remap_text_data_references(str(event[field]), path_map)
                    for field in ('selector', 'fallback_selector', 'value', 'failure_target', 'success_json')
                ]
                self.connection.execute(
                    'UPDATE events SET selector=?, fallback_selector=?, value=?, failure_target=?, success_json=?, data_path=?, guard_json=? WHERE id=?',
                    (*fields, path_map.get(event['data_path'], event['data_path']), remap_guard(event['guard_json']), event['id']),
                )
        return len(normalized)

    def get_browser_visible(self) -> bool:
        return self._get_meta('browser_visible') != '0'

    def set_browser_visible(self, visible: bool) -> None:
        self._set_meta('browser_visible', '1' if visible else '0')

    def _get_meta(self, key: str, default: str | None=None) -> str | None:
        """アプリ共通設定を文字列として取得する。"""
        row = self.connection.execute(
            'SELECT value FROM app_meta WHERE key=?', (key,),
        ).fetchone()
        return str(row['value']) if row is not None else default

    def _set_meta(self, key: str, value: object) -> None:
        """アプリ共通設定を保存する。"""
        self.connection.execute(
            'INSERT INTO app_meta(key, value) VALUES (?, ?) '
            'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
            (key, str(value)),
        )
        self.connection.commit()

    def get_start_url(self) -> str | None:
        """保存済みの既定開始 URL を返す。未設定の場合は None を返す。"""
        stored = self._get_meta('start_url')
        if stored is None:
            return None
        value = stored.strip()
        return value if value.startswith(('http://', 'https://')) else None

    def set_start_url(self, start_url: str) -> None:
        """要素選択と認証画面で使用する既定開始 URL を保存する。"""
        value = start_url.strip()
        if not value.startswith(('http://', 'https://')):
            raise ValueError('Invalid start URL')
        self._set_meta('start_url', value)

    def get_default_timeout_ms(self) -> int:
        stored = self._get_meta('default_timeout_ms')
        try:
            value = int(stored) if stored is not None else 10000
        except (TypeError, ValueError):
            return 10000
        return value if 1 <= value <= 3600000 else 10000

    def set_default_timeout_ms(self, timeout_ms: int) -> None:
        if not 1 <= timeout_ms <= 3600000:
            raise ValueError('Invalid default timeout')
        self._set_meta('default_timeout_ms', timeout_ms)

    def get_action_stable_ms(self) -> int:
        """操作直前に対象要素が静止している必要時間を返す。"""
        stored = self._get_meta('action_stable_ms')
        try:
            value = int(stored) if stored is not None else 250
        except (TypeError, ValueError):
            return 250
        return value if 0 <= value <= 5000 else 250

    def set_action_stable_ms(self, stable_ms: int) -> None:
        if not 0 <= stable_ms <= 5000:
            raise ValueError('Invalid action stable time')
        self._set_meta('action_stable_ms', stable_ms)

    def get_language(self) -> str:
        language = self._get_meta('language')
        return language if language in {'ja', 'zh'} else 'ja'

    def set_language(self, language: str) -> None:
        if language not in {'ja', 'zh'}:
            raise ValueError('Unsupported language')
        self._set_meta('language', language)

    def get_auth_profile(self) -> str:
        return self._get_meta('auth_profile', 'default') or 'default'

    def set_auth_profile(self, profile: str) -> None:
        self._set_meta('auth_profile', profile)

    def get_ui_font(self) -> tuple[str, int]:
        family = (self._get_meta('ui_font_family', 'Yu Gothic UI') or '').strip()
        stored_size = self._get_meta('ui_font_size')
        try:
            size = max(8, min(18, int(stored_size))) if stored_size is not None else 10
        except (TypeError, ValueError):
            size = 10
        return (family or 'Yu Gothic UI', size)

    def set_ui_font(self, family: str, size: int) -> None:
        family = family.strip()
        if not family or not 8 <= size <= 18:
            raise ValueError('Invalid UI font setting')
        with self.connection:
            self.connection.execute(
                'INSERT INTO app_meta(key, value) VALUES (?, ?) '
                'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                ('ui_font_family', family),
            )
            self.connection.execute(
                'INSERT INTO app_meta(key, value) VALUES (?, ?) '
                'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                ('ui_font_size', str(size)),
            )

    def get_pcl_session_limit(self) -> int:
        stored = self._get_meta('pcl_session_limit')
        if stored is None:
            return 2
        try:
            return max(1, min(20, int(stored)))
        except (TypeError, ValueError):
            return 2

    def set_pcl_session_limit(self, limit: int) -> None:
        if not 1 <= limit <= 20:
            raise ValueError('settings.session_count_invalid')
        self._set_meta('pcl_session_limit', limit)

    def create_run(self, workflow_id: int) -> int:
        with self._lock:
            cursor = self.connection.execute("INSERT INTO runs(workflow_id, status) VALUES (?, 'running')", (workflow_id,))
            self.connection.commit()
            return int(cursor.lastrowid)

    def finish_run(self, run_id: int, status: str, message: str) -> None:
        with self._lock:
            self.connection.execute('UPDATE runs SET finished_at=CURRENT_TIMESTAMP, status=?, message=? WHERE id=?', (status, message, run_id))
            self.connection.commit()

    def get_data_schema(self, _workflow_id: int=0) -> dict[str, Any]:
        row = self.connection.execute('SELECT schema_json FROM global_data_schema WHERE id=1').fetchone()
        if row is None:
            # 初回起動時はサンプル項目を作らず、利用者が設計する空の構造を返す。
            return {'name': 'Data', 'type': 'object', 'children': []}
        schema = json.loads(row['schema_json'])
        # 旧版の list ルートも、一件分のデータを表す object として扱う。
        schema['type'] = 'object'
        return schema

    def save_data_schema(self, _workflow_id: int, schema: dict[str, Any]) -> None:
        # UI・JSON・Excel など保存経路にかかわらず、名称パスの一意性を保証する。
        from core.data_templates import validate_unique_template_names
        validate_unique_template_names(schema)
        payload = json.dumps(schema, ensure_ascii=False)
        self.connection.execute('INSERT INTO global_data_schema(id, schema_json) VALUES (1, ?)\n               ON CONFLICT(id) DO UPDATE SET schema_json=excluded.schema_json', (payload,))
        self.connection.commit()

    def list_data_records(self, _workflow_id: int=0, enabled_only: bool=False) -> list[dict[str, Any]]:
        where = ' WHERE enabled=1' if enabled_only else ''
        rows = self.connection.execute('SELECT id, position, name, summary, enabled, execution_group, execution_status, data_json FROM global_data_records' + where + ' ORDER BY position').fetchall()
        return [{'id': row['id'], 'position': row['position'], 'name': row['name'], 'summary': row['summary'], 'enabled': bool(row['enabled']), 'data': json.loads(row['data_json'])} | {'execution_group': row['execution_group'], 'execution_status': row['execution_status']} for row in rows]

    def add_data_record(self, _workflow_id: int, name: str, data: dict[str, Any], summary: str='') -> int:
        position = self.connection.execute('SELECT COALESCE(MAX(position), 0) + 1 FROM global_data_records').fetchone()[0]
        cursor = self.connection.execute('INSERT INTO global_data_records(position, name, summary, data_json) VALUES (?, ?, ?, ?)', (position, name, summary.strip(), json.dumps(data, ensure_ascii=False)))
        self.connection.commit()
        return int(cursor.lastrowid)

    def reorder_data_records(self, record_ids: list[int]) -> None:
        existing = {row['id'] for row in self.connection.execute(
            'SELECT id FROM global_data_records').fetchall()}
        if set(record_ids) != existing or len(record_ids) != len(existing):
            raise ValueError('flow.order_mismatch')
        with self.connection:
            # position は UNIQUE のため、入れ替え途中の衝突を避けて一度負数へ退避する。
            for temporary_position, record_id in enumerate(record_ids, 1):
                self.connection.execute(
                    'UPDATE global_data_records SET position=? WHERE id=?',
                    (-temporary_position, record_id),
                )
            for position, record_id in enumerate(record_ids, 1):
                self.connection.execute(
                    'UPDATE global_data_records SET position=? WHERE id=?',
                    (position, record_id),
                )

    def update_data_record(self, record_id: int, name: str, data: dict[str, Any]) -> None:
        with self._lock:
            self.connection.execute('UPDATE global_data_records SET name=?, data_json=? WHERE id=?', (name, json.dumps(data, ensure_ascii=False), record_id))
            self.connection.commit()

    def set_data_record_summary(self, record_id: int, summary: str) -> None:
        self.connection.execute('UPDATE global_data_records SET summary=? WHERE id=?', (summary.strip(), record_id))
        self.connection.commit()

    def set_data_record_enabled(self, record_id: int, enabled: bool) -> None:
        # 今回の実行対象と前回の実行結果は独立して管理し、切替時に結果を失わない。
        self.connection.execute('UPDATE global_data_records SET enabled=? WHERE id=?', (int(enabled), record_id))
        self.connection.commit()

    def set_data_records_enabled(self, record_ids: list[int], enabled: bool | None) -> None:
        """複数データの今回実行を一度のトランザクションで更新する。"""
        if not record_ids:
            return
        with self._lock, self.connection:
            if enabled is None:
                self.connection.executemany(
                    'UPDATE global_data_records SET enabled=1-enabled WHERE id=?',
                    ((record_id,) for record_id in record_ids),
                )
            else:
                self.connection.executemany(
                    'UPDATE global_data_records SET enabled=? WHERE id=?',
                    ((int(enabled), record_id) for record_id in record_ids),
                )

    def set_data_record_group(self, record_id: int, group: str) -> None:
        group = group.strip()
        if not group:
            raise ValueError('execution.group_empty')
        self.connection.execute('UPDATE global_data_records SET execution_group=? WHERE id=?', (group, record_id))
        self.connection.commit()

    def set_data_records_group(self, record_ids: list[int], group: str) -> None:
        """選択データへ同じ実行グループをまとめて設定する。"""
        group = group.strip()
        if not group:
            raise ValueError('execution.group_empty')
        if not record_ids:
            return
        with self._lock, self.connection:
            self.connection.executemany(
                'UPDATE global_data_records SET execution_group=? WHERE id=?',
                ((group, record_id) for record_id in record_ids),
            )

    def reorder_group_data_records(self, group: str, ordered_ids: list[int]) -> None:
        """他グループの相対位置を変えず、指定グループ内だけを並べ替える。"""
        records = self.list_data_records()
        group_ids = [int(record['id']) for record in records if str(record['execution_group']) == group]
        if len(ordered_ids) != len(group_ids) or set(ordered_ids) != set(group_ids):
            raise ValueError('flow.order_mismatch')
        ordered = iter(ordered_ids)
        all_ids = [
            next(ordered) if str(record['execution_group']) == group else int(record['id'])
            for record in records
        ]
        self.reorder_data_records(all_ids)

    def reorder_enabled_group_data_records(self, group: str, ordered_ids: list[int]) -> None:
        """スキップ行の位置を保ったまま、実行対象行だけを並べ替える。"""
        records = self.list_data_records()
        group_records = [
            record for record in records if str(record['execution_group']) == group
        ]
        enabled_ids = [int(record['id']) for record in group_records if record['enabled']]
        if len(ordered_ids) != len(enabled_ids) or set(ordered_ids) != set(enabled_ids):
            raise ValueError('flow.order_mismatch')
        enabled_order = iter(ordered_ids)
        merged_ids = [
            next(enabled_order) if record['enabled'] else int(record['id'])
            for record in group_records
        ]
        self.reorder_group_data_records(group, merged_ids)

    def clear_data_record_statuses(self) -> None:
        """今回実行の設定を保ち、全データの前回結果だけをまとめて消去する。"""
        with self._lock, self.connection:
            self.connection.execute(
                "UPDATE global_data_records SET execution_status="
                "CASE WHEN enabled=1 THEN 'not_run' ELSE 'skipped' END"
            )

    def set_data_record_status(self, record_id: int, status: str) -> None:
        if status not in {'not_run', 'waiting', 'running', 'error_waiting', 'stopping', 'stopped', 'success', 'failed', 'skipped'}:
            raise ValueError(f'Invalid data execution status: {status}')
        with self._lock:
            # 並列グループから同時に完了通知が届くため、更新と commit を一体で保護する。
            self.connection.execute('UPDATE global_data_records SET execution_status=? WHERE id=?', (status, record_id))
            self.connection.commit()

    def replace_data_records(self, records: list[dict[str, Any]]) -> None:
        """全 PCL を、指定された順番のデータで原子的に置き換える。"""
        with self.connection:
            self.connection.execute('DELETE FROM global_data_records')
            self.connection.executemany('INSERT INTO global_data_records(position, name, summary, enabled, execution_group, execution_status, data_json) VALUES (?, ?, ?, ?, ?, ?, ?)', ((position, record['name'], str(record.get('summary', '')).strip(), int(bool(record.get('enabled', True))), str(record.get('execution_group', '1')), 'not_run' if record.get('enabled', True) else 'skipped', json.dumps(record['data'], ensure_ascii=False)) for position, record in enumerate(records, 1)))

    def prepare_data_record_statuses(self) -> None:
        # 実行対象だけを待機中へ進め、スキップ対象の前回結果はそのまま保持する。
        self.connection.execute("UPDATE global_data_records SET execution_status='waiting' WHERE enabled=1")
        self.connection.commit()

    def recover_interrupted_data_record_statuses(self) -> None:
        """前回終了時に残った実行途中状態を、再実行可能な中止結果へ戻す。"""
        self.connection.execute(
            "UPDATE global_data_records SET execution_status="
            "CASE WHEN execution_status='error_waiting' THEN 'failed' ELSE 'stopped' END "
            "WHERE execution_status IN ('waiting', 'running', 'error_waiting', 'stopping')"
        )
        self.connection.commit()

    def delete_data_record(self, _workflow_id: int, record_id: int) -> None:
        self.delete_data_records(_workflow_id, [record_id])

    def delete_data_records(self, _workflow_id: int, record_ids: list[int]) -> None:
        """複数の実行データを一括削除し、順番の再採番も一度だけ行う。"""
        ids = [int(record_id) for record_id in dict.fromkeys(record_ids)]
        if not ids:
            return
        with self.connection:
            placeholders = ','.join('?' for _ in ids)
            self.connection.execute(
                f'DELETE FROM global_data_records WHERE id IN ({placeholders})', ids,
            )
            rows = self.connection.execute('SELECT id FROM global_data_records ORDER BY position').fetchall()
            for position, row in enumerate(rows, 1):
                self.connection.execute('UPDATE global_data_records SET position=? WHERE id=?', (position, row['id']))

    def close(self) -> None:
        self.connection.close()
