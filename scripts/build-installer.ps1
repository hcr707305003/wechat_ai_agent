param(
    [string]$Python = ".\.venv\Scripts\python.exe",
    [string]$InnoCompiler = "",
    [string]$Version = "0.1.1",
    [switch]$SkipTests
)

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$pythonCandidate = if ([System.IO.Path]::IsPathRooted($Python)) {
    $Python
} else {
    Join-Path $projectRoot $Python
}
$pythonPath = (Resolve-Path -LiteralPath $pythonCandidate).Path

function Remove-ProjectDirectory([string]$RelativePath) {
    $target = [System.IO.Path]::GetFullPath((Join-Path $projectRoot $RelativePath))
    $expectedParent = [System.IO.Path]::GetFullPath((Join-Path $projectRoot ([System.IO.Path]::GetDirectoryName($RelativePath))))
    if ([System.IO.Path]::GetDirectoryName($target) -ne $expectedParent) {
        throw "Refusing to remove unexpected path: $target"
    }
    if (Test-Path -LiteralPath $target) {
        Remove-Item -LiteralPath $target -Recurse -Force
    }
}

if (-not $SkipTests) {
    & $pythonPath -m pytest tests\test_cli.py -q
    if ($LASTEXITCODE -ne 0) { throw 'CLI tests failed' }
    & $pythonPath -m pytest --ignore=tests\test_cli.py --ignore=tests\test_companion_qt.py --ignore=tests\test_manager_qt.py --ignore=tests\test_manager_widgets.py --ignore=tests\test_manager_agent_debug.py -q
    if ($LASTEXITCODE -ne 0) { throw 'Non-Qt tests failed' }
    & $pythonPath -m pytest tests\test_companion_qt.py -q
    if ($LASTEXITCODE -ne 0) { throw 'Workbench Qt tests failed' }
    & $pythonPath -m pytest tests\test_manager_widgets.py tests\test_manager_qt.py tests\test_manager_agent_debug.py -q
    if ($LASTEXITCODE -ne 0) { throw 'Manager Qt tests failed' }
}

& $pythonPath -m compileall -q (Join-Path $projectRoot 'agent_bridge')
if ($LASTEXITCODE -ne 0) { throw 'Python compile check failed' }

& $pythonPath -c "import PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) {
    throw 'PyInstaller is missing. Run: .\.venv\Scripts\python.exe -m pip install -r requirements-build.txt'
}

Remove-ProjectDirectory 'build\AgentBridge'
Remove-ProjectDirectory 'dist\AgentBridge'
Remove-ProjectDirectory 'dist\installer'

Push-Location $projectRoot
try {
    & $pythonPath -m PyInstaller --noconfirm --clean 'packaging\agent_bridge.spec'
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed' }
    & (Join-Path $projectRoot 'scripts\smoke-test-package.ps1') -PackageDirectory (Join-Path $projectRoot 'dist\AgentBridge')
} finally {
    Pop-Location
}

if (-not $InnoCompiler) {
    $candidates = @(
        (Join-Path $env:ProgramFiles 'Inno Setup 7\ISCC.exe'),
        (Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 7\ISCC.exe'),
        (Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6\ISCC.exe'),
        (Join-Path $env:ProgramFiles 'Inno Setup 6\ISCC.exe')
    )
    $InnoCompiler = $candidates | Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Leaf) } | Select-Object -First 1
}
if (-not $InnoCompiler -or -not (Test-Path -LiteralPath $InnoCompiler -PathType Leaf)) {
    throw 'Inno Setup compiler was not found. Install Inno Setup 7 (or 6) or pass -InnoCompiler C:\path\to\ISCC.exe'
}

Push-Location (Join-Path $projectRoot 'packaging')
try {
    & $InnoCompiler "/DMyAppVersion=$Version" 'agent_bridge.iss'
    if ($LASTEXITCODE -ne 0) { throw 'Inno Setup build failed' }
} finally {
    Pop-Location
}

$installer = Join-Path $projectRoot 'dist\installer\AgentBridge-Setup-x64.exe'
if (-not (Test-Path -LiteralPath $installer -PathType Leaf)) {
    throw "Installer was not generated: $installer"
}
$hash = (Get-FileHash -LiteralPath $installer -Algorithm SHA256).Hash.ToLowerInvariant()
$hashFile = "$installer.sha256"
Set-Content -LiteralPath $hashFile -Value "$hash  AgentBridge-Setup-x64.exe" -Encoding ascii
Write-Output "Installer: $installer"
Write-Output "SHA-256: $hash"
