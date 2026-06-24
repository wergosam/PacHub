"""
PacHub — backend.py
All pacman / system data functions: package queries, AUR search,
update checks, orphan detection, system info, and demo data.

Caching strategy
----------------
~/.cache/pachub/
  installed.json   — pacman -Q + -Qm output, invalidated when pacman -Q changes
  syncdb.json      — pacman -Sl output, TTL 6 h (changes only after pacman -Sy)
  packages.json    — merged full package list (served instantly on next launch)
"""

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import hashlib
from pathlib import Path
from gi.repository import GLib


# ─── Cache paths ──────────────────────────────────────────────────────────────

CACHE_DIR      = Path.home() / ".cache" / "pachub"
PKG_CACHE      = CACHE_DIR / "packages.json"
SYNCDB_CACHE   = CACHE_DIR / "syncdb.json"
INSTALLED_CACHE= CACHE_DIR / "installed.json"
CACHE_VERSION  = 2   # bump when the cached package schema changes, to force a rebuild
SYNCDB_TTL     = 6 * 3600   # 6 hours

def _ensure_cache_dir():
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

def _read_json(path):
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return None

def _write_json(path, data):
    try:
        _ensure_cache_dir()
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(data, f, separators=(",", ":"))
        tmp.replace(path)
    except Exception:
        pass

# ─── User settings ────────────────────────────────────────────────────────────

CONFIG_DIR    = Path.home() / ".config" / "pachub"
SETTINGS_FILE = CONFIG_DIR / "settings.json"
_DEFAULT_SETTINGS = {
    "aur_helper":               "auto",   # auto | yay | paru | pikaur | none
    "include_aur_updates":      True,
    "confirm_remove":           True,
    "check_updates_on_start":   True,
    "show_news_before_upgrade": True,
    "notify_updates":           True,
    "bg_check_interval":        "daily",  # hourly | 6h | daily
}
_settings_cache = None


def load_settings():
    global _settings_cache
    if _settings_cache is None:
        data = _read_json(SETTINGS_FILE) or {}
        _settings_cache = {**_DEFAULT_SETTINGS, **data}
    return _settings_cache


def get_setting(key):
    return load_settings().get(key, _DEFAULT_SETTINGS.get(key))


def save_settings(new_values):
    """Merge new_values into the stored settings and persist them."""
    global _settings_cache, _aur_helper_cache
    _settings_cache = {**_DEFAULT_SETTINGS, **load_settings(), **new_values}
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        tmp = SETTINGS_FILE.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(_settings_cache, f, indent=2)
        tmp.replace(SETTINGS_FILE)
    except Exception:
        pass
    _aur_helper_cache = "__unset__"   # re-resolve helper in case the preference changed


def _file_age(path):
    """Return seconds since file was last modified, or infinity."""
    try:
        return time.time() - path.stat().st_mtime
    except Exception:
        return float("inf")


# ─── Helpers ──────────────────────────────────────────────────────────────────

# Force a stable C locale for all parsed queries, so pacman field names
# ("Depends On", "Required By", …) and values ("None") stay English regardless
# of the user's system language.
_C_ENV = {**os.environ, "LC_ALL": "C", "LANG": "C"}


def run_command(cmd, timeout=30):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                           timeout=timeout, env=_C_ENV)
        return r.stdout.strip(), r.returncode
    except subprocess.TimeoutExpired:
        return "", 1
    except Exception as e:
        return str(e), 1


def run_command_stream(cmd, on_line, on_done, timeout=180):
    """Run a non-interactive command, streaming output line by line."""
    def worker():
        try:
            proc = subprocess.Popen(
                cmd, shell=True,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True, bufsize=1
            )
            for line in proc.stdout:
                GLib.idle_add(on_line, line.rstrip())
            proc.wait()
            GLib.idle_add(on_done, proc.returncode)
        except Exception as e:
            GLib.idle_add(on_line, f"Error: {e}")
            GLib.idle_add(on_done, 1)
    threading.Thread(target=worker, daemon=True).start()


