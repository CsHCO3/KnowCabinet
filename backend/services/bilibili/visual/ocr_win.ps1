# WinRT OCR (built into Windows, zero install): recognize all jpg in a dir, TSV output
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File ocr_win.ps1 -Dir <dir>
# Output per line: <filename>\t<char_count>\t<text>   (char_count=-1 on error, text=reason)
#
# ENCODING TRAP (twice bitten):
# 1) When python subprocess takes over stdout/stderr there is no console, so
#    [Console]::OutputEncoding = UTF8 silently fails and Write-Output falls back
#    to the ANSI codepage (GBK on zh-CN locale) -> mojibake on the python side.
# 2) This file must stay ASCII-only: PowerShell 5.1 parses BOM-less files as
#    ANSI/GBK, corrupting CJK literals at parse time.
# Fix for (1): bypass PowerShell formatting, write raw UTF-8 bytes through a
# StreamWriter bound to the standard output stream.
#
# SANDBOX TRAP (2026-10-31): under the DSH sandbox token, StorageFile.GetFileFromPathAsync
# fails with E_ACCESSDENIED ("The file ... is not in a location the app can access"),
# which surfaces as a useless "Exception calling Wait ... One or more errors occurred".
# Fix: never touch the StorageFile/StorageFolder broker. Read the bytes with plain
# System.IO (subject to ACL only) and hand BitmapDecoder an in-memory random access stream.
param([Parameter(Mandatory=$true)][string]$Dir)

$utf8NoBom = [System.Text.UTF8Encoding]::new($false)
$out = [System.IO.StreamWriter]::new([Console]::OpenStandardOutput(), $utf8NoBom)
$out.AutoFlush = $true

Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType=WindowsRuntime]
$null = [Windows.Storage.Streams.InMemoryRandomAccessStream, Windows.Storage.Streams, ContentType=WindowsRuntime]

$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() |
    Where-Object { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
                   $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]

function Await($WinRtTask, $ResultType) {
    $asTask = $asTaskGeneric.MakeGenericMethod($ResultType)
    $netTask = $asTask.Invoke($null, @($WinRtTask))
    if (-not $netTask.Wait(20000)) { throw 'winrt timeout' }
    $netTask.Result   # throws AggregateException when the WinRT call itself failed
}

$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
if (-not $engine) {
    $zh = [Windows.Media.Ocr.OcrEngine]::AvailableLanguages |
        Where-Object { $_.LanguageTag -like 'zh*' } | Select-Object -First 1
    if ($zh) { $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($zh) }
}
if (-not $engine) {
    $out.WriteLine('{"error": "no_ocr_engine"}')
    $out.Flush()
    exit 1
}

Get-ChildItem -Path $Dir -Filter *.jpg | Sort-Object Name | ForEach-Object {
    $name = $_.Name
    $full = $_.FullName
    try {
        $bytes = [System.IO.File]::ReadAllBytes($full)
        $ms = [System.IO.MemoryStream]::new($bytes)
        $ras = [System.IO.WindowsRuntimeStreamExtensions]::AsRandomAccessStream($ms)
        $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($ras)) ([Windows.Graphics.Imaging.BitmapDecoder])
        $bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
        $result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
        $text = (($result.Lines | ForEach-Object { $_.Text }) -join ' ') -replace '\s+', ' '
        $out.WriteLine(("{0}`t{1}`t{2}" -f $name, ($text -replace '\s', '').Length, $text))
        $bitmap.Dispose(); $ras.Dispose(); $ms.Dispose()
    } catch {
        $e = $_.Exception
        while ($e.InnerException) { $e = $e.InnerException }
        $out.WriteLine(("{0}`t-1`t{1}" -f $name, (($e.Message -replace '\s+', ' '))))
    }
}
$out.Flush()
