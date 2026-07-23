#!/usr/bin/env python3
"""
Snapshot a zellij session's tabs + the Claude Code session running in each,
then regenerate a zellij layout that resumes every Claude session on restore.

Windows-only. This is the Windows counterpart of the macOS skill: the pane->
session join is identical in spirit, but process introspection uses the Win32
APIs (ReadProcessMemory over the PEB, GetProcessTimes, CIM) instead of Darwin's
libproc / KERN_PROCARGS2. The macOS build lives upstream.

Core idea: don't reinvent zellij's layout format. Ask zellij for its live pane
table (`list-panes -aj`) -- tab name, cwd, stable pane id -- and for every pane
resolve which Claude session is *currently* active, then emit a minimal layout
that resumes each with `--resume <session-id>`.

How each pane's session is found -- by asking the OS, no side channel:

1. Enumerate Claude's per-process runtime files ~/.claude/sessions/<pid>.json
   (kind=interactive). Each records the process's CURRENT sessionId -- correct
   even after /clear and for launches without --resume.
2. Validate each pid's identity: alive (GetProcessTimes) + kernel creation time
   matches the file's `startedAt` within a tolerance (defeats pid reuse), and
   the image name is claude.exe. (procStart is NOT used on Windows: it is
   serialized as .NET ticks rendered with a machine-local timezone offset, so
   it is ambiguous; startedAt is unix-ms and only ~3-6s after kernel start.)
3. Read the process's EXACT environment via ReadProcessMemory over its PEB
   (RTL_USER_PROCESS_PARAMETERS.Environment). The env carries
   ZELLIJ_SESSION_NAME + ZELLIJ_PANE_ID: that's the join key. Two tabs in the
   same cwd stay distinct because the join is on pane id, not cwd.
4. The process must be a DESCENDANT of this session's `zellij.exe --server`
   process (walks CIM ParentProcessId; restored panes run claude under a pwsh
   wrapper so the chain is claude -> pwsh -> server). Rejects orphans from a
   dead same-name session whose pane ids could otherwise collide.

zellij's `pane_command` is NEVER used for identity. It is only grepped for a
stale `--resume` id as a last-resort hint for dead panes, recorded in the
manifest but NOT auto-restored (it may predate a /clear).

Replayed flags are filtered through an allowlist (ALLOW_FLAGS): session-
selection flags (--fork-session, --continue, --session-id, ...) would break
"resume the exact conversation". Unknown flags are recorded as unreplayed and
reported, never silently passed through.

Failure model (identity errors never silently degrade a snapshot):
- identity infrastructure errors (unreadable runtime JSON / env, ambiguous
  server, duplicate pane join) -> the whole save ABORTS, nothing is written;
- a pane with no live claude (shell pane, exited claude, or a child-session
  that never wrote a runtime file) -> skipped/failed, reported loudly;
- 0 resumable tabs -> refuse to overwrite the existing snapshot.

Restore is deliberately manual: `restore` only prints a doctor + the exact
commands to run in a FRESH terminal window. Auto-spawning a zellij server from
within a claude pane makes every restored pane inherit CLAUDE_CODE_CHILD_SESSION,
so they stop persisting their transcript -- conwrap.ps1 also forces persistence
as a fallback, but a clean launch context is the real fix.

Over SSH there is no such thing as a fresh LOCAL window: every window is a
descendant of the SSH connection, and Windows tears that whole tree down when
the connection drops -- taking the zellij server (and every claude in it) with
it. `spawn` exists for exactly this: it creates the session DETACHED via WMI
(Win32_Process.Create), whose child is parented to the WMI provider service --
outside every SSH job/console AND with the user's clean default environment
(no SSH_*, no CLAUDE_CODE_CHILD_SESSION). The SSH window then only ever runs a
disposable `zellij attach`.

Usage:
  snapshot.py save    [--session NAME]     # default: $ZELLIJ_SESSION_NAME
  snapshot.py restore [--session NAME]     # doctor + manual restore commands
  snapshot.py spawn   [--session NAME]     # create DETACHED via WMI (SSH-safe)
  snapshot.py show    [--session NAME]     # print the saved manifest
"""

import argparse
import ctypes
import ctypes.wintypes as wintypes
import glob
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time

HOME = os.path.expanduser("~")
SNAP_ROOT = os.path.join(HOME, ".claude", "zellij-snapshots")
PROJECTS = os.path.join(HOME, ".claude", "projects")
SESSIONS = os.path.join(HOME, ".claude", "sessions")

# pid-reuse guard: a runtime file is trusted only if its process's kernel
# creation time is within this many seconds of the file's `startedAt`
# (app-level registration, measured ~3-6s after kernel start). Wide enough to
# never reject a legitimate match, tight enough that a reused pid -- a different
# process started minutes or more later -- never passes.
START_TOLERANCE = 180

