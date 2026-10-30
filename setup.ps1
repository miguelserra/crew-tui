<#>
.SYNOPSIS
    Bootstrap script for crew-tui on Windows 11.
    Creates a Python virtual environment, installs dependencies, and sets up config.

.DESCRIPTION
    This script sets up crew-tui on Windows 11. It:
    1. Checks for Python 3.10+
    2. Creates a virtual environment in .venv
    3. Installs dependencies from requirements-windows.txt
    4. Copies .env.example to %APPDATA%\crew-tui\.env if not present
    5. Creates a run.ps1 launcher in the project root

.NOTES
    Requires PowerShell 5.1+ (built into Windows 11).
    Run from the crew-tui project root directory.
    Run with -Playwright to install Playwright Chromium for visual audits.
    Run with -Node to install Node.js via winget (for JS/TS oracle checks).
#>

param(
    [switch]$Playwright,
    [switch]$Node,
    [switch]$Force
)

$ErrorActionPreference = "Stop"

function Write-Info { param([string]$msg) Write-Host "[$(Get-Date -Format 'HH:mm:ss')] $msg" -ForegroundColor Cyan }
function Write-Ok   { param([string]$msg) Write-Host "[$(Get-Date -Format 'HH:mm:ss')] $msg" -ForegroundColor Green }
function Write-Warn { param([string]$msg) Write-Host "[$(Get-Date -Format 'HH:mm:ss')] $msg" -ForegroundColor Yellow }
function Write-Err  { param([string]$msg) Write-Host "[$(Get-Date -Format 'HH:mm:ss')] $msg" -ForegroundColor Red }

# Check PowerShell version
if ($PSVersionTable.PSVersion.Major -lt 5) {
    Write-Err "PowerShell 5.1+ required. Current: $($PSVersionTable.PSVersion)"
    exit 1
}

# Check Python
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) { $python = Get-Command python3 -ErrorAction SilentlyContinue }
if (-not $python) {
    Write-Err "Python not found in PATH. Install Python 3.10+ from python.org or winget install Python.Python.3.11"
    exit 1
}
$version = & $python --version 2>&1
if ($version -notmatch '3\.(1[0-9]|[2-9]\d)') {
    Write-Warn "Python version $version detected. crew-tui requires 3.10+. Proceeding anyway."
} else {
    Write-Ok "Python $version OK"
}

$projectRoot = $PSScriptRoot
$venvDir = Join-Path $projectRoot ".venv"
$envDir = Join-Path $env:APPDATA "crew-tui"
$envFile = Join-Path $envDir ".env"
$reqFile = Join-Path $projectRoot "requirements-windows.txt"

# Create virtual environment
if (Test-Path $venvDir -and -not $Force) {
    Write-Info "Virtual environment already exists at $venvDir (use -Force to recreate)"
} else {
    if (Test-Path $venvDir) { Remove-Item $venvDir -Recurse -Force }
    Write-Info "Creating virtual environment at $venvDir..."
    & $python -m venv $venvDir
    Write-Ok "Virtual environment created"
}

# Activate and upgrade pip
$pip = Join-Path $venvDir "Scripts\pip.exe"
& $pip install --upgrade pip setuptools wheel

# Install requirements
if (Test-Path $reqFile) {
    Write-Info "Installing dependencies from $reqFile..."
    & $pip install -r $reqFile
    Write-Ok "Dependencies installed"
} else {
    Write-Warn "requirements-windows.txt not found at $reqFile"
}

# Optional: Playwright Chromium
if ($Playwright) {
    Write-Info "Installing Playwright Chromium..."
    & $pip install playwright
    & (Join-Path $venvDir "Scripts\playwright.exe") install chromium
    Write-Ok "Playwright Chromium installed"
}

# Optional: Node.js for JS/TS oracle
if ($Node) {
    if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
        Write-Info "Installing Node.js via winget..."
        winget install --id OpenJS.NodeJS --accept-source-agreements --accept-package-agreements
        $env:PATH = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
        refreshenv
    }
    Write-Ok "Node.js available"
}

# Create user config directory and .env template
if (-not (Test-Path $envDir)) {
    New-Item -ItemType Directory -Path $envDir | Out-Null
}
if (-not (Test-Path $envFile) -or $Force) {
    $example = Join-Path $projectRoot ".env.example"
    if (Test-Path $example) {
        Copy-Item $example $envFile -Force
        Write-Info "Created $envFile from .env.example"
        Write-Warn "Edit $envFile to add your API keys (OPENCODE_API_KEY, GEMINI_API_KEY, DEEPSEEK_API_KEY, etc.)"
    } else {
        Write-Warn ".env.example not found at $example"
    }
} else {
    Write-Info "Config file already exists at $envFile"
}

# Create run.ps1 launcher in project root
$runScript = Join-Path $projectRoot "run.ps1"
$runContent = @"
# crew-tui launcher for Windows
# Usage: .\run.ps1 [--factory "goal" | --resume] [--project DIR] [--yes] [--unattended]

param(
    [string]`$Factory,
    [string]`$Project,
    [switch]`$Yes,
    [switch]`$Resume,
    [switch]`$Unattended
)

`$ErrorActionPreference = "Stop"
`$projectRoot = $PSScriptRoot
`$venvPython = Join-Path `$projectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path `$venvPython)) {
    Write-Error "Virtual environment not found at `$venvPython. Run .\setup.ps1 first."
    exit 1
}

`$args = @()
if (`$Factory) { `$args += "--factory", `$Factory }
if (`$Project) { `$args += "--project", `$Project }
if (`$Yes) { `$args += "--yes" }
if (`$Resume) { `$args += "--resume" }
if (`$Unattended) { `$args += "--unattended" }

& `$venvPython -m crew_tui `$args
"@
Set-Content -Path $runScript -Value $runContent -Encoding UTF8
Write-Ok "Created run.ps1 launcher"

Write-Ok "Setup complete!"
Write-Info "Next steps:"
Write-Info "  1. Edit $envFile with your API keys"
Write-Info "  2. Run .\run.ps1 to start the TUI"
Write-Info "  3. Or run .\run.ps1 --factory \"your goal\" for headless factory mode"