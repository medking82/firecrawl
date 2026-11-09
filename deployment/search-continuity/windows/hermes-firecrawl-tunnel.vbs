' Install as %LOCALAPPDATA%\hermes\scripts\firecrawl-tunnel.vbs.
' PC 127.0.0.1:3002 -> GA401 search-continuity 127.0.0.1:3003.
' Start with wscript.exe; Startup and watchdog launch this same installed path.
' Never use WScript.Shell.Exec here: it can flash a console window.
Option Explicit
Dim wmi, sh, procs, p, n, ssh
Set wmi = GetObject("winmgmts:\\.\root\cimv2")
Set sh = CreateObject("WScript.Shell")
ssh = "C:\Windows\System32\OpenSSH\ssh.exe -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o ConnectTimeout=15 -o BatchMode=yes -L 127.0.0.1:3002:127.0.0.1:3003 ga401"

n = 0
Set procs = wmi.ExecQuery("SELECT CommandLine FROM Win32_Process WHERE Name='wscript.exe'")
For Each p In procs
    If Not IsNull(p.CommandLine) Then
        If InStr(LCase(p.CommandLine), "scripts\firecrawl-tunnel.vbs") > 0 Then n = n + 1
    End If
Next
If n > 1 Then WScript.Quit 0

Do
    sh.Run ssh, 0, True
    WScript.Sleep 10000
Loop
