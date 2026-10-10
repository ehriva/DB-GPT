[CmdletBinding(SupportsShouldProcess = $true)]
param()

$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$statusFile = Join-Path $projectRoot '.cache\lan-access-status.json'
$ruleName = 'DBGPT-WiFi-Trial-5670'

try {
    $wifi = @(Get-NetIPAddress -InterfaceAlias 'WLAN' -AddressFamily IPv4 |
        Where-Object { $_.IPAddress -notlike '169.254.*' })
    if ($wifi.Count -ne 1) {
        throw 'Expected one active IPv4 address on WLAN.'
    }
    $address = $wifi[0].IPAddress
    $prefix = [int]$wifi[0].PrefixLength
    $networkBytes = [Net.IPAddress]::Parse($address).GetAddressBytes()
    for ($i = 0; $i -lt 4; $i++) {
        $bits = [Math]::Min(8, [Math]::Max(0, $prefix - 8 * $i))
        $mask = if ($bits -eq 0) { 0 } else { (255 -shl (8 - $bits)) -band 255 }
        $networkBytes[$i] = $networkBytes[$i] -band $mask
    }
    $subnet = ([Net.IPAddress]::new($networkBytes)).IPAddressToString + '/' + $prefix
    $listener = @(Get-NetTCPConnection -LocalPort 5670 -State Listen |
        Where-Object { $_.LocalAddress -in @('0.0.0.0', $address) })
    if ($listener.Count -ne 1) {
        throw 'DB-GPT must already listen on IPv4 port 5670.'
    }
    $program = (Get-Process -Id $listener[0].OwningProcess).Path
    if (-not $program.StartsWith($projectRoot + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw 'The listening program does not belong to this project.'
    }
    $health = Invoke-RestMethod 'http://127.0.0.1:5670/api/health' -TimeoutSec 10
    if ($health.status -ne 'ok') {
        throw 'DB-GPT health check failed.'
    }

    $ruleParams = @{
        Name = $ruleName
        Direction = 'Inbound'
        Action = 'Allow'
        Enabled = 'True'
        Profile = 'Any'
        InterfaceAlias = 'WLAN'
        Protocol = 'TCP'
        LocalPort = 5670
        LocalAddress = $address
        RemoteAddress = $subnet
        Program = $program
        EdgeTraversalPolicy = 'Block'
    }
    $applied = $false
    if ($PSCmdlet.ShouldProcess("$address`:5670 from $subnet on WLAN", 'Allow DB-GPT Wi-Fi access')) {
        $principal = [Security.Principal.WindowsPrincipal]::new(
            [Security.Principal.WindowsIdentity]::GetCurrent())
        if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
            throw 'Run this script from an Administrator PowerShell window.'
        }
        $existing = Get-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue
        if ($existing) {
            if ($existing.DisplayName -ne 'DB-GPT Wi-Fi trial TCP 5670') {
                throw 'A rule with this name already exists and is not managed by this script.'
            }
            Set-NetFirewallRule @ruleParams | Out-Null
        } else {
            New-NetFirewallRule @ruleParams -DisplayName 'DB-GPT Wi-Fi trial TCP 5670' | Out-Null
        }
        $health = Invoke-RestMethod "http://$address`:5670/api/health" -TimeoutSec 10
        if ($health.status -ne 'ok') {
            throw 'The LAN-address health check failed.'
        }
        $applied = $true
    }
    $result = [ordered]@{
        success = $true
        applied = $applied
        url = "http://$address`:5670"
        subnet = $subnet
        interface = 'WLAN'
        program = $program
        rule = $ruleName
    }
    $result | ConvertTo-Json | Set-Content -LiteralPath $statusFile -Encoding UTF8
    $result | ConvertTo-Json
} catch {
    @{ success = $false; applied = $false; error = $_.Exception.Message } |
        ConvertTo-Json | Set-Content -LiteralPath $statusFile -Encoding UTF8
    throw
}