# Flags replayed on restore, with arity. Conservative: only flags safe to
# re-apply to a resumed session. Session-selection / one-shot flags
# (--fork-session, --continue, --session-id, --print, ...) are dropped: they
# would break "resume the exact conversation". Anything unrecognized is recorded
# as unreplayed and reported, never silently passed through.
ALLOW_FLAGS = {"--dangerously-skip-permissions": 0, "--model": 1,
               "--add-dir": 1, "--permission-mode": 1}
DROP_FLAGS = {"--resume": 1, "-r": 1, "--continue": 0, "-c": 0,
              "--session-id": 1, "--fork-session": 0, "--print": 0, "-p": 0}


class IdentityError(Exception):
    """A failure that makes pane->session identity untrustworthy. Aborts the
    whole save (fail closed) instead of degrading the snapshot."""


# -- Win32 process introspection (ctypes, stdlib-only) -------------------------

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_ntdll = ctypes.WinDLL("ntdll")
_shell32 = ctypes.WinDLL("shell32")

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_PROCESS_QUERY_INFORMATION = 0x0400
_PROCESS_VM_READ = 0x0010
_FT_TO_UNIX = 11644473600          # seconds from 1601-01-01 to 1970-01-01

_k32.OpenProcess.restype = wintypes.HANDLE
_k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_k32.CloseHandle.argtypes = [wintypes.HANDLE]
_k32.ReadProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p,
                                   ctypes.c_void_p, ctypes.c_size_t,
                                   ctypes.POINTER(ctypes.c_size_t)]
_k32.ReadProcessMemory.restype = wintypes.BOOL
_shell32.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
_shell32.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR,
                                        ctypes.POINTER(ctypes.c_int)]


class _FILETIME(ctypes.Structure):
    _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]


class _PROCESS_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [("Reserved1", ctypes.c_void_p),
                ("PebBaseAddress", ctypes.c_void_p),
                ("Reserved2", ctypes.c_void_p * 2),
                ("UniqueProcessId", ctypes.c_void_p),
                ("Reserved3", ctypes.c_void_p)]


# Signatures for the remaining calls (set here because GetProcessTimes needs
# _FILETIME). Explicit HANDLE argtypes stop ctypes from passing a handle through
# the default c_int marshalling.
_k32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(_FILETIME)] * 4
_k32.GetProcessTimes.restype = wintypes.BOOL
_ntdll.NtQueryInformationProcess.argtypes = [
    wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong,
    ctypes.POINTER(ctypes.c_ulong)]
_ntdll.NtQueryInformationProcess.restype = ctypes.c_long   # NTSTATUS
_k32.LocalFree.argtypes = [ctypes.c_void_p]
_k32.LocalFree.restype = ctypes.c_void_p


def creation_unix(pid):
    """Kernel process-creation time (unix seconds) for a live pid via
    GetProcessTimes, or None if the process is gone / unreadable. Doubles as the
    liveness check."""
    h = _k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None
    try:
        c, e, k, u = _FILETIME(), _FILETIME(), _FILETIME(), _FILETIME()
        if not _k32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e),
                                    ctypes.byref(k), ctypes.byref(u)):
            return None
        return (((c.high << 32) | c.low) / 1e7) - _FT_TO_UNIX
    finally:
        _k32.CloseHandle(h)


def _read_mem(h, addr, size):
    buf = ctypes.create_string_buffer(size)
    n = ctypes.c_size_t(0)
    if not _k32.ReadProcessMemory(h, ctypes.c_void_p(addr), buf, size,
                                  ctypes.byref(n)):
        return None
    return buf.raw[:n.value]


def _read_u64(h, addr):
    """Read a little-endian u64 (pointer or size) from the target, or None."""
    raw = _read_mem(h, addr, 8)
    return struct.unpack("<Q", raw)[0] if raw else None


def proc_env(pid):
    """EXACT environment dict of a live same-user pid, read from its PEB via
    ReadProcessMemory, or None if it can't be read -- the process is gone, or
    alive but unreadable (typically a claude started from an ELEVATED terminal,
    where OpenProcess(VM_READ) is denied by UIPI). x64 offsets: PEB+0x20 ->
    ProcessParameters; RTL_USER_PROCESS_PARAMETERS Environment @0x80,
    EnvironmentSize @0x3F0."""
    h = _k32.OpenProcess(_PROCESS_QUERY_INFORMATION | _PROCESS_VM_READ,
                         False, pid)
    if not h:
        return None
    try:
        pbi = _PROCESS_BASIC_INFORMATION()
        if _ntdll.NtQueryInformationProcess(h, 0, ctypes.byref(pbi),
                                            ctypes.sizeof(pbi), None) != 0:
            return None
        peb = pbi.PebBaseAddress          # c_void_p field -> int or None
        if not peb:
            return None
        pp = _read_u64(h, peb + 0x20)
        if not pp:
            return None
        env_ptr = _read_u64(h, pp + 0x80)
        env_sz = _read_u64(h, pp + 0x3F0)
        if not env_ptr or not env_sz or env_sz > (1 << 22):
            return None
        raw = _read_mem(h, env_ptr, env_sz)
        if raw is None:
            return None
        env = {}
        for pair in raw.decode("utf-16-le", "replace").split("\x00"):
            key, sep, val = pair.partition("=")
            if sep and key:           # skip the "=C:" cmd.exe drive entries
                env[key] = val
        return env
    finally:
        _k32.CloseHandle(h)


