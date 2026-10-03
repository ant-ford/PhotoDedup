#Requires -Version 5.1
<#
.SYNOPSIS
v2.3 - Safely deduplicates Google Photos Takeout media exports using SHA-256 content hashing.

.DESCRIPTION
Duplicate identity = same byte size + same SHA-256 content hash. File names are NEVER
used to identify duplicates.

Keeper eligibility, applied in order:
    1. Copies outside Google Photos Trash/Bin folders beat copies inside them, so a
       keeper is never one that is due to be cleared out with the Trash.
    2. A copy with supplemental metadata is ALWAYS preferred: when any eligible copy
       has metadata, only metadata-bearing copies can be the keeper.

Supplemental metadata is found by name, then by location:
    Names     <name.ext>.supplemental-metadata.json
              <name>(N).ext  ->  <name.ext>.supplemental-metadata(N).json
              <name>-edited.ext  ->  the original's sidecar (shared, never moved)
    Location  SameFolder - next to the media file.
              OtherPart  - at the same Takeout-relative path in a different export
                           folder (normal for split Takeout zips), e.g.
                           Imports\<partA>\Takeout\Google Photos\X.jpg  with
                           Imports\<partB>\Takeout\Google Photos\X.jpg.supplemental-metadata.json

Modes:
    Report     (default) - CSV report only; nothing under SourceRoot is modified.

    Quarantine - moves losing media copies to QuarantineRoot, preserving the original
                 relative folder structure. Metadata JSON is LEFT IN PLACE by default
                 (it may describe albums, descriptions or people that the keeper's own
                 JSON lacks). -MoveMetadataWithMedia restores the v2.2 behaviour of
                 moving a loser's same-folder sidecar together with it as a pair.

                 Every file is re-verified immediately before its move:
                 exists + size + SHA-256. Any mismatch becomes ReviewRequired and the
                 file is not touched.

                 The keeper of each duplicate group is also re-verified (exists +
                 size + SHA-256) before any of its losers are touched. If the keeper
                 no longer matches its scan-time content, the whole group is left
                 untouched and marked ReviewRequired.

                 Quarantine uses best-effort pair-preserving moves with rollback.
                 It is not a database-style transaction. If rollback cannot safely
                 restore the original state, the affected pair is left intact and
                 marked for manual review.

                 Writes an Actions CSV that can be used by Restore-Quarantine.ps1
                 to roll back successful quarantine moves.

    Delete     - permanently removes losing media copies ONLY.
                 Requires -IAcceptPermanentDeletion plus typing DELETE DUPLICATES.

                 Companion metadata JSON files are NEVER deleted in any mode.
                 Delete mode may therefore leave orphaned supplemental-metadata JSON
                 files next to deleted media. It is a media-only deletion mode, not a
                 fully clean Takeout reconciliation mode.

Safety properties:
    - Stateless: every run re-enumerates, re-hashes and re-scores from disk.
      The report CSV is never consumed as an action list.

    - Deterministic: duplicate groups are processed deterministically, and keepers are
      chosen by explicit metadata eligibility, score, shortest full path, then lexical path.

    - Keeper integrity: a group's losers are only touched after the keeper has been
      re-verified against its scan-time size + SHA-256 (cached per group).

    - Never overwrites: quarantine destination collisions are resolved by
      verify-or-review, never by clobbering or auto-renaming. A pre-existing item of
      ANY kind (file or folder) at a destination path blocks the move.

    - No reparse traversal: junctions, directory symlinks and reparse-point files are
      skipped during enumeration via manual stack recursion. Under Windows PowerShell 5.1, 
      Get-ChildItem -Recurse follows junctions, which can cause infinite loops or make 
      one physical file appear under two paths.

    - Wildcard-safe moves: quarantine refuses to move any file whose source or
      destination path contains [ or ] (Move-Item treats these as wildcards in
      -Destination). Such files are flagged ReviewRequired instead.

    - Enumeration guard: Quarantine and Delete are blocked if SourceRoot enumeration
      was incomplete, unless -AllowIncompleteScan is supplied.

.PARAMETER SourceRoot
Root folder containing the extracted Takeout exports.
Default: C:\Media\Imports

.PARAMETER Mode
Report, Quarantine, or Delete.
Default: Report.

.PARAMETER QuarantineRoot
Destination root for Quarantine mode.
Default: C:\Media\DuplicateQuarantine.
Must not overlap SourceRoot.

.PARAMETER ReportRoot
Folder for run artifacts: report CSV, log, and Actions CSV.
Default: C:\Media\DedupeReports.
Must not overlap SourceRoot.

.PARAMETER MediaExtensions
Extensions treated as media.
Default: .jpg .jpeg .heic .png .mp4 .mov .avi

.PARAMETER MoveMetadataWithMedia
Quarantine only. Also moves each losing copy's own same-folder sidecar JSON with it, as
a verified pair (v2.2 behaviour). Off by default: JSON stays where it is.

.PARAMETER TrashFolderNames
Names of Google Photos deleted-items folders (directly under "Google Photos").
Copies inside them are never chosen as keeper while a copy exists elsewhere.
Default: Trash, Bin

.PARAMETER IAcceptPermanentDeletion
Required for Mode Delete. Still combined with an interactive typed confirmation phrase.

.PARAMETER AllowIncompleteScan
Allows Quarantine or Delete mode to continue even if enumeration errors occurred.
By default, Quarantine and Delete are blocked when enumeration is incomplete.
Report mode still runs but warns that the scan was incomplete.

.EXAMPLE
.\Invoke-TakeoutDedupe.ps1 -Mode Report

.EXAMPLE
.\Invoke-TakeoutDedupe.ps1 -Mode Quarantine -WhatIf

.EXAMPLE
.\Invoke-TakeoutDedupe.ps1 -Mode Quarantine

.EXAMPLE
.\Invoke-TakeoutDedupe.ps1 -Mode Delete -IAcceptPermanentDeletion

.NOTES
PowerShell 7+ recommended for long-path support and faster hashing.
Supports -WhatIf.

Quarantine is safest when SourceRoot and QuarantineRoot are on the same NTFS volume.
Cross-volume moves are effectively copy-then-delete and rely on the strict post-move
verification performed by this script.

In Delete mode an Actions CSV is still written; it is an audit record only -
DeletedMedia rows are permanent and cannot be restored by Restore-Quarantine.ps1.
#>

[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$SourceRoot = 'C:\Media\Imports',

    [ValidateSet('Report', 'Quarantine', 'Delete')]
    [string]$Mode = 'Report',

    [string]$QuarantineRoot = 'C:\Media\DuplicateQuarantine',
    [string]$ReportRoot = 'C:\Media\DedupeReports',

    [string[]]$MediaExtensions = @('.jpg', '.jpeg', '.heic', '.png', '.mp4', '.mov', '.avi'),

    [switch]$MoveMetadataWithMedia,
    [string[]]$TrashFolderNames = @('Trash', 'Bin'),

    [switch]$IAcceptPermanentDeletion,
    [switch]$AllowIncompleteScan
)

$ErrorActionPreference = 'Stop'
$ScriptVersion = 'v2.3'
$MetadataSuffix = '.supplemental-metadata.json'
$SidecarPattern = '\.supplemental-metadata(\(\d+\))?\.json$'
$DirectorySeparator = [System.IO.Path]::DirectorySeparatorChar

$TrashPattern = '(^|\\)Google Photos\\({0})\\' -f (($TrashFolderNames | ForEach-Object { [regex]::Escape($_) }) -join '|')

# Normalise media extensions once.
$MediaExtensions = @($MediaExtensions | ForEach-Object { $_.ToLowerInvariant() })

# =============================================================================
# FUNCTIONS
# =============================================================================

function Write-Log {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Message,
        [ValidateSet('INFO', 'WARN', 'ERROR', 'SUCCESS', 'ACTION', 'HEADER')][string]$Level = 'INFO'
    )

    $line = '[{0}] [{1,-7}] {2}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss.fff'), $Level, $Message

    if ($script:LogWriter) {
        $script:LogWriter.WriteLine($line)
    }

    switch ($Level) {
        'ERROR'   { Write-Host $line -ForegroundColor Red }
        'WARN'    { Write-Host $line -ForegroundColor Yellow }
        'SUCCESS' { Write-Host $line -ForegroundColor Green }
        'ACTION'  { Write-Host $line -ForegroundColor Cyan }
        'HEADER'  { Write-Host $line -ForegroundColor White }
        default   { Write-Host $line -ForegroundColor Gray }
    }
}

