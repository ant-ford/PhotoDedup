#Requires -Version 7.0
<#
.SYNOPSIS
Fixture tests for Invoke-TakeoutDedupe.ps1 and Restore-Quarantine.ps1.

.DESCRIPTION
Builds a small synthetic Takeout layout (random bytes, no real photos) in a temporary
folder, runs the scripts against it and checks the results. Nothing outside the
temporary folder is read or written. Exit code 0 = all passed.

.EXAMPLE
    pwsh -File .\Tests\Invoke-TakeoutDedupe.Tests.ps1
    pwsh -File .\Tests\Invoke-TakeoutDedupe.Tests.ps1 -KeepFixture
#>
param(
    [string]$FixtureRoot = (Join-Path ([System.IO.Path]::GetTempPath()) ('TakeoutDedupeTest_' + [guid]::NewGuid().ToString('N').Substring(0, 8))),
    [switch]$KeepFixture
)

$ErrorActionPreference = 'Stop'
$ToolsRoot = Split-Path -Parent $PSScriptRoot

# Use the long form of the path (TEMP is often an 8.3 short name), as the scripts report it.
[void][System.IO.Directory]::CreateDirectory($FixtureRoot)
$FixtureRoot = (Get-Item -LiteralPath $FixtureRoot).FullName
$Dedupe = Join-Path $ToolsRoot 'Invoke-TakeoutDedupe.ps1'
$Restore = Join-Path $ToolsRoot 'Restore-Quarantine.ps1'

$script:Failures = 0
$script:Passes = 0

function Assert-Equal($Actual, $Expected, [string]$Message) {
    if ("$Actual" -ceq "$Expected") {
        $script:Passes++
        Write-Host "  PASS  $Message" -ForegroundColor Green
    }
    else {
        $script:Failures++
        Write-Host "  FAIL  $Message (expected '$Expected', got '$Actual')" -ForegroundColor Red
    }
}

function Assert-True([bool]$Condition, [string]$Message) { Assert-Equal $Condition $true $Message }

function New-Blob([int]$Seed, [int]$Size = 4096) {
    $bytes = [byte[]]::new($Size)
    [System.Random]::new($Seed).NextBytes($bytes)
    return , $bytes
}

function New-FixtureFile([string]$Rel, [byte[]]$Bytes) {
    $path = Join-Path $script:Src $Rel
    [void][System.IO.Directory]::CreateDirectory((Split-Path -Parent $path))
    [System.IO.File]::WriteAllBytes($path, $Bytes)
}

function New-Sidecar([string]$Rel) {
    New-FixtureFile $Rel ([System.Text.Encoding]::UTF8.GetBytes('{"title":"' + (Split-Path -Leaf $Rel) + '"}'))
}

