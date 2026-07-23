#!/usr/bin/env python3
"""
Snapshot a zellij session's tabs + the Claude Code session running in each,
then regenerate a zellij layout that resumes every Claude session on restore.

macOS-only (uses Darwin's libproc / KERN_PROCARGS2).

Core idea: don't reinvent zellij's layout format. Ask zellij for its live pane
table (`list-panes -aj`) — tab name, cwd, stable pane id — and for every pane
resolve which Claude session is *currently* active, then emit a minimal layout
that resumes each with `--resume <session-id>`.

How each pane's session is found — by asking the OS, no side channel:

1. Enumerate Claude's per-process runtime files `~/.claude/sessions/<pid>.json`
   (kind=interactive). Each records the process's CURRENT sessionId — correct
   even after /clear and for launches without --resume. (Not pgrep: it has
   been observed to miss live claude processes.)
2. Validate each pid's identity: alive + kernel start time (proc_pidinfo)
   matches the file's procStart (parsed as UTC, fixed English format) within
   2s — defeats pid reuse; argv[0] must be claude (kernel comm is the version
   string, e.g. '2.1.217', so comm can't be used).
3. Read the process's EXACT argv + env via sysctl KERN_PROCARGS2 (NUL
   boundaries — no `ps` string-splitting ambiguity). The env carries
   ZELLIJ_SESSION_NAME + ZELLIJ_PANE_ID: that's the join key.
4. The process must be a DESCENDANT of this session's `zellij --server`
   process (direct ppid is the fast path; restored panes run under a zsh
   wrapper so the chain is claude → zsh → server). This rejects orphans from
   a dead same-name session whose pane ids could otherwise collide.

zellij's `pane_command` is NEVER used for identity — it reports the current
foreground child (an MCP server, caffeinate, …), not the launch command. It is
only grepped for a stale `--resume` id as a last-resort hint for dead panes,
which is recorded in the manifest but NOT auto-restored (it may predate a
/clear); save prints the manual command instead.

Replayed flags are filtered through an allowlist: session-selection flags
(--fork-session, --continue, --session-id, …) would break "resume the exact
conversation" (e.g. `--resume B --fork-session` forks C instead of continuing
B). Unknown flags are recorded as unreplayed and reported loudly.

Failure model (identity errors never silently degrade a snapshot):
- identity infrastructure errors (unreadable runtime JSON / argv, ambiguous
  server, duplicate pane join) → the whole save ABORTS, nothing is written;
- a pane with no live claude (shell pane, exited claude) → skipped/failed,
  reported loudly;
- 0 resumable tabs → refuse to overwrite the existing snapshot.

Restore is deliberately manual: `restore` only prints a doctor + the exact
commands to run in a FRESH terminal. Auto-spawning a zellij server from within
a claude pane makes it inherit CLAUDE_CODE_CHILD_SESSION, so every restored
pane stops persisting its transcript — the layout also prefixes
CLAUDE_CODE_FORCE_SESSION_PERSISTENCE=1 as a fallback, but a clean launch
context is the real fix.

Usage:
  snapshot.py save    [--session NAME]     # default: $ZELLIJ_SESSION_NAME
  snapshot.py restore [--session NAME]     # doctor + manual restore commands
  snapshot.py show    [--session NAME]     # print the saved manifest

Platform dispatch: this file is the macOS implementation. On Windows it hands
off to scripts/snapshot_windows.py (which uses the Win32 PEB/CIM APIs instead of
Darwin's libproc/KERN_PROCARGS2). The handoff happens up top, before any of the
Darwin-only module-level code below runs -- e.g. ctypes.CDLL(None), which is a
POSIX idiom that fails on Windows.
"""

import os
import sys
if sys.platform == "win32":
    import runpy
    runpy.run_path(
        os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "snapshot_windows.py"),
        run_name="__main__")
    sys.exit()

import argparse
import calendar
import ctypes
import glob
import json
import os
import re
import shlex
import struct
import subprocess
import sys
import tempfile
import time

HOME = os.path.expanduser("~")
SNAP_ROOT = os.path.join(HOME, ".claude", "zellij-snapshots")
PROJECTS = os.path.join(HOME, ".claude", "projects")
SESSIONS = os.path.join(HOME, ".claude", "sessions")

