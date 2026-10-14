$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) {
    $systemPython = (Get-Command py, python -ErrorAction SilentlyContinue | Select-Object -First 1).Source
    if (-not $systemPython) { throw 'Python 3.10 이상을 설치해 주세요.' }
    & $systemPython -m venv .venv
}
& $python -m pip install --disable-pip-version-check -r requirements.txt
$siteUrl = 'http://localhost:3000'
$healthUrl = 'http://127.0.0.1:3000'
try {
    Invoke-WebRequest -Uri $healthUrl -TimeoutSec 2 | Out-Null
} catch {
    Start-Process -FilePath $python -ArgumentList 'server.py' -WorkingDirectory $PSScriptRoot -WindowStyle Hidden | Out-Null
    $ready = $false
    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        Start-Sleep -Milliseconds 250
        try {
            Invoke-WebRequest -Uri $healthUrl -TimeoutSec 2 | Out-Null
            $ready = $true
            break
        } catch { }
    }
    if (-not $ready) { throw '사이트 서버가 시작되지 않았습니다.' }
}
Start-Process $siteUrl