function Initialize-Fixture {
    if (Test-Path -LiteralPath $FixtureRoot) { Remove-Item -LiteralPath $FixtureRoot -Recurse -Force }
    $script:Src = Join-Path $FixtureRoot 'Imports'
    $script:Qua = Join-Path $FixtureRoot 'Quarantine'
    $script:Rep = Join-Path $FixtureRoot 'Reports'
    [void][System.IO.Directory]::CreateDirectory($script:Src)

    $gp = 'Takeout\Google Photos'

    # 1. Cross-part metadata: media in partA, its sidecar only in partB; a bare copy in WhatsApp.
    New-FixtureFile "partA\$gp\Album\cross.jpg" (New-Blob 1)
    New-Sidecar     "partB\$gp\Album\cross.jpg.supplemental-metadata.json"
    New-FixtureFile 'WhatsApp-001\IMG-20240101-WA0001.jpg' (New-Blob 1)

    # 2. Numbered name: IMG(1).jpg pairs with IMG.jpg.supplemental-metadata(1).json.
    New-FixtureFile "partA\$gp\Album\IMG(1).jpg" (New-Blob 2)
    New-Sidecar     "partA\$gp\Album\IMG.jpg.supplemental-metadata(1).json"
    New-FixtureFile 'WhatsApp-001\IMG-20240101-WA0002.jpg' (New-Blob 2)

    # 3. Edited copy uses the original's sidecar, which is shared and must never move with it.
    New-FixtureFile "partA\$gp\Album\pic.jpg" (New-Blob 30)
    New-FixtureFile "partA\$gp\Album\pic-edited.jpg" (New-Blob 3)
    New-Sidecar     "partA\$gp\Album\pic.jpg.supplemental-metadata.json"
    New-FixtureFile "partB\$gp\Other\pic-edited.jpg" (New-Blob 3)

    # 4. Trash copy has metadata, album copy has none: album copy must still be the keeper.
    New-FixtureFile "partA\$gp\Trash\trashed.jpg" (New-Blob 4)
    New-Sidecar     "partA\$gp\Trash\trashed.jpg.supplemental-metadata.json"
    New-FixtureFile "partB\$gp\Album\trashed.jpg" (New-Blob 4)

    # 4b. Same with a "Bin" folder (Google's name in some account languages).
    New-FixtureFile "partA\$gp\Bin\binned.jpg" (New-Blob 5)
    New-Sidecar     "partA\$gp\Bin\binned.jpg.supplemental-metadata.json"
    New-FixtureFile "partB\$gp\Album\binned.jpg" (New-Blob 5)

    # 5. Same-folder sidecar beats other-part sidecar when otherwise equal.
    New-FixtureFile "partA\$gp\Album\near.jpg" (New-Blob 6)
    New-Sidecar     "partA\$gp\Album\near.jpg.supplemental-metadata.json"
    New-FixtureFile "partB\$gp\Album2\near.jpg" (New-Blob 6)
    New-Sidecar     "partC\$gp\Album2\near.jpg.supplemental-metadata.json"

    # 6. Loser with its own sidecar (used by nobody else): moves with -MoveMetadataWithMedia only.
    New-FixtureFile "partA\$gp\own.jpg" (New-Blob 7)
    New-Sidecar     "partA\$gp\own.jpg.supplemental-metadata.json"
    New-FixtureFile "partB\$gp\Deep\own.jpg" (New-Blob 7)
    New-Sidecar     "partB\$gp\Deep\own.jpg.supplemental-metadata.json"

    # 7. Loser's sidecar is also the metadata of a different, unique file in another part: never moved.
    New-FixtureFile "partA\$gp\shared.jpg" (New-Blob 8)
    New-Sidecar     "partA\$gp\shared.jpg.supplemental-metadata.json"
    New-FixtureFile "partB\$gp\Deep\shared.jpg" (New-Blob 8)
    New-Sidecar     "partB\$gp\Deep\shared.jpg.supplemental-metadata.json"
    New-FixtureFile "partC\$gp\Deep\shared.jpg" (New-Blob 80)

    # 8. Orphaned sidecar: no media anywhere.
    New-Sidecar     "partC\$gp\Album\gone.jpg.supplemental-metadata.json"
}

function Invoke-Dedupe([string]$Mode, [hashtable]$Extra = @{}) {
    # Run artifacts are named by the second; make sure each run gets its own.
    Start-Sleep -Milliseconds 1100
    $before = @(Get-ChildItem -LiteralPath $script:Rep -Filter '*_Report.csv' -ErrorAction SilentlyContinue).Count
    & $Dedupe -SourceRoot $script:Src -QuarantineRoot $script:Qua -ReportRoot $script:Rep -Mode $Mode @Extra 6>$null | Out-Null
    $reports = @(Get-ChildItem -LiteralPath $script:Rep -Filter '*_Report.csv' | Sort-Object Name)
    if ($reports.Count -le $before) { throw 'No new report CSV was written.' }
    $latest = $reports[-1]
    $stamp = $latest.Name -replace '_Report\.csv$', ''
    return [pscustomobject]@{
        Rows    = @(Import-Csv -LiteralPath $latest.FullName)
        Actions = Join-Path $script:Rep "${stamp}_Actions.csv"
        Log     = Get-Content -LiteralPath (Join-Path $script:Rep "$stamp.log") -Raw
    }
}

