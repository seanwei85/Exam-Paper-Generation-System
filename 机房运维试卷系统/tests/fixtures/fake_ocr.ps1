# Fake OCR engine for tests: never touches the real Windows OCR.
# Behaviours are switched by environment variables:
#   FAKE_OCR_MODE=questions -> engine reports zh-FAKE, returns $env:FAKE_OCR_TEXT
#   FAKE_OCR_MODE=gone      -> engine reports NOOCR (exit 2), like a missing language pack
param([string]$Path = "", [switch]$Check)

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

if ($env:FAKE_OCR_MODE -eq "gone") {
    Write-Output "NOOCR"
    exit 2
}
if ($Check) {
    Write-Output "zh-FAKE"
    exit 0
}
Write-Output $env:FAKE_OCR_TEXT
