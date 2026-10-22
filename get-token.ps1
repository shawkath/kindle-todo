# Gets a TickTick access token for the Kindle dashboard.
# Run in PowerShell:   powershell -ExecutionPolicy Bypass -File .\get-token.ps1
#
# Before running: create an app at https://developer.ticktick.com/manage
# and set its OAuth redirect URL to exactly:  http://localhost

$ErrorActionPreference = "Stop"
$redirect = "http://localhost"

$clientId     = Read-Host "TickTick Client ID"
$clientSecret = Read-Host "TickTick Client Secret"

$authUrl = "https://ticktick.com/oauth/authorize?scope=tasks:read&client_id=$clientId&state=kindle&redirect_uri=$([uri]::EscapeDataString($redirect))&response_type=code"
Write-Host ""
Write-Host "Opening TickTick in your browser. Click Allow."
Write-Host "The next page will fail to load - that's expected."
Write-Host "Copy the FULL address from the browser's address bar (it starts with http://localhost/?code=...)."
Start-Process $authUrl

Write-Host ""
$returned = Read-Host "Paste the full address here"
if ($returned -notmatch "code=([^&]+)") { throw "Couldn't find code= in what you pasted." }
$code = $Matches[1]

$basic = [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes("$($clientId):$($clientSecret)"))
$resp = Invoke-RestMethod -Method Post -Uri "https://ticktick.com/oauth/token" `
    -Headers @{ Authorization = "Basic $basic" } `
    -ContentType "application/x-www-form-urlencoded" `
    -Body @{ code = $code; grant_type = "authorization_code"; scope = "tasks:read"; redirect_uri = $redirect }

$chars = "abcdefghijkmnpqrstuvwxyz23456789".ToCharArray()
$dashPath = -join (1..24 | ForEach-Object { $chars | Get-Random })

Write-Host ""
Write-Host "================= copy these into GitHub secrets ================="
Write-Host "TICKTICK_TOKEN = $($resp.access_token)"
Write-Host "DASH_PATH      = $dashPath"
Write-Host "=================================================================="
if ($resp.expires_in) {
    $days = [math]::Round($resp.expires_in / 86400)
    Write-Host "Token is valid for about $days days. Run this script again when the Kindle says 'Reconnect TickTick'."
}