function Get-Row($Rows, [string]$Rel) {
    $full = Join-Path $script:Src $Rel
    return $Rows | Where-Object { $_.FilePath -eq $full } | Select-Object -First 1
}

function Test-Exists([string]$Root, [string]$Rel) { Test-Path -LiteralPath (Join-Path $Root $Rel) -PathType Leaf }

$gp = 'Takeout\Google Photos'

try {
    # ------------------------------------------------------------------ Report
    Write-Host 'Report mode: matching and keeper choice' -ForegroundColor Cyan
    Initialize-Fixture
    $run = Invoke-Dedupe 'Report'
    $rows = $run.Rows

    $r = Get-Row $rows "partA\$gp\Album\cross.jpg"
    Assert-Equal $r.Recommendation 'KEEP' 'cross-part: media with sidecar in another part is the keeper'
    Assert-Equal $r.MetadataLocation 'OtherPart' 'cross-part: MetadataLocation = OtherPart'
    Assert-Equal (Get-Row $rows 'WhatsApp-001\IMG-20240101-WA0001.jpg').Recommendation 'DELETE' 'cross-part: WhatsApp copy loses'

    $r = Get-Row $rows "partA\$gp\Album\IMG(1).jpg"
    Assert-Equal $r.Recommendation 'KEEP' 'numbered: IMG(1).jpg is the keeper'
    Assert-Equal $r.MetadataMatch 'Numbered' 'numbered: MetadataMatch = Numbered'
    Assert-Equal $r.MetadataLocation 'SameFolder' 'numbered: MetadataLocation = SameFolder'

    $r = Get-Row $rows "partA\$gp\Album\pic-edited.jpg"
    Assert-Equal $r.Recommendation 'KEEP' 'edited: copy beside the original''s sidecar is the keeper'
    Assert-Equal $r.MetadataMatch 'EditedOriginal' 'edited: MetadataMatch = EditedOriginal'

    $r = Get-Row $rows "partB\$gp\Album\trashed.jpg"
    Assert-Equal $r.Recommendation 'KEEP' 'trash: album copy without metadata beats Trash copy with metadata'
    $r = Get-Row $rows "partA\$gp\Trash\trashed.jpg"
    Assert-Equal $r.Recommendation 'DELETE' 'trash: Trash copy loses'
    Assert-Equal $r.InTrash 'True' 'trash: InTrash = True'
    Assert-Equal (Get-Row $rows "partB\$gp\Album\binned.jpg").Recommendation 'KEEP' 'bin: album copy beats Bin copy'

    Assert-Equal (Get-Row $rows "partA\$gp\Album\near.jpg").Recommendation 'KEEP' 'location: same-folder sidecar beats other-part sidecar'
    Assert-Equal (Get-Row $rows "partB\$gp\Album2\near.jpg").MetadataLocation 'OtherPart' 'location: loser shows OtherPart'

    Assert-Equal (Get-Row $rows "partA\$gp\own.jpg").Recommendation 'KEEP' 'own: shallower copy is the keeper'
    Assert-True ($run.Log -match '1 supplemental-metadata JSON files match no media') 'orphan: exactly 1 orphaned sidecar reported'
    Assert-True ($run.Log -match 'REPORT-ONLY MODE COMPLETE') 'report: completes without moving files'
    Assert-True (-not (Test-Path -LiteralPath $script:Qua)) 'report: no quarantine folder created'

    # ------------------------------------------------------- Quarantine default
    Write-Host 'Quarantine (default): JSON stays in place' -ForegroundColor Cyan
    $run = Invoke-Dedupe 'Quarantine'
    $actions = @(Import-Csv -LiteralPath $run.Actions)

    Assert-Equal @($actions | Where-Object Action -eq 'MovedMetadata').Count 0 'default: no MovedMetadata rows'
    Assert-Equal @($actions | Where-Object Action -eq 'MovedMedia').Count 8 'default: 8 losing media moved'
    Assert-True (Test-Exists $script:Qua "partB\$gp\Deep\own.jpg") 'default: loser media is in quarantine'
    Assert-True (Test-Exists $script:Src "partB\$gp\Deep\own.jpg.supplemental-metadata.json") 'default: loser sidecar stays in Imports'
    Assert-True (Test-Exists $script:Src "partA\$gp\Trash\trashed.jpg.supplemental-metadata.json") 'default: Trash sidecar stays in Imports'
    Assert-True (Test-Exists $script:Src "partA\$gp\Album\cross.jpg") 'default: keeper untouched'

    & $Restore -ManifestPath $run.Actions 6>$null | Out-Null
    Assert-True (Test-Exists $script:Src "partB\$gp\Deep\own.jpg") 'restore: loser media is back'
    Assert-Equal @(Get-ChildItem -LiteralPath $script:Qua -Recurse -File).Count 0 'restore: quarantine is empty again'

    # --------------------------------------------- Quarantine -MoveMetadataWithMedia
    Write-Host 'Quarantine -MoveMetadataWithMedia: own sidecars move, shared ones stay' -ForegroundColor Cyan
    Initialize-Fixture
    $run = Invoke-Dedupe 'Quarantine' @{ MoveMetadataWithMedia = $true }
    $actions = @(Import-Csv -LiteralPath $run.Actions)

    Assert-True (Test-Exists $script:Qua "partB\$gp\Deep\own.jpg.supplemental-metadata.json") 'move: unshared own sidecar moved with its media'
    Assert-True (-not (Test-Exists $script:Src "partB\$gp\Deep\own.jpg.supplemental-metadata.json")) 'move: unshared own sidecar gone from Imports'
    Assert-True (Test-Exists $script:Src "partB\$gp\Deep\shared.jpg.supplemental-metadata.json") 'move: sidecar also used by a remaining file stays'
    Assert-True (Test-Exists $script:Qua "partB\$gp\Deep\shared.jpg") 'move: ...while its media is still quarantined'
    Assert-True (Test-Exists $script:Src "partA\$gp\Album\pic.jpg.supplemental-metadata.json") 'move: edited original''s sidecar never moves'
    Assert-True ($run.Log -match 'METADATA-KEPT') 'move: kept sidecar is logged'

    $movedMeta = @($actions | Where-Object Action -eq 'MovedMetadata').Count
    & $Restore -ManifestPath $run.Actions 6>$null | Out-Null
    Assert-True ($movedMeta -ge 1) 'move: MovedMetadata rows written'
    Assert-True (Test-Exists $script:Src "partB\$gp\Deep\own.jpg.supplemental-metadata.json") 'restore: moved sidecar is back'
    Assert-Equal @(Get-ChildItem -LiteralPath $script:Qua -Recurse -File).Count 0 'restore: quarantine is empty again'

    # ------------------------------------------------------------- Safety: WhatIf
    Write-Host 'Quarantine -WhatIf moves nothing' -ForegroundColor Cyan
    Initialize-Fixture
    $null = Invoke-Dedupe 'Quarantine' @{ WhatIf = $true }
    Assert-True (-not (Test-Path -LiteralPath $script:Qua) -or @(Get-ChildItem -LiteralPath $script:Qua -Recurse -File).Count -eq 0) 'whatif: nothing in quarantine'
    Assert-True (Test-Exists $script:Src 'WhatsApp-001\IMG-20240101-WA0001.jpg') 'whatif: loser still in Imports'
}
finally {
    if ($KeepFixture) { Write-Host "Fixture kept at $FixtureRoot" }
    elseif (Test-Path -LiteralPath $FixtureRoot) { Remove-Item -LiteralPath $FixtureRoot -Recurse -Force }
}

Write-Host ''
Write-Host ('{0} passed, {1} failed' -f $script:Passes, $script:Failures) -ForegroundColor $(if ($script:Failures) { 'Red' } else { 'Green' })
exit [int]($script:Failures -gt 0)
