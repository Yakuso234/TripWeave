param(
    [ValidateRange(1024, 65535)][int]$Port = 8765,
    [switch]$CheckOnly
)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$env:PYTHONUTF8 = '1'
$env:PYDANTIC_AI_NO_BANNER = '1'
$launchArgs = @('-m', 'tripweave.launcher', '--port', "$Port")
if ($CheckOnly) { $launchArgs += '--check-only' }
& '.\.venv\Scripts\python.exe' @launchArgs
exit $LASTEXITCODE
