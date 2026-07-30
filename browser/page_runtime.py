"""Shared Playwright helpers for tabs, popup pages, and iframe-aware lookup."""
from __future__ import annotations

from typing import Any, Callable


def open_pages(context: Any) -> list[Any]:
    return [page for page in context.pages if not page.is_closed()]


def active_page(reference_page: Any) -> Any:
    """Return the newest open page in the same context.

    Playwright appends popup/new-tab pages to ``context.pages``. Workflows keep
    their original Page only as a context anchor and resolve the current page
    before every operation.
    """
    pages = open_pages(reference_page.context)
    return pages[-1] if pages else reference_page


def page_frames(page: Any) -> list[Any]:
    """Return all currently attached frames, main frame first."""
    return list(page.frames)


def locators_in_frames(page: Any, factory: Callable[[Any], Any]) -> list[Any]:
    locators: list[Any] = []
    for frame in page_frames(active_page(page)):
        try:
            locators.append(factory(frame))
        except Exception:
            # A frame can detach while a dynamic page is being inspected.
            continue
    return locators


def settle_new_page(page: Any, previous_pages: set[Any], timeout: int) -> Any:
    """Wait for a synchronously opened popup/tab to start navigation."""
    current = active_page(page)
    if current in previous_pages:
        return current
    try:
        current.wait_for_url(lambda url: url != 'about:blank', timeout=timeout)
    except Exception:
        # Some legitimate popup documents intentionally remain about:blank and
        # populate their contents with script, so the page must still be usable.
        pass
    try:
        current.wait_for_load_state('domcontentloaded', timeout=timeout)
    except Exception:
        pass
    try:
        current.bring_to_front()
    except Exception:
        pass
    return current
