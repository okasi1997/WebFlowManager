from pathlib import Path

import pytest

from browser.profile_runtime import (
    PROFILE_IN_USE_ERROR,
    acquire_profile_lease,
    profile_lock_error,
)


def test_profile_lease_rejects_duplicate_and_allows_reuse(tmp_path: Path) -> None:
    profile = tmp_path / 'profile'
    first = acquire_profile_lease(profile)
    try:
        with pytest.raises(RuntimeError, match=PROFILE_IN_USE_ERROR):
            acquire_profile_lease(profile)
    finally:
        first.release()

    reused = acquire_profile_lease(profile)
    reused.release()
    reused.release()


def test_profile_lock_error_converts_known_chrome_message() -> None:
    converted = profile_lock_error(Exception('Failed to create a ProcessSingleton for your profile'))

    assert isinstance(converted, RuntimeError)
    assert str(converted) == PROFILE_IN_USE_ERROR
    assert str(profile_lock_error(RuntimeError(PROFILE_IN_USE_ERROR))) == PROFILE_IN_USE_ERROR
    assert profile_lock_error(Exception('unrelated launch failure')) is None
