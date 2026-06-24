"""
PacHub — models.py
GObject data model (PackageItem), the virtualized package ListView factory
(make_package_listview / PackageRowContent), and the sidebar NavRow.
"""

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Gio, GObject, Pango

# ─── Repository badge mapping ─────────────────────────────────────────────────

REPO_BADGE_CLASS = {
    "core":     "badge-core",
    "extra":    "badge-extra",
    "aur":      "badge-aur",
    "multilib": "badge-multilib",
    "local":    "badge-local",
    "foreign":  "badge-foreign",
}

# ─── Symbolic icon fallbacks per package name ─────────────────────────────────

PKG_ICONS = {
    "linux":                  "utilities-terminal-symbolic",
    "linux-firmware":         "drive-harddisk-symbolic",
    "base":                   "package-x-generic-symbolic",
    "bash":                   "utilities-terminal-symbolic",
    "zsh":                    "utilities-terminal-symbolic",
    "fish":                   "utilities-terminal-symbolic",
    "git":                    "preferences-system-details-symbolic",
    "python":                 "text-x-script-symbolic",
    "python-pip":             "text-x-script-symbolic",
    "nodejs":                 "text-x-script-symbolic",
    "npm":                    "text-x-script-symbolic",
    "rust":                   "application-x-executable-symbolic",
    "go":                     "application-x-executable-symbolic",
    "cmake":                  "applications-engineering-symbolic",
    "docker":                 "application-x-executable-symbolic",
    "flatpak":                "package-x-generic-symbolic",
    "pacman":                 "package-x-generic-symbolic",
    "yay":                    "package-x-generic-symbolic",
    "paru":                   "package-x-generic-symbolic",
    "networkmanager":         "network-wireless-symbolic",
    "openssh":                "network-server-symbolic",
    "pipewire":               "audio-speakers-symbolic",
    "alsa-utils":             "audio-card-symbolic",
    "htop":                   "utilities-system-monitor-symbolic",
    "curl":                   "network-transmit-receive-symbolic",
    "wget":                   "network-transmit-receive-symbolic",
    "vim":                    "text-editor-symbolic",
    "nano":                   "text-editor-symbolic",
    "mesa":                   "video-display-symbolic",
    "timeshift":              "document-revert-symbolic",
    "systemd":                "preferences-system-symbolic",
    "firefox":                "web-browser-symbolic",
    "chromium":               "web-browser-symbolic",
    "google-chrome":          "web-browser-symbolic",
    "gimp":                   "applications-graphics-symbolic",
    "vlc":                    "applications-multimedia-symbolic",
    "visual-studio-code-bin": "text-editor-symbolic",
}


def pkg_icon(name):
    return PKG_ICONS.get(name, "package-x-generic-symbolic")


# ─── GObject model ────────────────────────────────────────────────────────────

class PackageItem(GObject.Object):
    __gtype_name__ = 'PacHubPackageItem'

    def __init__(self, name, version, repo="local", status="installed",
                 description="", foreign=False):
        super().__init__()
        self.pkg_name        = name
        self.pkg_version     = version
        self.pkg_repo        = repo
        self.pkg_status      = status
        self.pkg_description = description
        self.pkg_foreign     = foreign


# ─── Package list row (virtualized ListView) ─────────────────────────────────

_STATUS_PILL_CSS = ("status-installed", "status-update")
_REPO_BADGE_CSS = tuple(set(REPO_BADGE_CLASS.values()))


