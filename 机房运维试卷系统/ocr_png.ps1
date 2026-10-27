# OCR one PNG via the built-in Windows engine (Windows.Media.Ocr).
# Called by ocr_win.py:  powershell -File ocr_png.ps1 [-Check] [-Path x.png]
# stdout is the recognized text, one line per OCR line; "NOOCR" means the
# Chinese OCR language pack is missing.
param([string]$Path = "", [switch]$Check)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

# WinRT 类型在 -File 模式下不会自动投影，必须先显式预加载，否则 TypeNotFound
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]

$ocr = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
if ($Check) {
    if ($null -eq $ocr) { Write-Output "NOOCR" }
    else { Write-Output $ocr.RecognizerLanguage.LanguageTag }
    exit 0
}
if ($null -eq $ocr) { Write-Output "NOOCR"; exit 2 }

Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() |
    Where-Object { $_.Name -eq "AsTask" -and $_.GetParameters().Count -eq 1 -and
        $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($op, $type) {
    $netTask = $asTask.MakeGenericMethod($type).Invoke($null, @($op))
    $null = $netTask.Wait(-1)
    $netTask.Result
}

$null = [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType = WindowsRuntime]
$null = [Windows.Media.Ocr.OcrResult, Windows.Media.Ocr, ContentType = WindowsRuntime]

$file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($Path)) ([Windows.Storage.StorageFile])
$stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
$decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
$bmp = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
if ($bmp.BitmapPixelFormat -ne [Windows.Graphics.Imaging.BitmapPixelFormat]::Bgra8) {
    $bmp = [Windows.Graphics.Imaging.SoftwareBitmap]::Convert($bmp,
        [Windows.Graphics.Imaging.BitmapPixelFormat]::Bgra8,
        [Windows.Graphics.Imaging.BitmapAlphaMode]::Premultiplied)
}
$res = Await ($ocr.RecognizeAsync($bmp)) ([Windows.Media.Ocr.OcrResult])
foreach ($line in $res.Lines) { Write-Output $line.Text }
