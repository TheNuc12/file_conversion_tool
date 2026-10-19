# Downloads the FFmpeg and Pandoc builds that ship inside the Windows app, and checks
# each download against a pinned SHA-256 so a tampered file can never be bundled.
#
#   tools\       ffmpeg.exe, pandoc.exe   (bundled into Rotary.exe)
#   test-tools\  ffprobe.exe              (only used by the test suite)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"  # the progress bar makes Invoke-WebRequest crawl

$downloads = @(
    @{
        Url    = "https://github.com/GyanD/codexffmpeg/releases/download/7.1.1/ffmpeg-7.1.1-essentials_build.zip"
        Sha256 = "04861d3339c5ebe38b56c19a15cf2c0cc97f5de4fa8910e4d47e5e6404e4a2d4"
        Files  = @{
            "ffmpeg-7.1.1-essentials_build/bin/ffmpeg.exe"  = "tools"
            "ffmpeg-7.1.1-essentials_build/bin/ffprobe.exe" = "test-tools"
        }
    },
    @{
        Url    = "https://github.com/jgm/pandoc/releases/download/3.6.4/pandoc-3.6.4-windows-x86_64.zip"
        Sha256 = "a9e5feb3d56d2fb0e3e765d1c33b8ee6b72e6963d7de31504edeec8cd1be34b1"
        Files  = @{ "pandoc-3.6.4/pandoc.exe" = "tools" }
    }
)

$cache = Join-Path $PSScriptRoot "downloads"
foreach ($dir in @($cache, (Join-Path $PSScriptRoot "tools"), (Join-Path $PSScriptRoot "test-tools"))) {
    New-Item -ItemType Directory -Force $dir | Out-Null
}
Add-Type -AssemblyName System.IO.Compression.FileSystem

foreach ($d in $downloads) {
    $zip = Join-Path $cache ([IO.Path]::GetFileName($d.Url))
    if (-not (Test-Path $zip)) {
        Write-Host "Downloading $($d.Url)"
        Invoke-WebRequest $d.Url -OutFile $zip
    }
    $hash = (Get-FileHash $zip -Algorithm SHA256).Hash.ToLower()
    if ($hash -ne $d.Sha256) {
        Remove-Item $zip
        throw "Checksum mismatch for $zip (got $hash, expected $($d.Sha256))"
    }
    $archive = [IO.Compression.ZipFile]::OpenRead($zip)
    try {
        foreach ($file in $d.Files.GetEnumerator()) {
            $entry = $archive.GetEntry($file.Key)
            if (-not $entry) { throw "$($file.Key) is missing from $zip" }
            $target = Join-Path (Join-Path $PSScriptRoot $file.Value) $entry.Name
            [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $target, $true)
            Write-Host "  -> $target"
        }
    }
    finally {
        $archive.Dispose()
    }
}
