$ErrorActionPreference = "Stop"
function Path-Refresh { $env:Path = [Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [Environment]::GetEnvironmentVariable("Path","User") + ";$env:USERPROFILE\.local\bin" }

Write-Host "Installing Fortgeist..." -ForegroundColor Cyan
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { irm https://astral.sh/uv/install.ps1 | iex; Path-Refresh }
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) { winget install -e --id Gyan.FFmpeg --accept-source-agreements --accept-package-agreements; Path-Refresh }
uv tool install --force --python 3.12 https://github.com/levzb/fortgeist/archive/refs/heads/main.zip
uv tool update-shell | Out-Null; Path-Refresh

$exe = Join-Path (uv tool dir --bin) "fortgeist.exe"
& $exe config --no-open | Out-Null
$s = (New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path ([Environment]::GetFolderPath("Desktop")) "Fortgeist.lnk"))
$s.TargetPath = $exe; $s.WorkingDirectory = "$env:USERPROFILE\Fortgeist"; $s.Save()

Write-Host "Done! Open 'Fortgeist' on your desktop." -ForegroundColor Green
