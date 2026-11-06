$ErrorActionPreference = "Stop"

Set-Location $PSScriptRoot

python -m pip install -r requirements-build.txt
if ($LASTEXITCODE -ne 0) {
    throw "Could not install the Windows packaging dependencies."
}

python -m PyInstaller --noconfirm --clean --windowed --onedir `
    --workpath build-desktop `
    --distpath dist-desktop `
    --name ClearviewAnalytics `
    --add-data "templates;templates" `
    --add-data "static;static" `
    desktop.py
if ($LASTEXITCODE -ne 0) {
    throw "Could not package the desktop application."
}

$compiler = Get-Command ISCC.exe -ErrorAction SilentlyContinue
if (-not $compiler) {
    $candidates = @(
        (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"),
        (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe"),
        (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe")
    )
    $compilerPath = $candidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
    if (-not $compilerPath) {
        throw "Inno Setup 6 is required. Install JRSoftware.InnoSetup with winget, then rerun this script."
    }
    $compilerPath = $compilerPath
} else {
    $compilerPath = $compiler.Source
}

& $compilerPath installer.iss
if ($LASTEXITCODE -ne 0) {
    throw "Could not create the Windows installer."
}

Write-Host "Installer created at $PSScriptRoot\installer-output\ClearviewAnalyticsSetup.exe"
