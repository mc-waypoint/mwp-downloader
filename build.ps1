$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Push-Location $projectRoot

try {
    uv sync --group build
    if ($LASTEXITCODE -ne 0) {
        throw "uv dependency synchronization failed with exit code $LASTEXITCODE"
    }

    uv run --group build python -m nuitka `
        --mode=onefile `
        --zig `
        --assume-yes-for-downloads `
        --windows-console-mode=disable `
        --include-package-data=nicegui `
        --output-dir=dist `
        --output-filename=mwp-downloader.exe `
        main.py
    if ($LASTEXITCODE -ne 0) {
        throw "Nuitka build failed with exit code $LASTEXITCODE"
    }
}
finally {
    Pop-Location
}