def _is_demo():
    _, code = run_command("which pacman 2>/dev/null")
    return code != 0


_aur_helper_cache = "__unset__"

def _find_aur_helper():
    """Return the AUR helper to use, honouring the user's preference. Cached."""
    pref = get_setting("aur_helper")
    if pref == "none":
        return None
    global _aur_helper_cache
    if _aur_helper_cache == "__unset__":
        candidates = [pref] if (pref and pref != "auto") else ("paru", "yay", "pikaur", "trizen")
        _aur_helper_cache = None
        for h in candidates:
            _, c = run_command(f"which {h} 2>/dev/null")
            if c == 0:
                _aur_helper_cache = h
                break
    return _aur_helper_cache


# ─── Popular AUR packages to always show in the list ─────────────────────────
POPULAR_AUR_PACKAGES = [
    ("yay",                    "12.3.5-1",   "Yet another yogurt - AUR helper written in Go"),
    ("paru",                   "2.0.4-1",    "Feature packed AUR helper"),
    ("google-chrome",          "124.0-1",    "The popular web browser by Google"),
    ("visual-studio-code-bin", "1.89.0-1",   "Visual Studio Code editor from Microsoft"),
    ("discord",                "0.0.57-1",   "All-in-one voice and text chat for gamers"),
    ("spotify",                "1.2.25-1",   "A proprietary music streaming service"),
    ("1password",              "8.10.30-1",  "Password manager and secure digital wallet"),
    ("zoom",                   "6.0.2-1",    "Video conferencing, web conferencing, webinars"),
    ("slack-desktop",          "4.38.125-1", "Messaging app for teams"),
    ("telegram-desktop-bin",   "5.1.6-1",    "Official Telegram Desktop client"),
    ("obs-studio-browser",     "30.1.2-1",   "Free and open source streaming/recording software"),
    ("timeshift",              "24.01.1-1",  "System restore utility for Linux"),
    ("ventoy-bin",             "1.0.99-1",   "Tool to create bootable USB drives"),
    ("onlyoffice-bin",         "8.0.1-1",    "Free office suite compatible with MS Office"),
    ("bottles",                "51.14-1",    "Run Windows software on Linux using Wine"),
    ("protonup-qt",            "2.9.0-1",    "Install and manage Proton-GE and Luxtorpeda"),
    ("heroic-games-launcher",  "2.14.0-1",   "Open source Epic, GOG and Amazon Games launcher"),
    ("lutris",                 "0.5.17-1",   "Open gaming platform for Linux"),
    ("mangohud",               "0.7.1-1",    "Vulkan/OpenGL overlay for monitoring FPS and temps"),
    ("nerd-fonts-complete",    "3.2.1-1",    "Iconic font aggregator and collection"),
    ("ttf-ms-fonts",           "0.1-9",      "Core Microsoft fonts"),
    ("pamac-aur",              "11.6.0-1",   "A Package Manager with AUR support"),
    ("pikaur",                 "1.28-1",     "Lightweight AUR package manager"),
    ("sublime-text-4",         "4.0.4180-1", "Sophisticated text editor for code and prose"),
    ("jetbrains-toolbox",      "2.3.2-1",    "JetBrains IDE manager"),
    ("postman-bin",            "11.0.9-1",   "API development environment"),
    ("insomnia",               "9.2.0-1",    "Open-source API client"),
    ("dbeaver",                "24.0.5-1",   "Universal database tool and SQL client"),
    ("wine",                   "9.8-1",      "A compatibility layer for running Windows programs"),
    ("steam",                  "1.0.0.79-2", "Valve's digital software delivery system"),
]


# ─── Installed-package fingerprint ────────────────────────────────────────────

def _installed_fingerprint():
    """Fast fingerprint of installed packages using local DB mtime + count."""
    local_db = Path("/var/lib/pacman/local")
    out, code = run_command("pacman -Q 2>/dev/null")
    if code != 0:
        return None
    # Combine package count + local db mtime for a fast, reliable fingerprint
    try:
        mtime = str(int(local_db.stat().st_mtime))
    except Exception:
        mtime = "0"
    pkg_count = str(out.count("\n"))
    fingerprint = hashlib.md5(f"{mtime}:{pkg_count}".encode()).hexdigest()
    return fingerprint, out


