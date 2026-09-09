param(
    [Parameter(Mandatory = $true)]
    [string]$PackageDirectory
)

$ErrorActionPreference = 'Stop'
$packageRoot = (Resolve-Path -LiteralPath $PackageDirectory).Path
$executable = Join-Path $packageRoot 'AgentBridge.exe'
$template = Join-Path $packageRoot '_internal\config.example.yaml'
$codex = Join-Path $packageRoot '_internal\codex_cli_bin\bin\codex.exe'
$claude = Join-Path $packageRoot '_internal\claude_agent_sdk\_bundled\claude.exe'
$uia = Join-Path $packageRoot '_internal\uiautomation\bin\UIAutomationClient_VC140_X64.dll'

if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) {
    throw "Packaged executable is missing: $executable"
}
if (-not (Test-Path -LiteralPath $template -PathType Leaf)) {
    throw "Packaged configuration template is missing: $template"
}
foreach ($dependency in @($codex, $claude, $uia)) {
    if (-not (Test-Path -LiteralPath $dependency -PathType Leaf)) {
        throw "Packaged runtime dependency is missing: $dependency"
    }
}

function Invoke-SmokeProcess([string[]]$Arguments) {
    $child = Start-Process -FilePath $executable -ArgumentList $Arguments -PassThru -WindowStyle Hidden
    if (-not $child.WaitForExit(60000)) {
        # Only terminate the isolated process launched by this smoke test.
        Stop-Process -Id $child.Id -Force -ErrorAction SilentlyContinue
        throw 'Packaged smoke test exceeded 60 seconds'
    }
    $child.Refresh()
    if ($child.ExitCode -ne 0) {
        throw "Packaged smoke test failed with exit code $($child.ExitCode)"
    }
}

$smokeRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("AgentBridge-Smoke-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $smokeRoot | Out-Null
$previousLocalAppData = $env:LOCALAPPDATA
try {
    $env:LOCALAPPDATA = $smokeRoot
    Invoke-SmokeProcess @('--help')
    Invoke-SmokeProcess @('manager', '--smoke-test', '--user-data-dir', ('"' + $smokeRoot + '"'))
    if (-not (Test-Path -LiteralPath (Join-Path $smokeRoot 'config.yaml') -PathType Leaf)) {
        throw 'Packaged manager did not initialize its configuration'
    }
} finally {
    $env:LOCALAPPDATA = $previousLocalAppData
    $resolvedSmokeRoot = [System.IO.Path]::GetFullPath($smokeRoot)
    $resolvedTempRoot = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
    if (-not $resolvedSmokeRoot.StartsWith($resolvedTempRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to remove smoke directory outside the temp root: $resolvedSmokeRoot"
    }
    if (Test-Path -LiteralPath $resolvedSmokeRoot) {
        Remove-Item -LiteralPath $resolvedSmokeRoot -Recurse -Force
    }
}

Write-Output "Package smoke test passed: $packageRoot"