# Flags replayed on restore, with arity. Conservative: only flags that are
# safe to re-apply to a resumed session. Session-selection / one-shot flags
# (--fork-session, --continue, --session-id, --print, …) are dropped: they
# would break "resume the exact conversation". Anything unrecognized is
# recorded as unreplayed and reported, never silently passed through.
ALLOW_FLAGS = {"--dangerously-skip-permissions": 0, "--model": 1,
               "--add-dir": 1, "--permission-mode": 1}
DROP_FLAGS = {"--resume": 1, "-r": 1, "--continue": 0, "-c": 0,
              "--session-id": 1, "--fork-session": 0, "--print": 0, "-p": 0}


class IdentityError(Exception):
    """A failure that makes pane→session identity untrustworthy. Aborts the
    whole save (fail closed) instead of degrading the snapshot."""


# ── Darwin process introspection (libproc + sysctl, stdlib-only) ──────────────

_libc = ctypes.CDLL(None)
_PROC_PIDTBSDINFO = 3
_BSDINFO_SIZE = 136          # sizeof(struct proc_bsdinfo)
_CTL_KERN, _KERN_PROCARGS2 = 1, 49


def proc_bsdinfo(pid):
    """(ppid, comm, start_epoch) for a live pid, else None. Offsets are from
    struct proc_bsdinfo: pbi_ppid @16, pbi_comm @48 (16 bytes),
    pbi_start_tvsec @120."""
    buf = ctypes.create_string_buffer(_BSDINFO_SIZE)
    if _libc.proc_pidinfo(pid, _PROC_PIDTBSDINFO, 0, buf, _BSDINFO_SIZE) \
            != _BSDINFO_SIZE:
        return None
    ppid = struct.unpack_from("I", buf, 16)[0]
    comm = buf.raw[48:64].split(b"\0")[0].decode("utf-8", "replace")
    start = struct.unpack_from("Q", buf, 120)[0]
    return ppid, comm, start


def proc_argv_env(pid):
    """EXACT (argv, env) of a live same-uid pid via sysctl KERN_PROCARGS2
    (NUL-separated — preserves spaces inside arguments and env values), else
    None. Note: KERN_PROCARGS2 is __APPLE_API_UNSTABLE; callers fail closed."""
    mib = (ctypes.c_int * 3)(_CTL_KERN, _KERN_PROCARGS2, pid)
    sz = ctypes.c_size_t(0)
    if _libc.sysctl(mib, 3, None, ctypes.byref(sz), None, 0) != 0:
        return None
    buf = ctypes.create_string_buffer(sz.value)
    if _libc.sysctl(mib, 3, buf, ctypes.byref(sz), None, 0) != 0:
        return None
    raw = buf.raw[:sz.value]
    if len(raw) < 4:
        return None
    argc = struct.unpack_from("i", raw, 0)[0]
    parts = raw[4:].split(b"\0")
    i = 1                             # skip exec_path
    while i < len(parts) and parts[i] == b"":
        i += 1                        # skip NUL padding
    strings = [p.decode("utf-8", "replace") for p in parts[i:] if p != b""]
    return strings[:argc], strings[argc:]


def all_pids():
    """Every pid on the system (proc_listallpids)."""
    n = _libc.proc_listallpids(None, 0)
    buf = (ctypes.c_int * (n + 64))()
    n = _libc.proc_listallpids(buf, ctypes.sizeof(buf))
    return [p for p in buf[:n] if p > 0]


_MONTHS = {m: i for i, m in enumerate(calendar.month_abbr) if m}


def parse_proc_start(s):
    """Claude runtime `procStart` ('Wed Jul 22 13:37:10 2026', UTC-rendered)
    → epoch. Fixed English format, hand-parsed — no locale, no timezone
    ambiguity. Raises ValueError on any mismatch (callers fail closed).
    (The runtime's `startedAt` field is NOT usable here: it is app-level
    registration time, measured 3–6s after kernel process start.)"""
    _, mon, day, hms, yr = s.split()
    h, mi, sec = (int(x) for x in hms.split(":"))
    return calendar.timegm((int(yr), _MONTHS[mon], int(day), h, mi, sec,
                            0, 0, 0))


