"""
PacHub — dialogs.py
All modal tool dialogs:
  - TerminalDialog  : PTY-backed command runner with sudo password input
  - RepoManagerDialog : View/edit /etc/pacman.conf repositories
  - MirrorRaterDialog : rate-mirrors front end
  - OrphanFinderDialog: list and remove orphaned packages
  - SysInfoDialog     : system information overview
"""

import os
import pty
import re as _re
import shlex
import select
import fcntl
import termios
import struct
import threading

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, GLib, Pango

from backend import (run_command, get_orphans, get_system_info,
                     get_pacman_history, get_cached_versions,
                     get_pkgbuild, get_pacnew_files, get_file_diff)


# ─── Terminal dialog ──────────────────────────────────────────────────────────

def run_terminal_dialog(parent, cmd, title, on_success=None, on_done_extra=None):
    """
    Open a PTY-backed terminal dialog that runs *cmd*.
    Calls on_success() (on the main thread) if the command exits with code 0.
    """
    dialog = Adw.Dialog()
    dialog.set_title(title)
    dialog.set_content_width(720)
    dialog.set_content_height(520)
    dialog.set_follows_content_size(False)

    tv  = Adw.ToolbarView()
    hdr = Adw.HeaderBar()
    hdr.set_show_end_title_buttons(False)

    title_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    spinner   = Gtk.Spinner()
    spinner.start()
    spinner.set_size_request(16, 16)
    title_box.append(spinner)
    lbl = Gtk.Label(label=title)
    lbl.add_css_class("heading")
    title_box.append(lbl)
    hdr.set_title_widget(title_box)

    close_btn = Gtk.Button(label="Close")
    close_btn.add_css_class("suggested-action")
    close_btn.set_sensitive(False)
    hdr.pack_end(close_btn)

    cancel_btn = Gtk.Button(label="Cancel")
    cancel_btn.add_css_class("destructive-action")
    cancel_btn.add_css_class("flat")
    hdr.pack_start(cancel_btn)
    tv.add_top_bar(hdr)

    cmd_lbl = Gtk.Label(label=f"$ {cmd}")
    cmd_lbl.add_css_class("caption")
    cmd_lbl.add_css_class("dim-label")
    cmd_lbl.set_halign(Gtk.Align.START)
    cmd_lbl.set_ellipsize(Pango.EllipsizeMode.END)
    cmd_lbl.set_margin_start(14); cmd_lbl.set_margin_end(14)
    cmd_lbl.set_margin_top(6);    cmd_lbl.set_margin_bottom(4)
    tv.add_top_bar(cmd_lbl)

    outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    outer.set_margin_top(8);    outer.set_margin_bottom(12)
    outer.set_margin_start(12); outer.set_margin_end(12)

    scroll = Gtk.ScrolledWindow()
    scroll.set_vexpand(True); scroll.set_hexpand(True)
    scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
    scroll.add_css_class("card")

    term_buf  = Gtk.TextBuffer()
    term_view = Gtk.TextView(buffer=term_buf)
    term_view.set_editable(False)
    term_view.set_cursor_visible(False)
    term_view.set_wrap_mode(Gtk.WrapMode.CHAR)
    term_view.add_css_class("terminal-view")
    term_view.set_monospace(True)
    scroll.set_child(term_view)
    outer.append(scroll)

    # Password / stdin input row
    input_frame = Gtk.Frame()
    input_frame.add_css_class("card")
    input_frame.set_margin_top(2)

    input_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    input_box.set_margin_top(8);    input_box.set_margin_bottom(8)
    input_box.set_margin_start(10); input_box.set_margin_end(10)

    pw_icon = Gtk.Image.new_from_icon_name("dialog-password-symbolic")
    pw_icon.set_pixel_size(16)
    pw_icon.add_css_class("dim-label")
    input_box.append(pw_icon)

    pw_entry = Gtk.Entry()
    pw_entry.set_hexpand(True)
    pw_entry.set_visibility(False)
    pw_entry.set_input_purpose(Gtk.InputPurpose.PASSWORD)
    pw_entry.set_placeholder_text("Password or input — press Enter to send")
    input_box.append(pw_entry)

    send_btn = Gtk.Button(label="Send")
    send_btn.add_css_class("suggested-action")
    input_box.append(send_btn)

    toggle_vis_btn = Gtk.ToggleButton()
    toggle_vis_btn.set_icon_name("view-reveal-symbolic")
    toggle_vis_btn.add_css_class("flat")
    toggle_vis_btn.set_tooltip_text("Show/hide input")
    toggle_vis_btn.connect("toggled", lambda b, *_: pw_entry.set_visibility(b.get_active()))
    input_box.append(toggle_vis_btn)

    input_frame.set_child(input_box)
    outer.append(input_frame)

    tv.set_content(outer)
    dialog.set_child(tv)
    dialog.present(parent)

    # ── Internal state ────────────────────────────────────────────────────────
    _master_fd = [None]
    _proc      = [None]
    _running   = [True]

    _ANSI = _re.compile(
        r'\x1b\[[0-9;?]*[ -/]*[@-~]'
        r'|\x1b[()][AB012]'
        r'|\x1b[^[]'
        r'|\x08'
        r'|\r'
    )

    def strip_ansi(s):
        s = s.replace('\r\n', '\n').replace('\r', '\n')
        return _ANSI.sub('', s)

    def append_output(raw_text):
        cleaned = strip_ansi(raw_text)
        if not cleaned:
            return False
        end_iter = term_buf.get_end_iter()
        term_buf.insert(end_iter, cleaned)
        mark = term_buf.get_insert()
        term_view.scroll_mark_onscreen(mark)
        adj = scroll.get_vadjustment()
        GLib.idle_add(lambda: adj.set_value(adj.get_upper()))
        return False

    def send_input(*_):
        text = pw_entry.get_text()
        pw_entry.set_text("")
        if _master_fd[0] is not None:
            try:
                os.write(_master_fd[0], (text + "\n").encode())
                append_output("(input sent)\n")
            except OSError:
                pass

    pw_entry.connect("activate", send_input)
    send_btn.connect("clicked", send_input)

    def on_close_clicked(*_):
        close_btn.grab_focus()
        dialog.close()
    close_btn.connect("clicked", on_close_clicked)

    def do_cancel(*_):
        if _proc[0] is not None:
            try:
                os.killpg(os.getpgid(_proc[0].pid), __import__('signal').SIGTERM)
            except Exception:
                try:
                    _proc[0].terminate()
                except Exception:
                    pass
        cancel_btn.set_sensitive(False)
        cancel_btn.grab_focus()
        append_output("\n— Cancelled —\n")
    cancel_btn.connect("clicked", do_cancel)

    def on_done(code):
        _running[0] = False
        spinner.stop()
        cancel_btn.set_visible(False)
        close_btn.set_sensitive(True)
        close_btn.grab_focus()
        sep = "\n" + "─" * 56 + "\n"
        if code == 0:
            append_output(sep + "✓  Completed successfully\n")
        else:
            append_output(sep + f"✗  Failed  (exit code {code})\n")
        pw_entry.set_sensitive(False)
        send_btn.set_sensitive(False)
        if code == 0 and on_success:
            on_success()
        if on_done_extra:
            on_done_extra(code)
        return False

    # ── PTY worker ────────────────────────────────────────────────────────────
    def worker():
        master_fd, slave_fd = pty.openpty()
        _master_fd[0] = master_fd

        try:
            ws = struct.pack('HHHH', 40, 120, 0, 0)
            fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, ws)
        except Exception:
            pass

        safe_title = title.replace("'", "")
        wrapped = (
            f"printf '\\033[1m>>> {safe_title}\\033[0m\\n'; "
            f"echo; "
            f"{cmd}; "
            f"_ec=$?; "
            f"exit $_ec"
        )

        env = dict(os.environ)
        env['TERM'] = 'xterm-256color'
        env.pop('SUDO_ASKPASS', None)

        try:
            import subprocess
            proc = subprocess.Popen(
                ["sh", "-c", wrapped],
                stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
                close_fds=True, preexec_fn=os.setsid, env=env,
            )
            _proc[0] = proc
            os.close(slave_fd)

            partial = b""
            while True:
                try:
                    rlist, _, _ = select.select([master_fd], [], [], 0.05)
                except (ValueError, OSError):
                    break

                if rlist:
                    try:
                        chunk = os.read(master_fd, 8192)
                    except OSError:
                        break
                    if not chunk:
                        break
                    partial += chunk
                    try:
                        text = partial.decode('utf-8')
                        partial = b""
                    except UnicodeDecodeError:
                        for cut in range(len(partial), 0, -1):
                            try:
                                text = partial[:cut].decode('utf-8')
                                partial = partial[cut:]
                                break
                            except UnicodeDecodeError:
                                continue
                        else:
                            text = partial.decode('latin-1')
                            partial = b""
                    GLib.idle_add(append_output, text)

                elif proc.poll() is not None:
                    try:
                        while True:
                            r2, _, _ = select.select([master_fd], [], [], 0.05)
                            if not r2:
                                break
                            chunk = os.read(master_fd, 8192)
                            if not chunk:
                                break
                            GLib.idle_add(append_output, chunk.decode('utf-8', errors='replace'))
                    except OSError:
                        pass
                    break

            proc.wait()
            code = proc.returncode

        except Exception as exc:
            GLib.idle_add(append_output, f"\nInternal error: {exc}\n")
            code = 1

        finally:
            try:
                os.close(master_fd)
            except OSError:
                pass
            _master_fd[0] = None

        GLib.idle_add(on_done, code)

    threading.Thread(target=worker, daemon=True).start()


