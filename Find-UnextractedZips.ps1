#Requires -Version 7.0
<#
.SYNOPSIS
Read-only check of which Takeout zip archives have been fully extracted.

.DESCRIPTION
For every *.zip in ZipRoot, reads the archive's file list (nothing is extracted) and
checks that each entry exists on disk with the same byte size. An entry counts as
present if it is found at either:
    <SourceRoot>\<zip name>\<entry path>        (normal extraction)
    <QuarantineRoot>\<zip name>\<entry path>    (already quarantined by the dedup script)

If no folder named after the zip exists, the export folder holding most of the zip's
entries (same path + size) is used instead, so a zip extracted under another folder
name is still recognised.

Entries inside Google Photos "Trash" (or "Bin") folders are counted separately, because
those folders may have been removed on purpose.

With -VerifyContent, every missing media entry (not JSON) is SHA-256 hashed straight
from the zip and compared with same-size files anywhere in SourceRoot and
QuarantineRoot. This tells apart "not extracted" from "extracted, but now lives at a
different path" (for example a duplicate that was quarantined from another folder).

Nothing is created, moved or modified except the CSV in ReportRoot (skip with -NoCsv).

.EXAMPLE
    .\Find-UnextractedZips.ps1
    .\Find-UnextractedZips.ps1 -VerifyContent -ShowMissing 20
