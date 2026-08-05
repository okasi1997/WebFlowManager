from pathlib import Path

from ui.auth_state import AuthStateDialog


def test_profiles_hide_generated_parallel_group_states(tmp_path: Path) -> None:
    states = tmp_path / 'data' / 'browser_states'
    states.mkdir(parents=True)
    for name in (
        'sales.json',
        'sales_group_1.json',
        'sales_group_tokyo-east.json',
        'support.json',
    ):
        (states / name).write_text('{}', encoding='utf-8')

    dialog = AuthStateDialog.__new__(AuthStateDialog)
    dialog.project_dir = tmp_path

    assert dialog._profiles() == ['default', 'none', 'sales', 'support']