# ─── Repository manager dialog ────────────────────────────────────────────────

def show_repo_manager(parent, run_terminal_fn):
    dialog = Adw.Dialog()
    dialog.set_title("Manage Repositories")
    dialog.set_content_width(640)
    dialog.set_content_height(500)

    tv  = Adw.ToolbarView()
    hdr = Adw.HeaderBar()
    hdr.set_show_end_title_buttons(False)

    edit_btn = Gtk.Button(label="Edit pacman.conf")
    edit_btn.add_css_class("suggested-action")
    edit_btn.connect("clicked", lambda *_: (
        dialog.close(),
        run_terminal_fn("sudo -S ${VISUAL:-${EDITOR:-nano}} /etc/pacman.conf", "Edit pacman.conf")
    ))
    hdr.pack_end(edit_btn)

    close_btn = Gtk.Button(label="Close")
    close_btn.add_css_class("flat")
    close_btn.connect("clicked", lambda *_: dialog.close())
    hdr.pack_start(close_btn)
    tv.add_top_bar(hdr)

    outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
    outer.set_margin_top(12);   outer.set_margin_bottom(16)
    outer.set_margin_start(16); outer.set_margin_end(16)

    repos_group = Adw.PreferencesGroup()
    repos_group.set_title("Active Repositories")
    repos_group.set_description("Repositories currently enabled in /etc/pacman.conf")

    out, code = run_command("pacman -Sl 2>/dev/null | awk '{print $1}' | sort -u")
    repos = [r for r in out.splitlines() if r.strip()] if (out and code == 0) else ["core", "extra", "multilib"]

    for repo in repos:
        row = Adw.ActionRow()
        row.set_title(repo)
        icon = Gtk.Image.new_from_icon_name("folder-symbolic")
        icon.add_css_class("dim-label")
        row.add_prefix(icon)
        pkg_out, _ = run_command(f"pacman -Sl {repo} 2>/dev/null | wc -l")
        if pkg_out and pkg_out.strip().isdigit():
            count_lbl = Gtk.Label(label=f"{pkg_out.strip()} pkgs")
            count_lbl.add_css_class("caption"); count_lbl.add_css_class("dim-label")
            row.add_suffix(count_lbl)
        repos_group.add(row)
    outer.append(repos_group)

    conf_group = Adw.PreferencesGroup()
    conf_group.set_title("pacman.conf")
    conf_group.set_description("/etc/pacman.conf — read-only view")

    scroll = Gtk.ScrolledWindow()
    scroll.set_min_content_height(180)
    scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
    scroll.add_css_class("card")

    conf_out, _ = run_command("cat /etc/pacman.conf 2>/dev/null")
    buf = Gtk.TextBuffer()
    buf.set_text(conf_out or "# /etc/pacman.conf not found or not readable")
    conf_view = Gtk.TextView(buffer=buf)
    conf_view.set_editable(False); conf_view.set_monospace(True)
    conf_view.set_wrap_mode(Gtk.WrapMode.NONE)
    conf_view.add_css_class("terminal-view")
    scroll.set_child(conf_view)
    conf_group.add(scroll)
    outer.append(conf_group)

    scroller = Gtk.ScrolledWindow()
    scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    scroller.set_vexpand(True)
    scroller.set_child(outer)
    tv.set_content(scroller)
    dialog.set_child(tv)
    dialog.present(parent)