# ─── Sync-DB cache (pacman -Sl) ───────────────────────────────────────────────

def _load_syncdb_cache():
    """Return cached pacman -Sl data if it's fresh enough."""
    if _file_age(SYNCDB_CACHE) < SYNCDB_TTL:
        data = _read_json(SYNCDB_CACHE)
        if data:
            return data
    return None

def _parse_db_file(db_path):
    """Parse one pacman .db tarball and return {pkgname: (repo, version, desc)}."""
    import tarfile
    repo = db_path.stem  # filename without .db = repo name
    result = {}
    try:
        with tarfile.open(db_path, "r:*") as tar:
            members = {m.name: m for m in tar.getmembers()}
            desc_members = [m for name, m in members.items() if name.endswith("/desc")]
            for member in desc_members:
                f = tar.extractfile(member)
                if not f:
                    continue
                content = f.read().decode("utf-8", errors="replace")
                name = version = desc = None
                lines = content.splitlines()
                i = 0
                while i < len(lines):
                    tag = lines[i]
                    if tag in ("%NAME%", "%VERSION%", "%DESC%") and i + 1 < len(lines):
                        val = lines[i + 1].strip()
                        if tag == "%NAME%":
                            name = val
                        elif tag == "%VERSION%":
                            version = val
                        elif tag == "%DESC%":
                            desc = val
                    i += 1
                if name:
                    result[name] = (repo, version or "", desc or "")
    except Exception:
        pass
    return result


def _build_syncdb(installed_set):
    """Build sync DB from local pacman .db files (fast, no subprocess needed)."""
    from concurrent.futures import ThreadPoolExecutor
    sync_dir = Path("/var/lib/pacman/sync")
    pkgs = {}

    if sync_dir.exists():
        db_files = list(sync_dir.glob("*.db"))
        with ThreadPoolExecutor(max_workers=min(4, len(db_files) or 1)) as ex:
            futures = [ex.submit(_parse_db_file, db) for db in db_files]
            for future in futures:
                for name, (repo, version, desc) in future.result().items():
                    pkgs[name] = {"repo": repo, "version": version, "description": desc}

    # Fallback: if no .db files found, use pacman -Sl (slower)
    if not pkgs:
        sl_out, sl_code = run_command("pacman -Sl 2>/dev/null", timeout=60)
        if sl_out and sl_code == 0:
            for line in sl_out.splitlines():
                parts = line.strip().split()
                if len(parts) >= 3:
                    repo, pkgname, version = parts[0], parts[1], parts[2]
                    pkgs[pkgname] = {"repo": repo, "version": version, "description": ""}

    _write_json(SYNCDB_CACHE, pkgs)
    return pkgs


# ─── Main package list ────────────────────────────────────────────────────────

def _merge_into_list(installed_pkgs, syncdb, aur_set):
    """Combine installed + syncdb + popular AUR into the final package list."""
    all_pkgs = dict(installed_pkgs)

    for pkgname, info in syncdb.items():
        desc = info.get("description", "")
        if pkgname in all_pkgs:
            if not all_pkgs[pkgname]["foreign"]:
                all_pkgs[pkgname]["repo"] = info["repo"]
            # Always fill description from syncdb if missing
            if not all_pkgs[pkgname].get("description"):
                all_pkgs[pkgname]["description"] = desc
        else:
            all_pkgs[pkgname] = {
                "name": pkgname,
                "version": info["version"],
                "repo": info["repo"],
                "status": "available",
                "description": desc,
                "foreign": False,
            }

    # Popular AUR packages are injected only for discoverability. Their real
    # version is unknown without querying the AUR, so leave it blank rather than
    # show the hardcoded (and quickly stale) value from POPULAR_AUR_PACKAGES.
    for name, _version, desc in POPULAR_AUR_PACKAGES:
        if name not in all_pkgs:
            all_pkgs[name] = {
                "name": name, "version": "", "repo": "aur",
                "status": "available", "description": desc, "foreign": True,
            }

    return list(all_pkgs.values())


