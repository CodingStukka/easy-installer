"""Run blocking work in worker threads and hand the results back to the GTK main loop.

Widgets must only ever be touched from the main loop. Everything here delivers through
``GLib.idle_add``, so callbacks can update the UI directly.
"""

from __future__ import annotations

import logging
import threading
import traceback
from typing import Any, Callable

from gi.repository import GLib

log = logging.getLogger(__name__)

ResultCallback = Callable[[Any, "BaseException | None"], None]
ProgressCallback = Callable[["float | None", str], None]


def run_in_thread(fn: Callable[..., Any], callback: ResultCallback, *args: Any,
                  **kwargs: Any) -> threading.Thread:
    """Run ``fn(*args, **kwargs)`` in a daemon thread; then ``callback(result, error)`` on the main loop.

    Exactly one of ``result``/``error`` is meaningful: ``error`` is the raised exception (with its
    traceback attached) or None.
    """

    def worker() -> None:
        result: Any = None
        error: BaseException | None = None
        try:
            result = fn(*args, **kwargs)
        except BaseException as exc:  # handed over to the main loop, never swallowed
            error = exc
        GLib.idle_add(_deliver, callback, result, error)

    name = getattr(fn, "__name__", "task")
    thread = threading.Thread(target=worker, name=f"easy-installer-{name}", daemon=True)
    thread.start()
    return thread


def _deliver(callback: ResultCallback, result: Any, error: BaseException | None) -> bool:
    try:
        callback(result, error)
    except Exception:
        log.exception("unhandled error in a main-loop callback")
    return GLib.SOURCE_REMOVE


def format_exception(error: BaseException) -> str:
    """Traceback text for an "unexpected error" details view."""
    return "".join(traceback.format_exception(type(error), error, error.__traceback__)).strip()


class MainLoopProgress:
    """Thread-safe progress callback that forwards updates to ``callback`` on the main loop.

    Bursts are coalesced: while an update is waiting to be delivered, newer ones replace it, so a
    fast worker (e.g. hashing 1 MiB chunks) cannot flood the main loop. Call :meth:`close` to drop
    updates that arrive after the UI moved on.
    """

    def __init__(self, callback: ProgressCallback):
        self._callback = callback
        self._lock = threading.Lock()
        self._pending: tuple[float | None, str] | None = None
        self._scheduled = False
        self._closed = False

    def __call__(self, fraction: float | None, message: str) -> None:
        with self._lock:
            if self._closed:
                return
            self._pending = (fraction, message)
            if self._scheduled:
                return
            self._scheduled = True
        GLib.idle_add(self._flush)

    def _flush(self) -> bool:
        with self._lock:
            pending, self._pending = self._pending, None
            self._scheduled = False
            closed = self._closed
        if pending is not None and not closed:
            try:
                self._callback(*pending)
            except Exception:
                log.exception("unhandled error in a progress callback")
        return GLib.SOURCE_REMOVE

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._pending = None


def main_loop_progress(callback: ProgressCallback) -> MainLoopProgress:
    return MainLoopProgress(callback)