# ─── Mirror rater dialog ──────────────────────────────────────────────────────

def show_mirror_rater(parent, run_terminal_fn):
    dialog = Adw.Dialog()
    dialog.set_title("Rate Mirrors")
    dialog.set_content_width(600)
    dialog.set_content_height(560)

    tv  = Adw.ToolbarView()
    hdr = Adw.HeaderBar()
    hdr.set_show_end_title_buttons(False)
    close_btn = Gtk.Button(label="Close")
    close_btn.add_css_class("flat")
    close_btn.connect("clicked", lambda *_: dialog.close())
    hdr.pack_start(close_btn)
    tv.add_top_bar(hdr)

    scroll = Gtk.ScrolledWindow()
    scroll.set_vexpand(True)
    scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

    outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
    outer.set_margin_top(16);   outer.set_margin_bottom(24)
    outer.set_margin_start(16); outer.set_margin_end(16)

    _, code = run_command("which rate-mirrors 2>/dev/null")
    has_rate_mirrors = (code == 0)

    if has_rate_mirrors:
        options_group = Adw.PreferencesGroup()
        options_group.set_title("Mirror Options")
        options_group.set_description(
            "rate-mirrors tests all Arch mirrors and saves the fastest to /etc/pacman.d/mirrorlist"
        )

        country_row = Adw.ActionRow()
        country_row.set_title("Countries")
        country_row.set_subtitle("Comma-separated country names, or blank for all")
        country_entry = Gtk.Entry()
        country_entry.set_placeholder_text("e.g. India, Germany, France")
        country_entry.set_hexpand(True)
        country_entry.set_valign(Gtk.Align.CENTER)
        country_entry.set_width_chars(24)
        country_row.add_suffix(country_entry)
        options_group.add(country_row)

        sort_row = Adw.ActionRow()
        sort_row.set_title("Sort by")
        sort_row.set_subtitle("How mirrors are ranked")
        sort_store = Gtk.StringList()
        sort_options = [
            ("score_asc",  "Score ↑  (best reliability first)"),
            ("score_desc", "Score ↓  (worst reliability first)"),
            ("delay_asc",  "Delay ↑  (freshest mirrors first)"),
            ("delay_desc", "Delay ↓  (oldest mirrors first)"),
            ("random",     "Random   (shuffle before testing)"),
        ]
        for _, label in sort_options:
            sort_store.append(label)
        sort_drop = Gtk.DropDown(model=sort_store)
        sort_drop.set_selected(0)
        sort_drop.set_valign(Gtk.Align.CENTER)
        sort_row.add_suffix(sort_drop)
        options_group.add(sort_row)

        protocol_row = Adw.ActionRow()
        protocol_row.set_title("HTTPS only")
        protocol_row.set_subtitle("Filter out plain HTTP mirrors")
        https_switch = Gtk.Switch()
        https_switch.set_active(True)
        https_switch.set_valign(Gtk.Align.CENTER)
        protocol_row.add_suffix(https_switch)
        protocol_row.set_activatable_widget(https_switch)
        options_group.add(protocol_row)

        backup_row = Adw.ActionRow()
        backup_row.set_title("Backup current mirrorlist")
        backup_row.set_subtitle("Saves existing list to mirrorlist-backup first")
        backup_switch = Gtk.Switch()
        backup_switch.set_active(True)
        backup_switch.set_valign(Gtk.Align.CENTER)
        backup_row.add_suffix(backup_switch)
        backup_row.set_activatable_widget(backup_switch)
        options_group.add(backup_row)

        delay_row = Adw.ActionRow()
        delay_row.set_title("Max mirror delay (hours)")
        delay_row.set_subtitle("Skip mirrors that are behind by more than this")
        delay_spin = Gtk.SpinButton()
        delay_spin.set_range(1, 72); delay_spin.set_increments(1, 6); delay_spin.set_value(6)
        delay_spin.set_valign(Gtk.Align.CENTER)
        delay_row.add_suffix(delay_spin)
        options_group.add(delay_row)

        top_row = Adw.ActionRow()
        top_row.set_title("Number of mirrors to keep")
        top_row.set_subtitle("0 = keep all ranked mirrors")
        top_spin = Gtk.SpinButton()
        top_spin.set_range(0, 50); top_spin.set_increments(1, 5); top_spin.set_value(0)
        top_spin.set_valign(Gtk.Align.CENTER)
        top_row.add_suffix(top_spin)
        options_group.add(top_row)

        outer.append(options_group)

        run_btn = Gtk.Button(label="Find Fastest Mirrors")
        run_btn.add_css_class("suggested-action")
        run_btn.set_halign(Gtk.Align.CENTER)

        def on_run(*_):
            countries_raw = country_entry.get_text().strip()
            sort_idx      = sort_drop.get_selected()
            sort_key      = sort_options[sort_idx][0]
            https_only    = https_switch.get_active()
            backup        = backup_switch.get_active()
            max_delay     = int(delay_spin.get_value()) * 3600
            top_n         = int(top_spin.get_value())

            global_flags = []
            if https_only:
                global_flags.append("--protocol=https")
            if top_n > 0:
                global_flags.append(f"--top-mirrors={top_n}")
            if countries_raw:
                first = countries_raw.split(",")[0].strip()
                global_flags.append(f"--entry-country={first!r}")

            sub_flags = [f"--sort-mirrors-by={sort_key}", f"--max-delay={max_delay}"]
            gf = " ".join(global_flags)
            sf = " ".join(sub_flags)

            if backup:
                cmd = (
                    f'sudo -S -v && '
                    f'TMPFILE="$(mktemp)" && '
                    f'rate-mirrors {gf} --save="$TMPFILE" arch {sf} '
                    f'&& sudo mv /etc/pacman.d/mirrorlist /etc/pacman.d/mirrorlist-backup '
                    f'&& sudo mv "$TMPFILE" /etc/pacman.d/mirrorlist '
                    f'&& echo "Done — backup saved to /etc/pacman.d/mirrorlist-backup"'
                )
            else:
                cmd = (
                    f'sudo -S -v && '
                    f'rate-mirrors {gf} arch {sf} '
                    f'| sudo tee /etc/pacman.d/mirrorlist > /dev/null '
                    f'&& echo "Done — /etc/pacman.d/mirrorlist updated"'
                )

            dialog.close()
            run_terminal_fn(cmd, "Rate Mirrors")

        run_btn.connect("clicked", on_run)
        outer.append(run_btn)

        preview_lbl = Gtk.Label()
        preview_lbl.add_css_class("caption"); preview_lbl.add_css_class("dim-label")
        preview_lbl.set_wrap(True); preview_lbl.set_wrap_mode(Pango.WrapMode.CHAR)
        preview_lbl.set_selectable(True); preview_lbl.set_halign(Gtk.Align.CENTER)

        def update_preview(*_):
            countries_raw = country_entry.get_text().strip()
            sort_idx  = sort_drop.get_selected()
            sort_key  = sort_options[sort_idx][0]
            https_only = https_switch.get_active()
            max_delay = int(delay_spin.get_value()) * 3600
            top_n     = int(top_spin.get_value())
            gflags = []
            if https_only: gflags.append("--protocol=https")
            if top_n > 0:  gflags.append(f"--top-mirrors={top_n}")
            if countries_raw:
                first = countries_raw.split(",")[0].strip()
                gflags.append(f"--entry-country={first!r}")
            sflags = [f"--sort-mirrors-by={sort_key}", f"--max-delay={max_delay}"]
            preview_lbl.set_label(
                f"rate-mirrors {' '.join(gflags)} arch {' '.join(sflags)} | sudo tee /etc/pacman.d/mirrorlist"
            )

        country_entry.connect("changed", update_preview)
        sort_drop.connect("notify::selected", update_preview)
        https_switch.connect("notify::active", update_preview)
        delay_spin.connect("value-changed", update_preview)
        top_spin.connect("value-changed", update_preview)
        update_preview()
        outer.append(preview_lbl)

    else:
        status = Adw.StatusPage()
        status.set_icon_name("network-transmit-receive-symbolic")
        status.set_title("rate-mirrors not installed")
        status.set_description(
            "rate-mirrors uses geo-aware routing to benchmark\n"
            "all Arch mirrors and pick the fastest ones."
        )
        install_btn = Gtk.Button(label="Install rate-mirrors")
        install_btn.add_css_class("suggested-action")
        install_btn.set_halign(Gtk.Align.CENTER)
        install_btn.connect("clicked", lambda *_: (
            dialog.close(),
            run_terminal_fn("sudo -S pacman -S --noconfirm rate-mirrors", "Install rate-mirrors")
        ))
        status.set_child(install_btn)
        outer.append(status)

    scroll.set_child(outer)
    tv.set_content(scroll)
    dialog.set_child(tv)
    dialog.present(parent)


