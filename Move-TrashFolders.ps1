#Requires -Version 7.0
<#
.SYNOPSIS
Moves Google Photos Trash/Bin folders out of the library into a holding folder.

.DESCRIPTION
Finds every "Trash" or "Bin" folder directly under a "Google Photos" folder in each of
-Roots, and checks every media file in them by SHA-256 content:

    InLibrary       - an identical copy exists in SourceRoot outside any Trash/Bin.
    QuarantineOnly  - the only copy outside Trash/Bin is in QuarantineRoot. Usually an
                      album copy that an earlier dedup run quarantined while keeping the
                      Trash copy. Removing the Trash copy would leave that album photo
                      only in quarantine, so -Execute refuses to run while any exist,
                      unless -AllowQuarantineOnly confirms those photos are trash too.
    TrashOnly       - no copy anywhere outside Trash/Bin (a genuinely deleted photo).

Default is a read-only plan (CSV in ReportRoot). With -Execute, every file in those
folders is moved to HoldingRoot\<root folder name>\<relative path>, verified by size +
SHA-256, never overwriting anything, and an Actions-style manifest is written that
Restore-Quarantine.ps1 can reverse. Emptied Trash/Bin folders are then removed.

Nothing is deleted. Empty HoldingRoot yourself once you are satisfied.

.EXAMPLE
    .\Move-TrashFolders.ps1                 # plan only
    .\Move-TrashFolders.ps1 -Execute -WhatIf
    .\Move-TrashFolders.ps1 -Execute
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$SourceRoot = 'C:\Media\Imports',
    [string]$QuarantineRoot = 'C:\Media\DuplicateQuarantine',
    [string[]]$Roots = @('C:\Media\Imports', 'C:\Media\DuplicateQuarantine'),
    [string]$HoldingRoot = 'C:\Media\TrashHolding',
    [string]$ReportRoot = 'C:\Media\DedupeReports',
    [string[]]$TrashFolderNames = @('Trash', 'Bin'),
    [string[]]$MediaExtensions = @('.jpg', '.jpeg', '.heic', '.png', '.gif', '.webp', '.bmp', '.mp4', '.mov', '.avi', '.3gp', '.m4v', '.mkv'),
    [switch]$Execute,
    [switch]$AllowQuarantineOnly,
    [int]$ExpectedFileCount = -1
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$sep = [System.IO.Path]::DirectorySeparatorChar
$ci = [System.StringComparer]::OrdinalIgnoreCase
$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$MediaExtensions = @($MediaExtensions | ForEach-Object { $_.ToLowerInvariant() })

function Get-FullPath([string]$Path) {
    return (Get-Item -LiteralPath $Path -Force).FullName.TrimEnd($sep)
}

$hashCache = [System.Collections.Generic.Dictionary[string, string]]::new($ci)
function Get-Sha256([string]$Path) {
    $h = $null
    if ($hashCache.TryGetValue($Path, [ref]$h)) { return $h }
    $fs = [System.IO.File]::Open($Path, 'Open', 'Read', 'Read')
    try {
        $sha = [System.Security.Cryptography.SHA256]::Create()
        try { $h = [System.Convert]::ToHexString($sha.ComputeHash($fs)) } finally { $sha.Dispose() }
    }
    finally { $fs.Dispose() }
    $hashCache[$Path] = $h
    return $h
}

function Get-AllFiles([string]$Root) {
    $opts = [System.IO.EnumerationOptions]::new()
    $opts.RecurseSubdirectories = $true
    $opts.IgnoreInaccessible = $false
    $opts.AttributesToSkip = [System.IO.FileAttributes]::ReparsePoint
    return [System.IO.DirectoryInfo]::new($Root).EnumerateFiles('*', $opts)
}

# Captures the Trash/Bin folder itself, e.g. 'partA\Takeout\Google Photos\Trash'.
$trashPattern = '^((?:.*\\)?Google Photos\\(?:{0}))\\' -f (($TrashFolderNames | ForEach-Object { [regex]::Escape($_) }) -join '|')

$SourceRoot = Get-FullPath $SourceRoot
$QuarantineRoot = if (Test-Path -LiteralPath $QuarantineRoot) { Get-FullPath $QuarantineRoot } else { '' }
$Roots = @($Roots | Where-Object { Test-Path -LiteralPath $_ -PathType Container } | ForEach-Object { Get-FullPath $_ })

# ---------------------------------------------------------------- inventory
Write-Host 'Scanning...'
$trashFiles = [System.Collections.Generic.List[object]]::new()
$outsideBySize = [System.Collections.Generic.Dictionary[long, System.Collections.Generic.List[object]]]::new()

