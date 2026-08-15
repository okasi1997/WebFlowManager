"""認証・要素選択・実行で共有する Chrome プロファイルのパスを管理する。"""
from __future__ import annotations

import shutil
import threading
from pathlib import Path
from typing import Final


DEFAULT_STATE_FILE = 'browser_state.json'
PROFILE_IN_USE_ERROR: Final = 'error.browser_profile_in_use'

# 同一アプリ内で同じ Chrome プロファイルを同時に起動しないための占有表。
# Chrome 起動を試してから失敗を待つよりも速く、不要なプロセス生成も避けられる。
_profile_lease_lock = threading.Lock()
_profile_leases: set[Path] = set()


class ProfileLease:
    """Chrome プロファイルの占有権を保持し、重複解放を安全に処理する。"""

    def __init__(self, profile_dir: Path) -> None:
        self.profile_dir = profile_dir.resolve()
        self._released = False

    def release(self) -> None:
        """保持中の占有権を解放する。複数回呼び出しても副作用はない。"""
        with _profile_lease_lock:
            if self._released:
                return
            _profile_leases.discard(self.profile_dir)
            self._released = True


def acquire_profile_lease(profile_dir: Path) -> ProfileLease:
    """指定プロファイルを占有し、使用中の場合は画面表示用エラーを返す。"""
    resolved = profile_dir.resolve()
    with _profile_lease_lock:
        if resolved in _profile_leases:
            raise RuntimeError(PROFILE_IN_USE_ERROR)
        _profile_leases.add(resolved)
    return ProfileLease(resolved)


def profile_lock_error(error: Exception) -> RuntimeError | None:
    """Chrome が返す代表的な外部プロファイル占有エラーを共通エラーへ変換する。"""
    if str(error) == PROFILE_IN_USE_ERROR:
        return RuntimeError(PROFILE_IN_USE_ERROR)
    message = str(error).casefold()
    indicators = (
        'processsingleton',
        'profile in use',
        'user data directory is already in use',
        'opening in existing browser session',
    )
    return RuntimeError(PROFILE_IN_USE_ERROR) if any(value in message for value in indicators) else None


def profile_name_from_state(state_path: Path | None) -> str | None:
    if state_path is None:
        return None
    return 'default' if state_path.name == DEFAULT_STATE_FILE else state_path.stem


def persistent_profile_dir(project_dir: Path, state_path: Path | None) -> Path | None:
    name = profile_name_from_state(state_path)
    return None if name is None else project_dir / 'data' / 'chrome_profiles' / name


def profile_has_state(profile_dir: Path | None) -> bool:
    if profile_dir is None or not profile_dir.is_dir():
        return False
    return any(profile_dir.iterdir())


def clear_profile(project_dir: Path, profile_dir: Path) -> None:
    """アプリ管理下であることを確認した Chrome プロファイルだけを削除する。"""
    profiles_root = (project_dir / 'data' / 'chrome_profiles').resolve()
    target = profile_dir.resolve()
    if target.parent != profiles_root:
        raise ValueError(f'プロファイルのパスがアプリ管理フォルダー外です: {target}')
    if target.exists():
        shutil.rmtree(target)