# ─── Orphan finder dialog ─────────────────────────────────────────────────────

def show_orphan_finder(parent, run_terminal_fn):
    dialog = Adw.Dialog()
    dialog.set_title("Orphaned Packages")
    dialog.set_content_width(560)
    dialog.set_content_height(460)

    tv  = Adw.ToolbarView()
    hdr = Adw.HeaderBar()
    hdr.set_show_end_title_buttons(False)
    close_btn = Gtk.Button(label="Close")
    close_btn.add_css_class("flat")
    close_btn.connect("clicked", lambda *_: dialog.close())
    hdr.pack_start(close_btn)
    tv.add_top_bar(hdr)

    outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
    outer.set_margin_top(0); outer.set_margin_bottom(0)

    orphans = get_orphans()

    if not orphans:
        status = Adw.StatusPage()
        status.set_icon_name("emblem-ok-symbolic")
        status.set_title("No Orphans Found")
        status.set_description("Your system has no orphaned packages.")
        status.set_vexpand(True)
        outer.append(status)
    else:
        info_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        info_bar.set_margin_start(16); info_bar.set_margin_end(16)
        info_bar.set_margin_top(12);   info_bar.set_margin_bottom(8)
        info_icon = Gtk.Image.new_from_icon_name("dialog-warning-symbolic")
        info_icon.set_pixel_size(16)
        info_bar.append(info_icon)
        info_lbl = Gtk.Label(
            label=f"{len(orphans)} orphaned package(s) — installed as dependencies but no longer required"
        )
        info_lbl.add_css_class("caption")
        info_lbl.set_hexpand(True); info_lbl.set_halign(Gtk.Align.START); info_lbl.set_wrap(True)
        info_bar.append(info_lbl)
        outer.append(info_bar)

        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_margin_start(12); scroll.set_margin_end(12)

        listbox = Gtk.ListBox()
        listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        listbox.add_css_class("boxed-list")

        for o in orphans:
            row = Adw.ActionRow()
            row.set_title(o["name"]); row.set_subtitle(o["version"])
            icon = Gtk.Image.new_from_icon_name("package-x-generic-symbolic")
            icon.add_css_class("dim-label")
            row.add_prefix(icon)
            rm_btn = Gtk.Button(label="Remove")
            rm_btn.add_css_class("destructive-action"); rm_btn.add_css_class("flat")
            rm_btn.set_valign(Gtk.Align.CENTER)
            name = o["name"]
            rm_btn.connect("clicked", lambda *_, n=name: (
                dialog.close(),
                run_terminal_fn(f"sudo -S pacman -R --noconfirm {shlex.quote(n)}", f"Remove {n}")
            ))
            row.add_suffix(rm_btn)
            listbox.append(row)

        scroll.set_child(listbox)
        outer.append(scroll)

        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_box.set_halign(Gtk.Align.CENTER)
        btn_box.set_margin_top(12); btn_box.set_margin_bottom(16)
        names = " ".join(shlex.quote(o["name"]) for o in orphans)
        remove_all_btn = Gtk.Button(label=f"Remove All {len(orphans)} Orphans")
        remove_all_btn.add_css_class("destructive-action")
        remove_all_btn.connect("clicked", lambda *_: (
            dialog.close(),
            run_terminal_fn(f"sudo -S pacman -Rns --noconfirm {names}", "Remove All Orphans")
        ))
        btn_box.append(remove_all_btn)
        outer.append(btn_box)

    tv.set_content(outer)
    dialog.set_child(tv)
    dialog.present(parent)


