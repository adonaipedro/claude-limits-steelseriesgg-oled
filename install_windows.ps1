$ErrorActionPreference = "Stop"

$ClaudeDir = Join-Path $HOME ".claude"
$ScriptSource = Join-Path $PSScriptRoot "claude_gamesense_statusline.py"
$ScriptTarget = Join-Path $ClaudeDir "claude_gamesense_statusline.py"
$SettingsPath = Join-Path $ClaudeDir "settings.json"
$Updater = Join-Path $PSScriptRoot "update_settings.py"

function Get-PythonCommand {
  try {
    & py -3 --version *> $null
    if ($LASTEXITCODE -eq 0) { return @{Exe="py"; Args=@("-3")} }
  } catch {}

  try {
    & python --version *> $null
    if ($LASTEXITCODE -eq 0) { return @{Exe="python"; Args=@()} }
  } catch {}

  throw "Python 3 not found. Install Python 3 or enable the 'py -3'/'python' command on PATH."
}

New-Item -ItemType Directory -Force -Path $ClaudeDir | Out-Null
Copy-Item -Force $ScriptSource $ScriptTarget

# Seed default display knobs without clobbering an existing user-edited copy.
$ConfigSource = Join-Path $PSScriptRoot "dac_config.json"
$ConfigTarget = Join-Path $ClaudeDir "dac_config.json"
if ((Test-Path $ConfigSource) -and -not (Test-Path $ConfigTarget)) {
  Copy-Item $ConfigSource $ConfigTarget
  Write-Host "Wrote default config: $ConfigTarget"
}

$Python = Get-PythonCommand
& $Python.Exe @($Python.Args) $Updater $SettingsPath "python" $ScriptTarget
if ($LASTEXITCODE -ne 0) { throw "Failed to update settings.json" }

Write-Host "Installed at: $ScriptTarget"
Write-Host "Restart Claude Code or send a message to reload the statusLine."
