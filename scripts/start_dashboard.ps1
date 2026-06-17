# The dashboard is served by the API (apps/api serves apps/dashboard/index.html on /).
# This script just opens the dashboard in your browser. The API must already be running.

$port = if ($env:API_PORT) { $env:API_PORT } else { "8001" }
$url  = "http://127.0.0.1:{0}/" -f $port

Start-Process $url
Write-Host ("Opened {0} - this is the dashboard." -f $url) -ForegroundColor Cyan