def find_server_pid(session):
    """pid of THIS session's `zellij --server` process. The server's last argv
    is its socket path, whose basename is exactly the session name. Must match
    exactly one process — 0 or ≥2 makes every join untrustworthy."""
    found = []
    for pid in all_pids():
        info = proc_bsdinfo(pid)
        if not info or info[1] != "zellij":
            continue
        pa = proc_argv_env(pid)
        if not pa:
            continue                  # other-uid zellij — not ours
        argv = pa[0]
        if "--server" in argv and argv \
                and os.path.basename(argv[-1]) == session:
            found.append(pid)
    if len(found) != 1:
        raise IdentityError(
            f"expected exactly 1 `zellij --server` for '{session}', "
            f"found {len(found)} ({found})")
    return found[0]


def is_descendant(pid, ancestor, max_depth=10):
    """Whether `ancestor` is in pid's parent chain. Direct ppid is the fast
    path; restored panes run claude under a zsh wrapper (claude → zsh →
    server), hence the walk. Raises IdentityError if the tree can't be read
    mid-walk (fail closed)."""
    cur = pid
    for _ in range(max_depth):
        info = proc_bsdinfo(cur)
        if info is None:
            raise IdentityError(
                f"process tree changed while validating pid {pid}")
        ppid = info[0]
        if ppid == ancestor:
            return True
        if ppid <= 1:
            return False
        cur = ppid
    return False


# ── pane → session join ───────────────────────────────────────────────────────

def discover_claude_panes(session):
    """pane_id → {pid, session_id, cwd, argv} for every live interactive
    claude in `session`, validated end-to-end. Raises IdentityError on any
    condition that makes identity untrustworthy."""
    server = find_server_pid(session)
    join = {}
    for f in sorted(glob.glob(os.path.join(SESSIONS, "*.json"))):
        try:
            with open(f) as fh:
                d = json.load(fh)
        except (OSError, json.JSONDecodeError) as e:
            raise IdentityError(f"unreadable runtime file {f}: {e}")
        if d.get("kind") != "interactive" or not d.get("sessionId"):
            continue
        try:
            pid = int(d["pid"])
        except (KeyError, ValueError, TypeError):
            raise IdentityError(f"runtime file {f} has no valid pid")
        info = proc_bsdinfo(pid)
        if info is None:
            continue                  # process gone — stale file, normal
        _, _, kstart = info
        try:
            pstart = parse_proc_start(d["procStart"])
        except (KeyError, ValueError) as e:
            raise IdentityError(f"unparseable procStart in {f}: {e}")
        if abs(kstart - pstart) > 2:
            continue                  # pid reused by a NEWER process — stale
        pa = proc_argv_env(pid)
        if pa is None:
            raise IdentityError(
                f"cannot read argv/env of live pid {pid} ({f})")
        argv, env = pa
        if not argv or os.path.basename(argv[0]) != "claude":
            continue                  # pid reused by something else — stale
        envd = dict(e.split("=", 1) for e in env if "=" in e)
        if envd.get("ZELLIJ_SESSION_NAME") != session:
            continue                  # a claude in another zellij session
        try:
            pane_id = int(envd["ZELLIJ_PANE_ID"])
        except (KeyError, ValueError):
            continue                  # claude not under zellij
        if not is_descendant(pid, server):
            continue                  # orphan of a dead same-name session
        if pane_id in join:
            raise IdentityError(
                f"two live claudes claim pane {pane_id} of '{session}': "
                f"pids {join[pane_id]['pid']} and {pid}")
        join[pane_id] = {"pid": pid, "session_id": d["sessionId"],
                         "cwd": d.get("cwd"), "argv": argv}
    return join


def replay_flags(argv_tail):
    """Split a claude argv (minus argv[0]) into (replayable, unreplayed).
    Allowlisted flags are kept with their known arity; session-selection
    flags are dropped; everything else — unknown options, values following
    them, positionals, anything after `--` — is recorded, not replayed."""
    keep, unreplayed = [], []
    i, positional_only = 0, False
    while i < len(argv_tail):
        t = argv_tail[i]
        if positional_only:
            unreplayed.append(t)
            i += 1
        elif t == "--":
            positional_only = True
            i += 1
        elif t in ALLOW_FLAGS:
            n = ALLOW_FLAGS[t]
            keep.extend(argv_tail[i:i + 1 + n])
            i += 1 + n
        elif t in DROP_FLAGS:
            i += 1 + DROP_FLAGS[t]
        else:
            unreplayed.append(t)
            i += 1
    return keep, unreplayed


