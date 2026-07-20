"""SQLite のスキーマ、移行処理、および永続化 API を提供する。"""
from __future__ import annotations
import json
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
        columns = {row['name'] for row in self.connection.execute('PRAGMA table_info(workflows)').fetchall()}
        if 'position' not in columns:
            self.connection.execute('ALTER TABLE workflows ADD COLUMN position INTEGER NOT NULL DEFAULT 0')
        if 'enabled' not in columns:
            self.connection.execute('ALTER TABLE workflows ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1')
        if 'pcl_loop_start' not in columns:
            self.connection.execute('ALTER TABLE workflows ADD COLUMN pcl_loop_start INTEGER NOT NULL DEFAULT 0')
        if 'guard_json' not in columns:
            self.connection.execute("ALTER TABLE workflows ADD COLUMN guard_json TEXT NOT NULL DEFAULT ''")
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
        if 'guard_json' not in event_columns:
            self.connection.execute("ALTER TABLE events ADD COLUMN guard_json TEXT NOT NULL DEFAULT ''")
        global_record_columns = {row['name'] for row in self.connection.execute('PRAGMA table_info(global_data_records)').fetchall()}
        if 'enabled' not in global_record_columns:
            self.connection.execute('ALTER TABLE global_data_records ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1')
        if 'execution_status' not in global_record_columns:
            self.connection.execute("ALTER TABLE global_data_records ADD COLUMN execution_status TEXT NOT NULL DEFAULT 'not_run'")
        if 'execution_group' not in global_record_columns:
            self.connection.execute("ALTER TABLE global_data_records ADD COLUMN execution_group TEXT NOT NULL DEFAULT '1'")
        self._initialize_workflow_positions()
        self._migrate_global_data()
        self._migrate_combined_event_groups()
        self.connection.commit()

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

    @staticmethod
    def _default_data_schema() -> dict[str, Any]:
        return {'name': 'Data', 'type': 'list', 'children': [{'name': 'case_no', 'type': 'text'}, {'name': 'opp_name', 'type': 'text'}, {'name': 'plans', 'type': 'list', 'children': [{'name': 'plan', 'type': 'text'}, {'name': 'quantity', 'type': 'number'}, {'name': 'attrs', 'type': 'list', 'children': [{'name': 'attr', 'type': 'text'}]}]}]}

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

    def list_workflows(self) -> list[sqlite3.Row]:
        return self.connection.execute('SELECT * FROM workflows ORDER BY position, id').fetchall()

    def add_workflow(self, name: str, description: str='') -> int:
        position = self.connection.execute('SELECT COALESCE(MAX(position), 0) + 1 FROM workflows').fetchone()[0]
        cursor = self.connection.execute('INSERT INTO workflows(name, description, position) VALUES (?, ?, ?)', (name.strip(), description.strip(), position))
        self.connection.commit()
        return int(cursor.lastrowid)

    def update_workflow(self, workflow_id: int, name: str, description: str) -> None:
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
            self._normalize_workflow_positions()

    def reorder_workflows(self, workflow_ids: list[int]) -> None:
        existing = {row['id'] for row in self.list_workflows()}
        if set(workflow_ids) != existing or len(workflow_ids) != len(existing):
            raise ValueError('msg.0096')
        with self.connection:
            for position, workflow_id in enumerate(workflow_ids, 1):
                self.connection.execute('UPDATE workflows SET position=? WHERE id=?', (position, workflow_id))

    def move_workflow(self, workflow_id: int, direction: int) -> None:
        ids = [row['id'] for row in self.list_workflows()]
        if workflow_id not in ids:
            return
        old = ids.index(workflow_id)
        new = old + direction
        if new < 0 or new >= len(ids):
            return
        ids[old], ids[new] = (ids[new], ids[old])
        self.reorder_workflows(ids)

    def _normalize_workflow_positions(self) -> None:
        for position, row in enumerate(self.list_workflows(), 1):
            self.connection.execute('UPDATE workflows SET position=? WHERE id=?', (position, row['id']))

    def list_events(self, workflow_id: int) -> list[sqlite3.Row]:
        return self.connection.execute('SELECT * FROM events WHERE workflow_id=? ORDER BY position', (workflow_id,)).fetchall()

    def add_event(self, workflow_id: int, data: dict[str, Any]) -> int:
        position = self.connection.execute('SELECT COALESCE(MAX(position), 0) + 1 FROM events WHERE workflow_id=?', (workflow_id,)).fetchone()[0]
        guard_json = json.dumps(decode_guard(data.get('guard', data.get('guard_json', ''))), ensure_ascii=False)
        cursor = self.connection.execute('INSERT INTO events\n               (workflow_id, position, name, action, selector_type, selector,\n                fallback_selector_type, fallback_selector, value,\n                timeout_ms, enabled, continue_on_error, refresh_on_retry, failure_action, failure_target, data_path, guard_json)\n               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', (workflow_id, position, data['name'], data['action'], data['selector_type'], data['selector'], data.get('fallback_selector_type', 'none'), data.get('fallback_selector', ''), data['value'], data['timeout_ms'], data['enabled'], data['continue_on_error'], 0, data.get('failure_action', 'none'), data.get('failure_target', ''), data.get('data_path', ''), guard_json))
        self.connection.commit()
        return int(cursor.lastrowid)

    def update_event(self, event_id: int, data: dict[str, Any]) -> None:
        guard_json = json.dumps(decode_guard(data.get('guard', data.get('guard_json', ''))), ensure_ascii=False)
        self.connection.execute('UPDATE events SET name=?, action=?, selector_type=?, selector=?,\n               fallback_selector_type=?, fallback_selector=?, value=?,\n               timeout_ms=?, enabled=?, continue_on_error=?, refresh_on_retry=0, failure_action=?, failure_target=?, data_path=?, guard_json=? WHERE id=?', (data['name'], data['action'], data['selector_type'], data['selector'], data.get('fallback_selector_type', 'none'), data.get('fallback_selector', ''), data['value'], data['timeout_ms'], data['enabled'], data['continue_on_error'], data.get('failure_action', 'none'), data.get('failure_target', ''), data.get('data_path', ''), guard_json, event_id))
        self.connection.commit()

    def set_event_enabled(self, event_id: int, enabled: bool) -> None:
        self.connection.execute('UPDATE events SET enabled=? WHERE id=?', (int(enabled), event_id))
        self.connection.commit()

    def delete_event(self, event_id: int, workflow_id: int) -> None:
        with self.connection:
            self.connection.execute('DELETE FROM events WHERE id=?', (event_id,))
            self._normalize_positions(workflow_id)

    def delete_events(self, event_ids: list[int], workflow_id: int) -> None:
        unique_ids = list(dict.fromkeys(event_ids))
        with self.connection:
            self.connection.executemany('DELETE FROM events WHERE id=? AND workflow_id=?', ((event_id, workflow_id) for event_id in unique_ids))
            self._normalize_positions(workflow_id)

    def move_event(self, event_id: int, workflow_id: int, direction: int) -> None:
        rows = self.list_events(workflow_id)
        ids = [row['id'] for row in rows]
        if event_id not in ids:
            return
        old = ids.index(event_id)
        new = old + direction
        if new < 0 or new >= len(ids):
            return
        ids[old], ids[new] = (ids[new], ids[old])
        self.reorder_events(workflow_id, ids)

    def reorder_events(self, workflow_id: int, event_ids: list[int]) -> None:
        existing = {row['id'] for row in self.list_events(workflow_id)}
        if set(event_ids) != existing or len(event_ids) != len(existing):
            raise ValueError('msg.0097')
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
                    'timeout_ms': 10000, 'enabled': int(bool(item.get('enabled', 1))),
                    'continue_on_error': 0, 'refresh_on_retry': 0,
                    'data_path': str(item.get('data_path', '')) if loop_enabled else '',
                    'guard': item.get('guard')}
            events.append(base)
            events.extend(cls._group_items_to_events(item.get('events', [])))
            end = dict(base)
            end.update(action='group_end', value='', data_path='', refresh_on_retry=0, guard={})
            events.append(end)
        return events

    def export_workflow(self, workflow_id: int, path: Path) -> None:
        workflow = self.connection.execute('SELECT name, description, guard_json FROM workflows WHERE id=?', (workflow_id,)).fetchone()
        if workflow is None:
            raise ValueError('Workflow not found')
        events = [dict(row) for row in self.list_events(workflow_id)]
        for event in events:
            event.pop('id', None)
            event.pop('workflow_id', None)
            event.pop('refresh_on_retry', None)
            event['guard'] = decode_guard(event.pop('guard_json', ''))
        workflow_data = dict(workflow)
        workflow_data['guard'] = decode_guard(workflow_data.pop('guard_json', ''))
        payload = {'version': 2, 'workflow': workflow_data, 'events': self._events_to_group_items(events)}
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')

    def import_workflow(self, path: Path, allowed_actions: tuple[str, ...] | None=None, allowed_selector_types: tuple[str, ...] | None=None) -> int:
        try:
            payload: Any = json.loads(path.read_text(encoding='utf-8-sig'))
        except json.JSONDecodeError as error:
            raise ValueError(f'msg.0098{error}') from error
        if not isinstance(payload, dict) or payload.get('version') not in {1, 2}:
            raise ValueError('msg.0099')
        workflow = payload.get('workflow')
        events = payload.get('events')
        if not isinstance(workflow, dict) or not isinstance(events, list):
            raise ValueError('msg.0100')
        events = self._group_items_to_events(events)
        data_schema = payload.get('data_schema')
        data_records = payload.get('data_records', [])
        if data_schema is not None and (not isinstance(data_schema, dict)):
            raise ValueError('msg.0101')
        if not isinstance(data_records, list):
            raise ValueError('msg.0102')
        for record_index, record in enumerate(data_records, 1):
            if not isinstance(record, dict) or not isinstance(record.get('name'), str) or (not isinstance(record.get('data'), dict)):
                raise ValueError(f'msg.0103{record_index}msg.0104')
        name = workflow.get('name')
        description = workflow.get('description', '')
        if not isinstance(name, str) or not name.strip():
            raise ValueError('msg.0105')
        if not isinstance(description, str):
            raise ValueError('msg.0106')
        required = {'name', 'action', 'selector_type', 'selector', 'value'}
        normalized: list[dict[str, Any]] = []
        for index, event in enumerate(events, 1):
            if not isinstance(event, dict) or not required.issubset(event):
                raise ValueError(f'msg.0103{index}msg.0107')
            if any((not isinstance(event[key], str) for key in required)):
                raise ValueError(f'msg.0103{index}msg.0108')
            if allowed_actions is not None and event['action'] not in allowed_actions:
                raise ValueError(f'msg.0103{index}msg.0109')
            if allowed_selector_types is not None and event['selector_type'] not in allowed_selector_types:
                raise ValueError(f'msg.0103{index}msg.0110')
            try:
                timeout = int(event.get('timeout_ms', 10000))
            except (TypeError, ValueError) as error:
                raise ValueError(f'msg.0103{index}msg.0111') from error
            if timeout <= 0:
                raise ValueError(f'msg.0103{index}msg.0112')
            failure_action = str(event.get('failure_action', 'none'))
            if failure_action not in {'none', 'refresh', 'goto'}:
                failure_action = 'none'
            normalized.append({'name': event['name'].strip(), 'action': event['action'], 'selector_type': event['selector_type'], 'selector': event['selector'], 'fallback_selector_type': str(event.get('fallback_selector_type', 'none')), 'fallback_selector': str(event.get('fallback_selector', '')), 'value': event['value'], 'timeout_ms': timeout, 'enabled': int(bool(event.get('enabled', 1))), 'continue_on_error': int(bool(event.get('continue_on_error', 0))), 'refresh_on_retry': 0, 'failure_action': failure_action, 'failure_target': str(event.get('failure_target', '')), 'data_path': str(event.get('data_path', '')), 'guard': decode_guard(event.get('guard'))})
            if not normalized[-1]['name']:
                raise ValueError(f'msg.0103{index}msg.0113')
        existing_names = {row['name'] for row in self.list_workflows()}
        imported_name = name.strip()
        if imported_name in existing_names:
            suffix = 2
            while f'{imported_name} ({suffix})' in existing_names:
                suffix += 1
            imported_name = f'{imported_name} ({suffix})'
        with self.connection:
            position = self.connection.execute('SELECT COALESCE(MAX(position), 0) + 1 FROM workflows').fetchone()[0]
            workflow_guard = json.dumps(decode_guard(workflow.get('guard')), ensure_ascii=False)
            cursor = self.connection.execute('INSERT INTO workflows(name, description, position, guard_json) VALUES (?, ?, ?, ?)', (imported_name, description.strip(), position, workflow_guard))
            workflow_id = int(cursor.lastrowid)
            for position, event in enumerate(normalized, 1):
                cursor = self.connection.execute('INSERT INTO events\n                       (workflow_id, position, name, action, selector_type, selector,\n                        fallback_selector_type, fallback_selector, value,\n                        timeout_ms, enabled, continue_on_error, refresh_on_retry, data_path, guard_json)\n                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', (workflow_id, position, event['name'], event['action'], event['selector_type'], event['selector'], event['fallback_selector_type'], event['fallback_selector'], event['value'], event['timeout_ms'], event['enabled'], event['continue_on_error'], 0, event['data_path'], json.dumps(event['guard'], ensure_ascii=False)))
                self.connection.execute('UPDATE events SET failure_action=?, failure_target=? WHERE id=?', (event['failure_action'], event['failure_target'], cursor.lastrowid))
        self._migrate_combined_event_groups()
        self.connection.commit()
        return workflow_id

    def export_workflow_collection(self, path: Path) -> None:
        workflows: list[dict[str, Any]] = []
        for workflow in self.list_workflows():
            events = [dict(row) for row in self.list_events(workflow['id'])]
            for event in events:
                event.pop('id', None)
                event.pop('workflow_id', None)
                event.pop('refresh_on_retry', None)
                event['guard'] = decode_guard(event.pop('guard_json', ''))
            workflows.append({'name': workflow['name'], 'description': workflow['description'], 'position': workflow['position'], 'enabled': int(workflow['enabled']), 'pcl_loop_start': int(workflow['pcl_loop_start']), 'guard': decode_guard(workflow['guard_json']), 'events': self._events_to_group_items(events)})
        payload = {'version': 2, 'type': 'web-flow-collection', 'browser_visible': self.get_browser_visible(), 'workflows': workflows}
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')

    def import_workflow_collection(self, path: Path, allowed_actions: tuple[str, ...], allowed_selector_types: tuple[str, ...]) -> int:
        # 検証がすべて完了するまで既存データを変更しない。
        # 不正なファイルで現在のフローが消えることを防ぐためである。
        try:
            payload = json.loads(path.read_text(encoding='utf-8-sig'))
        except json.JSONDecodeError as error:
            raise ValueError(f'msg.0098{error}') from error
        if not isinstance(payload, dict) or payload.get('version') not in {1, 2}:
            raise ValueError('msg.0114')
        workflows = payload.get('workflows')
        if payload.get('type') not in {'web-flow-collection', 'salesforce-flow-collection'} or not isinstance(workflows, list):
            raise ValueError('msg.0115')
        browser_visible = payload.get('browser_visible', True)
        if not isinstance(browser_visible, bool):
            raise ValueError('msg.0116')
        normalized: list[dict[str, Any]] = []
        names: set[str] = set()
        for workflow_index, workflow in enumerate(workflows, 1):
            if not isinstance(workflow, dict) or not isinstance(workflow.get('name'), str) or (not workflow['name'].strip()):
                raise ValueError(f'msg.0103{workflow_index}msg.0117')
            name = workflow['name'].strip()
            if name in names:
                raise ValueError(f'msg.0118{name}')
            names.add(name)
            events = workflow.get('events')
            if not isinstance(events, list):
                raise ValueError(f'msg.0119{name}msg.0120')
            events = self._group_items_to_events(events)
            checked_events: list[dict[str, Any]] = []
            required = ('name', 'action', 'selector_type', 'selector', 'value')
            for event_index, event in enumerate(events, 1):
                if not isinstance(event, dict) or any((not isinstance(event.get(key), str) for key in required)):
                    raise ValueError(f'msg.0119{name}msg.0121{event_index}msg.0122')
                if event['action'] not in allowed_actions:
                    raise ValueError(f'msg.0119{name}msg.0123{event['action']}')
                if event['selector_type'] not in allowed_selector_types:
                    raise ValueError(f'msg.0119{name}msg.0124{event['selector_type']}')
                try:
                    timeout = int(event.get('timeout_ms', 10000))
                except (TypeError, ValueError) as error:
                    raise ValueError(f'msg.0119{name}msg.0121{event_index}msg.0125') from error
                if timeout <= 0:
                    raise ValueError(f'msg.0119{name}msg.0121{event_index}msg.0126')
                failure_action = str(event.get('failure_action', 'none'))
                if failure_action not in {'none', 'refresh', 'goto'}:
                    failure_action = 'none'
                checked_events.append({'name': event['name'], 'action': event['action'], 'selector_type': event['selector_type'], 'selector': event['selector'], 'fallback_selector_type': str(event.get('fallback_selector_type', 'none')), 'fallback_selector': str(event.get('fallback_selector', '')), 'value': event['value'], 'timeout_ms': timeout, 'enabled': int(bool(event.get('enabled', 1))), 'continue_on_error': int(bool(event.get('continue_on_error', 0))), 'refresh_on_retry': 0, 'failure_action': failure_action, 'failure_target': str(event.get('failure_target', '')), 'data_path': str(event.get('data_path', '')), 'guard': decode_guard(event.get('guard'))})
            normalized.append({'name': name, 'description': str(workflow.get('description', '')), 'enabled': int(bool(workflow.get('enabled', 1))), 'events': checked_events, 'pcl_loop_start': int(bool(workflow.get('pcl_loop_start', 0))), 'guard': decode_guard(workflow.get('guard'))})
        if sum((workflow['pcl_loop_start'] for workflow in normalized)) > 1:
            raise ValueError('msg.0127')
        with self.connection:
            self.connection.execute('DELETE FROM runs')
            self.connection.execute('DELETE FROM events')
            self.connection.execute('DELETE FROM workflows')
            self.connection.execute("INSERT INTO app_meta(key, value) VALUES ('browser_visible', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", ('1' if browser_visible else '0',))
            for workflow_position, workflow in enumerate(normalized, 1):
                cursor = self.connection.execute('INSERT INTO workflows\n                       (name, description, position, enabled, pcl_loop_start, guard_json)\n                       VALUES (?, ?, ?, ?, ?, ?)', (workflow['name'], workflow['description'], workflow_position, workflow['enabled'], workflow['pcl_loop_start'], json.dumps(workflow['guard'], ensure_ascii=False)))
                workflow_id = int(cursor.lastrowid)
                for event_position, event in enumerate(workflow['events'], 1):
                    event_cursor = self.connection.execute('INSERT INTO events\n                           (workflow_id, position, name, action, selector_type, selector,\n                            fallback_selector_type, fallback_selector, value,\n                            timeout_ms, enabled, continue_on_error, refresh_on_retry, data_path, guard_json)\n                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', (workflow_id, event_position, event['name'], event['action'], event['selector_type'], event['selector'], event['fallback_selector_type'], event['fallback_selector'], event['value'], event['timeout_ms'], event['enabled'], event['continue_on_error'], 0, event['data_path'], json.dumps(event['guard'], ensure_ascii=False)))
                    self.connection.execute('UPDATE events SET failure_action=?, failure_target=? WHERE id=?', (event['failure_action'], event['failure_target'], event_cursor.lastrowid))
        self._migrate_combined_event_groups()
        self.connection.commit()
        return len(normalized)

    def get_browser_visible(self) -> bool:
        row = self.connection.execute("SELECT value FROM app_meta WHERE key='browser_visible'").fetchone()
        return row is None or row['value'] != '0'

    def set_browser_visible(self, visible: bool) -> None:
        self.connection.execute("INSERT INTO app_meta(key, value) VALUES ('browser_visible', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", ('1' if visible else '0',))
        self.connection.commit()

    def get_language(self) -> str:
        row = self.connection.execute("SELECT value FROM app_meta WHERE key='language'").fetchone()
        return row['value'] if row is not None and row['value'] in {'ja', 'zh'} else 'ja'

    def set_language(self, language: str) -> None:
        if language not in {'ja', 'zh'}:
            raise ValueError('Unsupported language')
        self.connection.execute("INSERT INTO app_meta(key, value) VALUES ('language', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (language,))
        self.connection.commit()

    def get_auth_profile(self) -> str:
        row = self.connection.execute("SELECT value FROM app_meta WHERE key='auth_profile'").fetchone()
        return str(row['value']) if row else 'default'

    def set_auth_profile(self, profile: str) -> None:
        self.connection.execute("INSERT INTO app_meta(key, value) VALUES ('auth_profile', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (profile,))
        self.connection.commit()

    def get_ui_font(self) -> tuple[str, int]:
        family_row = self.connection.execute("SELECT value FROM app_meta WHERE key='ui_font_family'").fetchone()
        size_row = self.connection.execute("SELECT value FROM app_meta WHERE key='ui_font_size'").fetchone()
        family = family_row['value'].strip() if family_row is not None else 'Yu Gothic UI'
        try:
            size = max(8, min(18, int(size_row['value']))) if size_row is not None else 10
        except (TypeError, ValueError):
            size = 10
        return (family or 'Yu Gothic UI', size)

    def set_ui_font(self, family: str, size: int) -> None:
        family = family.strip()
        if not family or not 8 <= size <= 18:
            raise ValueError('Invalid UI font setting')
        with self.connection:
            self.connection.execute("INSERT INTO app_meta(key, value) VALUES ('ui_font_family', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (family,))
            self.connection.execute("INSERT INTO app_meta(key, value) VALUES ('ui_font_size', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(size),))

    def get_pcl_session_limit(self) -> int:
        row = self.connection.execute("SELECT value FROM app_meta WHERE key='pcl_session_limit'").fetchone()
        if row is None:
            return 2
        try:
            return max(1, min(20, int(row['value'])))
        except (TypeError, ValueError):
            return 2

    def set_pcl_session_limit(self, limit: int) -> None:
        if not 1 <= limit <= 20:
            raise ValueError('msg.0128')
        self.connection.execute("INSERT INTO app_meta(key, value) VALUES ('pcl_session_limit', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(limit),))
        self.connection.commit()

    def create_run(self, workflow_id: int) -> int:
        with self._lock:
            cursor = self.connection.execute("INSERT INTO runs(workflow_id, status) VALUES (?, 'running')", (workflow_id,))
            self.connection.commit()
            return int(cursor.lastrowid)

    def finish_run(self, run_id: int, status: str, message: str) -> None:
        with self._lock:
            self.connection.execute('UPDATE runs SET finished_at=CURRENT_TIMESTAMP, status=?, message=? WHERE id=?', (status, message, run_id))
            self.connection.commit()

    def list_input_rows(self, workflow_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute('SELECT id, position, name FROM input_rows WHERE workflow_id=? ORDER BY position', (workflow_id,)).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            cells = self.connection.execute('SELECT variable_name, value FROM input_cells WHERE row_id=?', (row['id'],)).fetchall()
            result.append({'id': row['id'], 'position': row['position'], 'name': row['name'], 'values': {cell['variable_name']: cell['value'] for cell in cells}})
        return result

    def add_input_row(self, workflow_id: int, name: str | None=None) -> int:
        position = self.connection.execute('SELECT COALESCE(MAX(position), 0) + 1 FROM input_rows WHERE workflow_id=?', (workflow_id,)).fetchone()[0]
        cursor = self.connection.execute('INSERT INTO input_rows(workflow_id, position, name) VALUES (?, ?, ?)', (workflow_id, position, name or tr(f'msg.0129{position}')))
        self.connection.commit()
        return int(cursor.lastrowid)

    def update_input_cell(self, row_id: int, variable_name: str, value: str) -> None:
        self.connection.execute('INSERT INTO input_cells(row_id, variable_name, value) VALUES (?, ?, ?)\n               ON CONFLICT(row_id, variable_name) DO UPDATE SET value=excluded.value', (row_id, variable_name, value))
        self.connection.commit()

    def update_input_row_name(self, row_id: int, name: str) -> None:
        self.connection.execute('UPDATE input_rows SET name=? WHERE id=?', (name, row_id))
        self.connection.commit()

    def delete_input_row(self, workflow_id: int, row_id: int) -> None:
        with self.connection:
            self.connection.execute('DELETE FROM input_cells WHERE row_id=?', (row_id,))
            self.connection.execute('DELETE FROM input_rows WHERE id=? AND workflow_id=?', (row_id, workflow_id))
            rows = self.connection.execute('SELECT id FROM input_rows WHERE workflow_id=? ORDER BY position', (workflow_id,)).fetchall()
            for position, row in enumerate(rows, 1):
                self.connection.execute('UPDATE input_rows SET position=? WHERE id=?', (position, row['id']))

    def get_data_schema(self, _workflow_id: int=0) -> dict[str, Any]:
        row = self.connection.execute('SELECT schema_json FROM global_data_schema WHERE id=1').fetchone()
        if row is None:
            return self._default_data_schema()
        return json.loads(row['schema_json'])

    def save_data_schema(self, _workflow_id: int, schema: dict[str, Any]) -> None:
        payload = json.dumps(schema, ensure_ascii=False)
        self.connection.execute('INSERT INTO global_data_schema(id, schema_json) VALUES (1, ?)\n               ON CONFLICT(id) DO UPDATE SET schema_json=excluded.schema_json', (payload,))
        self.connection.commit()

    def list_data_records(self, _workflow_id: int=0, enabled_only: bool=False) -> list[dict[str, Any]]:
        where = ' WHERE enabled=1' if enabled_only else ''
        rows = self.connection.execute('SELECT id, position, name, enabled, execution_group, execution_status, data_json FROM global_data_records' + where + ' ORDER BY position').fetchall()
        return [{'id': row['id'], 'position': row['position'], 'name': row['name'], 'enabled': bool(row['enabled']), 'data': json.loads(row['data_json'])} | {'execution_group': row['execution_group'], 'execution_status': row['execution_status']} for row in rows]

    def add_data_record(self, _workflow_id: int, name: str, data: dict[str, Any]) -> int:
        position = self.connection.execute('SELECT COALESCE(MAX(position), 0) + 1 FROM global_data_records').fetchone()[0]
        cursor = self.connection.execute('INSERT INTO global_data_records(position, name, data_json) VALUES (?, ?, ?)', (position, name, json.dumps(data, ensure_ascii=False)))
        self.connection.commit()
        return int(cursor.lastrowid)

    def update_data_record(self, record_id: int, name: str, data: dict[str, Any]) -> None:
        with self._lock:
            self.connection.execute('UPDATE global_data_records SET name=?, data_json=? WHERE id=?', (name, json.dumps(data, ensure_ascii=False), record_id))
            self.connection.commit()

    def set_data_record_enabled(self, record_id: int, enabled: bool) -> None:
        self.connection.execute('UPDATE global_data_records SET enabled=?, execution_status=? WHERE id=?', (int(enabled), 'not_run' if enabled else 'skipped', record_id))
        self.connection.commit()

    def set_data_record_group(self, record_id: int, group: str) -> None:
        group = group.strip()
        if not group:
            raise ValueError('msg.0130')
        self.connection.execute('UPDATE global_data_records SET execution_group=? WHERE id=?', (group, record_id))
        self.connection.commit()

    def set_data_record_status(self, record_id: int, status: str) -> None:
        if status not in {'not_run', 'waiting', 'running', 'success', 'failed', 'skipped'}:
            raise ValueError(f'Invalid data execution status: {status}')
        with self._lock:
            # 並列グループから同時に完了通知が届くため、更新と commit を一体で保護する。
            self.connection.execute('UPDATE global_data_records SET execution_status=? WHERE id=?', (status, record_id))
            self.connection.commit()

    def replace_data_records(self, records: list[dict[str, Any]]) -> None:
        """全 PCL を、指定された順番のデータで原子的に置き換える。"""
        with self.connection:
            self.connection.execute('DELETE FROM global_data_records')
            self.connection.executemany('INSERT INTO global_data_records(position, name, enabled, execution_group, execution_status, data_json) VALUES (?, ?, ?, ?, ?, ?)', ((position, record['name'], int(bool(record.get('enabled', True))), str(record.get('execution_group', '1')), 'not_run' if record.get('enabled', True) else 'skipped', json.dumps(record['data'], ensure_ascii=False)) for position, record in enumerate(records, 1)))

    def prepare_data_record_statuses(self) -> None:
        self.connection.execute("UPDATE global_data_records SET execution_status=CASE WHEN enabled=1 THEN 'waiting' ELSE 'skipped' END")
        self.connection.commit()

    def delete_data_record(self, _workflow_id: int, record_id: int) -> None:
        with self.connection:
            self.connection.execute('DELETE FROM global_data_records WHERE id=?', (record_id,))
            rows = self.connection.execute('SELECT id FROM global_data_records ORDER BY position').fetchall()
            for position, row in enumerate(rows, 1):
                self.connection.execute('UPDATE global_data_records SET position=? WHERE id=?', (position, row['id']))

    def close(self) -> None:
        self.connection.close()
