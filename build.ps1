$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Push-Location $projectRoot

try {
    uv sync --group build
    if ($LASTEXITCODE -ne 0) {
        throw "uv dependency synchronization failed with exit code $LASTEXITCODE"
    }

    $niceguiTemplates = (
        uv run --group build python -c "from pathlib import Path; import nicegui; print(Path(nicegui.__file__).parent / 'templates')"
    ).Trim()
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to locate NiceGUI templates"
    }

    $templateDataOption = "--include-data-dir=$niceguiTemplates=nicegui/templates"
    uv run --group build python -m nuitka `
        --mode=onefile `
        --zig `
        --assume-yes-for-downloads `
        --windows-console-mode=disable `
        --include-package=nicegui `
        $templateDataOption `
        --output-dir=dist `
        --output-filename=mwp-downloader.exe `
        --remove-output `
        main.py
    if ($LASTEXITCODE -ne 0) {
        throw "Nuitka build failed with exit code $LASTEXITCODE"
    }
}
finally {
    Pop-Location
}
