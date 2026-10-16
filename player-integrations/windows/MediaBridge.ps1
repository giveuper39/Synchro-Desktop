# Windows PowerShell 5.1 hosts the OS WinRT projection; no SDK/runtime install.
# One JSON request per line, one JSON response per line. stdout is protocol only.
$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$null = Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager, Windows.Media.Control, ContentType=WindowsRuntime]
$null = [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionMediaProperties, Windows.Media.Control, ContentType=WindowsRuntime]
$null = Add-Type -Path (Join-Path $PSScriptRoot 'MediaBridge.cs')
$script:asTask = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.IsGenericMethodDefinition -and
    $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
} | Select-Object -First 1

function Await-Result($operation, [Type]$resultType) {
    $task = $script:asTask.MakeGenericMethod($resultType).Invoke($null, @($operation))
    if (-not $task.Wait(10000)) { throw 'The Windows media operation timed out' }
    return $task.GetAwaiter().GetResult()
}

$script:manager = Await-Result ([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager]::RequestAsync()) ([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager])

function Session-Entries {
    $counts = @{}
    foreach ($session in $script:manager.GetSessions()) {
        $appId = $session.SourceAppUserModelId
        if (-not $counts.ContainsKey($appId)) { $counts[$appId] = 0 }
        $ordinal = $counts[$appId]++
        $id = $appId
        if ($ordinal -gt 0) { $id = $appId + '#' + $ordinal }
        @{ id = $id; session = $session }
    }
}

function Describe-Session($entry) {
    $session = $entry.session
    $media = Await-Result ($session.TryGetMediaPropertiesAsync()) ([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionMediaProperties])
    $playback = $session.GetPlaybackInfo()
    $timeline = $session.GetTimelineProperties()
    $position = $timeline.Position.TotalMilliseconds
    $playing = $playback.PlaybackStatus.ToString() -eq 'Playing'
    # Position is a sample at LastUpdatedTime, not a continuously ticking value.
    if ($playing) {
        $elapsed = ([DateTimeOffset]::UtcNow - $timeline.LastUpdatedTime).TotalMilliseconds
        $rate = 1.0
        if ($null -ne $playback.PlaybackRate) { $rate = [double]$playback.PlaybackRate }
        $position += [Math]::Max(0, $elapsed) * $rate
    }
    $duration = [Math]::Max(0, $timeline.EndTime.TotalMilliseconds)
    $position = [Math]::Max(0, $position)
    if ($duration -gt 0) { $position = [Math]::Min($duration, $position) }
    return @{
        id = $entry.id; appId = $session.SourceAppUserModelId; title = $media.Title
        positionMs = [long][Math]::Round($position); durationMs = [long][Math]::Round($duration)
        paused = -not $playing; canPause = [bool]$playback.Controls.IsPauseEnabled
        canSeek = [bool]$playback.Controls.IsPlaybackPositionEnabled
    }
}

while ($null -ne ($line = [Console]::ReadLine())) {
    $request = $null
    try {
        $request = $line | ConvertFrom-Json
        switch ($request.action) {
            'sessions' {
                $result = @()
                foreach ($entry in @(Session-Entries)) {
                    try { $result += Describe-Session $entry } catch { }
                }
            }
            'files' { $result = @([Synchro.MediaBridge]::OpenMediaFiles()) }
            'open' {
                $result = [Synchro.MediaBridge]::OpenMediaFile([string]$request.path)
            }
            { $_ -in @('pause', 'play', 'seek') } {
                $entry = @(Session-Entries) | Where-Object { $_.id -ceq $request.sessionId } | Select-Object -First 1
                if ($null -eq $entry) { throw 'The selected media session is no longer open' }
                if ($request.action -eq 'pause') {
                    $ok = Await-Result ($entry.session.TryPauseAsync()) ([bool])
                } elseif ($request.action -eq 'play') {
                    $ok = Await-Result ($entry.session.TryPlayAsync()) ([bool])
                } else {
                    $position = [long]$request.positionMs
                    if ($position -lt 0 -or $position -gt 900719925474099) { throw 'Invalid playback position' }
                    $ok = Await-Result ($entry.session.TryChangePlaybackPositionAsync($position * 10000)) ([bool])
                }
                if (-not $ok) { throw 'The media player rejected the operation' }
                $result = Describe-Session $entry
            }
            default { throw 'Unknown Windows media operation' }
        }
        $reply = @{ requestId = $request.requestId; result = $result }
    } catch {
        $reply = @{ requestId = $request.requestId; error = $_.Exception.Message }
    }
    [Console]::WriteLine(($reply | ConvertTo-Json -Depth 8 -Compress))
}