# ─── System info dialog ───────────────────────────────────────────────────────

def show_sysinfo_dialog(parent):
    dialog = Adw.Dialog()
    dialog.set_title("System Information")
    dialog.set_content_width(520)
    dialog.set_content_height(520)

    tv  = Adw.ToolbarView()
    hdr = Adw.HeaderBar()
    hdr.set_show_end_title_buttons(False)
    close_btn = Gtk.Button(label="Close")
    close_btn.add_css_class("flat")
    close_btn.connect("clicked", lambda *_: dialog.close())
    hdr.pack_start(close_btn)
    tv.add_top_bar(hdr)

    scroll = Gtk.ScrolledWindow()
    scroll.set_vexpand(True)
    scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

    outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
    outer.set_margin_top(16);   outer.set_margin_bottom(24)
    outer.set_margin_start(16); outer.set_margin_end(16)

    loading_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
    loading_box.set_halign(Gtk.Align.CENTER); loading_box.set_valign(Gtk.Align.CENTER)
    loading_box.set_vexpand(True)
    loading_spinner = Gtk.Spinner()
    loading_spinner.start(); loading_spinner.set_size_request(32, 32)
    loading_spinner.set_halign(Gtk.Align.CENTER)
    loading_box.append(loading_spinner)
    loading_lbl = Gtk.Label(label="Gathering system info…")
    loading_lbl.add_css_class("dim-label")
    loading_box.append(loading_lbl)
    outer.append(loading_box)

    scroll.set_child(outer)
    tv.set_content(scroll)
    dialog.set_child(tv)
    dialog.present(parent)

    def populate(info):
        outer.remove(loading_box)

        sys_group = Adw.PreferencesGroup()
        sys_group.set_title("System")
        for key in ("OS", "Kernel", "Architecture"):
            if key in info:
                row = Adw.ActionRow(); row.set_title(key)
                val_lbl = Gtk.Label(label=info[key])
                val_lbl.add_css_class("caption"); val_lbl.add_css_class("dim-label")
                val_lbl.set_selectable(True)
                row.add_suffix(val_lbl)
                sys_group.add(row)
        outer.append(sys_group)

        hw_group = Adw.PreferencesGroup()
        hw_group.set_title("Hardware")
        for key in ("RAM", "Disk (/)"):
            if key in info:
                row = Adw.ActionRow(); row.set_title(key)
                val_lbl = Gtk.Label(label=info[key])
                val_lbl.add_css_class("caption"); val_lbl.add_css_class("dim-label")
                val_lbl.set_selectable(True)
                row.add_suffix(val_lbl)
                hw_group.add(row)
        outer.append(hw_group)

        pkg_group = Adw.PreferencesGroup()
        pkg_group.set_title("Packages")
        for key in ("Pacman", "Installed Packages", "Foreign (AUR) Packages", "Package Cache Size"):
            if key in info:
                row = Adw.ActionRow(); row.set_title(key)
                val_lbl = Gtk.Label(label=info[key])
                val_lbl.add_css_class("caption"); val_lbl.add_css_class("dim-label")
                val_lbl.set_selectable(True)
                row.add_suffix(val_lbl)
                pkg_group.add(row)
        outer.append(pkg_group)
        return False

    def worker():
        info = get_system_info()
        GLib.idle_add(populate, info)

    threading.Thread(target=worker, daemon=True).start()


# ─── Package history (pacman log) ─────────────────────────────────────────────

_HISTORY_ICONS = {
    "installed":   ("package-x-generic-symbolic",        "status-installed"),
    "removed":     ("user-trash-symbolic",               "status-foreign"),
    "upgraded":    ("software-update-available-symbolic", "status-update"),
    "downgraded":  ("go-down-symbolic",                  "status-update"),
    "reinstalled": ("view-refresh-symbolic",             None),
}


def show_history_dialog(parent):
    dialog = Adw.Dialog()
    dialog.set_title("Package History")
    dialog.set_content_width(640)
    dialog.set_content_height(560)

    tv  = Adw.ToolbarView()
    hdr = Adw.HeaderBar()
    hdr.set_show_end_title_buttons(False)
    close_btn = Gtk.Button(label="Close")
    close_btn.add_css_class("flat")
    close_btn.connect("clicked", lambda *_: dialog.close())
    hdr.pack_start(close_btn)
    tv.add_top_bar(hdr)

    outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

    search = Gtk.SearchEntry()
    search.set_placeholder_text("Filter by package name…")
    search.set_margin_start(12); search.set_margin_end(12)
    search.set_margin_top(10);   search.set_margin_bottom(6)
    outer.append(search)

    scroll = Gtk.ScrolledWindow()
    scroll.set_vexpand(True)
    scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    scroll.set_margin_start(12); scroll.set_margin_end(12); scroll.set_margin_bottom(12)
    listbox = Gtk.ListBox()
    listbox.set_selection_mode(Gtk.SelectionMode.NONE)
    listbox.add_css_class("boxed-list")
    scroll.set_child(listbox)
    outer.append(scroll)

    tv.set_content(outer)
    dialog.set_child(tv)
    dialog.present(parent)

    def render(entries):
        while listbox.get_first_child():
            listbox.remove(listbox.get_first_child())
        q = search.get_text().strip().lower()
        shown = 0
        for e in entries:
            if q and q not in e["name"].lower():
                continue
            row = Adw.ActionRow()
            row.set_title(e["name"])
            row.set_subtitle(f"{e['action']} · {e['version']} · {e['time']}")
            row.set_subtitle_selectable(True)
            icon_name, css = _HISTORY_ICONS.get(e["action"], ("package-x-generic-symbolic", None))
            icon = Gtk.Image.new_from_icon_name(icon_name)
            icon.add_css_class("dim-label")
            row.add_prefix(icon)
            badge = Gtk.Label(label=e["action"].upper())
            badge.add_css_class("row-status-pill")
            if css:
                badge.add_css_class(css)
            badge.set_valign(Gtk.Align.CENTER)
            row.add_suffix(badge)
            listbox.append(row)
            shown += 1
        if shown == 0:
            empty = Adw.ActionRow()
            empty.set_title("No matching entries")
            listbox.append(empty)

    def worker():
        entries = get_pacman_history()
        def show():
            render(entries)
            search.connect("search-changed", lambda *_: render(entries))
            return False
        GLib.idle_add(show)

    threading.Thread(target=worker, daemon=True).start()