function Get-FileSha256 {
    <#
    Streams the file through a 1 MB BufferedStream.
    Memory use is constant regardless of file size.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$LiteralPath
    )

    $sha = [System.Security.Cryptography.SHA256]::Create()

    try {
        # FileShare.Read is stricter than ReadWrite. It prevents another process from
        # modifying the file while we are hashing it.
        $fs = [System.IO.File]::Open(
            $LiteralPath,
            [System.IO.FileMode]::Open,
            [System.IO.FileAccess]::Read,
            [System.IO.FileShare]::Read
        )

        try {
            $buffered = New-Object System.IO.BufferedStream($fs, 1048576)

            try {
                return ([System.BitConverter]::ToString($sha.ComputeHash($buffered))).Replace('-', '')
            }
            finally {
                $buffered.Dispose()
            }
        }
        finally {
            $fs.Dispose()
        }
    }
    finally {
        $sha.Dispose()
    }
}

function Test-FileMatchesExpected {
    <#
    Unified validation function.
    True only if:
        - file exists
        - file size matches expected size
        - SHA-256 matches expected hash
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$LiteralPath,
        [Parameter(Mandatory = $true)][long]$ExpectedLength,
        [Parameter(Mandatory = $true)][string]$ExpectedHash
    )

    if (-not (Test-Path -LiteralPath $LiteralPath -PathType Leaf)) {
        return $false
    }

    try {
        $item = Get-Item -LiteralPath $LiteralPath -Force

        if ($item.Length -ne $ExpectedLength) {
            return $false
        }

        return ((Get-FileSha256 -LiteralPath $LiteralPath) -eq $ExpectedHash)
    }
    catch {
        return $false
    }
}

function Test-PathsOverlap {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$PathA,
        [Parameter(Mandatory = $true)][string]$PathB
    )

    $sep = [System.IO.Path]::DirectorySeparatorChar

    $a = [System.IO.Path]::GetFullPath($PathA).TrimEnd($sep) + $sep
    $b = [System.IO.Path]::GetFullPath($PathB).TrimEnd($sep) + $sep

    return ($a.StartsWith($b, [System.StringComparison]::OrdinalIgnoreCase)) -or
           ($b.StartsWith($a, [System.StringComparison]::OrdinalIgnoreCase))
}

function Test-KeeperIntact {
    <#
    Returns $true only when the keeper of a duplicate group still exists and still 
    matches the size + SHA-256 recorded during this run's scan.
    Results are cached per keeper path, so each keeper is re-hashed at most once per run.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$KeeperPath
    )

    if ([string]::IsNullOrEmpty($KeeperPath)) { return $false }

    if ($null -eq $script:KeeperByPath -or
        $null -eq $script:KeeperVerified -or
        $null -eq $script:KeeperFailed) {
        return $false
    }

    if ($script:KeeperFailed.Contains($KeeperPath)) { return $false }
    if ($script:KeeperVerified.Contains($KeeperPath)) { return $true }

    $keeper = $script:KeeperByPath[$KeeperPath]

    if ($null -eq $keeper) {
        [void]$script:KeeperFailed.Add($KeeperPath)
        return $false
    }

    if (Test-FileMatchesExpected -LiteralPath $keeper.FullName -ExpectedLength $keeper.Length -ExpectedHash $keeper.Hash) {
        [void]$script:KeeperVerified.Add($KeeperPath)
        return $true
    }

    [void]$script:KeeperFailed.Add($KeeperPath)

    Write-Log (
        'KEEPER-CHANGED: keeper no longer matches its scan-time size/SHA-256; all of its duplicates will be left untouched: {0}' -f
        $KeeperPath
    ) ERROR

    return $false
}

function Get-PartRelativePath {
    <#
    Strips the export-part folder from a SourceRoot-relative path:
        'partA\Takeout\Google Photos\X.jpg' -> 'Takeout\Google Photos\X.jpg'
    Files directly in SourceRoot have no part folder and are returned unchanged.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$RelativePath
    )

    $cut = $RelativePath.IndexOf($DirectorySeparator)

    if ($cut -lt 0) {
        return $RelativePath
    }

    return $RelativePath.Substring($cut + 1)
}

function Get-SidecarNames {
    <#
    Returns the sidecar file names that can hold a media file's metadata, best first.
    Each entry: Name, Match (Exact | Numbered | EditedOriginal), Own.
    Own = $false means the sidecar belongs to another file (the unedited original)
    and must never be moved together with this one.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$MediaName
    )

    $ext = [System.IO.Path]::GetExtension($MediaName)
    $stem = [System.IO.Path]::GetFileNameWithoutExtension($MediaName)

    [pscustomobject]@{ Name = $MediaName + $MetadataSuffix; Match = 'Exact'; Own = $true }

    # Takeout names a second same-named file 'IMG(1).jpg' and its sidecar 'IMG.jpg.supplemental-metadata(1).json'.
    if ($stem -match '^(.+)\((\d+)\)$') {
        [pscustomobject]@{ Name = '{0}{1}.supplemental-metadata({2}).json' -f $Matches[1], $ext, $Matches[2]; Match = 'Numbered'; Own = $true }
    }

    # Edited copies have no sidecar of their own; the original's applies.
    if ($stem -match '^(.+)-edited$') {
        [pscustomobject]@{ Name = $Matches[1] + $ext + $MetadataSuffix; Match = 'EditedOriginal'; Own = $false }
    }
}

# =============================================================================
# VALIDATION & SETUP
# =============================================================================

if (-not (Test-Path -LiteralPath $SourceRoot -PathType Container)) {
    throw "SourceRoot does not exist or is not a folder: $SourceRoot"
}

# Get-Item expands 8.3 short names (C:\Users\ABC~1) the same way enumeration does, so
# SourceRoot-relative paths are cut at the right place.
$SourceRoot = (Get-Item -LiteralPath $SourceRoot -Force).FullName.TrimEnd($DirectorySeparator)

# Bare drive root normalization
if ($SourceRoot -match '^[A-Za-z]:$') {
    $SourceRoot += $DirectorySeparator
}

if ($Mode -eq 'Delete' -and -not $IAcceptPermanentDeletion) {
    throw "Mode 'Delete' requires the -IAcceptPermanentDeletion switch. Run -Mode Report first, review the CSV, and prefer -Mode Quarantine."
}

# v2.2 FIX (F2): Resolve relative paths through the PowerShell engine so that
# overlap checks and subsequent file operations use the exact same absolute path,
# avoiding edge cases where PS location differs from .NET process CWD.
try {
    $ReportRoot = $PSCmdlet.SessionState.Path.GetUnresolvedProviderPathFromPSPath($ReportRoot)
} catch {
    throw "Cannot resolve ReportRoot '${ReportRoot}': $($_.Exception.Message)"
}

try {
    $QuarantineRoot = $PSCmdlet.SessionState.Path.GetUnresolvedProviderPathFromPSPath($QuarantineRoot)
} catch {
    throw "Cannot resolve QuarantineRoot '${QuarantineRoot}': $($_.Exception.Message)"
}

if (Test-PathsOverlap -PathA $ReportRoot -PathB $SourceRoot) {
    throw "ReportRoot ('$ReportRoot') must not overlap SourceRoot ('$SourceRoot')."
}

if ($Mode -eq 'Quarantine' -and (Test-PathsOverlap -PathA $QuarantineRoot -PathB $SourceRoot)) {
    throw "QuarantineRoot ('$QuarantineRoot') must not overlap SourceRoot ('$SourceRoot')."
}

$QuarantineRoot = $QuarantineRoot.TrimEnd($DirectorySeparator)

if ($QuarantineRoot -match '^[A-Za-z]:$') {
    $QuarantineRoot += $DirectorySeparator
}

$RunStamp = Get-Date -Format 'yyyyMMdd_HHmmss'

try {
    [void][System.IO.Directory]::CreateDirectory($ReportRoot)
}
catch {
    throw "Cannot create ReportRoot '${ReportRoot}': $($_.Exception.Message)"
}

$ReportCsv  = Join-Path $ReportRoot "Dedupe_${RunStamp}_Report.csv"
$LogPath    = Join-Path $ReportRoot "Dedupe_${RunStamp}.log"
$ActionsCsv = Join-Path $ReportRoot "Dedupe_${RunStamp}_Actions.csv"

$script:LogWriter = [System.IO.StreamWriter]::new($LogPath, $true)
$script:LogWriter.AutoFlush = $true

