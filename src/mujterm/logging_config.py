from __future__ import annotations

import logging
import os
import sys
import threading
from dataclasses import dataclass
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

from .paths import ensure_private_dir, state_dir


LOGGER_NAME = "mujterm"
LOG_FILENAME = "mujterm.log"
LOG_MAX_BYTES = 1_000_000
LOG_BACKUP_COUNT = 3


@dataclass(frozen=True)
class RuntimeErrorRecord:
    occurred_at: datetime
    context: str
    message: str

    def summary(self) -> str:
        stamp = self.occurred_at.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
        return f"{stamp} — {self.context}: {self.message}"


_configured_path: Optional[Path] = None
_last_runtime_error: Optional[RuntimeErrorRecord] = None
_state_lock = threading.Lock()
_original_excepthook = sys.excepthook
_hooks_installed = False


class _PrivateRotatingFileHandler(RotatingFileHandler):
    def _open(self):  # type: ignore[no-untyped-def]
        stream = super()._open()
        self._secure(Path(self.baseFilename))
        return stream

    def doRollover(self) -> None:
        super().doRollover()
        self._secure(Path(self.baseFilename))
        for index in range(1, self.backupCount + 1):
            self._secure(Path(f"{self.baseFilename}.{index}"))

    @staticmethod
    def _secure(path: Path) -> None:
        try:
            path.chmod(0o600)
        except OSError:
            pass


def default_log_path() -> Path:
    return state_dir() / LOG_FILENAME


def current_log_path() -> Path:
    return _configured_path or default_log_path()


def configure_logging(
    path: Optional[Path] = None,
    *,
    max_bytes: int = LOG_MAX_BYTES,
    backup_count: int = LOG_BACKUP_COUNT,
) -> Path:
    """Configure one private rotating handler for all ``mujterm.*`` loggers."""
    global _configured_path

    target = path or default_log_path()
    ensure_private_dir(target.parent)
    target = target.resolve()
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.INFO)
    logger.propagate = False

    for handler in list(logger.handlers):
        if not getattr(handler, "_mujterm_rotating_handler", False):
            continue
        existing = Path(handler.baseFilename).resolve()
        if existing == target:
            _configured_path = target
            return target
        logger.removeHandler(handler)
        handler.close()

    handler = _PrivateRotatingFileHandler(
        target,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    handler._mujterm_rotating_handler = True  # type: ignore[attr-defined]
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s [%(threadName)s] %(message)s"
        )
    )
    logger.addHandler(handler)
    try:
        target.chmod(0o600)
    except OSError:
        pass
    _configured_path = target
    logger.info("Runtime logging configured; log=%s", target)
    return target


def install_exception_hook() -> None:
    """Record otherwise uncaught main-thread exceptions before normal reporting."""
    global _hooks_installed
    if _hooks_installed:
        return

    def handle_exception(
        exception_type: type[BaseException],
        exception: BaseException,
        traceback: object,
    ) -> None:
        if issubclass(exception_type, KeyboardInterrupt):
            _original_excepthook(exception_type, exception, traceback)
            return
        record_runtime_error("Unhandled application error", exception)
        _original_excepthook(exception_type, exception, traceback)

    sys.excepthook = handle_exception
    _hooks_installed = True


def record_runtime_error(
    context: str, error: BaseException | str
) -> RuntimeErrorRecord:
    message = str(error).strip() or type(error).__name__
    record = RuntimeErrorRecord(datetime.now().astimezone(), context, message)
    with _state_lock:
        global _last_runtime_error
        _last_runtime_error = record

    exception_info = None
    if isinstance(error, BaseException) and error.__traceback__ is not None:
        exception_info = (type(error), error, error.__traceback__)
    logging.getLogger(f"{LOGGER_NAME}.runtime").error(
        "%s: %s",
        context,
        message,
        exc_info=exception_info,
    )
    return record


def last_runtime_error() -> Optional[RuntimeErrorRecord]:
    with _state_lock:
        return _last_runtime_error


def recent_log_lines(
    path: Optional[Path] = None,
    *,
    line_limit: int = 20,
    character_limit: int = 12_000,
) -> tuple[str, ...]:
    if line_limit <= 0 or character_limit <= 0:
        return ()
    target = path or current_log_path()
    try:
        with target.open("rb") as stream:
            size = stream.seek(0, os.SEEK_END)
            stream.seek(max(0, size - character_limit), os.SEEK_SET)
            content = stream.read().decode("utf-8", errors="replace")
    except OSError:
        return ()
    lines = content.splitlines()
    if size > character_limit and lines:
        lines = lines[1:]
    return tuple(lines[-line_limit:])


def _reset_logging_for_tests() -> None:
    """Detach MujTerm handlers and transient state; intended for isolated tests."""
    global _configured_path, _last_runtime_error
    logger = logging.getLogger(LOGGER_NAME)
    for handler in list(logger.handlers):
        if getattr(handler, "_mujterm_rotating_handler", False):
            logger.removeHandler(handler)
            handler.close()
    with _state_lock:
        _configured_path = None
        _last_runtime_error = None