def get_packages():
    """
    Return package list as fast as possible.

    Launch path:
      1. If packages.json cache exists AND installed fingerprint matches → return cache instantly.
      2. Otherwise build from scratch (pacman -Q + cached/fresh pacman -Sl) and save cache.
    """
    if _is_demo():
        demo = []
        for name, version, desc in POPULAR_AUR_PACKAGES:
            demo.append({"name": name, "version": version, "repo": "aur",
                          "status": "available", "description": desc, "foreign": True})
        return demo

    # ── Fast path: fingerprint check ─────────────────────────────────────────
    result = _installed_fingerprint()
    if result is None:
        return []
    fingerprint, raw_Q = result

    cached = _read_json(PKG_CACHE)
    if (cached and cached.get("version") == CACHE_VERSION
            and cached.get("fingerprint") == fingerprint):
        return cached["packages"]

    # ── Slow path: rebuild ────────────────────────────────────────────────────
    # Step 1 — installed packages
    installed_pkgs = {}
    for line in raw_Q.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2:
            installed_pkgs[parts[0]] = {
                "name": parts[0], "version": parts[1],
                "repo": "local", "status": "installed",
                "description": "", "foreign": False,
            }

    # Step 2 — mark AUR/foreign
    foreign_out, _ = run_command("pacman -Qm 2>/dev/null")
    for line in (foreign_out or "").splitlines():
        parts = line.strip().split(None, 1)
        if parts and parts[0] in installed_pkgs:
            installed_pkgs[parts[0]]["foreign"] = True
            installed_pkgs[parts[0]]["repo"] = "aur"

    # Step 3 — sync DB (use cache if fresh, else rebuild)
    syncdb = _load_syncdb_cache()
    if syncdb is None:
        syncdb = _build_syncdb(set(installed_pkgs))

    # Step 4 — merge
    packages = _merge_into_list(installed_pkgs, syncdb, set())

    # Step 5 — save cache
    _write_json(PKG_CACHE, {"version": CACHE_VERSION, "fingerprint": fingerprint,
                            "packages": packages})

    return packages


def invalidate_cache():
    """Call this after install/remove/upgrade so next load rebuilds."""
    try:
        PKG_CACHE.unlink(missing_ok=True)
    except Exception:
        pass


def invalidate_syncdb_cache():
    """Call this after pacman -Sy so the sync DB is re-fetched."""
    try:
        SYNCDB_CACHE.unlink(missing_ok=True)
    except Exception:
        pass


# ─── Package info / files ─────────────────────────────────────────────────────

def get_package_info(pkg_name):
    q = shlex.quote(pkg_name)
    out, code = run_command(f"pacman -Qi {q} 2>/dev/null")
    if out and code == 0:
        return out
    out2, code2 = run_command(f"pacman -Si --noconfirm {q} 2>/dev/null")
    if out2 and code2 == 0:
        return out2

    # Not installed and not in the sync DB — for AUR packages, ask a helper.
    helper = _find_aur_helper()
    if helper:
        out3, code3 = run_command(f"{helper} -Si {q} 2>/dev/null", timeout=30)
        if out3 and code3 == 0:
            return out3

    if _is_demo():
        return (f"Name           : {pkg_name}\nVersion        : 1.0.0-1\n"
                f"Description    : Demo package (not on Arch Linux)\n"
                f"Architecture   : x86_64\nURL            : https://example.com/{pkg_name}\n"
                f"Licenses       : GPL\nGroups         : None\nProvides       : None\n"
                f"Depends On     : glibc\nOptional Deps  : None\nConflicts With : None\n"
                f"Replaces       : None\nInstalled Size : 1.20 MiB\nPackager       : Arch Linux\n"
                f"Build Date     : Thu 01 Jan 2026\nInstall Date   : Thu 01 Jan 2026\n"
                f"Install Reason : Explicitly installed\nValidated By   : Signature\n")

    # Honest fallback on a real system: don't fabricate version/dates/deps.
    return (f"Name           : {pkg_name}\n"
            f"Description    : No detailed information available — this package "
            f"is not installed and was not found in the sync databases"
            f"{' or AUR' if helper else ''}.\n")