# ─── Downgrade from cache ─────────────────────────────────────────────────────

def show_downgrade_dialog(parent, pkg_name, run_terminal_fn):
    dialog = Adw.Dialog()
    dialog.set_title(f"Downgrade {pkg_name}")
    dialog.set_content_width(560)
    dialog.set_content_height(420)

    tv  = Adw.ToolbarView()
    hdr = Adw.HeaderBar()
    hdr.set_show_end_title_buttons(False)
    close_btn = Gtk.Button(label="Close")
    close_btn.add_css_class("flat")
    close_btn.connect("clicked", lambda *_: dialog.close())
    hdr.pack_start(close_btn)
    tv.add_top_bar(hdr)

    outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
    versions = get_cached_versions(pkg_name)

    if not versions:
        status = Adw.StatusPage()
        status.set_icon_name("package-x-generic-symbolic")
        status.set_title("No Cached Versions")
        status.set_description(
            f"No package files for {pkg_name} were found in /var/cache/pacman/pkg.\n"
            "Older versions are only available while they remain in the cache.")
        status.set_vexpand(True)
        outer.append(status)
    else:
        info_bar = Gtk.Label(
            label=f"{len(versions)} cached version(s) — pick one to install with pacman -U")
        info_bar.add_css_class("caption"); info_bar.set_wrap(True)
        info_bar.set_halign(Gtk.Align.START)
        info_bar.set_margin_start(16); info_bar.set_margin_end(16)
        info_bar.set_margin_top(12);   info_bar.set_margin_bottom(8)
        outer.append(info_bar)

        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_margin_start(12); scroll.set_margin_end(12); scroll.set_margin_bottom(12)
        listbox = Gtk.ListBox()
        listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        listbox.add_css_class("boxed-list")

        for version, filepath in versions:
            row = Adw.ActionRow()
            row.set_title(version)
            row.set_subtitle(filepath)
            row.set_subtitle_selectable(True)
            btn = Gtk.Button(label="Install")
            btn.add_css_class("suggested-action"); btn.add_css_class("flat")
            btn.set_valign(Gtk.Align.CENTER)
            btn.connect("clicked", lambda *_, fp=filepath, v=version: (
                dialog.close(),
                run_terminal_fn(f"sudo -S pacman -U --noconfirm {shlex.quote(fp)}",
                                f"Downgrade {pkg_name} to {v}")
            ))
            row.add_suffix(btn)
            listbox.append(row)

        scroll.set_child(listbox)
        outer.append(scroll)

    tv.set_content(outer)
    dialog.set_child(tv)
    dialog.present(parent)


# ─── PKGBUILD viewer (AUR) ────────────────────────────────────────────────────

def show_pkgbuild_dialog(parent, pkg_name, on_install):
    dialog = Adw.Dialog()
    dialog.set_title(f"PKGBUILD — {pkg_name}")
    dialog.set_content_width(760)
    dialog.set_content_height(560)

    tv  = Adw.ToolbarView()
    hdr = Adw.HeaderBar()
    hdr.set_show_end_title_buttons(False)
    close_btn = Gtk.Button(label="Close")
    close_btn.add_css_class("flat")
    close_btn.connect("clicked", lambda *_: dialog.close())
    hdr.pack_start(close_btn)
    install_btn = Gtk.Button(label="Install")
    install_btn.add_css_class("suggested-action")
    install_btn.connect("clicked", lambda *_: (dialog.close(), on_install()))
    hdr.pack_end(install_btn)
    tv.add_top_bar(hdr)

    scroll = Gtk.ScrolledWindow()
    scroll.set_vexpand(True)
    label = Gtk.Label(label="Loading PKGBUILD…")
    label.set_selectable(True); label.set_wrap(True)
    label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    label.add_css_class("monospace"); label.add_css_class("caption")
    label.set_xalign(0); label.set_yalign(0)
    label.set_margin_start(12); label.set_margin_end(12)
    label.set_margin_top(8);    label.set_margin_bottom(8)
    scroll.set_child(label)
    tv.set_content(scroll)
    dialog.set_child(tv)
    dialog.present(parent)

    def worker():
        text = get_pkgbuild(pkg_name)
        GLib.idle_add(label.set_label, text)

    threading.Thread(target=worker, daemon=True).start()


# ─── .pacnew / .pacsave manager ───────────────────────────────────────────────

