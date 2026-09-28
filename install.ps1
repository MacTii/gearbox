<#
.SYNOPSIS
    One-shot installer for gearbox.
.DESCRIPTION
    Run after cloning the repo:
        git clone https://github.com/MacTii/gearbox.git
        cd gearbox
        .\install.ps1
    Creates a virtual environment, installs gearbox into it, and wires it into
    Claude Code (env var + SessionStart hook + autostart). See README.md for details.
#>
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot

function Find-Python {
    foreach ($cmd in @("py", "python", "python3")) {
        $found = Get-Command $cmd -ErrorAction SilentlyContinue
        if ($found) { return $found.Source }
    }
    throw "Python 3 not found on PATH. Install it from https://www.python.org/downloads/ and re-run this script."
}

$py = Find-Python
Write-Host "python: $py"

if (-not (Test-Path "$root\.venv")) {
    Write-Host "creating virtual environment..."
    & $py -m venv "$root\.venv"
}

Write-Host "installing gearbox..."
& "$root\.venv\Scripts\pip.exe" install -e $root --quiet

Write-Host "wiring into Claude Code..."
& "$root\.venv\Scripts\gearbox.exe" install

Write-Host ""
Write-Host "Done. Open a new Claude Code session - it will route through gearbox automatically."
Write-Host "Try: gearbox status / gearbox report / gearbox watch"