def get_package_files(pkg_name):
    out, code = run_command(f"pacman -Ql {shlex.quote(pkg_name)} 2>/dev/null")
    if out and code == 0:
        return out.splitlines()
    return [f"{pkg_name} /usr/bin/{pkg_name}", f"{pkg_name} /usr/share/man/man1/{pkg_name}.1"]


# ─── Updates / orphans / sysinfo ─────────────────────────────────────────────

def check_updates():
    """Repo updates (checkupdates) plus AUR updates from a helper, if present."""
    updates = []
    seen = set()
    out, code = run_command("checkupdates 2>/dev/null || pacman -Qu 2>/dev/null", timeout=60)
    if out and code == 0:
        for line in out.splitlines():
            parts = line.strip().split()
            if len(parts) >= 4 and parts[0] not in seen:
                updates.append({"name": parts[0], "old": parts[1],
                                "new": parts[3], "aur": False})
                seen.add(parts[0])

    helper = _find_aur_helper()
    if helper and get_setting("include_aur_updates"):
        aout, acode = run_command(f"{helper} -Qua 2>/dev/null", timeout=90)
        if aout and acode == 0:
            for line in aout.splitlines():
                parts = line.strip().split()
                if len(parts) >= 4 and parts[0] not in seen:
                    updates.append({"name": parts[0], "old": parts[1],
                                    "new": parts[3], "aur": True})
                    seen.add(parts[0])
    return updates


def get_orphans():
    out, _ = run_command("pacman -Qdt 2>/dev/null")
    orphans = []
    if out:
        for line in out.splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) == 2:
                orphans.append({"name": parts[0], "version": parts[1]})
    if not orphans and _is_demo():
        orphans = [
            {"name": "lib32-libpng12", "version": "1.2.56-2"},
            {"name": "perl-encode-locale", "version": "1.05-7"},
            {"name": "python2", "version": "2.7.18-3"},
        ]
    return orphans


def _human_size(num_bytes):
    """Format a byte count base-1024, e.g. 1536 → '1.5 KiB'."""
    size = float(num_bytes)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024:
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PiB"


def _dir_size(path):
    """Total size of all files under `path`, in bytes (resilient to errors)."""
    total = 0
    try:
        with os.scandir(path) as it:
            for entry in it:
                try:
                    if entry.is_file(follow_symlinks=False):
                        total += entry.stat(follow_symlinks=False).st_size
                    elif entry.is_dir(follow_symlinks=False):
                        total += _dir_size(entry.path)
                except OSError:
                    pass
    except OSError:
        pass
    return total


