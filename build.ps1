$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot

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
        --include-package=nicegui `
        --include-package-data=nicegui `
        --onefile-tempdir-spec='{CACHE_DIR}/{PRODUCT}/{VERSION}' `
        --output-dir=dist `
        --output-filename=mwp-downloader.exe `
        --remove-output `
        --product-name=mc_waypoint `
        --product-version=1.0.0 `
        main.py
    if ($LASTEXITCODE -ne 0) {
        throw "Nuitka build failed with exit code $LASTEXITCODE"
    }
}
finally {
    Pop-Location
}
