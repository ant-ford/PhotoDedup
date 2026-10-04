#Requires -Version 7.0
<#
.SYNOPSIS
Extracts Takeout / Google Photos album zips that have not been extracted yet.

.DESCRIPTION
Each <name>.zip in -ZipRoot is extracted to <ExtractRoot>\<name>\ (the zip's own folder
structure kept, file dates taken from the zip). A zip whose folder already exists is
skipped. Nothing is ever overwritten; after extracting, every entry is checked to be on
disk with the right size.

.EXAMPLE
    .\Expand-TakeoutZips.ps1 -WhatIf
    .\Expand-TakeoutZips.ps1
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$ZipRoot = 'C:\Media\Imports',
    [string]$ExtractRoot = 'C:\Media\Imports'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.IO.Compression.FileSystem

$done = 0; $skipped = 0; $failed = 0
foreach ($zip in Get-ChildItem -LiteralPath $ZipRoot -Filter '*.zip' -File | Sort-Object Name) {
    $target = Join-Path $ExtractRoot $zip.BaseName
    if (Test-Path -LiteralPath $target) { $skipped++; continue }
    if (-not $PSCmdlet.ShouldProcess($zip.Name, "extract to $target")) { continue }

    $archive = [System.IO.Compression.ZipFile]::OpenRead($zip.FullName)
    try {
        $entries = @($archive.Entries | Where-Object { -not $_.FullName.EndsWith('/') })
        $root = [System.IO.Path]::GetFullPath($target) + [System.IO.Path]::DirectorySeparatorChar
        foreach ($e in $entries) {
            $dest = [System.IO.Path]::GetFullPath((Join-Path $target ($e.FullName -replace '/', '\')))
            if (-not $dest.StartsWith($root, [System.StringComparison]::OrdinalIgnoreCase)) { throw "Unsafe path in zip: $($e.FullName)" }
            [void][System.IO.Directory]::CreateDirectory((Split-Path -Parent $dest))
            [System.IO.Compression.ZipFileExtensions]::ExtractToFile($e, $dest, $false)  # $false: never overwrite
        }
        $bad = @($entries | Where-Object {
            $p = Join-Path $target ($_.FullName -replace '/', '\')
            -not (Test-Path -LiteralPath $p -PathType Leaf) -or (Get-Item -LiteralPath $p).Length -ne $_.Length
        })
        if ($bad.Count) { throw "$($bad.Count) entries missing or wrong size after extraction" }
        $done++
        Write-Host ('  {0}: {1:n0} files' -f $zip.Name, $entries.Count)
    }
    catch {
        $failed++
        Write-Host ('  FAILED {0}: {1}' -f $zip.Name, $_.Exception.Message) -ForegroundColor Red
    }
    finally { $archive.Dispose() }
}
Write-Host ('Extracted {0}, already extracted {1}, failed {2}.' -f $done, $skipped, $failed)
exit $(if ($failed) { 1 } else { 0 })
