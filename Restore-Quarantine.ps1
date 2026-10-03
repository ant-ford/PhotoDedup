#Requires -Version 5.1
<#
.SYNOPSIS
    Rolls back a quarantine run performed by Invoke-TakeoutDedupe.ps1 (v2).
.EXAMPLE
    .\Restore-Quarantine.ps1 -ManifestPath "C:\Media\DedupeReports\Dedupe_20260825_132500_Actions.csv"
.EXAMPLE
    .\Restore-Quarantine.ps1 -ManifestPath "...\Dedupe_20260825_132500_Actions.csv" -WhatIf
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [Parameter(Mandatory = $true)][string]$ManifestPath
)
 $ErrorActionPreference = 'Stop'

if (-not (Test-Path -LiteralPath $ManifestPath -PathType Leaf)) { throw "Manifest not found: $ManifestPath" }

 $rows = @(Import-Csv -LiteralPath $ManifestPath | Where-Object { $_.Action -in @('MovedMedia', 'MovedMetadata') })
if ($rows.Count -eq 0) { Write-Host 'No MovedMedia/MovedMetadata entries in this manifest - nothing to roll back.'; return }

[array]::Reverse($rows)   # undo the last action first

 $restored = 0; $skipped = 0; $failed = 0
 $i = 0
foreach ($row in $rows) {
    $i++
    Write-Progress -Activity 'Rolling back quarantine' -Status ('{0} / {1}' -f $i, $rows.Count) -PercentComplete ([int](100.0 * $i / $rows.Count))
    $src = $row.DestinationPath      # quarantine location
    $dst = $row.SourcePath           # original location

    if (-not (Test-Path -LiteralPath $src -PathType Leaf)) {
        Write-Host "SKIP (not found in quarantine): $src" -ForegroundColor Yellow; $skipped++; continue
    }
    if (Test-Path -LiteralPath $dst) {
        Write-Host "SKIP (original location already occupied - not overwriting): $dst" -ForegroundColor Yellow; $skipped++; continue
    }
    if ($PSCmdlet.ShouldProcess($src, "restore -> $dst")) {
        try {
            [void][System.IO.Directory]::CreateDirectory((Split-Path -Parent $dst))
            Move-Item -LiteralPath $src -Destination $dst -ErrorAction Stop
            if ((Test-Path -LiteralPath $dst) -and (-not (Test-Path -LiteralPath $src))) {
                $restored++; Write-Host "RESTORED: $dst" -ForegroundColor Green
            }
            else { $failed++; Write-Host "VERIFY FAILED: $src" -ForegroundColor Red }
        }
        catch { $failed++; Write-Host "FAILED: $src :: $($_.Exception.Message)" -ForegroundColor Red }
    }
}
Write-Progress -Activity 'Rolling back quarantine' -Completed
Write-Host ''
Write-Host "Rollback complete: $restored restored, $skipped skipped, $failed failed."
Write-Host 'Next step: re-run  .\Invoke-TakeoutDedupe.ps1 -Mode Report  and confirm the duplicate groups reappear.'