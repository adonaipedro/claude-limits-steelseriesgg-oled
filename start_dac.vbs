' Relaunches the DAC daemon (Claude limits) hidden. Double-click to start.
' Resolves the user profile and pythonw from the environment instead of
' hardcoding a machine-specific path, so it works on any account/Python install.
Set sh = CreateObject("WScript.Shell")
claudeDir = sh.ExpandEnvironmentStrings("%USERPROFILE%") & "\.claude"
sh.CurrentDirectory = claudeDir
sh.Run "pythonw.exe """ & claudeDir & "\dac_subscription_daemon.py"" --loop 300", 0, False