class PackageRowContent(Gtk.Box):
    """Recyclable row widget for the package ListView.

    Built once per visible slot in `setup`, then re-bound to whatever
    PackageItem scrolls into it via `bind` — so only ~screenful of widgets
    ever exist, regardless of list size.
    """
    __gtype_name__ = 'PacHubPackageRowContent'

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.set_margin_top(9);    self.set_margin_bottom(9)
        self.set_margin_start(10); self.set_margin_end(10)
        self.add_css_class("pkg-row")

        self.icon = Gtk.Image()
        self.icon.set_pixel_size(20)
        self.icon.set_valign(Gtk.Align.CENTER)
        self.icon.add_css_class("dim-label")
        self.append(self.icon)

        info_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        info_box.set_hexpand(True)
        info_box.set_valign(Gtk.Align.CENTER)

        self.name_label = Gtk.Label()
        self.name_label.set_halign(Gtk.Align.START)
        self.name_label.add_css_class("body")
        self.name_label.set_ellipsize(Pango.EllipsizeMode.END)
        attrs = Pango.AttrList()
        attrs.insert(Pango.attr_weight_new(Pango.Weight.SEMIBOLD))
        self.name_label.set_attributes(attrs)
        info_box.append(self.name_label)

        self.desc_label = Gtk.Label()
        self.desc_label.set_halign(Gtk.Align.START)
        self.desc_label.add_css_class("caption")
        self.desc_label.add_css_class("dim-label")
        self.desc_label.set_ellipsize(Pango.EllipsizeMode.END)
        info_box.append(self.desc_label)
        self.append(info_box)

        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        right.set_valign(Gtk.Align.CENTER)
        right.set_halign(Gtk.Align.END)

        badges_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        badges_row.set_halign(Gtk.Align.END)
        self.status_badge = Gtk.Label()
        self.status_badge.add_css_class("row-status-pill")
        badges_row.append(self.status_badge)
        self.repo_badge = Gtk.Label()
        self.repo_badge.add_css_class("badge")
        badges_row.append(self.repo_badge)
        right.append(badges_row)

        self.ver_label = Gtk.Label()
        self.ver_label.add_css_class("caption")
        self.ver_label.add_css_class("dim-label")
        self.ver_label.set_halign(Gtk.Align.END)
        right.append(self.ver_label)
        self.append(right)

    def bind(self, pkg):
        self.icon.set_from_icon_name(pkg_icon(pkg.pkg_name))
        self.name_label.set_label(pkg.pkg_name)
        self.desc_label.set_label(pkg.pkg_description or "")
        self.desc_label.set_visible(bool(pkg.pkg_description))

        # Status pill — only for installed/update; clear stale classes first
        for cls in _STATUS_PILL_CSS:
            self.status_badge.remove_css_class(cls)
        if pkg.pkg_status in ("installed", "update"):
            is_update = pkg.pkg_status == "update"
            self.status_badge.set_label("UPDATE" if is_update else "INSTALLED")
            self.status_badge.add_css_class("status-update" if is_update else "status-installed")
            self.status_badge.set_visible(True)
        else:
            self.status_badge.set_visible(False)

        # Repo badge — swap class on every rebind
        repo_str = "aur" if pkg.pkg_foreign else (pkg.pkg_repo or "local")
        for cls in _REPO_BADGE_CSS:
            self.repo_badge.remove_css_class(cls)
        self.repo_badge.set_label(repo_str.upper())
        self.repo_badge.add_css_class(REPO_BADGE_CLASS.get(repo_str.lower(), "badge-local"))

        self.ver_label.set_label(pkg.pkg_version or "")


def make_package_listview(on_activate):
    """Build a virtualized package ListView backed by a Gio.ListStore.

    Returns (listview, store, selection). `on_activate(item)` fires on a single
    click or Enter, with the activated PackageItem (or None).
    """
    store = Gio.ListStore(item_type=PackageItem)
    selection = Gtk.SingleSelection(model=store)
    selection.set_autoselect(False)
    selection.set_can_unselect(True)

    factory = Gtk.SignalListItemFactory()
    factory.connect("setup", lambda f, li: li.set_child(PackageRowContent()))
    factory.connect("bind", lambda f, li: li.get_child().bind(li.get_item()))

    listview = Gtk.ListView(model=selection, factory=factory)
    listview.add_css_class("navigation-sidebar")
    listview.set_single_click_activate(True)
    listview.connect("activate", lambda lv, pos: on_activate(store.get_item(pos)))
    return listview, store, selection


# ─── Sidebar navigation row ───────────────────────────────────────────────────

class NavRow(Gtk.ListBoxRow):
    def __init__(self, icon_name, label, count=None, badge_css=None):
        super().__init__()
        self.add_css_class("nav-row")
        self.set_activatable(True)

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        box.set_margin_top(7);    box.set_margin_bottom(7)
        box.set_margin_start(10); box.set_margin_end(10)

        icon = Gtk.Image.new_from_icon_name(icon_name)
        icon.set_pixel_size(16)
        icon.set_valign(Gtk.Align.CENTER)
        icon.add_css_class("dim-label")
        box.append(icon)

        lbl = Gtk.Label(label=label)
        lbl.set_hexpand(True)
        lbl.set_halign(Gtk.Align.START)
        lbl.set_valign(Gtk.Align.CENTER)
        box.append(lbl)

        self.count_lbl = None
        if count is not None:
            self.count_lbl = Gtk.Label(label=str(count))
            self.count_lbl.add_css_class("count-badge")
            if badge_css:
                self.count_lbl.add_css_class(badge_css)
            self.count_lbl.set_valign(Gtk.Align.CENTER)
            self.count_lbl.set_visible(int(str(count)) > 0 if str(count).isdigit() else True)
            box.append(self.count_lbl)

        self._badge_css = badge_css
        self.set_child(box)

    def set_count(self, n):
        if self.count_lbl:
            self.count_lbl.set_label(str(n))
            self.count_lbl.set_visible(n > 0)
