param(
    [Parameter(Mandatory = $true)][Alias('Pid')][int]$ProcessId,
    [string]$WitnessDirectory = $env:KILL_WITNESS_DIR
)
$ErrorActionPreference = 'Stop'
if ($ProcessId -le 0) { throw 'Supply a positive process identifier.' }
if (-not $WitnessDirectory) {
    $WitnessDirectory = Join-Path ([System.IO.Path]::GetTempPath()) 'claude-kill-witness'
}
# Read local process state. This helper never stops a process.
$rootProcess = Get-Process -Id $ProcessId -ErrorAction Stop
$rootStarted = $rootProcess.StartTime.ToUniversalTime().ToString('o')
$processRows = @(Get-CimInstance -ClassName Win32_Process)
$targets = [System.Collections.Generic.HashSet[int]]::new()
$null = $targets.Add($ProcessId)
do {
    $added = $false
    foreach ($row in $processRows) {
        if ($targets.Contains([int]$row.ParentProcessId) -and $targets.Add([int]$row.ProcessId)) {
            $added = $true
        }
    }
} while ($added)
$totalCpu = 0.0
$complete = $true
foreach ($targetId in $targets) {
    try {
        $process = Get-Process -Id $targetId -ErrorAction Stop
        $totalCpu += $process.TotalProcessorTime.TotalSeconds
    } catch { $complete = $false }
}
$rootAfter = Get-Process -Id $ProcessId -ErrorAction Stop
if ($rootAfter.StartTime.ToUniversalTime().ToString('o') -ne $rootStarted) {
    throw 'The process identifier changed owners while sampling.'
}
$sample = [ordered]@{
    at = [DateTimeOffset]::UtcNow.ToString('o')
    pid = $ProcessId
    process_started_at = $rootStarted
    tree_size = $targets.Count
    cpu_total_s = $(if ($complete) { $totalCpu } else { $null })
}
$null = [System.IO.Directory]::CreateDirectory($WitnessDirectory)
$witnessPath = Join-Path $WitnessDirectory ($ProcessId.ToString() + '.jsonl')
[System.IO.File]::AppendAllText($witnessPath, (($sample | ConvertTo-Json -Compress) + "`n"), [System.Text.UTF8Encoding]::new($false))
Write-Output $witnessPath
