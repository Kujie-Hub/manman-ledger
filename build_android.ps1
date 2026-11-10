# Build the self-contained Android APK.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not $env:PUB_HOSTED_URL) {
    $env:PUB_HOSTED_URL = "https://pub.flutter-io.cn"
}
if (-not $env:FLUTTER_STORAGE_BASE_URL) {
    $env:FLUTTER_STORAGE_BASE_URL = "https://storage.flutter-io.cn"
}

if (-not (Test-Path ".venv\Scripts\flet.exe")) {
    throw "Run install_dependencies.bat first."
}

& ".venv\Scripts\flet.exe" build apk --yes --log-format plain `
    --build-version 1.1.3 --build-number 113 .

Write-Host "APK output is under build\apk."

