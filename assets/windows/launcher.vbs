' Spark Windows launcher — wraps the PyApp exe to show a splash on first run.
' On first launch, PyApp extracts Python and installs dependencies.
' This script shows a Windows message box so the user knows what's happening.

Set objShell = CreateObject("WScript.Shell")
Set objFSO = CreateObject("Scripting.FileSystemObject")

' Determine paths
strDir = objFSO.GetParentFolderName(WScript.ScriptFullName)
strEngine = objFSO.BuildPath(strDir, "spark-engine.exe")

' Check if this is a first run — PyApp stores data in %LOCALAPPDATA%\pyapp
strPyAppData = objShell.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\pyapp"
bFirstRun = False

If Not objFSO.FolderExists(strPyAppData) Then
    bFirstRun = True
ElseIf objFSO.GetFolder(strPyAppData).SubFolders.Count = 0 And _
       objFSO.GetFolder(strPyAppData).Files.Count = 0 Then
    bFirstRun = True
End If

If bFirstRun Then
    ' Show a non-blocking toast notification via PowerShell
    strPS = "powershell -WindowStyle Hidden -Command """ & _
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null; " & _
        "$template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02); " & _
        "$textNodes = $template.GetElementsByTagName('text'); " & _
        "$textNodes.Item(0).AppendChild($template.CreateTextNode('Spark — First Launch')); " & _
        "$textNodes.Item(1).AppendChild($template.CreateTextNode('Setting up environment. This may take a minute. Spark will open in your browser when ready.')); " & _
        "$toast = [Windows.UI.Notifications.ToastNotification]::new($template); " & _
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Spark').Show($toast)" & """"
    objShell.Run strPS, 0, False

    ' Also show a simple popup that auto-closes after 30 seconds
    objShell.Popup "Spark is setting up its environment for the first time." & vbCrLf & vbCrLf & _
        "This includes extracting the Python runtime and installing dependencies. " & _
        "It may take a minute or two." & vbCrLf & vbCrLf & _
        "The application will open in your browser when ready." & vbCrLf & _
        "This dialog will close automatically.", _
        30, "Spark — First Launch", 64

End If

' Launch the engine
objShell.Run """" & strEngine & """", 0, False
