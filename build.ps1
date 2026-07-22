param(
    [string]$PythonExecutable = "python"
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

& $PythonExecutable -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) {
    throw "Dependency installation failed with exit code $LASTEXITCODE."
}

& $PythonExecutable -m unittest -v test_strategy.py
if ($LASTEXITCODE -ne 0) {
    throw "Tests failed with exit code $LASTEXITCODE."
}

$specFiles = @(Get-ChildItem -LiteralPath $PSScriptRoot -Filter "*.spec" -File)
if ($specFiles.Count -ne 1) {
    throw "Expected exactly one PyInstaller spec file, found $($specFiles.Count)."
}

$buildStarted = Get-Date
& $PythonExecutable -m PyInstaller --noconfirm --clean $specFiles[0].FullName
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE."
}

$artifacts = @(
    Get-ChildItem -LiteralPath (Join-Path $PSScriptRoot "dist") -Filter "*.exe" -File |
        Where-Object { $_.LastWriteTime -ge $buildStarted.AddSeconds(-2) }
)
if ($artifacts.Count -ne 1) {
    throw "Expected exactly one executable artifact, found $($artifacts.Count)."
}

$artifactPath = $artifacts[0].FullName
$artifacts[0] | Select-Object FullName, Length, LastWriteTime
Get-FileHash -Algorithm SHA256 -LiteralPath $artifactPath
