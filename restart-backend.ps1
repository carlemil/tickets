# Restart the Tickets backend (uvicorn on 127.0.0.1:8123, serving this folder).
#   powershell -NoProfile -File restart-backend.ps1            restart now
#   powershell -NoProfile -File restart-backend.ps1 -Delay 30  return at once; restart 30 s
#     later in a detached process (the agent's deploy uses this: it reports its result
#     through the backend, so the restart must come after it)
#
# It returns only once the port is being served again. Starting straight after the kill
# races the dying process for the port: the new uvicorn dies on "address already in use"
# and nothing is left serving the board — which is what a deploy landing on top of another
# restart used to do. What each run did goes in restart-backend.log, because a delayed
# restart is detached and has nowhere else to say it failed.
#
# Check it survives a restart landing on a restart (the log shows the retry doing its job):
#   .\restart-backend.ps1 -Delay 2 ; .\restart-backend.ps1    # 8123 must still answer after
param([int]$Delay = 0, [int]$Sleep = 0)
$PORT = 8123
if ($Delay -gt 0) {
    Start-Process powershell -WindowStyle Hidden -ArgumentList '-NoProfile', '-File', "`"$PSCommandPath`"", '-Sleep', $Delay
    return
}
Start-Sleep -Seconds $Sleep

function Listening { [bool](Get-NetTCPConnection -LocalPort $PORT -State Listen -ErrorAction SilentlyContinue) }

function Wait-Port([bool]$state, [int]$seconds) {
    $end = (Get-Date).AddSeconds($seconds)
    while ((Listening) -ne $state -and (Get-Date) -lt $end) { Start-Sleep -Milliseconds 300 }
    return (Listening) -eq $state
}

function Log($message) {
    "$(Get-Date -Format s) $message" | Add-Content -Path "$PSScriptRoot\restart-backend.log"
}

# Success is "the port is served", not "my uvicorn is the one serving it": two restarts that
# overlap both finish happy as soon as either one's backend is up.
for ($try = 1; $try -le 3; $try++) {
    Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match "uvicorn.*app:app.*$PORT" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    if (-not (Wait-Port $false 15)) { Log "port $PORT still held 15 s after the kill; starting anyway" }
    Start-Process -FilePath uv -ArgumentList run, uvicorn, app:app, --host, 127.0.0.1, --port, $PORT, `
        --log-config, "`"$PSScriptRoot\log-config.json`"" `
        -WorkingDirectory $PSScriptRoot -WindowStyle Minimized
    if (Wait-Port $true 25) {
        Log "backend up on $PORT (try $try)"
        return
    }
    Log "backend did not come up on $PORT (try $try)"
}
Log "GIVING UP: nothing is serving $PORT"
throw "the backend did not come up on ${PORT}: see restart-backend.log"
