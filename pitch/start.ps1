$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root
Write-Host "Quorum pitch desk  http://127.0.0.1:8787"
uv run python server.py
