[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateScript({ Test-Path -LiteralPath $_ -PathType Leaf })]
    [string]$HandBrakeArchive,

    [Parameter(Mandatory = $true)]
    [ValidateScript({ Test-Path -LiteralPath $_ -PathType Leaf })]
    [string]$FfmpegArchive,

    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[A-Fa-f0-9]{64}$')]
    [string]$FfmpegArchiveSha256,

    [Parameter(Mandatory = $true)]
    [ValidatePattern('^https://')]
    [string]$FfmpegArchiveUrl,

    [Parameter(Mandatory = $true)]
    [ValidatePattern('^https://')]
    [string]$FfmpegSourceUrl,

    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[A-Fa-f0-9]{64}$')]
    [string]$FfmpegSourceSha256,

    [string]$PythonCommand = "py",
    [string]$OutputDirectory = (Join-Path $PSScriptRoot "..\release"),
    [string]$WorkDirectory = (Join-Path $PSScriptRoot "..\.release-build"),
    [switch]$KeepWorkDirectory
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$AppVersion = "1.0.0"
$AssetName = "Espresso-Compresso-Windows-x64.zip"
$HandBrakeArchiveSha256 = "80bfe8d5f5d11cc3ef76b834add3ed4e82dee6523ffeb435c283f88b1a21f09d"
$SourceRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$OutputDirectory = [System.IO.Path]::GetFullPath($OutputDirectory)
$WorkDirectory = [System.IO.Path]::GetFullPath($WorkDirectory)
$VenvDirectory = Join-Path $WorkDirectory "venv"
$GuiBuildDirectory = Join-Path $WorkDirectory "gui"
$WorkerBuildDirectory = Join-Path $WorkDirectory "worker"
$StageDirectory = Join-Path $WorkDirectory "stage"
$StagedTools = Join-Path $StageDirectory "tools"
$ReleaseRoot = Join-Path $OutputDirectory "Espresso Compresso"
$IconPath = Join-Path $WorkDirectory "espresso_compresso.ico"