#>
[CmdletBinding()]
param(
    [string]$ZipRoot = 'C:\Media\Imports',
    [string]$SourceRoot = 'C:\Media\Imports',
    [string]$QuarantineRoot = 'C:\Media\DuplicateQuarantine',
    [string]$ReportRoot = 'C:\Media\DedupeReports',
    [switch]$VerifyContent,
    [int]$ShowMissing = 0,
    [switch]$NoCsv
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.IO.Compression.FileSystem

$sep = [System.IO.Path]::DirectorySeparatorChar
$ci = [System.StringComparer]::OrdinalIgnoreCase

function Get-FileIndex {
    # ByRel:  "<folder>\<path in folder>" -> size
    # ByPart: "<path in folder>" -> list of folder names holding that path (any size)
    # BySize: size -> list of full paths (for content checks)
    param([string]$Root)

    $index = @{
        ByRel  = [System.Collections.Generic.Dictionary[string, long]]::new($ci)
        ByPart = [System.Collections.Generic.Dictionary[string, System.Collections.Generic.List[object]]]::new($ci)
        BySize = [System.Collections.Generic.Dictionary[long, System.Collections.Generic.List[string]]]::new()
    }

    if (-not (Test-Path -LiteralPath $Root -PathType Container)) { return $index }

    $rootFull = [System.IO.Path]::GetFullPath($Root).TrimEnd($sep) + $sep
    $opts = [System.IO.EnumerationOptions]::new()
    $opts.RecurseSubdirectories = $true
    $opts.IgnoreInaccessible = $true
    $opts.AttributesToSkip = [System.IO.FileAttributes]::ReparsePoint

    foreach ($fi in [System.IO.DirectoryInfo]::new($rootFull).EnumerateFiles('*', $opts)) {
        $rel = $fi.FullName.Substring($rootFull.Length)
        $index.ByRel[$rel] = $fi.Length

        if (-not $index.BySize.ContainsKey($fi.Length)) {
            $index.BySize[$fi.Length] = [System.Collections.Generic.List[string]]::new()
        }
        $index.BySize[$fi.Length].Add($fi.FullName)

        $cut = $rel.IndexOf($sep)
        if ($cut -gt 0) {
            $partRel = $rel.Substring($cut + 1)
            if (-not $index.ByPart.ContainsKey($partRel)) {
                $index.ByPart[$partRel] = [System.Collections.Generic.List[object]]::new()
            }
            $index.ByPart[$partRel].Add([pscustomobject]@{ Folder = $rel.Substring(0, $cut); Length = $fi.Length })
        }
    }

    return $index
}

$script:HashCache = [System.Collections.Generic.Dictionary[string, string]]::new($ci)

function Get-StreamHash([System.IO.Stream]$Stream) {
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try { return [System.Convert]::ToHexString($sha.ComputeHash($Stream)) }
    finally { $sha.Dispose() }
}

function Get-DiskHash([string]$Path) {
    $h = $null
    if ($script:HashCache.TryGetValue($Path, [ref]$h)) { return $h }
    $fs = [System.IO.File]::Open($Path, 'Open', 'Read', 'Read')
    try { $h = Get-StreamHash $fs } finally { $fs.Dispose() }
    $script:HashCache[$Path] = $h
    return $h
}

$zips = @(Get-ChildItem -LiteralPath $ZipRoot -Filter '*.zip' -File | Sort-Object Name)
if ($zips.Count -eq 0) {
    Write-Host "No zip files found in $ZipRoot"
    return
}

Write-Host ('Indexing {0} and {1} ...' -f $SourceRoot, $QuarantineRoot)
$src = Get-FileIndex -Root $SourceRoot
$qua = Get-FileIndex -Root $QuarantineRoot
Write-Host ('  {0:n0} files in source, {1:n0} in quarantine' -f $src.ByRel.Count, $qua.ByRel.Count)
Write-Host ''

$results = foreach ($zip in $zips) {
    $base = $zip.BaseName
    $row = [ordered]@{
        Zip = $zip.Name; SizeGB = [math]::Round($zip.Length / 1GB, 2); Status = ''
        Files = 0; Present = 0; InQuarantine = 0; MissingTrash = 0; Missing = 0
        MissingJson = 0; ContentInSource = 0; ContentInQuarantineOnly = 0; ContentNowhere = 0
        PercentPresent = 0.0; Folder = $base; Note = ''; MissingList = @()
    }

    $archive = $null
    try {
        $archive = [System.IO.Compression.ZipFile]::OpenRead($zip.FullName)
    }
    catch {
        $row.Status = 'UNREADABLE'
        $row.Note = 'Cannot open zip (still copying, or corrupt): ' + $_.Exception.Message
        [pscustomobject]$row
        continue
    }

    try {
        $entries = @($archive.Entries | Where-Object { -not ($_.FullName.EndsWith('/') -or $_.FullName.EndsWith('\')) })
        $row.Files = $entries.Count

        # Which folder holds this zip? Normally the one named after it.
        $folder = $base
        if (-not (Test-Path -LiteralPath (Join-Path $SourceRoot $base) -PathType Container)) {
            $votes = @{}
            foreach ($e in $entries) {
                $cands = $null
                if ($src.ByPart.TryGetValue($e.FullName.Replace('/', $sep), [ref]$cands)) {
                    foreach ($c in $cands) { if ($c.Length -eq $e.Length) { $votes[$c.Folder] = 1 + [int]$votes[$c.Folder] } }
                }
            }
            $best = $votes.GetEnumerator() | Sort-Object Value -Descending | Select-Object -First 1
            if ($best -and $best.Value -ge 0.5 * $entries.Count) {
                $folder = $best.Key
                $row.Note = 'No folder named after the zip; using the folder that holds most of its files'
            }
            else {
                $folder = ''
            }
        }
        $row.Folder = $folder

        $missing = [System.Collections.Generic.List[string]]::new()
        $missingEntries = [System.Collections.Generic.List[object]]::new()

        foreach ($e in $entries) {
            $rel = $e.FullName.Replace('/', $sep)
            $len = 0L

            if ($folder) {
                if ($src.ByRel.TryGetValue($folder + $sep + $rel, [ref]$len) -and $len -eq $e.Length) { $row.Present++; continue }
                if ($qua.ByRel.TryGetValue($folder + $sep + $rel, [ref]$len) -and $len -eq $e.Length) { $row.InQuarantine++; continue }
            }

            # Google names the deleted-items folder "Trash" or "Bin" depending on account language.
            if ($rel -match '(^|\\)Google Photos\\(Trash|Bin)\\') { $row.MissingTrash++; continue }

            $missing.Add($rel)
            if ($rel.EndsWith('.json', [System.StringComparison]::OrdinalIgnoreCase)) { $row.MissingJson++ }
            else { $missingEntries.Add($e) }
        }

        $row.Missing = $missing.Count
        $row.MissingList = $missing
        $ok = $row.Present + $row.InQuarantine
        $row.PercentPresent = if ($entries.Count -gt 0) { [math]::Round(100.0 * $ok / $entries.Count, 1) } else { 100 }

        $row.Status =
            if ($entries.Count -eq 0) { 'EMPTY ZIP' }
            elseif ($ok -eq 0) { 'NOT EXTRACTED' }
            elseif ($missing.Count -eq 0 -and $row.MissingTrash -eq 0) { 'EXTRACTED' }
            elseif ($missing.Count -eq 0) { 'EXTRACTED (Trash removed)' }
            else { 'PARTIAL' }

        # Content check: is each missing media entry's content somewhere else?
        if ($VerifyContent -and $row.Status -eq 'PARTIAL') {
            foreach ($e in $missingEntries) {
                $srcCands = $null; $quaCands = $null
                [void]$src.BySize.TryGetValue([long]$e.Length, [ref]$srcCands)
                [void]$qua.BySize.TryGetValue([long]$e.Length, [ref]$quaCands)

                if (-not $srcCands -and -not $quaCands) { $row.ContentNowhere++; continue }

                $s = $e.Open()
                try { $zipHash = Get-StreamHash $s } finally { $s.Dispose() }

                $inSrc = $false
                if ($srcCands) { foreach ($p in $srcCands) { if ((Get-DiskHash $p) -eq $zipHash) { $inSrc = $true; break } } }
                if ($inSrc) { $row.ContentInSource++; continue }

                $inQua = $false
                if ($quaCands) { foreach ($p in $quaCands) { if ((Get-DiskHash $p) -eq $zipHash) { $inQua = $true; break } } }
                if ($inQua) { $row.ContentInQuarantineOnly++ } else { $row.ContentNowhere++ }
            }
        }
    }
    finally {
        $archive.Dispose()
    }

    [pscustomobject]$row
}

$cols = @('Zip', 'SizeGB', 'Status', 'Files', 'Present', 'InQuarantine', 'MissingTrash', 'Missing', 'PercentPresent')
if ($VerifyContent) { $cols += @('MissingJson', 'ContentInSource', 'ContentInQuarantineOnly', 'ContentNowhere') }

$results | Format-Table $cols -AutoSize | Out-String -Width 300 | Write-Host

foreach ($r in $results | Where-Object { $_.Note }) { Write-Host ('{0}: {1} [{2}]' -f $r.Zip, $r.Note, $r.Folder) }

if ($ShowMissing -gt 0) {
    foreach ($r in $results | Where-Object { $_.Status -eq 'PARTIAL' }) {
        Write-Host ''
        Write-Host ('{0} - first {1} missing entries:' -f $r.Zip, [Math]::Min($ShowMissing, $r.Missing))
        $r.MissingList | Select-Object -First $ShowMissing | ForEach-Object { Write-Host "    $_" }
    }
}

$todo = @($results | Where-Object { $_.Status -in @('NOT EXTRACTED', 'PARTIAL', 'UNREADABLE') })
Write-Host ''
Write-Host ('{0} of {1} zips need attention.' -f $todo.Count, $results.Count)
Write-Host 'Missing = entry not at its expected path. With -VerifyContent, ContentNowhere = media whose content exists nowhere on disk (truly not extracted).'

if (-not $NoCsv) {
    $csv = Join-Path $ReportRoot ('ZipExtractionCheck_{0}.csv' -f (Get-Date -Format 'yyyyMMdd_HHmmss'))
    $results | Select-Object ($cols + @('MissingJson', 'ContentInSource', 'ContentInQuarantineOnly', 'ContentNowhere', 'Folder', 'Note') | Select-Object -Unique) |
        Export-Csv -LiteralPath $csv -NoTypeInformation -Encoding UTF8
    Write-Host "CSV: $csv"
}
