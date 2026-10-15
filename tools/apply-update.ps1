param([Parameter(Mandatory=$true)][string]$PlanPath)
$ErrorActionPreference = 'Stop'
$plan = Get-Content -LiteralPath $PlanPath -Raw -Encoding UTF8 | ConvertFrom-Json
$appRoot = [IO.Path]::GetFullPath($plan.root)
$sourceRoot = [IO.Path]::GetFullPath($plan.source)
if (-not (Test-Path -LiteralPath (Join-Path $appRoot 'BUILD-MANIFEST.json')) -or
    -not (Test-Path -LiteralPath (Join-Path $sourceRoot 'BUILD-MANIFEST.json'))) { throw 'Portable distribution required' }
if ($appRoot -eq [IO.Path]::GetPathRoot($appRoot) -or $sourceRoot.StartsWith($appRoot + '\')) { throw 'Invalid update roots' }
$process = Get-Process -Id $plan.pid -ErrorAction SilentlyContinue
if ($process) { $process.WaitForExit(30000) | Out-Null }
if (Get-Process -Id $plan.pid -ErrorAction SilentlyContinue) { throw 'Service has not exited; no files changed' }
$backup = Join-Path (Split-Path -Parent $appRoot) ('Syncro-backup-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $backup | Out-Null
$oldNames = @()
$newNames = @()
try {
    foreach ($item in Get-ChildItem -LiteralPath $appRoot -Force) {
        if ($item.Name -eq '.data') { continue }
        $oldNames += $item.Name
        Move-Item -LiteralPath $item.FullName -Destination (Join-Path $backup $item.Name)
    }
    foreach ($item in Get-ChildItem -LiteralPath $sourceRoot -Force) {
        if ($item.Name -eq '.data') { throw 'Release contains private data' }
        $newNames += $item.Name
        Move-Item -LiteralPath $item.FullName -Destination (Join-Path $appRoot $item.Name)
    }
    $started = Start-Process -FilePath (Join-Path $appRoot 'runtime/pythonw.exe') -ArgumentList 'desktop-launcher.py' -WorkingDirectory $appRoot -WindowStyle Hidden -PassThru
    $ready = $false
    for ($i=0; $i -lt 40; $i++) {
        Start-Sleep -Milliseconds 500
        try { Invoke-WebRequest -Uri ('http://127.0.0.1:' + $plan.port + '/api/health') -UseBasicParsing -TimeoutSec 1 | Out-Null }
        catch { if ($_.Exception.Response.StatusCode.value__ -eq 401 -and $_.Exception.Response.Headers['Server'] -like ('*' + $plan.version + '*')) { $ready=$true; break } }
    }
    if (-not $ready) { Stop-Process -Id $started.Id -ErrorAction SilentlyContinue; throw 'Updated service did not become ready' }
    # Keep one recoverable backup. It contains program files, never clipboard or pairing state.
} catch {
    $failure = $_.Exception.Message
    $failed = Join-Path (Split-Path -Parent $backup) ('Syncro-failed-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $failed | Out-Null
    foreach ($name in $newNames) {
        $itemPath = Join-Path $appRoot $name
        if (Test-Path -LiteralPath $itemPath) { Move-Item -LiteralPath $itemPath -Destination (Join-Path $failed $name) }
    }
    foreach ($name in $oldNames) {
        $itemPath = Join-Path $backup $name
        if (Test-Path -LiteralPath $itemPath) { Move-Item -LiteralPath $itemPath -Destination (Join-Path $appRoot $name) }
    }
    Start-Process -FilePath (Join-Path $appRoot 'runtime/pythonw.exe') -ArgumentList 'desktop-launcher.py' -WorkingDirectory $appRoot -WindowStyle Hidden
    [IO.File]::WriteAllText((Join-Path $appRoot '.data/update-error.txt'), $failure)
}
