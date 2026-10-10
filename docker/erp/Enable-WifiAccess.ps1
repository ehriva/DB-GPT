[CmdletBinding()]
param(
    # WLAN address of this host and the subnet allowed to reach port 5670.
    # Override both when the Wi-Fi environment changes; defaults match the
    # original deployment. The "block outside subnet" ranges are derived from
    # -WlanSubnetCidr, so the two parameters must stay consistent.
    [string]$WlanIpAddress = '192.168.3.11',
    [string]$WlanSubnetCidr = '192.168.3.0/24'
)

$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$statusFile = Join-Path $projectRoot '.cache\docker-wifi-firewall.json'
$ruleName = 'DBGPT-Docker-WLAN-5670'

function ConvertTo-UInt32([string]$Ip) {
    $bytes = [IPAddress]::Parse($Ip).GetAddressBytes()
    return [uint32]((($bytes[0] * 16777216) + ($bytes[1] * 65536)) + (($bytes[2] * 256) + $bytes[3]))
}

function ConvertTo-IpString([uint32]$Value) {
    return '{0}.{1}.{2}.{3}' -f ((($Value / 16777216) % 256) -as [int]),
        ((($Value / 65536) % 256) -as [int]),
        ((($Value / 256) % 256) -as [int]),
        ($Value % 256)
}

try {
    $principal = [Security.Principal.WindowsPrincipal]::new(
        [Security.Principal.WindowsIdentity]::GetCurrent())
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'Run this script from an Administrator PowerShell window.'
    }
    $subnetParts = $WlanSubnetCidr.Split('/')
    if ($subnetParts.Count -ne 2) {
        throw "Invalid -WlanSubnetCidr '$WlanSubnetCidr'; expected an a.b.c.d/prefix form."
    }
    $prefixLength = $subnetParts[1] -as [int]
    if (($null -eq $prefixLength) -or ($prefixLength -lt 0) -or ($prefixLength -gt 32)) {
        throw "Invalid prefix length in -WlanSubnetCidr '$WlanSubnetCidr'."
    }
    $networkUInt = (ConvertTo-UInt32 $subnetParts[0]) -band ([uint32](4294967295 -shl (32 - $prefixLength)))
    $broadcastUInt = $networkUInt + [uint32]([math]::Pow(2, 32 - $prefixLength) - 1)
    if ($networkUInt -gt 0) {
        $belowSubnetRange = "0.0.0.0-$(ConvertTo-IpString ($networkUInt - 1))"
    }
    if ($broadcastUInt -lt [uint32]::MaxValue) {
        $aboveSubnetRange = "$(ConvertTo-IpString ($broadcastUInt + 1))-255.255.255.255"
    }
    $wifi = Get-NetIPAddress -InterfaceAlias 'WLAN' -AddressFamily IPv4 |
        Where-Object { $_.IPAddress -eq $WlanIpAddress -and $_.PrefixLength -eq $prefixLength }
    if (-not $wifi) {
        throw 'WLAN address changed. Review the Compose mapping and firewall scope first.'
    }
    $profile = Get-NetConnectionProfile -InterfaceAlias 'WLAN'
    if ($profile.NetworkCategory -ne 'Public') {
        throw 'WLAN profile changed. Review the firewall profile first.'
    }
    $params = @{
        Name = $ruleName
        Direction = 'Inbound'
        Action = 'Allow'
        Enabled = 'True'
        Profile = 'Public'
        InterfaceAlias = 'WLAN'
        Protocol = 'TCP'
        LocalAddress = $WlanIpAddress
        LocalPort = 5670
        RemoteAddress = $WlanSubnetCidr
        EdgeTraversalPolicy = 'Block'
    }
    $existing = Get-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue
    if ($existing) {
        if ($existing.DisplayName -ne 'DB-GPT Docker Wi-Fi TCP 5670') {
            throw 'The rule name is already used by an unrelated rule.'
        }
        Set-NetFirewallRule @params | Out-Null
    } else {
        New-NetFirewallRule @params -DisplayName 'DB-GPT Docker Wi-Fi TCP 5670' | Out-Null
    }
    # Docker Desktop already allows its backend from any source. A scoped block
    # takes precedence without changing Docker's rules for other containers.
    $blockParams = $params.Clone()
    $blockParams.Name = "$ruleName-OutsideSubnet"
    $blockParams.Action = 'Block'
    $blockParams.Profile = 'Any'
    $blockParams.RemoteAddress = @($belowSubnetRange, $aboveSubnetRange)
    $existingBlock = Get-NetFirewallRule -Name $blockParams.Name -ErrorAction SilentlyContinue
    if ($existingBlock) {
        if ($existingBlock.DisplayName -ne 'DB-GPT Docker block outside Wi-Fi subnet') {
            throw 'The block rule name is already used by an unrelated rule.'
        }
        Set-NetFirewallRule @blockParams | Out-Null
    } else {
        New-NetFirewallRule @blockParams -DisplayName 'DB-GPT Docker block outside Wi-Fi subnet' | Out-Null
    }
    $rule = Get-NetFirewallRule -Name $ruleName
    $blockRule = Get-NetFirewallRule -Name $blockParams.Name
    $result = [ordered]@{
        success = $true
        rule = $rule | Select-Object Name, Enabled, Profile, Direction, Action
        address = $rule | Get-NetFirewallAddressFilter | Select-Object LocalAddress, RemoteAddress
        port = $rule | Get-NetFirewallPortFilter | Select-Object Protocol, LocalPort
        interface = $rule | Get-NetFirewallInterfaceFilter | Select-Object InterfaceAlias
        outsideSubnetBlock = $blockRule | Select-Object Name, Enabled, Profile, Action
        blockedAddresses = $blockRule | Get-NetFirewallAddressFilter | Select-Object LocalAddress, RemoteAddress
    }
    $result | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $statusFile -Encoding UTF8
} catch {
    @{ success = $false; error = $_.Exception.Message } |
        ConvertTo-Json | Set-Content -LiteralPath $statusFile -Encoding UTF8
    throw
}