function Get-Sha256([string]$Path) {
    return (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToLowerInvariant()
}

function Require-Hash([string]$Path, [string]$Expected, [string]$Label) {
    $actual = Get-Sha256 $Path
    if ($actual -ne $Expected.ToLowerInvariant()) {
        throw "$Label SHA-256 did not match. Expected $Expected; got $actual."
    }
}

function Find-OneFile([string]$Root, [string]$Name) {
    $matches = @(Get-ChildItem -LiteralPath $Root -Recurse -File -Filter $Name)
    if ($matches.Count -ne 1) {
        throw "Expected exactly one $Name below $Root; found $($matches.Count)."
    }
    return $matches[0]
}

function Find-NoticeFiles([string]$Root) {
    return @(Get-ChildItem -LiteralPath $Root -Recurse -File |
        Where-Object { $_.Name -like "LICENSE*" -or $_.Name -like "COPYING*" -or $_.Name -like "README*" })
}

if ($env:OS -ne "Windows_NT") {
    throw "This release builder must run on Windows."
}
if (Test-Path -LiteralPath $OutputDirectory) {
    throw "Release output already exists: $OutputDirectory. Choose an empty output directory."
}
if (Test-Path -LiteralPath $WorkDirectory) {
    throw "Release work directory already exists: $WorkDirectory. Choose an empty work directory."
}

Require-Hash $HandBrakeArchive $HandBrakeArchiveSha256 "HandBrakeCLI 1.11.2 archive"
Require-Hash $FfmpegArchive $FfmpegArchiveSha256 "FFmpeg 8.1.2 archive"
if ([string]::IsNullOrWhiteSpace($FfmpegArchiveUrl)) {
    throw "FFmpeg archive URL is required for release provenance."
}
if ([string]::IsNullOrWhiteSpace($FfmpegSourceUrl) -or [string]::IsNullOrWhiteSpace($FfmpegSourceSha256)) {
    throw "FFmpeg corresponding source URL and SHA-256 are required for release provenance."
}

New-Item -ItemType Directory -Path $WorkDirectory, $StageDirectory, $StagedTools, $OutputDirectory | Out-Null
try {
    & $PythonCommand -3.10 -m venv $VenvDirectory
    if ($LASTEXITCODE -ne 0) { throw "Could not create the isolated Python 3.10 build environment." }
    $Python = Join-Path $VenvDirectory "Scripts\python.exe"
    & $Python -m pip install --upgrade pip
    & $Python -m pip install --requirement (Join-Path $SourceRoot "requirements-build.txt")
    & $Python -m pip check
    if ($LASTEXITCODE -ne 0) { throw "The isolated build environment has unresolved dependencies." }

    $HandBrakeExtract = Join-Path $StageDirectory "handbrake"
    $FfmpegExtract = Join-Path $StageDirectory "ffmpeg"
    Expand-Archive -LiteralPath $HandBrakeArchive -DestinationPath $HandBrakeExtract
    Expand-Archive -LiteralPath $FfmpegArchive -DestinationPath $FfmpegExtract
    Copy-Item -LiteralPath (Find-OneFile $HandBrakeExtract "HandBrakeCLI.exe").FullName -Destination (Join-Path $StagedTools "HandBrakeCLI.exe")
    Copy-Item -LiteralPath (Find-OneFile $FfmpegExtract "ffmpeg.exe").FullName -Destination (Join-Path $StagedTools "ffmpeg.exe")
    Copy-Item -LiteralPath (Find-OneFile $FfmpegExtract "ffprobe.exe").FullName -Destination (Join-Path $StagedTools "ffprobe.exe")
    $ffmpegBin = (Find-OneFile $FfmpegExtract "ffmpeg.exe").Directory
    $ffmpegDlls = @(Get-ChildItem -LiteralPath $ffmpegBin.FullName -File -Filter "*.dll")
    if ($ffmpegDlls.Count -eq 0) { throw "The FFmpeg archive contains no runtime DLLs beside ffmpeg.exe." }
    Copy-Item -LiteralPath $ffmpegDlls.FullName -Destination $StagedTools
    if (Test-Path -LiteralPath (Join-Path $StagedTools "ffplay.exe")) { throw "ffplay.exe must not be packaged." }

    & $Python (Join-Path $SourceRoot "packaging\make_icon.py") --source (Join-Path $SourceRoot "espresso_compresso_icon.svg") --output $IconPath
    if ($LASTEXITCODE -ne 0) { throw "Could not generate the Windows icon." }

    Push-Location $SourceRoot
    try {
        & $Python -m PyInstaller --noconfirm --clean --onedir --windowed --contents-directory _internal `
            --name "Espresso Compresso" --icon $IconPath --version-file (Join-Path $SourceRoot "packaging\version_info.txt") `
            --add-data "$StagedTools;tools" --distpath $GuiBuildDirectory --workpath (Join-Path $WorkDirectory "pyinstaller-gui") `
            "espresso_compresso.py"
        if ($LASTEXITCODE -ne 0) { throw "PyInstaller GUI build failed." }
        & $Python -m PyInstaller --noconfirm --clean --onedir --console --contents-directory _internal `
            --name "Espresso Compresso Worker" --icon $IconPath --version-file (Join-Path $SourceRoot "packaging\version_info.txt") `
            --distpath $WorkerBuildDirectory --workpath (Join-Path $WorkDirectory "pyinstaller-worker") `
            "espresso_compresso_worker.py"
        if ($LASTEXITCODE -ne 0) { throw "PyInstaller worker build failed." }
    }
    finally {
        Pop-Location
    }

    Copy-Item -LiteralPath (Join-Path $GuiBuildDirectory "Espresso Compresso") -Destination $ReleaseRoot -Recurse
    $WorkerTarget = Join-Path $ReleaseRoot "_internal\worker"
    New-Item -ItemType Directory -Path $WorkerTarget | Out-Null
    Get-ChildItem -LiteralPath (Join-Path $WorkerBuildDirectory "Espresso Compresso Worker") -Force |
        Copy-Item -Destination $WorkerTarget -Recurse
    Copy-Item -LiteralPath (Join-Path $SourceRoot "START HERE.txt"), (Join-Path $SourceRoot "README.md"), (Join-Path $SourceRoot "THIRD_PARTY_NOTICES.md") -Destination $ReleaseRoot
    @(
        "Espresso Compresso v$AppVersion third-party provenance",
        "",
        "HandBrakeCLI archive: https://github.com/HandBrake/HandBrake/releases/download/1.11.2/HandBrakeCLI-1.11.2-win-x86_64.zip",
        "HandBrakeCLI archive SHA-256: $HandBrakeArchiveSha256",
        "HandBrakeCLI source: https://github.com/HandBrake/HandBrake/releases/download/1.11.2/HandBrake-1.11.2-source.tar.bz2",
        "",
        "FFmpeg archive: $FfmpegArchiveUrl",
        "FFmpeg archive SHA-256: $FfmpegArchiveSha256",
        "FFmpeg corresponding source: $FfmpegSourceUrl",
        "FFmpeg corresponding source SHA-256: $FfmpegSourceSha256"
    ) | Set-Content -LiteralPath (Join-Path $ReleaseRoot "THIRD_PARTY_PROVENANCE.txt") -Encoding utf8
    New-Item -ItemType Directory -Path (Join-Path $ReleaseRoot "LICENSES\HandBrake") | Out-Null
    $HandBrakeLicenses = @(Find-NoticeFiles $HandBrakeExtract)
    if ($HandBrakeLicenses.Count -eq 0) { throw "The HandBrake archive has no distributable license or notice files." }
    Copy-Item -LiteralPath $HandBrakeLicenses.FullName -Destination (Join-Path $ReleaseRoot "LICENSES\HandBrake")
    $FfmpegLicenses = @(Find-NoticeFiles $FfmpegExtract)
    if ($FfmpegLicenses.Count -eq 0) { throw "The FFmpeg archive has no distributable license or notice files." }
    New-Item -ItemType Directory -Path (Join-Path $ReleaseRoot "LICENSES\FFmpeg") | Out-Null
    Copy-Item -LiteralPath $FfmpegLicenses.FullName -Destination (Join-Path $ReleaseRoot "LICENSES\FFmpeg")

    $ChecksumPath = Join-Path $ReleaseRoot "SHA256SUMS.txt"
    Get-ChildItem -LiteralPath $ReleaseRoot -Recurse -File |
        Sort-Object FullName |
        ForEach-Object {
            $relative = $_.FullName.Substring($ReleaseRoot.Length + 1).Replace("\\", "/")
            "$(Get-Sha256 $_.FullName)  $relative"
        } | Set-Content -LiteralPath $ChecksumPath -Encoding utf8
    $ZipPath = Join-Path $OutputDirectory $AssetName
    Compress-Archive -LiteralPath $ReleaseRoot -DestinationPath $ZipPath
    "$(Get-Sha256 $ZipPath)  $AssetName" | Set-Content -LiteralPath (Join-Path $OutputDirectory "SHA256SUMS.txt") -Encoding utf8
    Write-Host "Built Espresso Compresso v${AppVersion}: $ZipPath"
}
finally {
    if (-not $KeepWorkDirectory -and (Test-Path -LiteralPath $WorkDirectory)) {
        Remove-Item -LiteralPath $WorkDirectory -Recurse -Force
    }
}
