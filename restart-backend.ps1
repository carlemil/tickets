# Restart the Tickets backend (uvicorn on 127.0.0.1:8123, serving this folder).
#   powershell -NoProfile -File restart-backend.ps1            restart now
#   powershell -NoProfile -File restart-backend.ps1 -Delay 30  return at once; restart 30 s
#     later in a detached process (the agent's deploy uses this: it reports its result
#     through the backend, so the restart must come after it)
param([int]$Delay = 0, [int]$Sleep = 0)
if ($Delay -gt 0) {
    Start-Process powershell -WindowStyle Hidden -ArgumentList '-NoProfile', '-File', "`"$PSCommandPath`"", '-Sleep', $Delay
    return
}
Start-Sleep -Seconds $Sleep
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'uvicorn.*app:app.*8123' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Process -FilePath uv -ArgumentList run, uvicorn, app:app, --host, 127.0.0.1, --port, 8123, `
    --log-config, "`"$PSScriptRoot\log-config.json`"" `
    -WorkingDirectory $PSScriptRoot -WindowStyle Minimized