def cmdline_to_argv(cmdline):
    """Split a Windows command-line string into argv exactly as the CRT does,
    via shell32 CommandLineToArgvW (handles quoting/spaces). [] on empty."""
    if not cmdline:
        return []
    n = ctypes.c_int(0)
    p = _shell32.CommandLineToArgvW(cmdline, ctypes.byref(n))
    if not p:
        return []
    try:
        return [p[i] for i in range(n.value)]
    finally:
        _k32.LocalFree(p)


def _pwsh():
    return shutil.which("pwsh") or shutil.which("powershell") or "powershell"


def cim_processes():
    """{pid: {'name', 'ppid', 'cmdline'}} for every process, in one CIM call.
    Output is forced to UTF-8 so CJK paths / command lines survive."""
    script = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
              "Get-CimInstance Win32_Process | "
              "Select-Object ProcessId,ParentProcessId,Name,CommandLine | "
              "ConvertTo-Json -Compress")
    r = subprocess.run([_pwsh(), "-NoProfile", "-NonInteractive", "-Command",
                        script], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    out = (r.stdout or "").strip()
    if not out:
        raise IdentityError("Win32_Process query returned nothing "
                            "(is PowerShell available?)")
    try:
        data = json.loads(out)
    except json.JSONDecodeError as e:
        raise IdentityError(f"could not parse Win32_Process JSON: {e}")
    if isinstance(data, dict):            # ConvertTo-Json emits a bare object for 1
        data = [data]
    procs = {}
    for d in data:
        pid = d.get("ProcessId")
        if pid is None:
            continue
        procs[int(pid)] = {"name": d.get("Name") or "",
                           "ppid": int(d.get("ParentProcessId") or 0),
                           "cmdline": d.get("CommandLine") or ""}
    return procs


def _server_pids(session, procs):
    """All `zellij.exe --server` pids for `session`. The server's last argv is
    its socket path, whose basename is exactly the session name."""
    found = []
    for pid, p in procs.items():
        if p["name"].lower() != "zellij.exe":
            continue
        argv = cmdline_to_argv(p["cmdline"])
        if argv and "--server" in argv and \
                os.path.basename(argv[-1]) == session:
            found.append(pid)
    return found


def find_server_pid(session, procs):
    """pid of THIS session's `zellij.exe --server` process. Must match exactly
    one process -- 0 or >=2 makes every join untrustworthy."""
    found = _server_pids(session, procs)
    if len(found) != 1:
        raise IdentityError(
            f"expected exactly 1 `zellij --server` for '{session}', "
            f"found {len(found)} ({found})")
    return found[0]


def is_descendant(pid, ancestor, procs, max_depth=12):
    """Whether `ancestor` is in pid's parent chain (via the CIM ParentProcessId
    snapshot). Restored panes run claude under a pwsh wrapper (claude -> pwsh ->
    server), hence the walk rather than a bare ppid check."""
    cur = pid
    for _ in range(max_depth):
        p = procs.get(cur)
        if p is None:
            return False
        ppid = p["ppid"]
        if ppid == ancestor:
            return True
        if ppid <= 0 or ppid == cur:
            return False
        cur = ppid
    return False


# -- pane -> session join ------------------------------------------------------

def discover_claude_panes(session):
    """pane_id -> {pid, session_id, cwd, argv} for every live interactive claude
    in `session`, validated end-to-end. Raises IdentityError on any condition
    that makes identity untrustworthy."""
    procs = cim_processes()
    server = find_server_pid(session, procs)
    join = {}
    for f in sorted(glob.glob(os.path.join(SESSIONS, "*.json"))):
        try:
            with open(f, encoding="utf-8") as fh:
                d = json.load(fh)
        except (OSError, json.JSONDecodeError) as e:
            raise IdentityError(f"unreadable runtime file {f}: {e}")
        if d.get("kind") != "interactive" or not d.get("sessionId"):
            continue
        try:
            pid = int(d["pid"])
        except (KeyError, ValueError, TypeError):
            raise IdentityError(f"runtime file {f} has no valid pid")
        p = procs.get(pid)
        if not p or p["name"].lower() != "claude.exe":
            continue                  # pid gone, or reused by a non-claude
        ct = creation_unix(pid)
        if ct is None:
            continue                  # process gone -- stale file, normal
        started = d.get("startedAt")
        if not isinstance(started, (int, float)):
            raise IdentityError(f"runtime file {f} has no numeric startedAt")
        if abs(ct - started / 1000.0) > START_TOLERANCE:
            continue                  # pid reused by a different process -- stale
        env = proc_env(pid)
        if not env:
            # Unreadable env: the process either raced to exit, or is alive but
            # protected -- typically a claude started from an ELEVATED terminal
            # (same user, higher integrity) where OpenProcess(VM_READ) is denied
            # by UIPI. Skip this pid so its pane degrades to a `failed` entry
            # (still gets a stale hint from pane_command) rather than aborting
            # the whole save. On macOS same-uid env is always readable so this
            # never fires; cross-elevation is common on Windows, so a hard abort
            # here would be a footgun.
            continue
        if env.get("ZELLIJ_SESSION_NAME") != session:
            continue                  # a claude in another zellij session / none
        try:
            pane_id = int(env["ZELLIJ_PANE_ID"])
        except (KeyError, ValueError):
            continue                  # claude not running under zellij
        if not is_descendant(pid, server, procs):
            continue                  # orphan of a dead same-name session
        if pane_id in join:
            raise IdentityError(
                f"two live claudes claim pane {pane_id} of '{session}': "
                f"pids {join[pane_id]['pid']} and {pid}")
        join[pane_id] = {"pid": pid, "session_id": d["sessionId"],
                         "cwd": d.get("cwd"), "argv": cmdline_to_argv(p["cmdline"])}
    return join


def replay_flags(argv_tail):
    """Split a claude argv (minus argv[0]) into (replayable, unreplayed).
    Allowlisted flags are kept with their known arity; session-selection flags
    are dropped; everything else -- unknown options, their values, positionals,
    anything after `--` -- is recorded, not replayed."""
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


# -- zellij / claude filesystem helpers ----------------------------------------

def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def current_session():
    name = os.environ.get("ZELLIJ_SESSION_NAME")
    if not name:
        sys.exit("Not inside a zellij session (ZELLIJ_SESSION_NAME unset). "
                 "Run this from within the session you want to snapshot, "
                 "or pass --session NAME.")
    return name


def project_dir_for(cwd_abs):
    # Claude names project dirs by replacing every non-alphanumeric char (drive
    # colon, backslash, dot, space) with '-': C:\Users\niqib -> C--Users-niqib.
    slug = re.sub(r"[^A-Za-z0-9]", "-", os.path.normpath(cwd_abs))
    return os.path.join(PROJECTS, slug)


def list_panes(session):
    """zellij's live pane table as a flat list of pane dicts. Works from
    anywhere via --session, so save doesn't need to run inside zellij."""
    r = run(["zellij", "--session", session, "action", "list-panes", "-aj"])
    if r.returncode != 0:
        sys.exit(f"`zellij action list-panes` failed for '{session}':\n"
                 f"{r.stderr}   Is it running?  `zellij list-sessions`\n"
                 f"   (A session can be ALIVE yet unlisted if its %TEMP% "
                 f"socket file was cleaned away -- run the restore doctor "
                 f"to diagnose:  snapshot.py restore --session {session})")
    out = r.stdout or ""
    # Trim any shell-banner noise before the JSON. list-panes may emit either a
    # top-level array ('[') or a tab-keyed object ('{'), so trim to whichever
    # comes first -- cutting at the first '[' would corrupt the object form
    # (handled below) by slicing into its first inner array.
    starts = [i for i in (out.find("["), out.find("{")) if i >= 0]
    if starts:
        out = out[min(starts):]
    try:
        data = json.loads(out)
    except json.JSONDecodeError as e:
        sys.exit(f"Could not parse list-panes JSON for '{session}': {e}")
    if isinstance(data, dict):        # some zellij builds key by tab position
        return [p for v in data.values() for p in v]
    return data


def resolve_cwd(sid, cwds):
    r"""Pick the cwd to relaunch `claude --resume <sid>` in: the first candidate
    whose Claude project dir actually holds <sid>.jsonl. This is what makes the
    worktree/subdir case correct -- a session created in repo\X then cd'd into a
    worktree still stores its transcript under repo\X's project dir, so we must
    restore in repo\X, not the worktree (claude's current cwd). Reversing the
    project-dir slug back to a path is unreliable (both '\' and '.' map to '-'),
    so we probe real cwds instead of reconstructing."""
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
    claude process table (pane id -> live session id) on PANE ID. Ordered by tab
    position. Raises IdentityError (whole save aborts) on identity failures;
    panes without a live claude become skip/failed entries."""
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
                # still-empty session) -- nothing resumable.
                manifest.append({
                    "tab": tab, "cwd": pane_cwd, "session_id": None,
                    "args": [], "unreplayed_flags": unreplayed,
                    "stale_candidate": sid, "source": "failed",
                    "pane_id": pane_id,
                })
            continue

        # No live claude for this pane. pane_command is unreliable for identity
        # (it can report the foreground child), but a `--resume <id>` in it is
        # worth recording as a MANUAL hint -- never auto-restored, it may
        # predate a /clear.
        m = _RESUME_RE.search(pane_command)
        if m or "claude" in pane_command.lower():
            manifest.append({
                "tab": tab, "cwd": pane_cwd, "session_id": None,
                "args": [], "unreplayed_flags": [],
                "stale_candidate": m.group(1) if m else None,
                "source": "failed", "pane_id": pane_id,
            })
        # else: a plain shell / other tool -- nothing claude to snapshot.
    return manifest


# -- layout generation ---------------------------------------------------------

def kdl_str(s):
    r"""Quote a string for KDL (backslash + double-quote escapes). Doubling
    backslashes matters on Windows: an un-escaped C:\Users is an invalid KDL
    escape and zellij rejects the whole layout."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def claude_pane_command():
    """(command, prefix_args) to launch claude in a zellij pane on Windows.
    zellij command panes hand the child PIPE std handles even though a ConPTY
    console is attached (and is all the pane renders), so a directly-launched
    claude sees no TTY, drops into headless mode, and `--resume` exits at once.
    conwrap.ps1 rebinds the real console handles (and forces transcript
    persistence) before spawning claude, restoring interactive mode."""
    pwsh = _pwsh()
    wrapper = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "conwrap.ps1")
    claude = shutil.which("claude") or "claude"
    return pwsh, ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                  wrapper, claude]


