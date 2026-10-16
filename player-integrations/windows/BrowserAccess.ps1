$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$framework = Join-Path $env:WINDIR 'Microsoft.NET/Framework64/v4.0.30319/WPF'
$references = @((Join-Path $framework 'UIAutomationClient.dll'), (Join-Path $framework 'UIAutomationTypes.dll'), 'System.Web.Extensions')
try {
    Add-Type -Path (Join-Path $PSScriptRoot 'BrowserAccess.cs') -ReferencedAssemblies $references
    [Synchro.BrowserAccess]::Run([Console]::In.ReadToEnd())
} catch {
    # Browser URLs and local process paths must never enter service diagnostics.
    [Console]::Write('{"error":"The selected browser could not expose its YouTube address"}')
    exit 1
}
