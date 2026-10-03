#Requires -Version 7.0
<#
.SYNOPSIS
Moves the images you marked "remove" in the triage review page to a holding folder.

.DESCRIPTION
Reads the TriageDecisions_<stamp>.csv exported from the review page and plans a move
for every row with Decision = reject. Default is a read-only plan.

With -Execute, each file is checked (still under SourceRoot, same size as when it was
classified), moved to HoldingRoot\<path relative to SourceRoot>, verified by SHA-256,
and never overwrites anything. An Actions-style manifest is written that
Restore-Quarantine.ps1 can reverse. Metadata JSON sidecars stay where they are.

For a WhatsApp copy, the move is refused if the photo it copies (CopyOf) is missing
or is itself marked for removal, so the better-quality photo is never lost with it.

Nothing is deleted. Empty HoldingRoot yourself once you are satisfied.

.EXAMPLE
    .\Move-TriageRejects.ps1 -DecisionsCsv "$HOME\Downloads\TriageDecisions_20261004_101500.csv"
.EXAMPLE
    .\Move-TriageRejects.ps1 -DecisionsCsv ... -Execute -ExpectedFileCount 5120
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [Parameter(Mandatory = $true)][string]$DecisionsCsv,
    [string]$SourceRoot = 'C:\Media\Imports',
    [string]$HoldingRoot = 'C:\Media\RejectQuarantine',
    [string]$ReportRoot = 'C:\Media\DedupeReports',
    [switch]$Execute,
    [int]$ExpectedFileCount = -1
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$SourceRoot = (Get-Item -LiteralPath $SourceRoot -Force).FullName.TrimEnd('\')
$ci = [System.StringComparer]::OrdinalIgnoreCase

function Get-Sha256([string]$Path) {
    $fs = [System.IO.File]::Open($Path, 'Open', 'Read', 'Read')
    try {
        $sha = [System.Security.Cryptography.SHA256]::Create()
        try { return [System.Convert]::ToHexString($sha.ComputeHash($fs)) } finally { $sha.Dispose() }
    }
    finally { $fs.Dispose() }
}

$all = @(Import-Csv -LiteralPath $DecisionsCsv)
foreach ($col in 'Path', 'Decision', 'SizeBytes') {
    if ($all.Count -and -not ($all[0].PSObject.Properties.Name -contains $col)) { throw "Decisions CSV has no '$col' column: $DecisionsCsv" }
}

$rejectSet = [System.Collections.Generic.HashSet[string]]::new($ci)
foreach ($r in $all) { if ($r.Decision -eq 'reject') { [void]$rejectSet.Add($r.Path) } }

$plan = foreach ($r in $all | Where-Object Decision -eq 'reject') {
    $status = 'Ready'
    $detail = ''
    $copyOf = if ($r.PSObject.Properties.Name -contains 'CopyOf') { $r.CopyOf } else { '' }

    if (-not $r.Path.StartsWith($SourceRoot + '\', [System.StringComparison]::OrdinalIgnoreCase)) {
        $status = 'Blocked'; $detail = 'Not under SourceRoot'
    }
    elseif (-not (Test-Path -LiteralPath $r.Path -PathType Leaf)) {
        $status = 'Missing'; $detail = 'File no longer exists'
    }
    elseif ((Get-Item -LiteralPath $r.Path -Force).Length -ne [long]$r.SizeBytes) {
        $status = 'Blocked'; $detail = 'File changed since it was classified'
    }
    elseif ($copyOf -and -not (Test-Path -LiteralPath $copyOf -PathType Leaf)) {
        $status = 'Blocked'; $detail = 'The better-quality photo it copies no longer exists'
    }
    elseif ($copyOf -and $rejectSet.Contains($copyOf)) {
        $status = 'Blocked'; $detail = 'The better-quality photo it copies is also marked for removal'
    }

    [pscustomobject]@{
        Status          = $status
        SourcePath      = $r.Path
        DestinationPath = Join-Path $HoldingRoot $r.Path.Substring($SourceRoot.Length + 1)
        SizeBytes       = [long]$r.SizeBytes
        Category        = if ($r.PSObject.Properties.Name -contains 'Category') { $r.Category } else { '' }
        Detail          = $detail
    }
}
$plan = @($plan)

[void][System.IO.Directory]::CreateDirectory($ReportRoot)
$planCsv = Join-Path $ReportRoot "TriageRejectPlan_${stamp}.csv"
$plan | Export-Csv -LiteralPath $planCsv -NoTypeInformation -Encoding UTF8 -WhatIf:$false

$ready = @($plan | Where-Object Status -eq 'Ready')
$plan | Group-Object Category | Sort-Object Count -Descending | ForEach-Object {
    [pscustomobject]@{
        Category = $_.Name; Files = $_.Count
        MB       = [math]::Round(($_.Group | Measure-Object SizeBytes -Sum).Sum / 1MB, 1)
        Ready    = @($_.Group | Where-Object Status -eq 'Ready').Count
    }
} | Format-Table -AutoSize | Out-String | Write-Host

Write-Host ('{0:n0} of {1:n0} images are marked for removal; {2:n0} ready to move ({3:n1} MB).' -f `
    $plan.Count, $all.Count, $ready.Count, (($ready | Measure-Object SizeBytes -Sum).Sum / 1MB))
foreach ($g in $plan | Where-Object Status -ne 'Ready' | Group-Object Detail) { Write-Host ('  {0}: {1}' -f $g.Name, $g.Count) -ForegroundColor Yellow }
Write-Host "Plan: $planCsv"

if (-not $Execute) {
    Write-Host 'Plan only - nothing was moved. Re-run with -Execute to move the Ready files.'
    return
}

if ($ExpectedFileCount -ge 0 -and $ready.Count -ne $ExpectedFileCount) {
    throw ('{0} files are ready to move, not the {1} expected. Nothing was moved; review the plan first.' -f $ready.Count, $ExpectedFileCount)
}

$actions = [System.Collections.Generic.List[object]]::new()
$moved = 0; $failed = 0; $i = 0

foreach ($p in $ready) {
    $i++
    Write-Progress -Activity 'Moving rejected images to holding' -Status ('{0} / {1}' -f $i, $ready.Count) -PercentComplete ([int](100.0 * $i / $ready.Count))
    if (-not $PSCmdlet.ShouldProcess($p.SourcePath, "move -> $($p.DestinationPath)")) { continue }

    try {
        if (Test-Path -LiteralPath $p.DestinationPath) { throw 'Destination already exists; not overwriting.' }
        $hash = Get-Sha256 $p.SourcePath
        [void][System.IO.Directory]::CreateDirectory((Split-Path -Parent $p.DestinationPath))
        [System.IO.File]::Move($p.SourcePath, $p.DestinationPath, $false)

        if ((Test-Path -LiteralPath $p.SourcePath) -or (Get-Sha256 $p.DestinationPath) -ne $hash) {
            throw 'Post-move verification failed.'
        }

        $moved++
        $actions.Add([pscustomobject][ordered]@{
            TimeStamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss.fff'; Action = 'MovedMedia'
            SourcePath = $p.SourcePath; DestinationPath = $p.DestinationPath; SizeBytes = $p.SizeBytes; Hash = $hash
            Detail = 'Triage reject (' + $p.Category + ')'
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
Write-Progress -Activity 'Moving rejected images to holding' -Completed

if ($actions.Count -gt 0) {
    $manifest = Join-Path $ReportRoot "TriageRejects_${stamp}_Actions.csv"
    $actions | Export-Csv -LiteralPath $manifest -NoTypeInformation -Encoding UTF8 -WhatIf:$false
    Write-Host "Manifest (rollback record for Restore-Quarantine.ps1): $manifest"
}

Write-Host ('Done: {0} moved, {1} failed. Nothing was deleted; files are in {2}.' -f $moved, $failed, $HoldingRoot) -ForegroundColor $(if ($failed) { 'Yellow' } else { 'Green' })