def get_system_info():
    """System overview, derived in-process where possible (no shell pipelines)."""
    info = {}
    uname = os.uname()
    info["Kernel"] = uname.release
    info["Architecture"] = uname.machine

    os_name = "Unknown"
    try:
        with open("/etc/os-release") as f:
            for line in f:
                if line.startswith("PRETTY_NAME="):
                    os_name = line.split("=", 1)[1].strip().strip('"')
                    break
    except Exception:
        pass
    info["OS"] = os_name

    out, code = run_command("pacman -V 2>/dev/null")
    m = re.search(r"Pacman v?([\w.\-]+)", out) if (out and code == 0) else None
    info["Pacman"] = m.group(1) if m else "Unknown"

    try:
        du = shutil.disk_usage("/")
        pct = round(du.used / du.total * 100) if du.total else 0
        info["Disk (/)"] = f"{_human_size(du.used)} / {_human_size(du.total)} ({pct}% used)"
    except Exception:
        info["Disk (/)"] = "N/A"

    try:
        meminfo = {}
        with open("/proc/meminfo") as f:
            for line in f:
                key, _, rest = line.partition(":")
                if key in ("MemTotal", "MemAvailable"):
                    meminfo[key] = int(rest.split()[0]) * 1024   # kB → bytes
        total = meminfo.get("MemTotal", 0)
        if total:
            used = total - meminfo.get("MemAvailable", 0)
            info["RAM"] = f"{_human_size(used)} / {_human_size(total)}"
        else:
            info["RAM"] = "N/A"
    except Exception:
        info["RAM"] = "N/A"

    # Installed count = subdirs of the local pacman DB (no subprocess needed)
    try:
        with os.scandir("/var/lib/pacman/local") as it:
            info["Installed Packages"] = str(sum(1 for e in it if e.is_dir()))
    except OSError:
        out, code = run_command("pacman -Q 2>/dev/null")
        info["Installed Packages"] = str(len(out.splitlines())) if (out and code == 0) else "N/A"

    out, code = run_command("pacman -Qm 2>/dev/null")
    info["Foreign (AUR) Packages"] = str(len(out.splitlines())) if (out and code == 0) else "0"

    cache_dir = "/var/cache/pacman/pkg"
    info["Package Cache Size"] = _human_size(_dir_size(cache_dir)) if os.path.isdir(cache_dir) else "N/A"

    return info


# ─── Pacman log / history ─────────────────────────────────────────────────────

PACMAN_LOG = Path("/var/log/pacman.log")
_LOG_RE = re.compile(
    r"^\[(.*?)\]\s+\[ALPM\]\s+"
    r"(installed|removed|upgraded|downgraded|reinstalled)\s+(\S+)\s+\((.*?)\)")


def get_pacman_history(limit=500):
    """Parse recent ALPM transactions from /var/log/pacman.log, newest first."""
    try:
        lines = PACMAN_LOG.read_text(errors="replace").splitlines()
    except Exception:
        return []
    entries = []
    for line in lines:
        m = _LOG_RE.match(line)
        if m:
            ts, action, name, ver = m.groups()
            entries.append({"time": ts, "action": action, "name": name, "version": ver})
    entries.reverse()
    return entries[:limit]


# ─── Arch news (pre-upgrade warning) ──────────────────────────────────────────

def get_arch_news(limit=6):
    """Fetch recent Arch Linux news headlines. Returns a list, or None on failure
    (so callers can tell 'no news' apart from 'couldn't reach the server')."""
    import urllib.request
    import xml.etree.ElementTree as ET
    try:
        req = urllib.request.Request("https://archlinux.org/feeds/news/",
                                     headers={"User-Agent": "PacHub"})
        with urllib.request.urlopen(req, timeout=12) as r:
            root = ET.fromstring(r.read())
    except Exception:
        return None
    items = []
    for item in root.iterfind(".//item"):
        items.append({
            "title": (item.findtext("title") or "").strip(),
            "date":  (item.findtext("pubDate") or "").strip(),
            "link":  (item.findtext("link") or "").strip(),
        })
        if len(items) >= limit:
            break
    return items


# ─── Explicit-package export ──────────────────────────────────────────────────

def get_explicit_packages():
    """Names of explicitly-installed packages (pacman -Qqe), repo + AUR."""
    out, code = run_command("pacman -Qqe 2>/dev/null")
    return out.splitlines() if (out and code == 0) else []


# ─── Downgrade: cached package versions ───────────────────────────────────────

def get_pkgbuild(pkg_name):
    """Fetch the PKGBUILD text for an AUR package (helper first, then the AUR web)."""
    helper = _find_aur_helper()
    if helper:
        out, code = run_command(f"{helper} -Gp {shlex.quote(pkg_name)}", timeout=30)
        if out and code == 0:
            return out
    import urllib.request
    url = f"https://aur.archlinux.org/cgit/aur.git/plain/PKGBUILD?h={pkg_name}"
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            return r.read().decode("utf-8", errors="replace")
    except Exception as e:
        return f"# Could not fetch PKGBUILD for {pkg_name}\n# {e}\n"


