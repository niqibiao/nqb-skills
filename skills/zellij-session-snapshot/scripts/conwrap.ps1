#Requires -Version 5
# Relaunch a console program with std handles rebound to the pane's real console.
#
# Why: zellij-on-Windows command panes give the child PIPE std handles, while a
# ConPTY console IS attached to the process (and is the only thing the pane
# renders). An interactive TUI like claude sees no TTY on any fd and drops into
# headless mode (`--resume` then exits with "Provide a prompt to continue").
# Fix: open CONIN$/CONOUT$ read-write (GetConsoleMode needs read access, so a
# cmd-style `< CON > CON` write-only redirect is NOT enough), mark them
# inheritable, and hand them to the child via STARTF_USESTDHANDLES.
#
# Usage: conwrap.ps1 <exe> [args...]
$ErrorActionPreference = 'Stop'

Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

public static class ConWrap {
    [DllImport("kernel32.dll", SetLastError=true, CharSet=CharSet.Unicode)]
    static extern IntPtr CreateFileW(string name, uint access, uint share, IntPtr sa, uint disp, uint flags, IntPtr tmpl);

    [DllImport("kernel32.dll", SetLastError=true)]
    static extern bool SetHandleInformation(IntPtr h, uint mask, uint flags);

    [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)]
    struct STARTUPINFO {
        public int cb;
        public string lpReserved, lpDesktop, lpTitle;
        public int dwX, dwY, dwXSize, dwYSize, dwXCountChars, dwYCountChars, dwFillAttribute, dwFlags;
        public short wShowWindow, cbReserved2;
        public IntPtr lpReserved2, hStdInput, hStdOutput, hStdError;
    }
    [StructLayout(LayoutKind.Sequential)]
    struct PROCESS_INFORMATION { public IntPtr hProcess, hThread; public int dwProcessId, dwThreadId; }

    [DllImport("kernel32.dll", SetLastError=true, CharSet=CharSet.Unicode)]
    static extern bool CreateProcessW(string app, string cmdline, IntPtr pa, IntPtr ta, bool inherit,
        uint flags, IntPtr env, string cwd, ref STARTUPINFO si, out PROCESS_INFORMATION pi);

    [DllImport("kernel32.dll")] static extern uint WaitForSingleObject(IntPtr h, uint ms);
    [DllImport("kernel32.dll")] static extern bool GetExitCodeProcess(IntPtr h, out uint code);

    static IntPtr OpenCon(string name) {
        IntPtr h = CreateFileW(name, 0xC0000000u /*GENERIC_READ|WRITE*/, 3u /*share rw*/, IntPtr.Zero, 3u /*OPEN_EXISTING*/, 0u, IntPtr.Zero);
        if (h == (IntPtr)(-1)) throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error(), "open " + name);
        SetHandleInformation(h, 1u, 1u); // HANDLE_FLAG_INHERIT
        return h;
    }

    public static int Run(string cmdline, string cwd) {
        var si = new STARTUPINFO();
        si.cb = Marshal.SizeOf(typeof(STARTUPINFO));
        si.dwFlags = 0x100; // STARTF_USESTDHANDLES
        si.hStdInput  = OpenCon("CONIN$");
        si.hStdOutput = OpenCon("CONOUT$");
        si.hStdError  = OpenCon("CONOUT$");
        PROCESS_INFORMATION pi;
        if (!CreateProcessW(null, cmdline, IntPtr.Zero, IntPtr.Zero, true, 0, IntPtr.Zero,
                string.IsNullOrEmpty(cwd) ? null : cwd, ref si, out pi))
            throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error(), "CreateProcess: " + cmdline);
        WaitForSingleObject(pi.hProcess, 0xFFFFFFFFu);
        uint code; GetExitCodeProcess(pi.hProcess, out code);
        return (int)code;
    }
}
'@

function Quote([string]$a) {
    if ($a -match '[\s"]') { '"' + ($a -replace '(\\*)"', '$1$1\"') + '"' } else { $a }
}

# Force Claude to persist its transcript even if this pane inherited a
# CLAUDE_CODE_CHILD_SESSION marker from a zellij server that was (wrongly)
# started inside another cc pane. Without this a restored claude decides it is a
# child session, turns transcript saving OFF, and the conversation evaporates on
# the next reboot / delete-session. Belt-and-suspenders with launching restore
# from a clean terminal (see snapshot.py cmd_restore). CreateProcessW inherits
# this (modified) environment, so the child claude sees the fix.
$env:CLAUDE_CODE_FORCE_SESSION_PERSISTENCE = '1'
Remove-Item Env:CLAUDE_CODE_CHILD_SESSION -ErrorAction SilentlyContinue

$cwd = (Get-Location).Path
$cmdline = ($args | ForEach-Object { Quote $_ }) -join ' '
[ConWrap]::Run($cmdline, $cwd) | Out-Null

# Claude exited: keep the pane alive as a normal interactive shell (like the
# macOS layout's `exec zsh`) instead of leaving a dead pane the user can't type
# into. Same console-handle rebinding, so the shell is interactive too.
$shell = (Get-Command pwsh -ErrorAction SilentlyContinue).Source
if (-not $shell) { $shell = (Get-Command powershell).Source }
exit [ConWrap]::Run((Quote $shell) + ' -NoLogo', $cwd)
