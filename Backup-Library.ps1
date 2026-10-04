#Requires -Version 7.0
<#
.SYNOPSIS
Copies the photo library to a backup drive and verifies every file by SHA-256.

.DESCRIPTION
1. Copies new and changed files from -Source to -Destination with robocopy
   (restartable, timestamps kept). Files that exist only on the backup are left alone,
   so the backup never loses anything because of a mistake on this computer.
2. Verifies: every source file must exist on the backup with the same size and the
   same SHA-256 hash. Results go to a CSV in -ReportRoot.

-Mirror additionally deletes backup files that no longer exist in the source (robocopy
/MIR). Use it only after checking the plan it prints; it asks for confirmation.

.EXAMPLE
    .\Backup-Library.ps1                         # copy + verify
    .\Backup-Library.ps1 -VerifyOnly             # re-check an existing backup
    .\Backup-Library.ps1 -Mirror                 # also remove files deleted from the library
#>
[CmdletBinding()]
param(
    [string]$Source = 'C:\Media\Library',
    [string]$Destination = 'E:\Photos Library',
    [string]$ReportRoot = 'C:\Media\DedupeReports',
    [switch]$VerifyOnly,
    [switch]$Mirror,
    [int]$Threads = 4
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'

if (-not (Test-Path -LiteralPath $Source -PathType Container)) { throw "Source not found: $Source" }
$Source = (Get-Item -LiteralPath $Source).FullName.TrimEnd('\')
$Destination = $Destination.TrimEnd('\')
$srcFull = [System.IO.Path]::GetFullPath($Source) + '\'
$dstFull = [System.IO.Path]::GetFullPath($Destination) + '\'
if ($dstFull.StartsWith($srcFull, 'OrdinalIgnoreCase') -or $srcFull.StartsWith($dstFull, 'OrdinalIgnoreCase')) {
    throw 'Source and destination must not be inside each other.'
}
[void][System.IO.Directory]::CreateDirectory($ReportRoot)

$srcFiles = @(Get-ChildItem -LiteralPath $Source -Recurse -File -Force)
$srcBytes = ($srcFiles | Measure-Object Length -Sum).Sum
Write-Host ('Source: {0:n0} files, {1:n1} GB in {2}' -f $srcFiles.Count, ($srcBytes / 1GB), $Source)

if (-not $VerifyOnly) {
    $drive = [System.IO.Path]::GetPathRoot($dstFull)
    if (-not (Test-Path -LiteralPath $drive)) { throw "Backup drive $drive is not connected." }
    $free = (Get-PSDrive -Name $drive.Substring(0, 1)).Free
    if ($free -lt $srcBytes * 1.05) { throw ('Not enough space on {0}: need ~{1:n1} GB, free {2:n1} GB.' -f $drive, ($srcBytes / 1GB), ($free / 1GB)) }

    $log = Join-Path $ReportRoot "Backup_${stamp}_robocopy.log"
    $opts = @('/E', '/COPY:DAT', '/DCOPY:DAT', '/R:2', '/W:5', '/XJ', '/MT:8', '/NP', '/NFL', '/NDL', "/UNILOG:$log")
    if ($Mirror) {
        $extra = @(robocopy $Source $Destination /L /MIR /NJH /NJS /NP /NDL /FP /XJ | Where-Object { $_ -match '\*EXTRA File' })
        Write-Host ("-Mirror would delete {0} file(s) from the backup that are no longer in the library:" -f $extra.Count) -ForegroundColor Yellow
        $extra | Select-Object -First 20 | ForEach-Object { Write-Host "   $($_.Trim())" }
        if ($extra.Count -gt 0 -and (Read-Host 'Type DELETE FROM BACKUP to continue') -cne 'DELETE FROM BACKUP') { throw 'Mirror cancelled; nothing was copied or deleted.' }
        $opts = @('/MIR') + $opts
    }
    Write-Host "Copying to $Destination (log: $log) ..."
    robocopy $Source $Destination @opts | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "robocopy reported failures (exit code $LASTEXITCODE). See $log" }
}

Write-Host 'Verifying every file by SHA-256 ...'
$results = $srcFiles | ForEach-Object -ThrottleLimit $Threads -Parallel {
    $rel = $_.FullName.Substring($using:Source.Length + 1)
    $dst = Join-Path $using:Destination $rel
    $status = 'OK'
    if (-not (Test-Path -LiteralPath $dst -PathType Leaf)) { $status = 'MissingOnBackup' }
    elseif ((Get-Item -LiteralPath $dst).Length -ne $_.Length) { $status = 'SizeDiffers' }
    elseif ((Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash -ne (Get-FileHash -LiteralPath $dst -Algorithm SHA256).Hash) { $status = 'HashDiffers' }
    [pscustomobject]@{ RelativePath = $rel; SizeBytes = $_.Length; Status = $status }
}

$report = Join-Path $ReportRoot "Backup_${stamp}_Verify.csv"
$results | Export-Csv -LiteralPath $report -NoTypeInformation -Encoding UTF8
$bad = @($results | Where-Object Status -ne 'OK')
$onlyOnBackup = @(Get-ChildItem -LiteralPath $Destination -Recurse -File -Force -ErrorAction SilentlyContinue).Count - ($results.Count - @($results | Where-Object Status -eq 'MissingOnBackup').Count)

Write-Host ('Verified: {0:n0} identical, {1:n0} problem(s). Files only on the backup: {2:n0}.' -f ($results.Count - $bad.Count), $bad.Count, [Math]::Max(0, $onlyOnBackup)) -ForegroundColor $(if ($bad.Count) { 'Red' } else { 'Green' })
$bad | Group-Object Status | ForEach-Object { Write-Host ('  {0}: {1}' -f $_.Name, $_.Count) -ForegroundColor Red }
Write-Host "Report: $report"
exit $(if ($bad.Count) { 1 } else { 0 })  # not robocopy's informational exit code
