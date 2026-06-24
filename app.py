"""
PacHub — app.py
Adw.Application subclass: registers GActions and wires the About dialog.
"""

import sys

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, Gio

from styles import load_css
from window import pachubWindow


class pachubApp(Adw.Application):
    def __init__(self):
        super().__init__(
            application_id="io.github.mrks1469.pachub",
            flags=Gio.ApplicationFlags.FLAGS_NONE,
        )
        self.connect("activate", self._on_activate)
        self.connect("shutdown", self._on_shutdown)

    def _on_shutdown(self, app):
        # Signal all background threads to stop and force-exit cleanly
        import os, signal
        os.kill(os.getpid(), signal.SIGTERM)

    def _on_activate(self, app):
        load_css()
        self.win = pachubWindow(app)
        self.win.connect("close-request", lambda *_: self.quit())

        actions = {
            "sync":          self.win._on_sync_db,
            "refresh":       self.win._on_refresh,
            "install":       self.win._on_install,
            "remove":        self.win._on_remove,
            "cache":         self.win._on_clean_cache,
            "check_updates": self.win._on_check_updates,
            "manage_repos":  self.win._on_manage_repos,
            "rate_mirrors":  self.win._on_rate_mirrors,
            "orphans":       self.win._on_show_orphans,
            "sysinfo":       self.win._on_show_sysinfo,
            "history":       self.win._on_show_history,
            "pacdiff":       self.win._on_show_pacdiff,
            "export_pkgs":   self.win._on_export_pkgs,
            "import_pkgs":   self.win._on_import_pkgs,
            "pkgbuild":      self.win._on_view_pkgbuild,
            "hold":          self.win._on_toggle_hold,
            "mark_explicit": self.win._on_mark_explicit,
            "mark_asdeps":   self.win._on_mark_asdeps,
            "preferences":   self.win._on_preferences,
            "shortcuts":     self.win._on_show_shortcuts,
            "search":        self.win._on_focus_search,
            "quit":          lambda *_: self.quit(),
            "about":         self._on_about,
        }
        for name, cb in actions.items():
            act = Gio.SimpleAction.new(name, None)
            act.connect("activate", cb)
            self.add_action(act)

        accels = {
            "app.search":        ["<Ctrl>f"],
            "app.sync":          ["F5"],
            "app.refresh":       ["<Ctrl>r"],
            "app.check_updates": ["<Ctrl>u"],
            "app.preferences":   ["<Ctrl>comma"],
            "app.shortcuts":     ["<Ctrl>question"],
            "app.quit":          ["<Ctrl>q"],
        }
        for action, keys in accels.items():
            self.set_accels_for_action(action, keys)

        self.win.present()

    def _on_about(self, *_):
        about = Adw.AboutDialog()
        about.set_application_name("PacHub")
        about.set_application_icon("io.github.mrks1469.pachub")
        about.set_version("3.0.0")
        about.set_developer_name("Manpreet Singh")
        about.set_license_type(Gtk.License.GPL_2_0)
        about.set_website("https://github.com/mrks1469/PacHub")
        about.set_issue_url("https://github.com/mrks1469/PacHub/issues")
        about.set_comments("A powerful Pacman/AUR front end.\n")
        about.set_developers(["Manpreet Singh https://github.com/mrks1469"])
        about.present(self.win)


def main():
    return pachubApp().run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