def show_pacdiff_dialog(parent, run_terminal_fn):
    dialog = Adw.Dialog()
    dialog.set_title("Config Files (.pacnew / .pacsave)")
    dialog.set_content_width(720)
    dialog.set_content_height(560)

    tv  = Adw.ToolbarView()
    hdr = Adw.HeaderBar()
    hdr.set_show_end_title_buttons(False)
    close_btn = Gtk.Button(label="Close")
    close_btn.add_css_class("flat")
    close_btn.connect("clicked", lambda *_: dialog.close())
    hdr.pack_start(close_btn)
    tv.add_top_bar(hdr)

    outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
    loading = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
    loading.set_halign(Gtk.Align.CENTER); loading.set_valign(Gtk.Align.CENTER)
    loading.set_vexpand(True)
    sp = Gtk.Spinner(); sp.start(); sp.set_size_request(32, 32)
    loading.append(sp)
    loading.append(Gtk.Label(label="Scanning for .pacnew/.pacsave files…"))
    outer.append(loading)

    tv.set_content(outer)
    dialog.set_child(tv)
    dialog.present(parent)

    def render(files):
        outer.remove(loading)
        if not files:
            status = Adw.StatusPage()
            status.set_icon_name("emblem-ok-symbolic")
            status.set_title("Nothing to Merge")
            status.set_description("No .pacnew or .pacsave files were found.")
            status.set_vexpand(True)
            outer.append(status)
            return

        info = Gtk.Label(label=(
            f"{len(files)} file(s) left behind by package updates. Review the diff, "
            "then keep the new version or discard it."))
        info.add_css_class("caption"); info.set_wrap(True); info.set_halign(Gtk.Align.START)
        info.set_margin_start(16); info.set_margin_end(16)
        info.set_margin_top(12);   info.set_margin_bottom(8)
        outer.append(info)

        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_margin_start(12); scroll.set_margin_end(12); scroll.set_margin_bottom(12)
        group = Adw.PreferencesGroup()

        for fdict in files:
            new, orig, kind = fdict["new"], fdict["orig"], fdict["kind"]
            exp = Adw.ExpanderRow()
            exp.set_title(orig)
            exp.set_subtitle(f"{kind} · {new}")

            diff_scroll = Gtk.ScrolledWindow()
            diff_scroll.set_min_content_height(160); diff_scroll.set_max_content_height(300)
            diff_lbl = Gtk.Label(label="Loading diff…")
            diff_lbl.set_selectable(True); diff_lbl.set_wrap(True)
            diff_lbl.set_wrap_mode(Pango.WrapMode.CHAR)
            diff_lbl.add_css_class("monospace"); diff_lbl.add_css_class("caption")
            diff_lbl.set_xalign(0); diff_lbl.set_yalign(0)
            diff_lbl.set_margin_start(12); diff_lbl.set_margin_end(12)
            diff_lbl.set_margin_top(6);    diff_lbl.set_margin_bottom(6)
            diff_scroll.set_child(diff_lbl)
            diff_row = Gtk.ListBoxRow(); diff_row.set_activatable(False)
            diff_row.set_child(diff_scroll)
            exp.add_row(diff_row)

            btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            btn_row.set_halign(Gtk.Align.END)
            btn_row.set_margin_start(12); btn_row.set_margin_end(12)
            btn_row.set_margin_top(6);    btn_row.set_margin_bottom(8)
            apply_btn = Gtk.Button(label="Use New (overwrite)")
            apply_btn.add_css_class("suggested-action")
            apply_btn.connect("clicked", lambda *_, n=new, o=orig: (
                dialog.close(),
                run_terminal_fn(f"sudo -S mv {shlex.quote(n)} {shlex.quote(o)}",
                                f"Apply {n}")))
            discard_btn = Gtk.Button(label="Discard")
            discard_btn.add_css_class("destructive-action"); discard_btn.add_css_class("flat")
            discard_btn.connect("clicked", lambda *_, n=new: (
                dialog.close(),
                run_terminal_fn(f"sudo -S rm {shlex.quote(n)}", f"Remove {n}")))
            btn_row.append(discard_btn)
            btn_row.append(apply_btn)
            wrap_row = Gtk.ListBoxRow(); wrap_row.set_activatable(False)
            wrap_row.set_child(btn_row)
            exp.add_row(wrap_row)

            group.add(exp)

            def load_diff(lbl=diff_lbl, o=orig, n=new):
                text = get_file_diff(o, n)
                GLib.idle_add(lbl.set_label, text)
            threading.Thread(target=load_diff, daemon=True).start()

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(group)
        scroll.set_child(box)
        outer.append(scroll)

    def worker():
        files = get_pacnew_files()
        GLib.idle_add(render, files)

    threading.Thread(target=worker, daemon=True).start()


# ─── Preferences ──────────────────────────────────────────────────────────────

def show_preferences(parent, on_changed):
    from backend import (load_settings, save_settings, is_update_timer_enabled,
                         enable_update_timer, disable_update_timer)
    s = load_settings()

    dlg = Adw.PreferencesDialog()
    dlg.set_title("Preferences")
    page = Adw.PreferencesPage()
    page.set_title("General")
    page.set_icon_name("preferences-system-symbolic")

    # AUR
    aur_group = Adw.PreferencesGroup()
    aur_group.set_title("AUR")

    helper_opts = ["auto", "yay", "paru", "pikaur", "none"]
    helper_row = Adw.ComboRow()
    helper_row.set_title("AUR Helper")
    helper_row.set_subtitle("Used for AUR installs, updates and PKGBUILDs")
    helper_row.set_model(Gtk.StringList.new(
        ["Auto-detect", "yay", "paru", "pikaur", "None (pacman only)"]))
    cur = s.get("aur_helper", "auto")
    helper_row.set_selected(helper_opts.index(cur) if cur in helper_opts else 0)
    helper_row.connect("notify::selected", lambda r, _: (
        save_settings({"aur_helper": helper_opts[r.get_selected()]}), on_changed()))
    aur_group.add(helper_row)

    inc_row = Adw.SwitchRow()
    inc_row.set_title("Include AUR in update checks")
    inc_row.set_active(s.get("include_aur_updates", True))
    inc_row.connect("notify::active", lambda r, _: (
        save_settings({"include_aur_updates": r.get_active()}), on_changed()))
    aur_group.add(inc_row)
    page.add(aur_group)

    # Behaviour
    beh = Adw.PreferencesGroup()
    beh.set_title("Behaviour")

    def _switch(title, subtitle, key):
        row = Adw.SwitchRow()
        row.set_title(title)
        if subtitle:
            row.set_subtitle(subtitle)
        row.set_active(s.get(key, True))
        row.connect("notify::active", lambda r, _: save_settings({key: r.get_active()}))
        beh.add(row)

    _switch("Confirm before removing packages", None, "confirm_remove")
    _switch("Check for updates on startup", None, "check_updates_on_start")
    _switch("Notify when updates are available", None, "notify_updates")
    _switch("Show Arch news before upgrades",
            "Warns about manual interventions before a system upgrade",
            "show_news_before_upgrade")
    page.add(beh)

    # Background service (systemd --user timer)
    svc = Adw.PreferencesGroup()
    svc.set_title("Background Service")
    svc.set_description("Check for updates and notify even when PacHub is closed, "
                        "via a systemd user timer")

    interval_opts = ["hourly", "6h", "daily"]
    interval_row = Adw.ComboRow()
    interval_row.set_title("Check interval")
    interval_row.set_model(Gtk.StringList.new(["Hourly", "Every 6 hours", "Daily"]))
    cur_int = s.get("bg_check_interval", "daily")
    interval_row.set_selected(interval_opts.index(cur_int) if cur_int in interval_opts else 2)

    bg_row = Adw.SwitchRow()
    bg_row.set_title("Run background update checks")
    bg_row.set_active(is_update_timer_enabled())

    def _apply_timer():
        if bg_row.get_active():
            enable_update_timer(interval_opts[interval_row.get_selected()])
        else:
            disable_update_timer()

    bg_row.connect("notify::active", lambda r, _: _apply_timer())

    def _on_interval(r, _):
        save_settings({"bg_check_interval": interval_opts[r.get_selected()]})
        if bg_row.get_active():        # re-arm with the new interval
            enable_update_timer(interval_opts[r.get_selected()])

    interval_row.connect("notify::selected", _on_interval)
    svc.add(bg_row)
    svc.add(interval_row)
    page.add(svc)

    dlg.add(page)
    dlg.present(parent)


