#Requires -Version 7.0
<#
.SYNOPSIS
Writes a register of every Takeout / album zip: what it is, what is in it, and a checksum.

.DESCRIPTION
One row per zip in -ZipRoot: file name, size, SHA-256, date, number of files and of
photos/videos inside, the Google account or album it came from, the folder it was
extracted to, and whether that extraction is complete. Also lists each zip's entries
(path, size, date) in a second CSV, so any single photo can be traced back to its zip.
New rows are merged with an existing register, so zips moved elsewhere stay listed.

.EXAMPLE
    .\Write-ZipRegister.ps1
#>
[CmdletBinding()]
param(
    [string]$ZipRoot = 'C:\Media\Imports',
    [string]$ExtractRoot = 'C:\Media\Imports',
    [string]$RegisterRoot = 'C:\Media\Records',
    [switch]$SkipChecksum
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.IO.Compression.FileSystem
[void][System.IO.Directory]::CreateDirectory($RegisterRoot)

$register = Join-Path $RegisterRoot 'Takeout-Zip-Register.csv'
$contents = Join-Path $RegisterRoot 'Takeout-Zip-Contents.csv'
$media = '\.(jpe?g|heic|png|webp|gif|bmp|mp4|mov|avi|3gp|m4v)$'

$old = @{}
if (Test-Path -LiteralPath $register) { Import-Csv -LiteralPath $register | ForEach-Object { $old[$_.ZipName] = $_ } }

$rows = [System.Collections.Generic.List[object]]::new()
$entryRows = [System.Collections.Generic.List[object]]::new()
foreach ($zip in Get-ChildItem -LiteralPath $ZipRoot -Filter '*.zip' -File | Sort-Object Name) {
    Write-Host "  $($zip.Name)"
    $archive = [System.IO.Compression.ZipFile]::OpenRead($zip.FullName)
    try {
        $entries = @($archive.Entries | Where-Object { -not $_.FullName.EndsWith('/') })
        $target = Join-Path $ExtractRoot $zip.BaseName
        $present = @($entries | Where-Object { Test-Path -LiteralPath (Join-Path $target ($_.FullName -replace '/', '\')) }).Count
        foreach ($e in $entries) {
            $entryRows.Add([pscustomobject]@{ ZipName = $zip.Name; EntryPath = $e.FullName; SizeBytes = $e.Length; EntryDate = $e.LastWriteTime.ToString('yyyy-MM-dd HH:mm:ss') })
        }
        $isTakeout = [bool]($entries | Where-Object { $_.FullName -like 'Takeout/*' } | Select-Object -First 1)
        $source = if ($isTakeout) { 'Google Takeout (account export): ' + ($zip.BaseName -replace '-\d+[A-Z]?$', '') } else { 'Google Photos album download: ' + ($zip.BaseName -replace '-\d+-\d+$', '') }
        $prev = $old[$zip.Name]
        $hash = if ($SkipChecksum) { if ($prev) { $prev.SHA256 } else { '' } }
                elseif ($prev -and $prev.SizeBytes -eq [string]$zip.Length -and $prev.SHA256) { $prev.SHA256 }
                else { (Get-FileHash -LiteralPath $zip.FullName -Algorithm SHA256).Hash }
        $rows.Add([pscustomobject][ordered]@{
            ZipName        = $zip.Name
            Source         = $source
            SizeBytes      = $zip.Length
            SizeGB         = [math]::Round($zip.Length / 1GB, 2)
            ZipDate        = $zip.LastWriteTime.ToString('yyyy-MM-dd HH:mm')
            Files          = $entries.Count
            PhotosVideos   = @($entries | Where-Object { $_.Name -match $media }).Count
            ExtractedTo    = $target
            # Once processed, photos move on to the Library, so "fewer files left" is expected.
            Extraction     = if (-not (Test-Path -LiteralPath $target)) { 'not extracted' }
                             elseif ($present -eq $entries.Count) { 'extracted, not yet processed' }
                             else { "extracted and processed ($($entries.Count - $present) of $($entries.Count) files since moved on)" }
            SHA256         = $hash
            LastChecked    = (Get-Date -Format 'yyyy-MM-dd HH:mm')
        })
    }
    finally { $archive.Dispose() }
}

# Keep zips that are no longer in ZipRoot (moved to an archive drive, say).
foreach ($name in $old.Keys) {
    if (-not ($rows | Where-Object ZipName -eq $name)) { $rows.Add($old[$name]) }
}

$rows | Sort-Object ZipName | Export-Csv -LiteralPath $register -NoTypeInformation -Encoding UTF8
if ($entryRows.Count) {
    $keep = @()
    if (Test-Path -LiteralPath $contents) {
        # Entries of zips not re-read this time (e.g. moved elsewhere) are kept.
        $keep = @(Import-Csv -LiteralPath $contents | Where-Object { $_.ZipName -notin $entryRows.ZipName })
    }
    @($keep) + @($entryRows) | Export-Csv -LiteralPath $contents -NoTypeInformation -Encoding UTF8
}
Write-Host ('Register: {0} ({1} zips, {2:n1} GB)' -f $register, $rows.Count, (($rows | Measure-Object SizeBytes -Sum).Sum / 1GB))
Write-Host "Contents: $contents"
