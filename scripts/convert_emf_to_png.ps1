param(
    [Parameter(Mandatory = $true)]
    [string]$Root,

    [switch]$Recurse
)

Add-Type -AssemblyName System.Drawing

if ($Recurse) {
    $files = Get-ChildItem -Path $Root -Recurse -File | Where-Object { $_.Extension -in '.emf', '.wmf', '.tif', '.tiff' }
}
else {
    $files = Get-ChildItem -Path $Root -File | Where-Object { $_.Extension -in '.emf', '.wmf', '.tif', '.tiff' }
}

$count = 0
foreach ($f in $files) {
    $png = Join-Path $f.DirectoryName ($f.BaseName + '.png')
    if (Test-Path -LiteralPath $png) {
        Write-Output "SKIP $($f.Name) (png exists)"
        continue
    }
    try {
        $img = [System.Drawing.Image]::FromFile($f.FullName)
        $bmp = New-Object System.Drawing.Bitmap($img.Width, $img.Height)
        $g = [System.Drawing.Graphics]::FromImage($bmp)
        $g.Clear([System.Drawing.Color]::White)
        $g.DrawImage($img, 0, 0, $img.Width, $img.Height)
        $bmp.Save($png, [System.Drawing.Imaging.ImageFormat]::Png)
        $g.Dispose()
        $bmp.Dispose()
        $img.Dispose()
        $count++
        Write-Output "OK $($f.Name) -> $(Split-Path $png -Leaf)"
    }
    catch {
        Write-Output "FAIL $($f.FullName): $($_.Exception.Message)"
    }
}

Write-Output "DONE converted=$count"