# ─── Arch news (pre-upgrade) ──────────────────────────────────────────────────

def show_news_dialog(parent, on_proceed):
    dialog = Adw.Dialog()
    dialog.set_title("Arch Linux News")
    dialog.set_content_width(640)
    dialog.set_content_height(520)

    tv  = Adw.ToolbarView()
    hdr = Adw.HeaderBar()
    hdr.set_show_end_title_buttons(False)
    cancel_btn = Gtk.Button(label="Cancel")
    cancel_btn.add_css_class("flat")
    cancel_btn.connect("clicked", lambda *_: dialog.close())
    hdr.pack_start(cancel_btn)
    proceed_btn = Gtk.Button(label="Upgrade Now")
    proceed_btn.add_css_class("suggested-action")
    proceed_btn.connect("clicked", lambda *_: (dialog.close(), on_proceed()))
    hdr.pack_end(proceed_btn)
    tv.add_top_bar(hdr)

    outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
    loading = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
    loading.set_halign(Gtk.Align.CENTER); loading.set_valign(Gtk.Align.CENTER)
    loading.set_vexpand(True)
    sp = Gtk.Spinner(); sp.start(); sp.set_size_request(32, 32)
    loading.append(sp)
    loading.append(Gtk.Label(label="Fetching latest news…"))
    outer.append(loading)

    tv.set_content(outer)
    dialog.set_child(tv)
    dialog.present(parent)

    def render(items):
        outer.remove(loading)
        if items is None:
            status = Adw.StatusPage()
            status.set_icon_name("network-offline-symbolic")
            status.set_title("Could Not Fetch News")
            status.set_description("You appear to be offline. You can still proceed "
                                   "with the upgrade.")
            status.set_vexpand(True)
            outer.append(status)
            return
        if not items:
            status = Adw.StatusPage()
            status.set_icon_name("emblem-ok-symbolic")
            status.set_title("No Recent News")
            status.set_vexpand(True)
            outer.append(status)
            return

        hint = Gtk.Label(label="Review recent announcements before upgrading:")
        hint.add_css_class("caption"); hint.set_halign(Gtk.Align.START)
        hint.set_margin_start(16); hint.set_margin_end(16)
        hint.set_margin_top(12);   hint.set_margin_bottom(8)
        outer.append(hint)

        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_margin_start(12); scroll.set_margin_end(12); scroll.set_margin_bottom(12)
        group = Adw.PreferencesGroup()
        for it in items:
            row = Adw.ActionRow()
            row.set_title(GLib.markup_escape_text(it["title"]))
            row.set_subtitle(it["date"])
            if it["link"]:
                link = Gtk.LinkButton.new_with_label(it["link"], "Open")
                link.set_valign(Gtk.Align.CENTER)
                row.add_suffix(link)
            group.add(row)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL); box.append(group)
        scroll.set_child(box)
        outer.append(scroll)

    def worker():
        from backend import get_arch_news
        items = get_arch_news()
        GLib.idle_add(render, items)

    threading.Thread(target=worker, daemon=True).start()


# ─── Keyboard shortcuts ───────────────────────────────────────────────────────

_SHORTCUTS = [
    ("Ctrl+F",      "Focus search"),
    ("F5",          "Sync databases"),
    ("Ctrl+R",      "Refresh package list"),
    ("Ctrl+U",      "Check for updates"),
    ("Ctrl+,",      "Preferences"),
    ("Ctrl+Q",      "Quit"),
]


def show_shortcuts_dialog(parent):
    dialog = Adw.Dialog()
    dialog.set_title("Keyboard Shortcuts")
    dialog.set_content_width(420)

    tv  = Adw.ToolbarView()
    hdr = Adw.HeaderBar()
    hdr.set_show_end_title_buttons(False)
    close_btn = Gtk.Button(label="Close")
    close_btn.add_css_class("flat")
    close_btn.connect("clicked", lambda *_: dialog.close())
    hdr.pack_start(close_btn)
    tv.add_top_bar(hdr)

    group = Adw.PreferencesGroup()
    group.set_margin_top(12); group.set_margin_bottom(16)
    group.set_margin_start(12); group.set_margin_end(12)
    for keys, desc in _SHORTCUTS:
        row = Adw.ActionRow()
        row.set_title(desc)
        kbd = Gtk.Label(label=keys)
        kbd.add_css_class("dim-label"); kbd.add_css_class("monospace")
        kbd.set_valign(Gtk.Align.CENTER)
        row.add_suffix(kbd)
        group.add(row)

    tv.set_content(group)
    dialog.set_child(tv)
    dialog.present(parent)
