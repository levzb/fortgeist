# Fortgeist installer. Run in PowerShell:
#   irm https://raw.githubusercontent.com/levzb/fortgeist/main/install.ps1 | iex
# The window always stays open at the end, so any error stays readable.

function Install-Fortgeist {
    $ErrorActionPreference = "Stop"
    $ProgressPreference = "SilentlyContinue"  # makes downloads much faster in Windows PowerShell
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    $bin = Join-Path $env:USERPROFILE ".local\bin"
    New-Item -ItemType Directory -Force -Path $bin | Out-Null

    function Refresh-Path {
        $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                    [Environment]::GetEnvironmentVariable("Path", "User") + ";$bin"
    }
    function Add-UserPath {
        $p = [Environment]::GetEnvironmentVariable("Path", "User")
        if (($p -split ";") -notcontains $bin) { [Environment]::SetEnvironmentVariable("Path", "$p;$bin", "User") }
        Refresh-Path
    }
    function Get-Zip($url, $names) {
        # Downloads a zip and copies the named exe files from anywhere inside it into $bin.
        $tmp = Join-Path ([IO.Path]::GetTempPath()) ([Guid]::NewGuid())
        New-Item -ItemType Directory -Path $tmp | Out-Null
        try {
            Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile "$tmp\a.zip"
            Expand-Archive -Path "$tmp\a.zip" -DestinationPath "$tmp\x" -Force
            foreach ($n in $names) {
                $f = Get-ChildItem -Path "$tmp\x" -Recurse -Filter $n | Select-Object -First 1
                if (-not $f) { throw "$n not found in $url" }
                Copy-Item $f.FullName (Join-Path $bin $n) -Force
            }
        } finally { Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue }
    }

    Refresh-Path
    Write-Host "Installing Fortgeist..." -ForegroundColor Cyan

    # 1. uv (Python tool manager). Its own installer calls `exit` on failure, so run it in a child process.
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        Write-Host "[1/3] Installing uv..."
        powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex"
        Refresh-Path
        if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
            Write-Host "      uv installer failed, downloading uv directly..." -ForegroundColor Yellow
            Get-Zip "https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip" @("uv.exe", "uvx.exe")
            Add-UserPath
        }
    } else { Write-Host "[1/3] uv already installed" }

    # 2. FFmpeg (video encoder): winget if present, otherwise a direct download.
    if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue) -or -not (Get-Command ffprobe -ErrorAction SilentlyContinue)) {
        Write-Host "[2/3] Installing FFmpeg..."
        if (Get-Command winget -ErrorAction SilentlyContinue) {
            try { winget install -e --id Gyan.FFmpeg --accept-source-agreements --accept-package-agreements } catch {}
            Refresh-Path
        }
        if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue) -or -not (Get-Command ffprobe -ErrorAction SilentlyContinue)) {
            Write-Host "      downloading FFmpeg directly (~100 MB)..." -ForegroundColor Yellow
            Get-Zip "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip" @("ffmpeg.exe", "ffprobe.exe")
            Add-UserPath
        }
    } else { Write-Host "[2/3] FFmpeg already installed" }

    # 3. Fortgeist itself.
    Write-Host "[3/3] Installing Fortgeist..."
    uv tool install --force --python 3.12 https://github.com/levzb/fortgeist/archive/refs/heads/main.zip
    if ($LASTEXITCODE -ne 0) { throw "uv tool install failed (exit code $LASTEXITCODE)" }
    uv tool update-shell | Out-Null
    Refresh-Path

    $exe = Join-Path (uv tool dir --bin) "fortgeist.exe"
    if (-not (Test-Path $exe)) { throw "fortgeist.exe not found at $exe" }
    & $exe config --no-open | Out-Null
    $s = (New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path ([Environment]::GetFolderPath("Desktop")) "Fortgeist.lnk"))
    $s.TargetPath = $exe; $s.WorkingDirectory = "$env:USERPROFILE\Fortgeist"; $s.Save()

    Write-Host ""
    Write-Host "Done! Open 'Fortgeist' on your desktop." -ForegroundColor Green
}

try {
    Install-Fortgeist
} catch {
    Write-Host ""
    Write-Host "Install failed: $_" -ForegroundColor Red
    Write-Host "Take a screenshot of this window and send it to whoever gave you the link." -ForegroundColor Red
}
Read-Host "Press Enter to close"
