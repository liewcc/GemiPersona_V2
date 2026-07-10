Set WshShell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

' Get the directory of this VBScript file
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
WshShell.CurrentDirectory = scriptDir

' Hide the CLI window when the flag is set (toggled in System Config)
If fso.FileExists("data\hide_cli.flag") Then
    ' Run silently (0 = hide window)
    WshShell.Run "cmd.exe /c run.bat", 0, False
Else
    ' Run normally (1 = activate and display window)
    WshShell.Run "cmd.exe /c run.bat", 1, False
End If