foreach ($root in (@($Roots) + @($SourceRoot, $QuarantineRoot) | Where-Object { $_ } | Select-Object -Unique)) {
    $isRoot = $Roots -contains $root
    $where = if ($root -eq $SourceRoot) { 'Library' } elseif ($root -eq $QuarantineRoot) { 'Quarantine' } else { 'Other' }

    foreach ($fi in (Get-AllFiles $root)) {
        $rel = $fi.FullName.Substring($root.Length + 1)

        if ($rel -match $trashPattern) {
            if ($isRoot) {
                $trashFiles.Add([pscustomobject]@{
                    Root = $root; RelativePath = $rel; FullName = $fi.FullName; Length = $fi.Length
                    Folder = $Matches[1]
                    IsMedia = $MediaExtensions -contains $fi.Extension.ToLowerInvariant()
                })
            }
        }
        elseif ($where -ne 'Other') {
            if (-not $outsideBySize.ContainsKey($fi.Length)) {
                $outsideBySize[$fi.Length] = [System.Collections.Generic.List[object]]::new()
            }
            $outsideBySize[$fi.Length].Add([pscustomobject]@{ FullName = $fi.FullName; Where = $where })
        }
    }
}

if ($trashFiles.Count -eq 0) {
    Write-Host 'No Trash/Bin folders with files found. Nothing to do.'
    return
}

# ------------------------------------------------------ content classification
Write-Host ('Checking {0:n0} media files for copies outside Trash/Bin...' -f @($trashFiles | Where-Object IsMedia).Count)
$plan = foreach ($t in $trashFiles) {
    $status = 'NotMedia'
    $otherCopy = ''

    if ($t.IsMedia) {
        $status = 'TrashOnly'
        $cands = $null

        if ($t.Length -gt 0 -and $outsideBySize.TryGetValue($t.Length, [ref]$cands)) {
            $h = Get-Sha256 $t.FullName
            $quaCopy = ''

            foreach ($c in ($cands | Sort-Object { $_.Where -ne 'Library' })) {
                if ((Get-Sha256 $c.FullName) -eq $h) {
                    if ($c.Where -eq 'Library') { $status = 'InLibrary'; $otherCopy = $c.FullName; break }
                    if (-not $quaCopy) { $quaCopy = $c.FullName }
                }
            }

            if ($status -ne 'InLibrary' -and $quaCopy) { $status = 'QuarantineOnly'; $otherCopy = $quaCopy }
        }
    }

    [pscustomobject]@{
        Root = $t.Root; TrashFolder = $t.Folder; RelativePath = $t.RelativePath; SourcePath = $t.FullName
        DestinationPath = [System.IO.Path]::Combine($HoldingRoot, (Split-Path -Leaf $t.Root), $t.RelativePath)
        SizeBytes = $t.Length; IsMedia = $t.IsMedia; Status = $status; OtherCopy = $otherCopy
    }
}

[void][System.IO.Directory]::CreateDirectory($ReportRoot)
$planCsv = Join-Path $ReportRoot "TrashPlan_${stamp}.csv"
$plan | Export-Csv -LiteralPath $planCsv -NoTypeInformation -Encoding UTF8 -WhatIf:$false

# ---------------------------------------------------------------- summary
$plan | Group-Object Root, TrashFolder | ForEach-Object {
    $g = $_.Group
    [pscustomobject]@{
        Folder         = [System.IO.Path]::Combine($g[0].Root, $g[0].TrashFolder)
        Files          = $g.Count
        MB             = [math]::Round(($g | Measure-Object SizeBytes -Sum).Sum / 1MB, 1)
        Media          = @($g | Where-Object IsMedia).Count
        InLibrary      = @($g | Where-Object Status -eq 'InLibrary').Count
        QuarantineOnly = @($g | Where-Object Status -eq 'QuarantineOnly').Count
        TrashOnly      = @($g | Where-Object Status -eq 'TrashOnly').Count
    }
} | Format-Table -AutoSize | Out-String -Width 250 | Write-Host

