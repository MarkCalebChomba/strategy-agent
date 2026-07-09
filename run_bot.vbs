' run_bot.vbs — Live bot launcher for Windows
' Runs python live_bot.py silently, auto-restarts on crash.
' Add a shortcut of this file to Windows Startup for 24/7 operation.

Dim shell, fso, scriptDir, pythonExe, logFile, retryDelay
retryDelay = 5000

Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetAbsolutePathName(".")
pythonExe = "python"
logFile = scriptDir & "\bot_output.log"
Set shell = CreateObject("WScript.Shell")

' Log startup
Call LogToFile("=== Bot launcher started ===")

Do While True
    Call LogToFile("Starting live_bot.py...")
    ' Run hidden (0 = no window), wait for exit (True)
    shell.Run "cmd /c """ & pythonExe & " " & scriptDir & "\live_bot.py >> """ & logFile & """ 2>&1""", 0, True
    Call LogToFile("Bot exited. Restarting in " & retryDelay/1000 & "s...")
    WScript.Sleep retryDelay
Loop

Sub LogToFile(msg)
    On Error Resume Next
    Dim ts, f
    Set f = fso.OpenTextFile(logFile, 8, True)  ' 8 = append
    ts = Now()
    f.WriteLine ts & " " & msg
    f.Close
End Sub
