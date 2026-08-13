"""画面から発生する実行ログを日次ファイルへ保存する。"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from threading import Lock, current_thread


class DailyLogWriter:
    """複数スレッドから受け取ったログを旧版互換の形式で追記する。"""

    def __init__(self, project_dir: Path) -> None:
        self.log_dir = project_dir / 'log'
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()

    def append(self, message: str) -> None:
        now = datetime.now()
        prefix = f'{now:%Y-%m-%d %H:%M:%S} [{current_thread().name}] '
        file_text = '\n'.join(
            prefix + line for line in (str(message).splitlines() or [''])
        ) + '\n'
        try:
            with self._lock, (self.log_dir / f'{now:%Y-%m-%d}.log').open(
                'a', encoding='utf-8', newline='',
            ) as log_file:
                log_file.write(file_text)
        except OSError:
            # ログ保存の失敗によって、実行処理そのものは中断しない。
            pass
