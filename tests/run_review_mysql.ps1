$ErrorActionPreference = 'Stop'
$reviewRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $reviewRoot
$reviewData = Join-Path $reviewRoot ('test-mysql-review-' + [guid]::NewGuid().ToString('N'))
# InnoDB skips dot-prefixed directories during tablespace discovery on startup.
# Always initialize a fresh, non-hidden directory; keep previous runs for diagnosis.
Write-Output "Temporary MySQL data and logs: $reviewData"
if (Test-Path -LiteralPath $reviewData) { throw 'Temporary database already exists; do not reuse automatically' }
& 'C:/Program Files/MySQL/MySQL Server 8.0/bin/mysqld.exe' --no-defaults --initialize-insecure "--datadir=$reviewData" --console
if ($LASTEXITCODE -ne 0) { throw 'Temporary MySQL initialization failed' }
$reviewProcess = Start-Process -FilePath 'C:/Program Files/MySQL/MySQL Server 8.0/bin/mysqld.exe' -ArgumentList '--no-defaults',"--datadir=$reviewData",'--port=33379','--bind-address=127.0.0.1','--mysqlx=OFF','--console' -WindowStyle Hidden -PassThru -RedirectStandardError (Join-Path $reviewData 'start.log')
try {
    Start-Sleep -Seconds 5
    $reviewProcess.Refresh()
    if ($reviewProcess.HasExited) { Get-Content (Join-Path $reviewData 'start.log'); throw 'Temporary MySQL startup failed' }
    .\.venv\Scripts\python.exe -m tests.mysql_integration
    if ($LASTEXITCODE -ne 0) { throw 'Existing MySQL integration checks failed' }
    .\.venv\Scripts\python.exe -m tests.mysql_trade_review
    if ($LASTEXITCODE -ne 0) { throw 'Manual refund MySQL integration checks failed' }
    .\.venv\Scripts\python.exe -m tests.mysql_trade_states
    if ($LASTEXITCODE -ne 0) { throw 'Order state MySQL integration checks failed' }
} finally {
    if (-not $reviewProcess.HasExited) {
        & 'C:/Program Files/MySQL/MySQL Server 8.0/bin/mysqladmin.exe' --no-defaults --host=127.0.0.1 --port=33379 --user=root shutdown
    }
}
