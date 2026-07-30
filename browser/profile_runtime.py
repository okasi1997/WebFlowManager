"""認証・要素選択・実行で共有する Chrome プロファイルのパスを管理する。"""
from __future__ import annotations

import shutil
from pathlib import Path


DEFAULT_STATE_FILE = 'browser_state.json'


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