# ── zellij / claude filesystem helpers ────────────────────────────────────────

def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def current_session():
    name = os.environ.get("ZELLIJ_SESSION_NAME")
    if not name:
        sys.exit("Not inside a zellij session (ZELLIJ_SESSION_NAME unset). "
                 "Run this from within the session you want to snapshot, "
                 "or pass --session NAME.")
    return name


def project_dir_for(cwd_abs):
    return os.path.join(PROJECTS, cwd_abs.replace("/", "-"))


def list_panes(session):
    """zellij's live pane table as JSON (one object per pane). Works from
    anywhere via --session, so save doesn't need to run inside zellij."""
    r = run(["zellij", "--session", session, "action", "list-panes", "-aj"])
    if r.returncode != 0:
        sys.exit(f"`zellij action list-panes` failed for '{session}':\n"
                 f"{r.stderr}   Is it running?  `zellij list-sessions`")
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError as e:
        sys.exit(f"Could not parse list-panes JSON for '{session}': {e}")


def resolve_cwd(sid, cwds):
    """Pick the cwd to relaunch `claude --resume <sid>` in: the first candidate
    whose Claude project dir actually holds <sid>.jsonl. This is what makes the
    worktree/subdir case correct — a session created in repo/X then cd'd into a
    worktree still stores its transcript under repo/X's project dir, so we must
    restore in repo/X, not the worktree (claude's current cwd). Reversing the
    project-dir encoding back to a path is unreliable (it maps both '/' and '.'
    to '-'), so we probe real cwds instead of reconstructing."""
    for c in cwds:
        if not c:
            continue
        c = os.path.normpath(c)
        if os.path.exists(os.path.join(project_dir_for(c), sid + ".jsonl")):
            return c
    return None


_RESUME_RE = re.compile(r"--resume[ =]([0-9a-f-]{36})")


def build_manifest(session):
    """Join zellij's live pane table (tab/cwd/pane id) with the OS-validated
    claude process table (pane id → live session id) on PANE ID. Ordered by
    tab position. Raises IdentityError (whole save aborts) on identity
    failures; panes without a live claude become skip/failed entries."""
    join = discover_claude_panes(session)
    panes = [p for p in list_panes(session)
             if not p.get("is_plugin") and not p.get("exited")]
    manifest = []
    for p in sorted(panes, key=lambda x: x.get("tab_position", 0)):
        tab = p.get("tab_name") or f"tab{p.get('tab_position', 0)}"
        pane_cwd = os.path.normpath(p.get("pane_cwd") or HOME)
        pane_id = p.get("id")
        pane_command = p.get("pane_command") or ""
        cand = join.pop(pane_id, None) if pane_id is not None else None

        if cand:
            sid = cand["session_id"]
            keep, unreplayed = replay_flags(cand["argv"][1:])
            cwd = resolve_cwd(sid, [cand["cwd"], pane_cwd])
            if cwd:
                manifest.append({
                    "tab": tab, "cwd": cwd, "session_id": sid,
                    "args": ["--resume", sid] + keep,
                    "unreplayed_flags": unreplayed,
                    "source": "process", "pane_id": pane_id,
                })
            else:
                # Live claude but no transcript on disk yet (brand-new,
                # still-empty session) — nothing resumable.
                manifest.append({
                    "tab": tab, "cwd": pane_cwd, "session_id": None,
                    "args": [], "unreplayed_flags": unreplayed,
                    "stale_candidate": sid,
                    "source": "failed", "pane_id": pane_id,
                })
            continue

        # No live claude for this pane. pane_command is unreliable for
        # identity (it reports the foreground child), but a `--resume <id>`
        # in it is worth recording as a MANUAL hint — never auto-restored,
        # it may predate a /clear.
        m = _RESUME_RE.search(pane_command)
        if m or "claude" in pane_command:
            manifest.append({
                "tab": tab, "cwd": pane_cwd, "session_id": None,
                "args": [], "unreplayed_flags": [],
                "stale_candidate": m.group(1) if m else None,
                "source": "failed", "pane_id": pane_id,
            })
        # else: a plain shell / other tool — nothing claude to snapshot.
    return manifest


