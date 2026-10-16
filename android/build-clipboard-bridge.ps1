[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$JavaHome,
    [string]$AndroidHome = (Join-Path (Split-Path -Parent $PSScriptRoot) '.tools\android-sdk')
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$bridgeBuild = Join-Path $projectRoot ('.data\clipboard-build-' + [guid]::NewGuid().ToString('N'))
$classes = Join-Path $bridgeBuild 'classes'
[IO.Directory]::CreateDirectory($classes) | Out-Null
[IO.Directory]::CreateDirectory((Join-Path $projectRoot 'dist')) | Out-Null
$androidJar = Join-Path $AndroidHome 'platforms\android-36\android.jar'
$source = Join-Path $projectRoot 'clipboard-bridge\src\dev\synchro\bridge\ClipboardBridge.java'
& (Join-Path $JavaHome 'bin\javac.exe') -encoding UTF-8 -source 8 -target 8 -classpath $androidJar -d $classes $source
if ($LASTEXITCODE -ne 0) { throw 'Clipboard bridge compilation failed' }
$classFiles = @(Get-ChildItem -LiteralPath $classes -Recurse -Filter *.class | Select-Object -ExpandProperty FullName)
$target = Join-Path $projectRoot 'dist\Synchro-Clipboard.jar'
& (Join-Path $JavaHome 'bin\java.exe') -cp (Join-Path $AndroidHome 'build-tools\35.0.0\lib\d8.jar') com.android.tools.r8.D8 --min-api 26 --lib $androidJar --output $target $classFiles
if ($LASTEXITCODE -ne 0) { throw 'Clipboard bridge DEX packaging failed' }
Write-Output "Clipboard bridge: $target"
