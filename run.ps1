<#>
.SYNOPSIS
    Launcher script for crew-tui on Windows.
    Runs the TUI or headless factory mode using the project's virtual environment.

.SYNTAX
    .\run.ps1 [--factory "goal"] [--project DIR] [--yes] [--resume] [--unattended]

.DESCRIPTION
    Activates the project's virtual environment and runs crew-tui.
    Equivalent to: python -m crew_tui [args]
    The virtual environment must exist (run setup.ps1 first).

.PARAMETER Factory
    Goal description for headless factory mode (steps 1-9 pipeline).

.PARAMETER Project
    Project directory (default: %USERPROFILE%\projetos_opencode\<slug>).

.PARAMETER Yes
    Assume yes to all prompts (non-interactive).

.PARAMETER Resume
    Resume from .factory/state.json.

.PARAMETER Unattended
    Allow suspension even if session is active (same as SLEEP_IF_ACTIVE=1).

.EXAMPLE
    .\run.ps1 --factory "Build a todo app in React" --yes

.EXAMPLE
    .\run.ps1 --resume --project C:\dev\my-project
#>

param(
    [string]$Factory,
    [string]$Project,
    [switch]$Yes,
    [switch]$Resume,
    [switch]$Unattended
)

$ErrorActionPreference = "Stop"

$projectRoot = $PSScriptRoot
$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path $venvPython)) {
    Write-Error "Virtual environment not found at $venvPython."
    Write-Error "Run .\setup.ps1 first to create the environment and install dependencies."
    exit 1
}

$args = @()
if ($Factory) { $args += "--factory", $Factory }
if ($Project) { $args += "--project", $Project }
if ($Yes) { $args += "--yes" }
if ($Resume) { $args += "--resume" }
if ($Unattended) { $args += "--unattended" }

& $venvPython -m crew_tui $args