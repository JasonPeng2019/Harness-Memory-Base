$ErrorActionPreference = "Stop"
$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = if ($env:MA_HARNESS_PYTHON) { $env:MA_HARNESS_PYTHON } else { "python" }
& $Python (Join-Path $ScriptRoot "scripts/install_product.py") @args
exit $LASTEXITCODE