# ─── .pacnew / .pacsave config files ──────────────────────────────────────────

def get_pacnew_files():
    """Find .pacnew/.pacsave files that need merging/reviewing."""
    out, _ = run_command(
        "find /etc /usr /boot /opt -xdev "
        "\\( -name '*.pacnew' -o -name '*.pacsave' \\) 2>/dev/null", timeout=30)
    files = []
    for line in out.splitlines():
        line = line.strip()
        if line.endswith(".pacnew") or line.endswith(".pacsave"):
            files.append({
                "new": line,
                "orig": line.rsplit(".", 1)[0],
                "kind": "pacnew" if line.endswith(".pacnew") else "pacsave",
            })
    return files


def get_file_diff(orig, new):
    out, _ = run_command(
        f"diff -u {shlex.quote(orig)} {shlex.quote(new)} 2>/dev/null", timeout=15)
    return out or "(files are identical or could not be read)"


# ─── IgnorePkg (hold) ─────────────────────────────────────────────────────────

PACMAN_CONF = Path("/etc/pacman.conf")


def get_ignored_packages():
    """Set of packages currently held back via IgnorePkg in pacman.conf."""
    ignored = set()
    try:
        for line in PACMAN_CONF.read_text(errors="replace").splitlines():
            s = line.strip()
            if s.startswith("#") or not s.startswith("IgnorePkg"):
                continue
            _, _, val = s.partition("=")
            ignored.update(val.split())
    except Exception:
        pass
    return ignored


def set_package_ignored(pkg_name, ignore):
    """Compute a pacman.conf with pkg_name added to / removed from IgnorePkg and
    write it to a temp file. Returns the temp path, or None if no change is needed.

    The editing happens here in Python (safe); the caller just needs to copy the
    temp file over /etc/pacman.conf with root privileges.
    """
    try:
        lines = PACMAN_CONF.read_text().splitlines()
    except Exception:
        return None

    idx, current = None, set()
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith("#"):
            continue
        if s.startswith("IgnorePkg"):
            idx = i
            _, _, val = s.partition("=")
            current = set(val.split())
            break

    if ignore:
        if pkg_name in current:
            return None
        current.add(pkg_name)
        new_line = "IgnorePkg = " + " ".join(sorted(current))
        if idx is not None:
            lines[idx] = new_line
        else:
            for i, line in enumerate(lines):
                if line.strip() == "[options]":
                    lines.insert(i + 1, new_line)
                    break
            else:
                lines.append(new_line)
    else:
        if pkg_name not in current:
            return None
        current.discard(pkg_name)
        if idx is not None:
            if current:
                lines[idx] = "IgnorePkg = " + " ".join(sorted(current))
            else:
                del lines[idx]

    import tempfile
    fd, tmp = tempfile.mkstemp(prefix="pachub-pacman-conf-", suffix=".conf")
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(lines) + "\n")
    return tmp


def get_cached_versions(pkg_name):
    """Return [(version, filepath), …] of cached .pkg files for pkg_name, newest first."""
    cache_dir = Path("/var/cache/pacman/pkg")
    results = []
    try:
        for f in cache_dir.glob(f"{pkg_name}-*.pkg.tar.*"):
            if f.name.endswith(".sig"):
                continue
            base = re.sub(r"\.pkg\.tar\.\w+$", "", f.name)
            parts = base.rsplit("-", 3)   # name-ver-rel-arch (name may contain '-')
            if len(parts) == 4 and parts[0] == pkg_name:
                results.append((f"{parts[1]}-{parts[2]}", str(f)))
    except Exception:
        pass
    results.sort(
        key=lambda vf: os.path.getmtime(vf[1]) if os.path.exists(vf[1]) else 0,
        reverse=True)
    return results


# ─── Search ───────────────────────────────────────────────────────────────────