def minimal_layout(manifest):
    """Build a clean, minimal layout: one tab per entry, each a single claude
    pane (launched through conwrap.ps1) with cwd + resume args. No captured pane
    sizes -- so zellij sizes everything to the CURRENT terminal (fixes wrong
    window sizes on restore).

    A default_tab_template with the stock tab-bar/status-bar plugins IS
    included: `--new-session-with-layout` uses THIS layout verbatim and ignores
    the user's default layout, so without these the restored session comes up
    with no tab bar -- all tabs exist but are invisible and un-switchable. The
    plugins carry no geometry, so terminal-fit still holds."""
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
    command, prefix = claude_pane_command()
    for t in manifest:
        args = prefix + (t.get("args") or [])
        quoted = " ".join(kdl_str(a) for a in args)
        cwd = f" cwd={kdl_str(t['cwd'])}" if t.get("cwd") else ""
        lines.append(f"    tab name={kdl_str(t['tab'])} {{")
        lines.append(f"        pane command={kdl_str(command)}{cwd} {{")
        if quoted:
            lines.append(f"            args {quoted}")
        lines.append("            start_suspended true")
        lines.append("        }")
        lines.append("    }")
    lines.append("}")
    return "\n".join(lines) + "\n"


def config_layout_path(session):
    """Named-layout path in zellij's config dir. This is CONFIG, not cache:
    zellij only reads it, never overwrites it on serialization. Restore with
    `zellij --session <s> --new-session-with-layout <s>`."""
    base = os.environ.get("APPDATA", os.path.join(HOME, "AppData", "Roaming"))
    return os.path.join(base, "Zellij", "config", "layouts", f"{session}.kdl")