$totalMB = [math]::Round(($plan | Measure-Object SizeBytes -Sum).Sum / 1MB, 1)
$quarantineOnly = @($plan | Where-Object Status -eq 'QuarantineOnly')
Write-Host ('Total: {0:n0} files, {1:n1} MB in {2} Trash/Bin folders.' -f $plan.Count, $totalMB, @($plan | Select-Object Root, TrashFolder -Unique).Count)
Write-Host ('Media: {0} with a copy in the library, {1} only in quarantine outside Trash, {2} with no copy outside Trash.' -f `
    @($plan | Where-Object Status -eq 'InLibrary').Count, $quarantineOnly.Count, @($plan | Where-Object Status -eq 'TrashOnly').Count)
Write-Host "Plan: $planCsv"

if (-not $Execute) {
    Write-Host 'Plan only - nothing was moved. Re-run with -Execute to move these files to the holding folder.'
    return
}

if ($ExpectedFileCount -ge 0 -and $plan.Count -ne $ExpectedFileCount) {
    throw ('The plan now has {0} files, not the {1} expected. Nothing was moved; review the new plan first.' -f $plan.Count, $ExpectedFileCount)
}

if ($quarantineOnly.Count -gt 0 -and -not $AllowQuarantineOnly) {
    throw ('{0} Trash/Bin media files have their only non-Trash copy in quarantine (see Status = QuarantineOnly in the plan). Restore those album copies first, or re-run with -AllowQuarantineOnly if they are trash too.' -f $quarantineOnly.Count)
}

# ---------------------------------------------------------------- execute
$actions = [System.Collections.Generic.List[object]]::new()
$moved = 0; $failed = 0
$i = 0

foreach ($p in $plan) {
    $i++
    Write-Progress -Activity 'Moving Trash/Bin files to holding' -Status ('{0} / {1}' -f $i, $plan.Count) -PercentComplete ([int](100.0 * $i / $plan.Count))

    if (-not $PSCmdlet.ShouldProcess($p.SourcePath, "move -> $($p.DestinationPath)")) { continue }

    try {
        if (Test-Path -LiteralPath $p.DestinationPath) { throw 'Destination already exists; not overwriting.' }

        $hash = Get-Sha256 $p.SourcePath
        [void][System.IO.Directory]::CreateDirectory((Split-Path -Parent $p.DestinationPath))
        [System.IO.File]::Move($p.SourcePath, $p.DestinationPath, $false)
        $hashCache.Remove($p.SourcePath) | Out-Null

        if ((Test-Path -LiteralPath $p.SourcePath) -or
            (Get-Item -LiteralPath $p.DestinationPath).Length -ne $p.SizeBytes -or
            (Get-Sha256 $p.DestinationPath) -ne $hash) {
            throw 'Post-move verification failed.'
        }

        $moved++
        $actions.Add([pscustomobject][ordered]@{
            TimeStamp       = Get-Date -Format 'yyyy-MM-dd HH:mm:ss.fff'
            Action          = $(if ($p.SourcePath -match '\.json$') { 'MovedMetadata' } else { 'MovedMedia' })
            SourcePath      = $p.SourcePath
            DestinationPath = $p.DestinationPath
            SizeBytes       = $p.SizeBytes
            Hash            = $hash
            Detail          = 'Trash/Bin folder moved to holding (' + $p.Status + ')'
        })
    }
    catch {
        $failed++
        Write-Host ('FAILED: {0} :: {1}' -f $p.SourcePath, $_.Exception.Message) -ForegroundColor Red
        $actions.Add([pscustomobject][ordered]@{
            TimeStamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss.fff'; Action = 'Failed'
            SourcePath = $p.SourcePath; DestinationPath = $p.DestinationPath; SizeBytes = $p.SizeBytes; Hash = ''
            Detail = $_.Exception.Message
        })
    }
}
Write-Progress -Activity 'Moving Trash/Bin files to holding' -Completed

if ($actions.Count -gt 0) {
    $manifest = Join-Path $ReportRoot "TrashHolding_${stamp}_Actions.csv"
    $actions | Export-Csv -LiteralPath $manifest -NoTypeInformation -Encoding UTF8 -WhatIf:$false
    Write-Host "Manifest (rollback record for Restore-Quarantine.ps1): $manifest"
}

# Remove the now-empty Trash/Bin folders (only if completely empty).
foreach ($dir in ($plan | ForEach-Object { [System.IO.Path]::Combine($_.Root, $_.TrashFolder) } | Select-Object -Unique)) {
    if ((Test-Path -LiteralPath $dir) -and -not (Get-ChildItem -LiteralPath $dir -Recurse -File -Force | Select-Object -First 1)) {
        if ($PSCmdlet.ShouldProcess($dir, 'remove empty folder')) { Remove-Item -LiteralPath $dir -Recurse -Force }
    }
}

Write-Host ('Done: {0} moved, {1} failed. Nothing was deleted; files are in {2}.' -f $moved, $failed, $HoldingRoot) -ForegroundColor $(if ($failed) { 'Yellow' } else { 'Green' })