# ── layout generation ─────────────────────────────────────────────────────────

def kdl_str(s):
    """Quote a string for KDL (backslash + double-quote escapes)."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def minimal_layout(manifest):
    """Build a clean, minimal layout: one tab per entry, each a single claude
    pane with cwd + resume args. No captured pane sizes — so zellij sizes
    everything to the CURRENT terminal (fixes wrong window sizes on restore).

    A default_tab_template with the stock tab-bar/status-bar plugins IS
    included: starting a new session with `--layout` uses THIS layout verbatim
    and ignores the user's default layout, so without these the restored
    session comes up with no tab bar — all tabs exist but are invisible and
    un-switchable. The plugins carry no geometry, so terminal-fit still holds."""
    lines = [
        "layout {",
        "    default_tab_template {",
        "        pane size=1 borderless=true {",
        '            plugin location="zellij:tab-bar"',
        "        }",
        "        children",
        "        pane size=2 borderless=true {",
        '            plugin location="zellij:status-bar"',
        "        }",
        "    }",
    ]
    for t in manifest:
        args = t.get("args") or []
        # Wrap claude in a shell so exiting claude drops the pane into a
        # normal terminal (relaunch, cd, etc.) instead of a dead pane.
        # Prefix CLAUDE_CODE_FORCE_SESSION_PERSISTENCE=1: a zellij server that
        # was started from inside a claude pane carries the inherited
        # CLAUDE_CODE_CHILD_SESSION marker, which makes every resumed claude
        # decide it's a child session and STOP writing its transcript — the
        # conversation then evaporates on reboot. This flag forces persistence
        # regardless. It's a belt-and-suspenders fallback; the primary fix is
        # to launch the restore from a clean terminal (see cmd_restore).
        claude_cmd = " ".join(
            ["CLAUDE_CODE_FORCE_SESSION_PERSISTENCE=1", "claude"]
            + [shlex.quote(a) for a in args])
        cwd = f" cwd={kdl_str(t['cwd'])}" if t.get("cwd") else ""
        lines.append(f"    tab name={kdl_str(t['tab'])} {{")
        lines.append(f'        pane command="/bin/zsh"{cwd} {{')
        lines.append(f'            args "-c" '
                     f'{kdl_str(claude_cmd + "; exec /bin/zsh -i")}')
        lines.append(f"            start_suspended true")
        lines.append(f"        }}")
        lines.append(f"    }}")
    lines.append("}")
    return "\n".join(lines) + "\n"


def config_layout_path(session):
    """Named-layout path in zellij's config dir. This is CONFIG, not cache:
    zellij only reads it, never overwrites it on serialization. Restore with
    `zellij --session <s> --new-session-with-layout <s>` (zellij >= 0.44
    reinterprets `--session --layout` as "append tabs to an EXISTING session",
    which silently no-ops when the session doesn't exist)."""
    return os.path.join(HOME, ".config", "zellij", "layouts", f"{session}.kdl")


def write_atomic(path, content):
    """tempfile + os.replace so a crash mid-write never corrupts a snapshot."""
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ── commands ──────────────────────────────────────────────────────────────────

def cmd_save(args):
    session = args.session or current_session()
    try:
        manifest = build_manifest(session)
    except IdentityError as e:
        sys.exit(f"✋ ABORTED — pane→session identity is untrustworthy:\n"
                 f"   {e}\n   Nothing was written; the existing snapshot is "
                 f"untouched.")
    resumable = [m for m in manifest if m["session_id"]]

    # Safety guard: never overwrite good snapshots with an empty/degraded one.
    if not resumable:
        sys.exit(f"✋ Refusing to save: resolved 0 resumable claude tabs in "
                 f"'{session}'.\n   This session isn't healthy, and saving "
                 f"would wipe the existing good snapshot.\n"
                 f"   Existing snapshot left untouched.")

    new_layout = minimal_layout(manifest)

    sdir = os.path.join(SNAP_ROOT, session)
    os.makedirs(sdir, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")

    # Write the restorable layout (latest + timestamped history) + manifest.
    write_atomic(os.path.join(sdir, "restore-layout.kdl"), new_layout)
    write_atomic(os.path.join(sdir, f"restore-layout-{stamp}.kdl"), new_layout)
    meta = {"session": session, "saved_at": stamp, "tabs": manifest}
    write_atomic(os.path.join(sdir, "manifest.json"),
                 json.dumps(meta, indent=2) + "\n")

    # Install as a named layout in zellij's CONFIG dir (never overwritten by
    # serialization), so restore is `zellij --session <s> --new-session-with-layout <s>`.
    clp = config_layout_path(session)
    try:
        os.makedirs(os.path.dirname(clp), exist_ok=True)
        write_atomic(clp, new_layout)
        installed = (f"   ↻ Installed named layout — restore from a plain "
                     f"terminal with:\n       zellij --session "
                     f"{shlex.quote(session)} --new-session-with-layout "
                     f"{shlex.quote(session)}")
    except Exception as e:
        installed = f"   ! Could not install named layout: {e}"

    print(f"✅ Saved snapshot of session '{session}' ({stamp})")
    print(f"   {len(manifest)} claude tab(s), {len(resumable)} with a "
          f"resumable session:\n")
    for m in manifest:
        sid = m["session_id"] or "(snapshot FAILED — will start fresh)"
        tag = {"process": "live ✓", "failed": "✗ failed"}.get(m["source"], "-")
        extra = [a for a in m.get("args", [])
                 if a != "--resume" and a != m["session_id"]]
        print(f"   • {m['tab']:<18} {m['cwd']}")
        print(f"       └─ {sid}  [{tag}]")
        print(f"          args: {' '.join(extra) if extra else '(none)'}")
        if m.get("unreplayed_flags"):
            print(f"          ⚠ NOT replayed on restore: "
                  f"{' '.join(m['unreplayed_flags'])}")
        if m.get("stale_candidate"):
            print(f"          ⚠ stale hint (NOT auto-restored — may predate "
                  f"a /clear); resume by hand with:\n"
                  f"            claude --resume {m['stale_candidate']}")
    failed = [m for m in manifest if m["source"] == "failed"]
    if failed:
        print(f"\n   ⚠️  SNAPSHOT FAILED for {len(failed)} tab(s): "
              f"{', '.join(m['tab'] for m in failed)}")
        print(f"       No live claude process owns that pane (exited claude, "
              f"or a brand-new session with no transcript on disk yet). "
              f"They restore as fresh claudes.")

    print(f"\n   Layout : {os.path.join(sdir, 'restore-layout.kdl')}")
    print(f"   Manifest: {os.path.join(sdir, 'manifest.json')}")
    print(f"\n{installed}")
    print(f"\n   Or restore manually:  snapshot.py restore --session {session}")


def session_state(session):
    """'running' | 'exited' | None for a zellij session name, robust to the
    ANSI colouring `list-sessions` adds."""
    r = run(["zellij", "list-sessions", "--no-formatting"])
    out = r.stdout if r.returncode == 0 else \
        run(["zellij", "list-sessions"]).stdout
    for line in out.splitlines():
        clean = re.sub(r"\x1b\[[0-9;]*m", "", line).strip()
        toks = clean.split()
        if toks and toks[0] == session:
            return "exited" if "EXITED" in clean else "running"
    return None


def cmd_restore(args):
    """Doctor + guidance — deliberately spawns NOTHING.

    A zellij server started from inside a claude pane inherits the
    CLAUDE_CODE_CHILD_SESSION marker; every pane it then forks decides it is a
    child session and turns transcript saving OFF, so restored conversations
    silently stop persisting and vanish on the next reboot. The robust fix is
    to launch the restore from a FRESH terminal (ancestor = launchd, clean
    env), which designs that precondition away. This command therefore prints
    the exact commands to run by hand, plus a health check of the launch
    context — rather than spawning a server from within (possibly) a cc pane.
    (The saved layout also prefixes CLAUDE_CODE_FORCE_SESSION_PERSISTENCE=1 as
    a fallback for when someone does launch from a dirty shell.)"""
    session = args.session or (os.environ.get("ZELLIJ_SESSION_NAME") or "")
    if not session:
        sys.exit("Pass --session NAME.")
    clp = config_layout_path(session)
    snap = os.path.join(SNAP_ROOT, session, "restore-layout.kdl")
    manifest_path = os.path.join(SNAP_ROOT, session, "manifest.json")
    if not (os.path.exists(clp) or os.path.exists(snap)):
        sys.exit(f"No snapshot/layout for '{session}'. Run `save` first.")
    layout_ref = session if os.path.exists(clp) else snap

    tabs = []
    if os.path.exists(manifest_path):
        with open(manifest_path) as f:
            tabs = json.load(f).get("tabs", [])
    resumable = [t for t in tabs if t.get("session_id")]

    in_zellij = os.environ.get("ZELLIJ_SESSION_NAME")
    in_claude = bool(os.environ.get("CLAUDE_CODE_CHILD_SESSION")
                     or os.environ.get("CLAUDECODE"))
    residual = session_state(session)

    print(f"🩺 Restore doctor for '{session}' — this prints instructions, it "
          f"does not launch anything.\n")
    print(f"   Layout : {clp if os.path.exists(clp) else snap}")
    print(f"   Tabs   : {len(tabs)} ({len(resumable)} will resume a conversation)")
    for t in tabs:
        mark = "✓ resume" if t.get("session_id") else "· fresh "
        print(f"              {mark}  {t['tab']:<16} {t.get('cwd', '')}")
        if t.get("stale_candidate"):
            print(f"                        (stale hint, resume by hand: "
                  f"claude --resume {t['stale_candidate']})")
    print()

    if not in_zellij and not in_claude:
        print("   ✓ Launch context looks clean (not inside zellij or a cc pane).")
    if in_zellij:
        print(f"   ⚠️  You are INSIDE zellij session '{in_zellij}'. Do not restore "
              f"from here — open a brand-new terminal window.")
    if in_claude:
        print("   ⚠️  This shell is inside a Claude Code pane. A zellij server "
              "started here inherits the child-session marker and restored "
              "panes would STOP saving transcripts. Launch from a fresh "
              "terminal (Terminal.app / iTerm), NOT a cc pane.")
    if residual == "running":
        print(f"   ⚠️  '{session}' is already RUNNING — a plain launch would "
              f"attach to it, not rebuild it. Delete it first (below).")
    elif residual == "exited":
        print(f"   ⚠️  A stale EXITED '{session}' exists — zellij would resurrect "
              f"it with the wrong layout. Delete it first (below).")

    q = shlex.quote
    print("\n   ▶ In a FRESH terminal window, run:\n")
    if residual:
        print(f"       zellij delete-session {q(session)} --force")
    print(f"       zellij --session {q(session)} --new-session-with-layout "
          f"{q(layout_ref)}\n")
    print("   Panes come up suspended — switch to each tab and press Enter to "
          "wake its `claude --resume`.")


def cmd_spawn(args):
    """Deprecated: background-spawning a restore is exactly what produced
    non-persisting child sessions. Redirect to the manual doctor."""
    print("ℹ️  `spawn` no longer background-creates a session — that path made "
          "restored panes inherit a non-persisting child session. Showing the "
          "manual restore doctor instead:\n")
    cmd_restore(args)


def cmd_show(args):
    session = args.session or current_session()
    mf = os.path.join(SNAP_ROOT, session, "manifest.json")
    if not os.path.exists(mf):
        sys.exit(f"No snapshot for '{session}'.")
    with open(mf) as f:
        print(f.read())


def main():
    if sys.platform != "darwin":
        sys.exit("This skill is macOS-only (it uses Darwin's libproc / "
                 "KERN_PROCARGS2 for pane→session identity).")
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("save", "restore", "show", "spawn"):
        sp = sub.add_parser(name)
        sp.add_argument("--session", help="session name (default: $ZELLIJ_SESSION_NAME)")
    args = p.parse_args()
    {"save": cmd_save, "restore": cmd_restore, "show": cmd_show,
     "spawn": cmd_spawn}[args.cmd](args)


if __name__ == "__main__":
    main()