def write_atomic(path, content):
    """tempfile + os.replace so a crash mid-write never corrupts a snapshot."""
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# -- commands ------------------------------------------------------------------

def cmd_save(args):
    session = args.session or current_session()
    try:
        manifest = build_manifest(session)
    except IdentityError as e:
        sys.exit(f"[X] ABORTED -- pane->session identity is untrustworthy:\n"
                 f"   {e}\n   Nothing was written; the existing snapshot is "
                 f"untouched.")
    resumable = [m for m in manifest if m["session_id"]]

    # Safety guard: never overwrite good snapshots with an empty/degraded one.
    if not resumable:
        sys.exit(f"[X] Refusing to save: resolved 0 resumable claude tabs in "
                 f"'{session}'.\n   This session isn't healthy (no live claude "
                 f"with a runtime file owns any pane -- e.g. every tab is a "
                 f"non-persisting child session), and saving would wipe the "
                 f"existing good snapshot.\n   Existing snapshot left untouched."
                 f"\n   Tip: check `/status` in each tab for "
                 f"'Transcript saving is off'.")

    new_layout = minimal_layout(manifest)

    sdir = os.path.join(SNAP_ROOT, session)
    os.makedirs(sdir, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")

    # Write the restorable layout (latest + timestamped history) + manifest.
    write_atomic(os.path.join(sdir, "restore-layout.kdl"), new_layout)
    write_atomic(os.path.join(sdir, f"restore-layout-{stamp}.kdl"), new_layout)
    meta = {"session": session, "saved_at": stamp, "tabs": manifest}
    write_atomic(os.path.join(sdir, "manifest.json"),
                 json.dumps(meta, indent=2, ensure_ascii=False) + "\n")

    # Install as a named layout in zellij's CONFIG dir (never overwritten by
    # serialization), so restore is `--new-session-with-layout <s>`.
    clp = config_layout_path(session)
    try:
        os.makedirs(os.path.dirname(clp), exist_ok=True)
        write_atomic(clp, new_layout)
        installed = (f"   Installed named layout -- restore from a FRESH "
                     f"terminal with:\n       zellij --session {session} "
                     f"--new-session-with-layout {session}")
    except Exception as e:
        installed = f"   ! Could not install named layout: {e}"

    print(f"[OK] Saved snapshot of session '{session}' ({stamp})")
    print(f"   {len(manifest)} claude tab(s), {len(resumable)} with a "
          f"resumable session:\n")
    for m in manifest:
        sid = m["session_id"] or "(snapshot FAILED -- will start fresh)"
        tag = {"process": "live", "failed": "x failed"}.get(m["source"], "-")
        extra = [a for a in m.get("args", [])
                 if a != "--resume" and a != m["session_id"]]
        print(f"   - {m['tab']:<18} {m['cwd']}")
        print(f"       -> {sid}  [{tag}]")
        print(f"          args: {' '.join(extra) if extra else '(none)'}")
        if m.get("unreplayed_flags"):
            print(f"          ! NOT replayed on restore: "
                  f"{' '.join(m['unreplayed_flags'])}")
        if m.get("stale_candidate"):
            print(f"          ! stale hint (NOT auto-restored -- may predate a "
                  f"/clear); resume by hand with:\n"
                  f"            claude --resume {m['stale_candidate']}")
    failed = [m for m in manifest if m["source"] == "failed"]
    if failed:
        print(f"\n   !! SNAPSHOT FAILED for {len(failed)} tab(s): "
              f"{', '.join(m['tab'] for m in failed)}")
        print(f"      No live claude with a runtime file owns that pane "
              f"(exited claude, a brand-new session with no transcript yet, or "
              f"a non-persisting child session). They restore as fresh claudes.")

    print(f"\n   Layout  : {os.path.join(sdir, 'restore-layout.kdl')}")
    print(f"   Manifest: {os.path.join(sdir, 'manifest.json')}")
    print(f"\n{installed}")
    print(f"\n   Or run the restore doctor:  snapshot.py restore --session {session}")


def session_state(session):
    """'running' | 'zombie' | 'exited' | None for a zellij session name.

    `list-sessions` alone CANNOT be trusted on Windows: the session registry
    is socket files under %TEMP%\\zellij, which temp cleaners delete while the
    server keeps running (observed live). Such a session is alive -- its
    claudes still run -- but unreachable by ANY new client: list, attach,
    save and delete-session all fail. So the process table is the authority
    for 'running'; a live server that list-sessions doesn't show is 'zombie'
    (must be taskkill'ed, not delete-session'ed); a listing without a live
    server is 'exited' (a resurrectable corpse to delete)."""
    listed = None
    r = run(["zellij", "list-sessions", "--no-formatting"])
    out = r.stdout if r.returncode == 0 else \
        run(["zellij", "list-sessions"]).stdout
    for line in (out or "").splitlines():
        clean = re.sub(r"\x1b\[[0-9;]*m", "", line).strip()
        toks = clean.split()
        if toks and toks[0] == session:
            listed = "exited" if "EXITED" in clean else "listed"
            break
    try:
        has_server = bool(_server_pids(session, cim_processes()))
    except IdentityError:
        # Process table unavailable -- degrade to the listing alone.
        return {"listed": "running", "exited": "exited"}.get(listed)
    if has_server:
        return "running" if listed == "listed" else "zombie"
    return "exited" if listed else None


def cmd_restore(args):
    """Doctor + guidance -- deliberately spawns NOTHING.

    A zellij server started from inside a claude pane inherits the
    CLAUDE_CODE_CHILD_SESSION marker; every pane it then forks decides it is a
    child session and turns transcript saving OFF, so restored conversations
    silently stop persisting and vanish on the next reboot. The robust fix is to
    launch the restore from a FRESH terminal window (clean env, no marker). This
    command therefore prints the exact commands to run by hand, plus a health
    check of the launch context -- rather than spawning a server from within
    (possibly) a cc pane. (conwrap.ps1 also forces persistence as a fallback for
    when someone does launch from a dirty shell.)

    Over SSH the fresh-window advice is unattainable (every window is still a
    descendant of the SSH connection and dies with it), so the doctor detects
    SSH and steers to `spawn` -- the WMI-detached create -- instead."""
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
        with open(manifest_path, encoding="utf-8") as f:
            tabs = json.load(f).get("tabs", [])
    resumable = [t for t in tabs if t.get("session_id")]

    in_zellij = os.environ.get("ZELLIJ_SESSION_NAME")
    in_claude = bool(os.environ.get("CLAUDE_CODE_CHILD_SESSION")
                     or os.environ.get("CLAUDECODE"))
    in_ssh = any(os.environ.get(k)
                 for k in ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY"))
    residual = session_state(session)

    print(f"[doctor] Restore doctor for '{session}' -- this prints "
          f"instructions, it does not launch anything.\n")
    print(f"   Layout : {clp if os.path.exists(clp) else snap}")
    print(f"   Tabs   : {len(tabs)} ({len(resumable)} will resume a conversation)")
    for t in tabs:
        mark = "resume" if t.get("session_id") else "fresh "
        print(f"              [{mark}]  {t['tab']:<16} {t.get('cwd', '')}")
        if t.get("stale_candidate"):
            print(f"                        (stale hint, resume by hand: "
                  f"claude --resume {t['stale_candidate']})")
    print()

    if not in_zellij and not in_claude and not in_ssh:
        print("   OK  Launch context looks clean (not inside zellij or a cc pane).")
    if in_zellij:
        print(f"   !!  You are INSIDE zellij session '{in_zellij}'. Do not "
              f"restore from here -- open a brand-new terminal window.")
    if in_claude:
        print("   !!  This shell is inside a Claude Code pane. A zellij server "
              "started here inherits the child-session marker and restored "
              "panes would STOP saving transcripts. Launch from a fresh "
              "Windows Terminal / PowerShell window, NOT a cc pane.")
    if in_ssh:
        print("   !!  This shell came in over SSH. Any window opened here is "
              "still a descendant of the SSH connection -- a zellij server "
              "launched from it DIES when the connection drops. Use `spawn` "
              "(WMI-detached create) instead of a plain launch.")
    if residual == "running":
        print(f"   !!  '{session}' is already RUNNING -- a plain launch would "
              f"attach to it, not rebuild it. Delete it first (below).")
    elif residual == "zombie":
        print(f"   !!  '{session}' has a LIVE server but its socket file "
              f"(%TEMP%\\zellij) is gone -- deleted by a temp cleaner. No new "
              f"client can reach it: attach/save/delete-session all fail. "
              f"Kill its process tree first (below); its claudes die with it, "
              f"but their transcripts are on disk and resume from the "
              f"snapshot.")
    elif residual == "exited":
        print(f"   !!  A stale EXITED '{session}' exists -- zellij would "
              f"resurrect it with the wrong layout. Delete it first (below).")

    clear_cmds = []
    if residual == "zombie":
        try:
            clear_cmds = [f"taskkill /F /T /PID {p}"
                          for p in _server_pids(session, cim_processes())]
        except IdentityError:
            clear_cmds = ["# (could not resolve the zombie server pid)"]
    elif residual:
        clear_cmds = [f"zellij delete-session {session} --force"]

    me = os.path.join(os.path.dirname(os.path.abspath(__file__)), "snapshot.py")
    if in_ssh:
        print("\n   > Over SSH: create the session DETACHED (survives "
              "disconnects), then attach:\n")
        for c in clear_cmds:
            print(f"       {c}")
        print(f"       python3 {me} spawn --session {session}")
        print(f"       zellij attach {session}\n")
    else:
        print("\n   > In a FRESH terminal window (Windows Terminal / pwsh), "
              "run:\n")
        for c in clear_cmds:
            print(f"       {c}")
        print(f"       zellij --session {session} --new-session-with-layout "
              f"{layout_ref}\n")
    print("   Panes come up suspended -- switch to each tab and press Enter to "
          "wake its `claude --resume`.")


def cmd_spawn(args):
    """Create <session> DETACHED from its saved layout via WMI, then print the
    attach command.

    Why WMI (Win32_Process.Create): a zellij server started from an SSH shell
    is a descendant of the sshd connection's process tree (job/ConPTY); when
    the connection drops, Windows tears the tree down and the session dies --
    zellij has no Unix-style daemonize escape on Windows. A WMI-created
    process is parented to the WMI provider service instead: outside every
    SSH job and console, and with the user's clean DEFAULT environment --
    correct TEMP/APPDATA (so the socket and named layout resolve), and no
    SSH_* / CLAUDE_CODE_CHILD_SESSION. That last part means this path is also
    immune to the child-session persistence trap that got the old
    auto-spawn-from-a-cc-pane removed: the env is clean by construction, not
    by cleanup. The SSH window then only ever runs a disposable
    `zellij attach` -- reconnect and re-attach after any drop."""
    session = args.session or (os.environ.get("ZELLIJ_SESSION_NAME") or "")
    if not session:
        sys.exit("Pass --session NAME.")
    clp = config_layout_path(session)
    snap = os.path.join(SNAP_ROOT, session, "restore-layout.kdl")
    if not (os.path.exists(clp) or os.path.exists(snap)):
        sys.exit(f"No snapshot/layout for '{session}'. Run `save` first.")
    layout_ref = session if os.path.exists(clp) else snap

    state = session_state(session)
    if state == "running":
        sys.exit(f"'{session}' is already running -- attach with:\n"
                 f"    zellij attach {session}\n"
                 f"To rebuild it from the snapshot instead, first run:\n"
                 f"    zellij delete-session {session} --force")
    if state == "zombie":
        # Live server whose %TEMP% socket file a temp cleaner deleted: no new
        # client can reach it (attach/save/delete-session all fail), and a
        # plain spawn would create an unreachable-twin name collision.
        try:
            pids = _server_pids(session, cim_processes())
        except IdentityError:
            pids = []
        kill = "\n".join(f"    taskkill /F /T /PID {p}" for p in pids) or \
            "    (could not resolve the zombie server pid)"
        sys.exit(f"'{session}' has a LIVE server but its socket file is gone "
                 f"(temp cleaner) -- unreachable by any client. Kill the old "
                 f"tree first, then re-run spawn (its claudes' transcripts "
                 f"are on disk and will resume):\n{kill}")
    if state == "exited":
        # Clear the stale corpse or zellij resurrects it with the wrong
        # layout. delete-session returns before it's actually gone -- poll.
        run(["zellij", "delete-session", session, "--force"])
        for _ in range(20):
            if session_state(session) is None:
                break
            time.sleep(0.25)
        else:
            sys.exit(f"Could not clear the stale EXITED '{session}'.")

    zellij = shutil.which("zellij") or "zellij"
    cmdline = (f'"{zellij}" --new-session-with-layout "{layout_ref}" '
               f'attach --create-background "{session}"')
    ps = ("$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
          "-Arguments @{ CommandLine = '" + cmdline.replace("'", "''") + "' }; "
          "Write-Output $r.ReturnValue")
    r = run([_pwsh(), "-NoProfile", "-NonInteractive", "-Command", ps])
    rv = (r.stdout or "").strip().splitlines()[-1:] or [""]
    if r.returncode != 0 or rv[0] != "0":
        sys.exit(f"WMI spawn failed (ReturnValue={rv[0] or '?'}):\n{r.stderr}")

    for _ in range(40):
        if session_state(session) == "running":
            break
        time.sleep(0.25)
    else:
        sys.exit(f"Spawned, but '{session}' never appeared in list-sessions.\n"
                 f"Check the layout parses:  zellij --session {session} "
                 f"--new-session-with-layout {layout_ref}")

    # Fail-loud sanity: the new server must be outside any SSH lineage with a
    # clean env, or the whole point of the detached spawn is defeated.
    warn = None
    try:
        server = find_server_pid(session, cim_processes())
        env = proc_env(server)
        if env is None:
            warn = "   ! could not read the new server's env to verify it."
        else:
            bad = [k for k in env if k.startswith("SSH_")
                   or k == "CLAUDE_CODE_CHILD_SESSION"]
            if bad:
                warn = (f"   ! server env unexpectedly carries "
                        f"{', '.join(bad)} -- it may not survive an SSH drop "
                        f"or persist transcripts.")
    except IdentityError as e:
        warn = f"   ! could not verify the new server: {e}"

    print(f"[OK] '{session}' created detached -- the server lives outside "
          f"this SSH/terminal session.")
    if warn:
        print(warn)
    print(f"   Attach with:  zellij attach {session}")
    print(f"   After an SSH drop, reconnect and re-attach -- the session "
          f"survives.")
    print(f"   Panes come up suspended -- press Enter in each tab to wake its "
          f"claude.")


def cmd_show(args):
    session = args.session or current_session()
    mf = os.path.join(SNAP_ROOT, session, "manifest.json")
    if not os.path.exists(mf):
        sys.exit(f"No snapshot for '{session}'.")
    with open(mf, encoding="utf-8") as f:
        print(f.read())


def main():
    if sys.platform != "win32":
        sys.exit("This is the Windows build of the skill (Win32 PEB/CIM process "
                 "introspection). On macOS use the upstream Darwin build.")
    if ctypes.sizeof(ctypes.c_void_p) != 8:
        sys.exit("This skill needs 64-bit Python: it reads x64 PEB offsets to "
                 "introspect claude processes.")
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
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