def search_packages_cmd(query):
    def parse_pacman_ss(out):
        pkgs = []
        lines = out.splitlines()
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            if '/' in line and not line.startswith(' '):
                parts = line.split()
                if parts:
                    repo_pkg = parts[0]
                    version = parts[1] if len(parts) > 1 else "unknown"
                    repo, name = repo_pkg.split('/', 1) if '/' in repo_pkg else ('', repo_pkg)
                    desc = lines[i + 1].strip() if i + 1 < len(lines) else ""
                    pkgs.append({"name": name, "version": version, "repo": repo,
                                 "description": desc, "status": "available",
                                 "foreign": repo.lower() == "aur"})
                    i += 2
                    continue
            i += 1
        return pkgs

    packages = []
    seen = set()

    qq = shlex.quote(query)
    out, code = run_command(f"pacman -Ss {qq} 2>/dev/null")
    if out and code == 0:
        for p in parse_pacman_ss(out):
            if p["name"] not in seen:
                seen.add(p["name"])
                packages.append(p)

    aur_helper = _find_aur_helper()
    if aur_helper:
        aur_out, aur_code = run_command(f"{aur_helper} -Ss --aur {qq} 2>/dev/null", timeout=30)
        if aur_out and aur_code == 0:
            for p in parse_pacman_ss(aur_out):
                if p["name"] not in seen:
                    p["foreign"] = True
                    if p["repo"].lower() not in ("core", "extra", "multilib", "community"):
                        p["repo"] = "aur"
                    seen.add(p["name"])
                    packages.append(p)

    return packages


# ─── Background update-check (systemd --user timer) ───────────────────────────

SYSTEMD_USER_DIR = Path.home() / ".config" / "systemd" / "user"
TIMER_UNIT       = "pachub-update-check"
_TIMER_INTERVALS = {"hourly": "1h", "6h": "6h", "daily": "1d"}


def _notifier_path():
    return str(Path(__file__).resolve().parent / "notifier.py")


def is_update_timer_enabled():
    out, code = run_command(f"systemctl --user is-enabled {TIMER_UNIT}.timer 2>/dev/null")
    return code == 0 and out.strip() == "enabled"


def enable_update_timer(interval="daily"):
    """Write & enable the systemd --user timer that runs the notifier periodically."""
    on_active = _TIMER_INTERVALS.get(interval, "1d")
    python = sys.executable or "python3"
    notifier = _notifier_path()
    service = (
        "[Unit]\n"
        "Description=PacHub background update check\n\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"ExecStart={python} {notifier}\n")
    timer = (
        "[Unit]\n"
        "Description=PacHub periodic update check\n\n"
        "[Timer]\n"
        "OnBootSec=5min\n"
        f"OnUnitActiveSec={on_active}\n"
        "Persistent=true\n\n"
        "[Install]\n"
        "WantedBy=timers.target\n")
    try:
        SYSTEMD_USER_DIR.mkdir(parents=True, exist_ok=True)
        (SYSTEMD_USER_DIR / f"{TIMER_UNIT}.service").write_text(service)
        (SYSTEMD_USER_DIR / f"{TIMER_UNIT}.timer").write_text(timer)
    except Exception:
        return False
    run_command("systemctl --user daemon-reload")
    _, code = run_command(f"systemctl --user enable --now {TIMER_UNIT}.timer")
    return code == 0


def disable_update_timer():
    run_command(f"systemctl --user disable --now {TIMER_UNIT}.timer")
    return True


def send_update_notification(n):
    """Send a desktop notification about n available updates (via notify-send)."""
    if run_command("which notify-send 2>/dev/null")[1] != 0:
        return
    body = f"{n} package update{'s' if n != 1 else ''} can be installed."
    run_command(
        "notify-send --app-name=PacHub --icon=io.github.mrks1469.pachub "
        f"{shlex.quote('PacHub: Updates Available')} {shlex.quote(body)}")


def run_update_notification_check():
    """Entry point for the systemd timer: check for updates and notify if any."""
    if not get_setting("notify_updates"):
        return 0
    n = len(check_updates())
    if n > 0:
        send_update_notification(n)
    return n
