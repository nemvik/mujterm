from __future__ import annotations

import logging
from typing import Optional

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gio, Gtk  # noqa: E402

from .agent_listener import AgentSocketListener
from .database import Database, DatabaseError
from .integrations import IntegrationManager
from .logging_config import record_runtime_error
from .tmux_backend import TmuxBackend, TmuxError
from .ui import MainWindow


LOGGER = logging.getLogger(__name__)


def disable_menu_bar_accelerator(settings: Optional[Gtk.Settings] = None) -> None:
    """Keep F10 available to terminal applications instead of GTK menus."""
    if settings is None:
        settings = Gtk.Settings.get_default()
    if settings is not None:
        settings.set_property("gtk-menu-bar-accel", None)


class MujTermApplication(Gtk.Application):
    def __init__(self) -> None:
        super().__init__(
            application_id="io.github.viktornemcok.MujTerm",
            flags=Gio.ApplicationFlags.FLAGS_NONE,
        )
        self.window: Optional[MainWindow] = None
        self.database: Optional[Database] = None
        self.listener: Optional[AgentSocketListener] = None

    def do_startup(self) -> None:
        Gtk.Application.do_startup(self)
        disable_menu_bar_accelerator()
        action = Gio.SimpleAction.new("quit", None)
        action.connect("activate", lambda *_args: self.quit())
        self.add_action(action)
        self.set_accels_for_action("app.quit", ["<Primary>q"])

    def do_activate(self) -> None:
        if self.window:
            self.window.present()
            return
        try:
            self.database = Database()
        except DatabaseError as exc:
            record_runtime_error("State database initialization failed", exc)
            self._fatal("Could not open MujTerm state", str(exc))
            return
        try:
            backend = TmuxBackend()
        except (OSError, TmuxError) as exc:
            record_runtime_error("tmux initialization failed", exc)
            self._fatal("Could not initialize tmux", str(exc))
            return
        if not backend.available():
            self._fatal("tmux is required", "Install tmux and launch MujTerm again.")
            return
        try:
            self.window = MainWindow(self, self.database, backend, IntegrationManager())
        except TmuxError as exc:
            record_runtime_error("Workspace restoration failed", exc)
            self._fatal("Could not initialize tmux", str(exc))
            return
        self.listener = AgentSocketListener(self._agent_event)
        try:
            self.listener.start()
        except OSError as exc:
            self.window.report_runtime_error("Agent event listener failed", exc)
            self.listener = None
        self.window.show_all()
        LOGGER.info("MujTerm window activated")

    def do_shutdown(self) -> None:
        LOGGER.info("Shutting down MujTerm")
        if self.window:
            self.window.shutdown()
        if self.listener:
            self.listener.stop()
        if self.database:
            self.database.close()
        Gtk.Application.do_shutdown(self)

    def _agent_event(self, payload: dict[str, object]) -> None:
        if self.window:
            self.window.agent_event_received(payload)

    def _fatal(self, title: str, message: str) -> None:
        dialog = Gtk.MessageDialog(
            transient_for=None,
            modal=True,
            message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.CLOSE,
            text=title,
        )
        dialog.format_secondary_text(message)
        dialog.run()
        dialog.destroy()
        self.quit()