try {
    # =========================================================================
    # RUN HEADER
    # =========================================================================

    Write-Log ('================ Google Photos Takeout Deduplication ({0}) ================' -f $ScriptVersion) HEADER
    Write-Log ('Mode       : {0}' -f $Mode)
    Write-Log ('SourceRoot : {0}' -f $SourceRoot)

    if ($Mode -eq 'Quarantine') {
        Write-Log ('Quarantine : {0}' -f $QuarantineRoot)
        Write-Log ('Metadata   : {0}' -f $(if ($MoveMetadataWithMedia) { 'own same-folder sidecar is moved WITH each losing copy (-MoveMetadataWithMedia)' } else { 'left in place (default)' }))
    }

    Write-Log ('Trash      : copies under Google Photos\{0} never win over copies elsewhere' -f ($TrashFolderNames -join ' or '))

    Write-Log ('Report     : {0}' -f $ReportCsv)
    Write-Log ('Log        : {0}' -f $LogPath)
    Write-Log ('Actions    : {0}' -f $ActionsCsv)
    Write-Log ('Host       : {0} \ {1} | PowerShell {2} ({3})' -f `
        $env:COMPUTERNAME, $env:USERNAME, $PSVersionTable.PSVersion, $PSVersionTable.PSEdition)

    if ($PSVersionTable.PSEdition -eq 'Desktop') {
        Write-Log 'Running under Windows PowerShell 5.1. PowerShell 7+ is recommended for long-path support and faster hashing.' WARN
    }

    try {
        $lp = (Get-ItemProperty -LiteralPath 'HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem' -ErrorAction Stop).LongPathsEnabled
        if ($lp -ne 1) {
            Write-Log 'Windows LongPathsEnabled is not 1; paths longer than 260 characters may fail.' WARN
        }
    }
    catch { }

    # =========================================================================
    # PHASE 1/5: ENUMERATE & CLASSIFY
    # =========================================================================

    Write-Log ('PHASE 1/5: Enumerating files under {0}' -f $SourceRoot) HEADER
    $sw = [System.Diagnostics.Stopwatch]::StartNew()

    $allFiles = New-Object System.Collections.Generic.List[object]
    $script:EnumCount = 0
    $enumErrors = New-Object System.Collections.Generic.List[object]

    # Manual recursion instead of 'Get-ChildItem -Recurse' to avoid traversing directory junctions.
    $reparseDirSkipped = 0
    $reparseFileSkipped = 0

    $dirStack = New-Object System.Collections.Generic.Stack[string]
    $dirStack.Push($SourceRoot)

    while ($dirStack.Count -gt 0) {
        $dir = $dirStack.Pop()

        try {
            $childItems = Get-ChildItem -LiteralPath $dir -Force -ErrorAction Stop

            foreach ($item in $childItems) {
                if ($item.PSIsContainer) {
                    if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                        $reparseDirSkipped++
                        if ($reparseDirSkipped -le 100) {
                            Write-Log ('  REPARSE-SKIP (folder not traversed): {0}' -f $item.FullName) WARN
                        }
                        continue
                    }
                    $dirStack.Push($item.FullName)
                }
                else {
                    if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                        $reparseFileSkipped++
                        if ($reparseFileSkipped -le 100) {
                            Write-Log ('  REPARSE-SKIP (file not processed): {0}' -f $item.FullName) WARN
                        }
                        continue
                    }

                    $script:EnumCount++

                    if (($script:EnumCount % 2000) -eq 0) {
                        Write-Progress -Activity 'Phase 1/5: Enumerating files' -Status ('{0} files found...' -f $script:EnumCount)
                    }

                    $allFiles.Add($item)
                }
            }
        }
        catch {
            $enumErrors.Add($_)
        }
    }

    Write-Progress -Activity 'Phase 1/5: Enumerating files' -Completed

    $enumErrorCount = $enumErrors.Count
    $scanComplete = ($enumErrorCount -eq 0)

    if ($enumErrorCount -gt 0) {
        $errs = $enumErrors
        Write-Log ('{0} enumeration problem(s) were skipped (first 100 shown):' -f $errs.Count) WARN

        for ($i = 0; $i -lt [Math]::Min($errs.Count, 100); $i++) {
            Write-Log ('  ENUM-ERROR: {0}' -f $errs[$i].Exception.Message) WARN
        }
    }

    if (-not $scanComplete) {
        Write-Log (
            'ENUMERATION INCOMPLETE: {0} error(s) occurred while scanning SourceRoot. The report may not represent all files.' -f
            $enumErrorCount
        ) ERROR

        if ($Mode -ne 'Report' -and -not $AllowIncompleteScan) {
            throw (
                'Blocking {0} mode because SourceRoot enumeration was incomplete ({1} errors). Resolve the errors or re-run with -AllowIncompleteScan if you knowingly accept an incomplete scan.' -f
                $Mode,
                $enumErrorCount
            )
        }

        if ($Mode -ne 'Report' -and $AllowIncompleteScan) {
            Write-Log 'AllowIncompleteScan supplied. Continuing despite incomplete enumeration. This run may not represent the full library.' WARN
        }
    }

    $metadataJsonSet = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
    $mediaFileInfos  = New-Object System.Collections.Generic.List[object]
    $supplementalCount = 0
    $otherCount = 0

    # Sidecars by Takeout-relative path (export-part folder stripped), for cross-part lookup.
    $rootLen = $SourceRoot.Length
    $metadataByPartPath = [System.Collections.Generic.Dictionary[string, System.Collections.Generic.List[string]]]::new([System.StringComparer]::OrdinalIgnoreCase)

    foreach ($f in $allFiles) {
        if ($f.Name -match $SidecarPattern) {
            [void]$metadataJsonSet.Add($f.FullName)
            $supplementalCount++

            $partPath = Get-PartRelativePath -RelativePath $f.FullName.Substring($rootLen).TrimStart($DirectorySeparator)
            if (-not $metadataByPartPath.ContainsKey($partPath)) {
                $metadataByPartPath[$partPath] = New-Object System.Collections.Generic.List[string]
            }
            $metadataByPartPath[$partPath].Add($f.FullName)
        }
        elseif ($MediaExtensions -contains $f.Extension.ToLowerInvariant()) {
            $mediaFileInfos.Add($f)
        }
        else {
            $otherCount++
        }
    }

    Write-Log ('Enumeration finished in {0:n1} s: {1} files total | {2} media | {3} supplemental-metadata JSON | {4} other/ignored | {5} reparse-point folder(s) skipped | {6} reparse-point file(s) skipped' -f `
        $sw.Elapsed.TotalSeconds, $allFiles.Count, $mediaFileInfos.Count, $supplementalCount, $otherCount, $reparseDirSkipped, $reparseFileSkipped)

    if (($reparseDirSkipped + $reparseFileSkipped) -gt 0) {
        Write-Log 'Reparse points (junctions/symlinks) were deliberately skipped: following them can make one physical file appear under multiple paths, which would let the script treat a file as a duplicate of itself.' WARN
    }

    # Build media record set.
    $media = New-Object System.Collections.Generic.List[object]
    $referencedJsonSet = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)

    foreach ($fi in $mediaFileInfos) {
        $relPath = $fi.FullName.Substring($rootLen).TrimStart($DirectorySeparator)
        $partDir = [System.IO.Path]::GetDirectoryName((Get-PartRelativePath -RelativePath $relPath))

        # Same folder first, then the same Takeout-relative path in any other export part.
        $sameFolder = New-Object System.Collections.Generic.List[string]
        $otherPart = New-Object System.Collections.Generic.List[string]
        $metaMatch = ''
        $companionPath = ''

        foreach ($sc in (Get-SidecarNames -MediaName $fi.Name)) {
            $siblingPath = [System.IO.Path]::Combine($fi.DirectoryName, $sc.Name)

            if ($metadataJsonSet.Contains($siblingPath)) {
                $sameFolder.Add($siblingPath)
                if (-not $metaMatch) { $metaMatch = $sc.Match }
                if ($sc.Own -and -not $companionPath) { $companionPath = $siblingPath }
            }

            $partKey = if ($partDir) { [System.IO.Path]::Combine($partDir, $sc.Name) } else { $sc.Name }
            $found = $null

            if ($metadataByPartPath.TryGetValue($partKey, [ref]$found)) {
                foreach ($p in $found) {
                    if (-not $sameFolder.Contains($p)) {
                        $otherPart.Add($p)
                        if (-not $metaMatch) { $metaMatch = $sc.Match }
                    }
                }
            }
        }

        $metaPaths = @($sameFolder) + @($otherPart)
        foreach ($p in $metaPaths) { [void]$referencedJsonSet.Add($p) }

        $metaPresent = $metaPaths.Count -gt 0
        $metaLocation = if ($sameFolder.Count -gt 0) { 'SameFolder' } elseif ($otherPart.Count -gt 0) { 'OtherPart' } else { 'None' }

        $cleanName = ($fi.Name -match '^[A-Za-z0-9][A-Za-z0-9 _.-]*\.[A-Za-z0-9]+$') -and
                     ($fi.Name -notmatch '\(\d+\)') -and
                     ($fi.Name -notmatch '(?i)(copy|edited|duplicate)')

        $media.Add([pscustomobject]@{
            FullName                = $fi.FullName
            Name                    = $fi.Name
            Length                  = [long]$fi.Length
            LastWriteTimeUtc        = $fi.LastWriteTimeUtc.ToString('u')
            RelativePath            = $relPath
            MetadataPresent         = [bool]$metaPresent
            MetadataLocation        = $metaLocation
            MetadataMatch           = $metaMatch
            MetadataPath            = ($metaPaths -join '; ')
            MetadataPaths           = $metaPaths
            CompanionPath           = $companionPath
            InTrash                 = [bool]($relPath -match $TrashPattern)
            KeeperPath              = ''
            KeeperMetadataPresent   = $false
            PathDepth               = [int](($relPath -split [regex]::Escape([string]$DirectorySeparator)).Count)
            PathLength              = [int]$fi.FullName.Length
            CleanName               = [bool]$cleanName
            Hash                    = $null
            HashStatus              = 'NotHashed'
            MetadataScore           = 0
            LocationScore           = 0
            DepthScore              = 0
            NameScore               = 0
            Score                   = 0
            GroupId                 = $null
            Recommendation          = 'UNIQUE'
            Reason                  = ''
        })
    }

    # Zero-byte files are excluded from deduplication.
    $zeroByteFiles = @($media | Where-Object { $_.Length -eq 0 })

    if ($zeroByteFiles.Count -gt 0) {
        Write-Log ('{0} zero-byte media files found (likely failed exports). EXCLUDED from dedup; they will not be touched.' -f $zeroByteFiles.Count) WARN

        foreach ($zb in $zeroByteFiles) {
            $zb.Recommendation = 'SKIP'
            $zb.Reason = 'Zero-byte file - excluded from dedup; review manually'
            Write-Log ('  ZERO-BYTE: {0}' -f $zb.FullName) WARN
        }
    }

    # Informational: metadata coverage and orphaned metadata JSONs.
    $metaSameFolderCount = @($media | Where-Object { $_.MetadataLocation -eq 'SameFolder' }).Count
    $metaOtherPartCount = @($media | Where-Object { $_.MetadataLocation -eq 'OtherPart' }).Count
    $metaNoneCount = $media.Count - $metaSameFolderCount - $metaOtherPartCount
    $inTrashCount = @($media | Where-Object { $_.InTrash }).Count

    Write-Log ('Metadata found for media: {0} same folder | {1} other export part only | {2} none' -f `
        $metaSameFolderCount, $metaOtherPartCount, $metaNoneCount)

    if ($inTrashCount -gt 0) {
        Write-Log ('{0} media files are inside Google Photos {1} folders; they never win over a copy elsewhere.' -f `
            $inTrashCount, ($TrashFolderNames -join '/'))
    }

    $orphanedJsonCount = $metadataJsonSet.Count - $referencedJsonSet.Count

    if ($orphanedJsonCount -gt 0) {
        Write-Log ('{0} supplemental-metadata JSON files match no media file in any export part (orphaned). They are ignored and left untouched.' -f $orphanedJsonCount) WARN
    }

    # =========================================================================
    # PHASE 2/5: SIZE PRE-FILTER
    # =========================================================================

    Write-Log 'PHASE 2/5: Pre-hash filter by file size' HEADER
    $sw.Restart()

    $eligible = @($media | Where-Object { $_.Length -gt 0 })
    $sizeGroups = @($eligible | Group-Object -Property Length)
    $hashCandidates = New-Object System.Collections.Generic.List[object]
    $uniqueBySize = 0

    foreach ($sg in $sizeGroups) {
        if ($sg.Count -gt 1) {
            foreach ($member in $sg.Group) {
                $hashCandidates.Add($member)
            }
        }
        else {
            $uniqueBySize++
        }
    }

    Write-Log ('Size filter: {0} files have a unique size (content duplicate impossible - hashing skipped). {1} files share a size with another file and will be hashed.' -f `
        $uniqueBySize, $hashCandidates.Count)

    # =========================================================================
    # PHASE 3/5: STREAMING SHA-256
    # =========================================================================

    Write-Log ('PHASE 3/5: SHA-256 hashing of {0} candidate files' -f $hashCandidates.Count) HEADER
    $sw.Restart()

    $totalToHash = $hashCandidates.Count
    $hashOk = 0
    $hashFail = 0

    for ($i = 0; $i -lt $totalToHash; $i++) {
        $file = $hashCandidates[$i]

        if ((($i + 1) % 20 -eq 0) -or (($i + 1) -eq $totalToHash)) {
            $pct = 0
            if ($totalToHash -gt 0) {
                $pct = [int](100.0 * ($i + 1) / $totalToHash)
            }

            Write-Progress -Activity 'Phase 3/5: SHA-256 hashing' `
                -Status ('{0} / {1} ({2}%)' -f ($i + 1), $totalToHash, $pct) `
                -PercentComplete $pct `
                -CurrentOperation $file.Name
        }

        try {
            $file.Hash = Get-FileSha256 -LiteralPath $file.FullName
            $file.HashStatus = 'Success'
            $hashOk++
        }
        catch {
            $hashFail++
            $file.HashStatus = 'Failed'
            $file.Recommendation = 'REVIEW'
            $file.Reason = 'Hash failed - never considered for deletion; review manually'

            Write-Log ('HASH-ERROR: {0} :: {1}' -f $file.FullName, $_.Exception.Message) ERROR
        }
    }

    Write-Progress -Activity 'Phase 3/5: SHA-256 hashing' -Completed
    Write-Log ('Hashing finished in {0:n1} s: {1} OK, {2} failed (failures are excluded from dedup and marked REVIEW).' -f `
        $sw.Elapsed.TotalSeconds, $hashOk, $hashFail)

    # =========================================================================
    # PHASE 4/5: GROUP BY LENGTH + HASH + SCORE + ELIGIBILITY
    # =========================================================================

    Write-Log 'PHASE 4/5: Grouping by Length + Hash, scoring, and applying eligibility rule' HEADER
    $sw.Restart()

    $hashed = @($media | Where-Object { $_.HashStatus -eq 'Success' })

    $hashGroups = @(
        $hashed |
            Group-Object -Property Length, Hash |
            Where-Object { $_.Count -gt 1 } |
            Sort-Object -Property Name
    )

    $filesInGroups = ($hashGroups | Measure-Object -Property Count -Sum).Sum
    if (-not $filesInGroups) {
        $filesInGroups = 0
    }

    Write-Log ('Duplicate groups: {0} groups containing {1} files.' -f $hashGroups.Count, $filesInGroups)

    # Initialize caches for Test-KeeperIntact
    $script:KeeperByPath = @{}
    $script:KeeperVerified = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
    $script:KeeperFailed = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)

    $groupId = 0

    foreach ($hg in $hashGroups) {
        $groupId++
        $gid = 'G{0:D5}' -f $groupId
        $group = @($hg.Group)

        $minDepth = ($group | Measure-Object -Property PathDepth -Minimum).Minimum

        foreach ($member in $group) {
            if ($member.MetadataPresent) {
                $member.MetadataScore = 100
            }

            # A sidecar beside the file keeps the pair together for later metadata embedding.
            if ($member.MetadataLocation -eq 'SameFolder') {
                $member.LocationScore = 2
            }

            if ($member.PathDepth -eq $minDepth) {
                $member.DepthScore = 5
            }

            if ($member.CleanName) {
                $member.NameScore = 1
            }

            $member.Score = $member.MetadataScore + $member.LocationScore + $member.DepthScore + $member.NameScore
            $member.GroupId = $gid
        }

        # Eligibility 1: never pick a Trash/Bin copy while a copy exists elsewhere.
        $nonTrashMembers = @($group | Where-Object { -not $_.InTrash })
        $candidateSet = if ($nonTrashMembers.Count -gt 0) { $nonTrashMembers } else { $group }

        # Eligibility 2: among those, only metadata-bearing copies when any exist.
        $metaMembers = @($candidateSet | Where-Object { $_.MetadataPresent })

        if ($metaMembers.Count -gt 0) {
            $eligibleSet = $metaMembers
        }
        else {
            $eligibleSet = $candidateSet
        }

        $winner = $eligibleSet |
            Sort-Object -Property @{Expression = 'Score'; Descending = $true},
                                  @{Expression = 'PathLength'; Ascending = $true},
                                  @{Expression = 'FullName'; Ascending = $true} |
            Select-Object -First 1

        $winner.Recommendation = 'KEEP'
        $winner.KeeperPath = $winner.FullName
        $winner.KeeperMetadataPresent = $winner.MetadataPresent
        
        # Cache keeper record for later integrity validation
        $script:KeeperByPath[$winner.FullName] = $winner

        if ($metaMembers.Count -eq 0 -and @($group | Where-Object { $_.MetadataPresent }).Count -gt 0) {
            $winner.Reason = 'KEEP: no copy outside Trash/Bin has metadata; deterministic fallback (score, shortest path, lexical)'
        }
        elseif ($metaMembers.Count -eq 0) {
            $winner.Reason = 'KEEP: no copy in group has metadata; deterministic fallback (score, shortest path, lexical)'
        }
        elseif ($metaMembers.Count -eq 1) {
            $winner.Reason = 'KEEP: only copy with supplemental metadata'
        }
        else {
            $winner.Reason = 'KEEP: best-scoring of {0} metadata-bearing copies (eligibility restricted to metadata copies)' -f $metaMembers.Count
        }

        if ($nonTrashMembers.Count -gt 0 -and $nonTrashMembers.Count -lt $group.Count) {
            $winner.Reason += '; Trash/Bin copies excluded'
        }

        foreach ($loser in ($group | Where-Object { $_.FullName -ne $winner.FullName })) {
            $loser.Recommendation = 'DELETE'
            $loser.KeeperPath = $winner.FullName
            $loser.KeeperMetadataPresent = $winner.MetadataPresent

            if ($loser.InTrash -and -not $winner.InTrash) {
                $loser.Reason = 'DELETE: duplicate content; copy is in Trash/Bin and another copy exists elsewhere'
            }
            elseif ((-not $loser.MetadataPresent) -and ($metaMembers.Count -gt 0)) {
                $loser.Reason = 'DELETE: duplicate content; no metadata while another copy has metadata'
            }
            elseif ($loser.MetadataPresent) {
                $loser.Reason = 'DELETE: duplicate content; metadata present but another metadata copy scored higher'
            }
            else {
                $loser.Reason = 'DELETE: duplicate content; lower score than keeper'
            }
        }
    }

    # =========================================================================
    # PHASE 5/5: REPORT + SUMMARY + MODE EXECUTION
    # =========================================================================

    Write-Log ('PHASE 5/5: Report + mode ''{0}''' -f $Mode) HEADER
    $sw.Restart()

    $dupRows = @(
        $media |
            Where-Object { $null -ne $_.GroupId } |
            Sort-Object -Property @{Expression = 'GroupId'; Ascending = $true},
                                  @{Expression = 'Score'; Descending = $true},
                                  @{Expression = 'PathLength'; Ascending = $true}
    )

    $advisoryRows = @($media | Where-Object { $_.Recommendation -in @('REVIEW', 'SKIP') })

    $plannedAction = switch ($Mode) {
        'Report'     { 'None' }
        'Quarantine' { 'Quarantine' }
        'Delete'     { 'Delete' }
    }

    $reportRows = @(foreach ($r in (@($dupRows) + @($advisoryRows))) {
        [pscustomobject][ordered]@{
            GroupId                 = $r.GroupId
            Hash                    = $r.Hash
            FilePath                = $r.FullName
            RelativePath            = $r.RelativePath
            FileName                = $r.Name
            FileSizeBytes           = $r.Length
            MetadataPresent         = $r.MetadataPresent
            MetadataLocation        = $r.MetadataLocation
            MetadataMatch           = $r.MetadataMatch
            MetadataPath            = $r.MetadataPath
            InTrash                 = $r.InTrash
            KeeperPath              = $r.KeeperPath
            KeeperMetadataPresent   = $r.KeeperMetadataPresent
            MetadataScore           = $r.MetadataScore
            LocationScore           = $r.LocationScore
            DepthScore              = $r.DepthScore
            NameScore               = $r.NameScore
            Score                   = $r.Score
            PathDepth               = $r.PathDepth
            PathLength              = $r.PathLength
            LastWriteTimeUtc        = $r.LastWriteTimeUtc
            HashStatus              = $r.HashStatus
            Recommendation          = $r.Recommendation
            Reason                  = $r.Reason
            PlannedAction           = $plannedAction
            Destination             = $(
                if (($Mode -eq 'Quarantine') -and ($r.Recommendation -eq 'DELETE')) {
                    Join-Path -Path $QuarantineRoot -ChildPath $r.RelativePath
                }
                else {
                    ''
                }
            )
        }
    })

    # The report is read-only output, so it is written even under -WhatIf (v2.2 skipped it).
    if ($reportRows.Count -gt 0) {
        $reportRows | Export-Csv -LiteralPath $ReportCsv -NoTypeInformation -Encoding UTF8 -WhatIf:$false
        Write-Log ('Report written: {0} ({1} rows)' -f $ReportCsv, $reportRows.Count) SUCCESS
    }
    else {
        [pscustomobject][ordered]@{
            GroupId='';Hash='';FilePath='';RelativePath='';FileName='';FileSizeBytes='';
            MetadataPresent='';MetadataLocation='';MetadataMatch='';MetadataPath='';InTrash='';
            KeeperPath='';KeeperMetadataPresent='';
            MetadataScore='';LocationScore='';DepthScore='';NameScore='';Score='';PathDepth='';PathLength='';
            LastWriteTimeUtc='';HashStatus='';Recommendation='';Reason='';PlannedAction='';Destination=''
        } | Export-Csv -LiteralPath $ReportCsv -NoTypeInformation -Encoding UTF8 -WhatIf:$false

        Write-Log ('No duplicate groups found. Header-only report written: {0}' -f $ReportCsv) SUCCESS
    }

    # ----- Summary -----

    $keepCount = @($dupRows | Where-Object { $_.Recommendation -eq 'KEEP' }).Count
    $losers = @($dupRows | Where-Object { $_.Recommendation -eq 'DELETE' })
    $deleteCount = $losers.Count

    $reclaimBytes = ($losers | Measure-Object -Property Length -Sum).Sum
    if (-not $reclaimBytes) {
        $reclaimBytes = 0
    }

    $reclaimGB = [math]::Round($reclaimBytes / 1GB, 2)

    $groupsWithMeta = @(
        $hashGroups |
            Where-Object { (@($_.Group | Where-Object { $_.MetadataPresent })).Count -gt 0 }
    ).Count

    # Groups that would have had no metadata copy under v2.2's same-folder-only matching.
    $groupsMetaOtherPartOnly = @(
        $hashGroups |
            Where-Object {
                (@($_.Group | Where-Object { $_.MetadataPresent })).Count -gt 0 -and
                (@($_.Group | Where-Object { $_.MetadataLocation -eq 'SameFolder' })).Count -eq 0
            }
    ).Count

    $trashLosers = @($losers | Where-Object { $_.InTrash }).Count

    Write-Log '------------------------------- SUMMARY -------------------------------' HEADER
    Write-Log ('Total files scanned                 : {0}' -f $allFiles.Count)
    Write-Log ('Media files found                   : {0}' -f $media.Count)
    Write-Log ('Supplemental metadata JSON files    : {0}' -f $supplementalCount)
    Write-Log ('Media with metadata: same folder    : {0}' -f $metaSameFolderCount)
    Write-Log ('Media with metadata: other part only: {0}' -f $metaOtherPartCount)
    Write-Log ('Media with no metadata found        : {0}' -f $metaNoneCount)
    Write-Log ('Media inside Trash/Bin folders      : {0}' -f $inTrashCount)
    Write-Log ('Zero-byte media (excluded)          : {0}' -f $zeroByteFiles.Count)
    Write-Log ('Unique-size media (not hashed)      : {0}' -f $uniqueBySize)
    Write-Log ('Media files hashed                  : {0}' -f $totalToHash)
    Write-Log ('Hash failures (ReviewRequired)      : {0}' -f $hashFail)
    Write-Log ('Duplicate groups                    : {0}' -f $hashGroups.Count)
    Write-Log ('  groups where >=1 copy has metadata: {0}' -f $groupsWithMeta)
    Write-Log ('    of which metadata only via other part: {0}' -f $groupsMetaOtherPartOnly)
    Write-Log ('  groups where no copy has metadata : {0}' -f ($hashGroups.Count - $groupsWithMeta))
    Write-Log ('Files in duplicate groups           : {0}' -f $dupRows.Count)
    Write-Log ('KEEP (winners)                      : {0}' -f $keepCount)
    Write-Log ('DELETE candidates (losers)          : {0}' -f $deleteCount)
    Write-Log ('  of which inside Trash/Bin         : {0}' -f $trashLosers)
    Write-Log ('Space reclaimable from losers       : {0} bytes ({1} GB)' -f $reclaimBytes, $reclaimGB)
    Write-Log ('Enumeration completeness            : {0}' -f $(if ($scanComplete) { 'Complete' } else { "INCOMPLETE ($enumErrorCount errors)" }))

    # =========================================================================
    # QUARANTINE MODE
    # =========================================================================

    if ($Mode -eq 'Quarantine' -and $deleteCount -gt 0) {
        if ($QuarantineRoot -match '^[A-Za-z]:') {
            $driveLetter = $QuarantineRoot.Substring(0, 1)
            $freeBytes = (Get-PSDrive -Name $driveLetter -ErrorAction SilentlyContinue).Free

            if ($null -ne $freeBytes -and $freeBytes -lt ($reclaimBytes * 1.1)) {
                throw ('Insufficient free space on drive {0}: need ~{1} GB, available {2} GB.' -f `
                    $driveLetter, [math]::Round($reclaimBytes / 1GB, 2), [math]::Round($freeBytes / 1GB, 2))
            }
        }

        Write-Log ('QUARANTINE: processing {0} losing copies -> {1}' -f $deleteCount, $QuarantineRoot) ACTION

        $actionRows = New-Object System.Collections.Generic.List[object]

        $movedOk = 0
        $alreadyThere = 0
        $reviewCount = 0
        $moveFail = 0
        $plannedMoveCount = 0
        $companionKeptCount = 0

        # Sidecars used by any file that is not being quarantined (keepers and unique files).
        $jsonNeededByRemaining = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
        foreach ($m in $media) {
            if ($m.Recommendation -ne 'DELETE') {
                foreach ($p in $m.MetadataPaths) { [void]$jsonNeededByRemaining.Add($p) }
            }
        }

        for ($i = 0; $i -lt $losers.Count; $i++) {
            $loser = $losers[$i]

            if ((($i + 1) % 10 -eq 0) -or (($i + 1) -eq $losers.Count)) {
                Write-Progress -Activity 'Quarantining duplicate copies' `
                    -Status ('{0} / {1}' -f ($i + 1), $losers.Count) `
                    -PercentComplete ([int](100.0 * ($i + 1) / $losers.Count))
            }

            # Verify keeper is still intact before touching any losers
            if (-not (Test-KeeperIntact -KeeperPath $loser.KeeperPath)) {
                $reviewCount++
                Write-Log ('REVIEW-REQUIRED (keeper changed/vanished; NOT touched): {0}' -f $loser.FullName) ERROR
                $actionRows.Add([pscustomobject][ordered]@{
                    TimeStamp = (Get-Date -Format 'yyyy-MM-dd HH:mm:ss.fff'); Action = 'ReviewRequired'
                    SourcePath = $loser.FullName; DestinationPath = ''; SizeBytes = $loser.Length; Hash = $loser.Hash
                    Detail = 'Keeper failed integrity check (exists/size/SHA-256)'
                })
                continue
            }

            # Pre-action revalidation
            if (-not (Test-FileMatchesExpected -LiteralPath $loser.FullName -ExpectedLength $loser.Length -ExpectedHash $loser.Hash)) {
                $reviewCount++

                Write-Log ('REVIEW-REQUIRED (file changed since hashing; NOT touched): {0}' -f $loser.FullName) ERROR

                $actionRows.Add([pscustomobject][ordered]@{
                    TimeStamp       = Get-Date -Format 'yyyy-MM-dd HH:mm:ss.fff'
                    Action          = 'ReviewRequired'
                    SourcePath      = $loser.FullName
                    DestinationPath = ''
                    SizeBytes       = $loser.Length
                    Hash            = $loser.Hash
                    Detail          = 'Failed pre-action revalidation (exists/size/SHA-256)'
                })

                continue
            }

            $destPath = Join-Path -Path $QuarantineRoot -ChildPath $loser.RelativePath

            # Only the loser's own same-folder sidecar can travel with it, and only on request.
            # Cross-part and edited-original sidecars are shared with other files and never move.
            $jsonSrc = $loser.CompanionPath

            $hasCompanionMetadata = $MoveMetadataWithMedia -and
                                    (-not [string]::IsNullOrEmpty($jsonSrc)) -and
                                    (Test-Path -LiteralPath $jsonSrc -PathType Leaf)

            if ($hasCompanionMetadata -and $jsonNeededByRemaining.Contains($jsonSrc)) {
                $hasCompanionMetadata = $false
                $companionKeptCount++
                Write-Log ('METADATA-KEPT (sidecar is also the metadata of a file that stays; left in place): {0}' -f $jsonSrc) WARN
            }

            $jsonDest = if ($hasCompanionMetadata) {
                Join-Path -Path $QuarantineRoot -ChildPath $jsonSrc.Substring($rootLen).TrimStart($DirectorySeparator)
            }
            else {
                ''
            }

            # Destination collision protection
            if (Test-Path -LiteralPath $destPath) {
                $isLeaf = Test-Path -LiteralPath $destPath -PathType Leaf
                $sameContent = $false
                
                if ($isLeaf) {
                    $sameContent = Test-FileMatchesExpected `
                        -LiteralPath $destPath `
                        -ExpectedLength $loser.Length `
                        -ExpectedHash $loser.Hash
                }

                if ($sameContent) {
                    $pairComplete = $false

                    if ($hasCompanionMetadata) {
                        if (Test-Path -LiteralPath $jsonDest -PathType Leaf) {
                            try {
                                $metaItem = Get-Item -LiteralPath $jsonSrc -Force
                                $metaLength = $metaItem.Length
                                $metaHash = Get-FileSha256 -LiteralPath $jsonSrc

                                if (Test-FileMatchesExpected -LiteralPath $jsonDest -ExpectedLength $metaLength -ExpectedHash $metaHash) {
                                    $pairComplete = $true
                                }
                            }
                            catch {
                                $pairComplete = $false
                            }
                        }
                    }
                    else {
                        $pairComplete = $true
                    }

                    if ($pairComplete) {
                        $alreadyThere++

                        Write-Log (
                            'ALREADY-IN-QUARANTINE (identical media and metadata pair already present; source left untouched): {0}' -f
                            $destPath
                        ) WARN

                        $actionRows.Add([pscustomobject][ordered]@{
                            TimeStamp       = Get-Date -Format 'yyyy-MM-dd HH:mm:ss.fff'
                            Action          = 'AlreadyInQuarantine'
                            SourcePath      = $loser.FullName
                            DestinationPath = $destPath
                            SizeBytes       = $loser.Length
                            Hash            = $loser.Hash
                            Detail          = 'Identical media and metadata already exist at destination; source left untouched'
                        })
                    }
                    else {
                        $reviewCount++

                        Write-Log (
                            'REVIEW-REQUIRED (destination has identical media but missing/different metadata; source NOT touched): {0}' -f
                            $destPath
                        ) ERROR

                        $actionRows.Add([pscustomobject][ordered]@{
                            TimeStamp       = Get-Date -Format 'yyyy-MM-dd HH:mm:ss.fff'
                            Action          = 'ReviewRequired'
                            SourcePath      = $loser.FullName
                            DestinationPath = $destPath
                            SizeBytes       = $loser.Length
                            Hash            = $loser.Hash
                            Detail          = 'Destination contains identical media but companion metadata is missing or differs'
                        })
                    }
                }
                else {
                    $reviewCount++
                    $detail = if ($isLeaf) { 'Destination collision with differing media content' } else { 'Destination path is occupied by a folder' }

                    Write-Log (
                        'REVIEW-REQUIRED (quarantine destination blocked; source NOT touched): {0}' -f
                        $destPath
                    ) ERROR

                    $actionRows.Add([pscustomobject][ordered]@{
                        TimeStamp       = Get-Date -Format 'yyyy-MM-dd HH:mm:ss.fff'
                        Action          = 'ReviewRequired'
                        SourcePath      = $loser.FullName
                        DestinationPath = $destPath
                        SizeBytes       = $loser.Length
                        Hash            = $loser.Hash
                        Detail          = $detail
                    })
                }

                continue
            }

            if ($hasCompanionMetadata -and (Test-Path -LiteralPath $jsonDest)) {
                $reviewCount++

                Write-Log (
                    'REVIEW-REQUIRED (metadata destination blocked; media NOT touched): {0}' -f
                    $jsonDest
                ) ERROR

                $actionRows.Add([pscustomobject][ordered]@{
                    TimeStamp       = Get-Date -Format 'yyyy-MM-dd HH:mm:ss.fff'
                    Action          = 'ReviewRequired'
                    SourcePath      = $loser.FullName
                    DestinationPath = $destPath
                    SizeBytes       = $loser.Length
                    Hash            = $loser.Hash
                    Detail          = 'Companion metadata destination already exists; media and metadata pair left untouched'
                })

                continue
            }

            # v2.2 FIX (F6): Move-Item -Destination treats [ and ] as wildcards.
            # If any source or destination path contains these characters, fail closed.
            $hasBrackets = ($loser.FullName -match '[\[\]]') -or ($destPath -match '[\[\]]')
            if ($hasCompanionMetadata) {
                $hasBrackets = $hasBrackets -or ($jsonSrc -match '[\[\]]') -or ($jsonDest -match '[\[\]]')
            }

            if ($hasBrackets) {
                $reviewCount++
                Write-Log ('REVIEW-REQUIRED (path contains [ or ] wildcard metacharacters; NOT touched): {0}' -f $loser.FullName) ERROR
                $actionRows.Add([pscustomobject][ordered]@{
                    TimeStamp       = Get-Date -Format 'yyyy-MM-dd HH:mm:ss.fff'
                    Action          = 'ReviewRequired'
                    SourcePath      = $loser.FullName
                    DestinationPath = $destPath
                    SizeBytes       = $loser.Length
                    Hash            = $loser.Hash
                    Detail          = 'Source or destination path contains [ or ] (wildcard metacharacters for Move-Item -Destination); skipped for safety'
                })
                continue
            }

            $plannedMoveCount++

            if ($PSCmdlet.ShouldProcess(
                    $loser.FullName,
                    ('quarantine media' + $(if ($hasCompanionMetadata) { ' + companion metadata' } else { '' }) + ' -> ' + $destPath)
                )) {

                $mediaMoveAttempted = $false

                $metadataMoveAttempted = $false
                $metadataMoved = $false

                $metaLength = 0
                $metaHash = ''

                try {
                    [void][System.IO.Directory]::CreateDirectory((Split-Path -Parent $destPath))

                    Move-Item -LiteralPath $loser.FullName -Destination $destPath -ErrorAction Stop
                    $mediaMoveAttempted = $true

                    if (-not (Test-Path -LiteralPath $destPath -PathType Leaf) -or
                        (Test-Path -LiteralPath $loser.FullName -PathType Leaf) -or
                        -not (Test-FileMatchesExpected -LiteralPath $destPath -ExpectedLength $loser.Length -ExpectedHash $loser.Hash)) {
                        throw 'Post-move media verification failed.'
                    }

                    if ($hasCompanionMetadata) {
                        try {
                            $metaItem = Get-Item -LiteralPath $jsonSrc -Force
                            $metaLength = $metaItem.Length
                            $metaHash = Get-FileSha256 -LiteralPath $jsonSrc
                        }
                        catch {
                            throw "Failed to read or hash companion metadata before move: $($_.Exception.Message)"
                        }

                        [void][System.IO.Directory]::CreateDirectory((Split-Path -Parent $jsonDest))

                        Move-Item -LiteralPath $jsonSrc -Destination $jsonDest -ErrorAction Stop
                        $metadataMoveAttempted = $true

                        if (-not (Test-FileMatchesExpected -LiteralPath $jsonDest -ExpectedLength $metaLength -ExpectedHash $metaHash) -or
                            (Test-Path -LiteralPath $jsonSrc -PathType Leaf)) {
                            throw 'Post-move metadata verification failed.'
                        }

                        $metadataMoved = $true
                    }

                    $movedOk++

                    $actionRows.Add([pscustomobject][ordered]@{
                        TimeStamp       = Get-Date -Format 'yyyy-MM-dd HH:mm:ss.fff'
                        Action          = 'MovedMedia'
                        SourcePath      = $loser.FullName
                        DestinationPath = $destPath
                        SizeBytes       = $loser.Length
                        Hash            = $loser.Hash
                        Detail          = $(
                            if ($hasCompanionMetadata) {
                                'Media moved and companion metadata moved successfully'
                            }
                            elseif ($loser.MetadataPresent) {
                                'Media moved; metadata JSON left in place'
                            }
                            else {
                                'Media moved; no metadata JSON found'
                            }
                        )
                    })

                    if ($metadataMoved) {
                        $actionRows.Add([pscustomobject][ordered]@{
                            TimeStamp       = Get-Date -Format 'yyyy-MM-dd HH:mm:ss.fff'
                            Action          = 'MovedMetadata'
                            SourcePath      = $jsonSrc
                            DestinationPath = $jsonDest
                            SizeBytes       = $metaLength
                            Hash            = $metaHash
                            Detail          = 'Companion metadata moved with media'
                        })
                    }

                    Write-Log (
                        'MOVED: {0} -> {1}{2}' -f
                        $loser.FullName,
                        $destPath,
                        $(if ($metadataMoved) { ' [metadata included]' } else { '' })
                    ) ACTION
                }
                catch {
                    $moveFail++

                    $detail = $_.Exception.Message

                    $anyMoveSucceededBeforeRollback = ($mediaMoveAttempted -or $metadataMoveAttempted)

                    if ($metadataMoveAttempted) {

                        try {
                            if ((Test-Path -LiteralPath $jsonDest -PathType Leaf) -and
                                (-not (Test-Path -LiteralPath $jsonSrc -PathType Leaf))) {

                                Move-Item -LiteralPath $jsonDest -Destination $jsonSrc -ErrorAction Stop

                                if ((Test-Path -LiteralPath $jsonSrc -PathType Leaf) -and
                                    (-not (Test-Path -LiteralPath $jsonDest -PathType Leaf)) -and
                                    (Test-FileMatchesExpected -LiteralPath $jsonSrc -ExpectedLength $metaLength -ExpectedHash $metaHash)) {

                                    $metadataMoveAttempted = $false
                                    $detail += ' Metadata rollback succeeded.'
                                }
                                else {
                                    $detail += ' CRITICAL: Metadata rollback verification FAILED.'
                                }
                            }
                            else {
                                $detail += ' CRITICAL: Metadata rollback pre-check failed (state mismatch).'
                            }
                        }
                        catch {
                            $detail += ' CRITICAL: Metadata rollback FAILED.'
                        }
                    }

                    if ($mediaMoveAttempted -and -not $metadataMoveAttempted) {

                        try {
                            if ((Test-Path -LiteralPath $destPath -PathType Leaf) -and
                                (-not (Test-Path -LiteralPath $loser.FullName -PathType Leaf))) {

                                Move-Item -LiteralPath $destPath -Destination $loser.FullName -ErrorAction Stop

                                if ((Test-Path -LiteralPath $loser.FullName -PathType Leaf) -and
                                    (-not (Test-Path -LiteralPath $destPath -PathType Leaf)) -and
                                    (Test-FileMatchesExpected -LiteralPath $loser.FullName -ExpectedLength $loser.Length -ExpectedHash $loser.Hash)) {

                                    $mediaMoveAttempted = $false
                                    $detail += ' Media rollback succeeded; source media restored.'
                                }
                                else {
                                    $detail += ' CRITICAL: Media rollback verification FAILED.'
                                }
                            }
                            else {
                                $detail += ' CRITICAL: Media rollback pre-check failed (state mismatch).'
                            }
                        }
                        catch {
                            $detail += ' CRITICAL: Media rollback FAILED.'
                        }
                    }
                    elseif ($mediaMoveAttempted -and $metadataMoveAttempted) {
                        $detail += ' CRITICAL: Metadata rollback failed. Media left in quarantine to keep pair together. Manual review required.'
                    }

                    Write-Log (
                        'QUARANTINE-ERROR: {0} :: {1}' -f
                        $loser.FullName,
                        $detail
                    ) ERROR

                    $actionStatus = 'ReviewRequired'

                    if ($anyMoveSucceededBeforeRollback -and
                        (-not $mediaMoveAttempted) -and
                        (-not $metadataMoveAttempted)) {
                        $actionStatus = 'QuarantineFailedRolledBack'
                    }

                    $actionRows.Add([pscustomobject][ordered]@{
                        TimeStamp       = Get-Date -Format 'yyyy-MM-dd HH:mm:ss.fff'
                        Action          = $actionStatus
                        SourcePath      = $loser.FullName
                        DestinationPath = $destPath
                        SizeBytes       = $loser.Length
                        Hash            = $loser.Hash
                        Detail          = $detail
                    })
                }
            }
        }

        Write-Progress -Activity 'Quarantining duplicate copies' -Completed

        if ($actionRows.Count -gt 0) {
            $actionRows | Export-Csv -LiteralPath $ActionsCsv -NoTypeInformation -Encoding UTF8
            Write-Log ('Actions manifest written: {0} ({1} rows). KEEP THIS FILE - it is your rollback record.' -f $ActionsCsv, $actionRows.Count) SUCCESS
        }

        if ($WhatIfPreference) {
            Write-Log (
                'WHATIF COMPLETE: planned pair moves: {0}; already present: {1}; review/blocked: {2}; failed: {3}; actual moves: 0.' -f
                $plannedMoveCount,
                $alreadyThere,
                $reviewCount,
                $moveFail
            ) SUCCESS
        }
        else {
            $finishLevel = if (($moveFail -eq 0) -and ($reviewCount -eq 0)) {
                'SUCCESS'
            }
            else {
                'WARN'
            }

            Write-Log (
                'Quarantine finished: {0} moved OK, {1} already in quarantine, {2} review-required, {3} failed.' -f
                $movedOk,
                $alreadyThere,
                $reviewCount,
                $moveFail
            ) $finishLevel
        }

        if ($companionKeptCount -gt 0) {
            Write-Log ('{0} sidecar(s) were left in place because a remaining file also uses them.' -f $companionKeptCount) WARN
        }
    }

    # =========================================================================
    # DELETE MODE
    # =========================================================================

    if ($Mode -eq 'Delete' -and $deleteCount -gt 0) {
        Write-Host ''
        Write-Host '  ======================================================================' -ForegroundColor Red
        Write-Host '   !!!  PERMANENT DELETION MODE  !!!' -ForegroundColor Red
        Write-Host ('   {0} files ({1} GB) are slated for permanent deletion.' -f $deleteCount, $reclaimGB) -ForegroundColor Red
        Write-Host '   This bypasses the Recycle Bin and cannot be undone.' -ForegroundColor Red
        Write-Host ''
        Write-Host '   Delete mode deletes duplicate MEDIA ONLY.' -ForegroundColor Yellow
        Write-Host '   Companion supplemental-metadata JSON files are NEVER deleted and may' -ForegroundColor Yellow
        Write-Host '   become orphaned. This is not a fully clean Takeout reconciliation mode.' -ForegroundColor Yellow
        Write-Host ''
        Write-Host '   Recommended alternative: -Mode Quarantine, review, then empty the' -ForegroundColor Yellow
        Write-Host '   quarantine folder manually.' -ForegroundColor Yellow
        Write-Host '  ======================================================================' -ForegroundColor Red

        $confirm = ''

        if (-not $WhatIfPreference) {
            $confirm = Read-Host '  Type DELETE DUPLICATES (all caps) to proceed, or anything else to abort'
        }

        if (($confirm -ceq 'DELETE DUPLICATES') -or $WhatIfPreference) {
            if ($WhatIfPreference) {
                Write-Log 'WhatIf active: no files will actually be deleted.' WARN
            }

            $actionRows = New-Object System.Collections.Generic.List[object]
            $deletedOk = 0
            $reviewCount = 0
            $deleteFail = 0

            for ($i = 0; $i -lt $losers.Count; $i++) {
                $loser = $losers[$i]

                if ((($i + 1) % 10 -eq 0) -or (($i + 1) -eq $losers.Count)) {
                    Write-Progress -Activity 'Permanently deleting duplicate copies' `
                        -Status ('{0} / {1}' -f ($i + 1), $losers.Count) `
                        -PercentComplete ([int](100.0 * ($i + 1) / $losers.Count))
                }

                # Verify keeper is still intact before touching any losers
                if (-not (Test-KeeperIntact -KeeperPath $loser.KeeperPath)) {
                    $reviewCount++
                    Write-Log ('REVIEW-REQUIRED (keeper changed/vanished; NOT deleted): {0}' -f $loser.FullName) ERROR
                    $actionRows.Add([pscustomobject][ordered]@{
                        TimeStamp = (Get-Date -Format 'yyyy-MM-dd HH:mm:ss.fff'); Action = 'ReviewRequired'
                        SourcePath = $loser.FullName; DestinationPath = ''; SizeBytes = $loser.Length; Hash = $loser.Hash
                        Detail = 'Keeper failed integrity check (exists/size/SHA-256)'
                    })
                    continue
                }

                # Pre-action revalidation
                if (-not (Test-FileMatchesExpected -LiteralPath $loser.FullName -ExpectedLength $loser.Length -ExpectedHash $loser.Hash)) {
                    $reviewCount++

                    Write-Log ('REVIEW-REQUIRED (file changed since hashing; NOT deleted): {0}' -f $loser.FullName) ERROR

                    $actionRows.Add([pscustomobject][ordered]@{
                        TimeStamp       = Get-Date -Format 'yyyy-MM-dd HH:mm:ss.fff'
                        Action          = 'ReviewRequired'
                        SourcePath      = $loser.FullName
                        DestinationPath = ''
                        SizeBytes       = $loser.Length
                        Hash            = $loser.Hash
                        Detail          = 'Failed pre-action revalidation (exists/size/SHA-256)'
                    })

                    continue
                }

                if ($PSCmdlet.ShouldProcess($loser.FullName, 'PERMANENTLY DELETE')) {
                    try {
                        Remove-Item -LiteralPath $loser.FullName -Force -ErrorAction Stop

                        if (-not (Test-Path -LiteralPath $loser.FullName)) {
                            $deletedOk++

                            $actionRows.Add([pscustomobject][ordered]@{
                                TimeStamp       = Get-Date -Format 'yyyy-MM-dd HH:mm:ss.fff'
                                Action          = 'DeletedMedia'
                                SourcePath      = $loser.FullName
                                DestinationPath = ''
                                SizeBytes       = $loser.Length
                                Hash            = $loser.Hash
                                Detail          = 'Media deleted; companion metadata intentionally preserved'
                            })

                            Write-Log ('DELETED: {0}' -f $loser.FullName) ACTION
                        }
                        else {
                            $deleteFail++
                            Write-Log ('DELETE VERIFY FAILED (file still exists): {0}' -f $loser.FullName) ERROR
                        }
                    }
                    catch {
                        $deleteFail++
                        Write-Log ('DELETE-ERROR: {0} :: {1}' -f $loser.FullName, $_.Exception.Message) ERROR
                    }
                }
            }

            Write-Progress -Activity 'Permanently deleting duplicate copies' -Completed

            if (-not $WhatIfPreference -and $actionRows.Count -gt 0) {
                $actionRows | Export-Csv -LiteralPath $ActionsCsv -NoTypeInformation -Encoding UTF8
                Write-Log ('Deletion audit manifest written: {0}. This is an audit record, not a rollback manifest.' -f $ActionsCsv) SUCCESS
            }

            $orphaned = @($losers | Where-Object { $_.CompanionPath })

            if ($orphaned.Count -gt 0) {
                Write-Log ('NOTE: {0} companion .supplemental-metadata.json files were intentionally left in place for manual review:' -f $orphaned.Count) WARN

                foreach ($o in $orphaned) {
                    Write-Log ('  ORPHANED-JSON: {0}' -f $o.CompanionPath) WARN
                }
            }

            $finishLevel = if (($deleteFail -eq 0) -and ($reviewCount -eq 0)) {
                'SUCCESS'
            }
            else {
                'WARN'
            }

            Write-Log ('Deletion finished: {0} deleted, {1} review-required, {2} failed.' -f $deletedOk, $reviewCount, $deleteFail) $finishLevel
        }
        else {
            Write-Log 'Delete mode aborted: confirmation phrase not entered. No files were deleted.' WARN
        }
    }

    if ($Mode -eq 'Report') {
        Write-Log 'REPORT-ONLY MODE COMPLETE. No files were moved or deleted.' SUCCESS
    }

    if (-not $scanComplete) {
        Write-Log 'WARNING: SourceRoot enumeration was incomplete. This run may not represent the full library. Do not treat it as a complete reconciliation until the enumeration errors are resolved.' WARN
    }

    Write-Log ('Run complete. All artifacts for run {0} are in {1}.' -f $RunStamp, $ReportRoot) SUCCESS
}
finally {
    if ($script:LogWriter) {
        $script:LogWriter.Dispose()
    }
}