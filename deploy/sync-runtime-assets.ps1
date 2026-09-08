param(
    [string]$DeployHost = "192.168.17.55",
    [int]$DeployPort = 6970,
    [string]$DeployUser = "ubuntu",
    [string]$RemoteBackendDir = "/home/ubuntu/savegeo/backend",
    [string]$LegacyModelsDir = ""
)

$ErrorActionPreference = "Stop"
$backendRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$remote = "${DeployUser}@${DeployHost}"

foreach ($relative in @("var\disaster_rasters", "var\saved_models")) {
    $path = Join-Path $backendRoot $relative
    if (-not (Test-Path $path -PathType Container)) {
        throw "Direktori aset tidak ditemukan: $path"
    }
}

if ([string]::IsNullOrWhiteSpace($LegacyModelsDir)) {
    $candidate = Join-Path $backendRoot "..\..\backend\saved_models"
    if (Test-Path $candidate -PathType Container) {
        $LegacyModelsDir = (Resolve-Path $candidate).Path
    }
}

$archiveName = "savegeo-runtime-assets-$PID.tar"
$archivePath = Join-Path ([IO.Path]::GetTempPath()) $archiveName
$tarArgs = @(
    "-cf", $archivePath,
    "-C", $backendRoot, "var\disaster_rasters",
    "var\saved_models"
)
if (Test-Path (Join-Path $backendRoot "var\samgeo") -PathType Container) {
    $tarArgs += @("var\samgeo")
}
if ($LegacyModelsDir -and (Test-Path $LegacyModelsDir -PathType Container)) {
    Write-Host "Memasukkan model legacy termasuk GEDI/ESA CCI..."
    # Legacy files are placed at the archive root and moved into the mounted
    # model directory on the server after extraction.
    $tarArgs += @("-C", $LegacyModelsDir, ".")
}

try {
    Write-Host "Membuat satu arsip runtime aset (transfer hanya meminta password SSH dua kali)..."
    & tar @tarArgs
    if ($LASTEXITCODE -ne 0) { throw "Gagal membuat arsip runtime aset." }

    Write-Host "Mengunggah $archiveName..."
    scp -P $DeployPort $archivePath "$remote`:$RemoteBackendDir/$archiveName"

    Write-Host "Mengekstrak aset dan me-restart API..."
    ssh -p $DeployPort $remote `
        "set -e; cd '$RemoteBackendDir'; mkdir -p var/disaster_rasters var/saved_models var/samgeo; tar -xf '$archiveName' -C .; find . -maxdepth 1 -type f \( -name '*.pkl' -o -name '*.json' \) -exec mv -f {} var/saved_models/ \;; rm -f '$archiveName'; docker compose -f docker-compose.prod.yml up -d --build api; echo raster_files=`$(find var/disaster_rasters -type f | wc -l); echo model_files=`$(find var/saved_models -type f | wc -l); echo samgeo_files=`$(find var/samgeo -type f | wc -l)"
}
finally {
    Remove-Item -LiteralPath $archivePath -Force -ErrorAction SilentlyContinue
}
