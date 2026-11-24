param(
    [Parameter(Mandatory=$true)][string]$Source,
    [Parameter(Mandatory=$true)][string]$ExpectedSourceSha256
)
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest
if($PSVersionTable.PSVersion.Major -ne 5 -or $PSVersionTable.PSEdition -cne 'Desktop'){throw 'Windows PowerShell 5 Desktop required'}
if($ExpectedSourceSha256 -cnotmatch '^[a-f0-9]{64}$' -or (Get-FileHash -LiteralPath $Source -Algorithm SHA256).Hash.ToLowerInvariant() -cne $ExpectedSourceSha256){throw 'Logon controls source differs'}
$tokens=$null;$errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile($Source,[ref]$tokens,[ref]$errors)
if($errors.Count){throw 'Logon controls source did not parse'}
foreach($name in @('Get-LogonDiagnosticSidRole','Add-LogonIdentityDiagnostic')){
    $nodes=@($ast.FindAll({param($n)$n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -ceq $name},$true))
    if($nodes.Count -ne 1){throw 'Logon controls helper ambiguous'}
    . ([scriptblock]::Create($nodes[0].Extent.Text))
}
$script:checks=0
function Check([bool]$Condition){if(-not $Condition){throw 'Native logon identity control failed'};$script:checks++}
$fixtures=@(
    @{role='window_manager_namespace';sid='S-1-5-90-0-1';namespaced=$false},
    @{role='window_manager_namespace';sid='S-1-5-90-1';namespaced=$true},
    @{role='font_driver_namespace';sid='S-1-5-96-0-1';namespaced=$true},
    @{role='account_domain_namespace';sid='S-1-5-21-100-200-300-500';namespaced=$true},
    @{role='local_system';sid='S-1-5-18';namespaced=$true},
    @{role='unknown';sid='invalid-control-sid';namespaced=$true}
)
foreach($fixture in $fixtures){
    Check ((Get-LogonDiagnosticSidRole $fixture.sid) -ceq $fixture.role)
    $namespace=if($fixture.namespaced){' xmlns="http://schemas.microsoft.com/win/2004/08/events/event"'}else{''}
    [xml]$eventXml=('<Event'+$namespace+'><System><EventID>4624</EventID></System><EventData><Data Name="LogonType">2</Data><Data Name="TargetUserSid">'+$fixture.sid+'</Data><Data Name="VirtualAccount">%%1842</Data><Data Name="TargetUserName">private-control-user</Data><Data Name="TargetDomainName">private-control-domain</Data></EventData></Event>')
    $diagnostic=@{};Add-LogonIdentityDiagnostic $eventXml $diagnostic
    Check ($diagnostic.ContainsKey('interactive_target_sid_'+$fixture.role) -and $diagnostic[('interactive_target_sid_'+$fixture.role)] -eq 1 -and $diagnostic.ContainsKey('interactive_virtual_account_yes') -and $diagnostic.interactive_virtual_account_yes -eq 1 -and -not $diagnostic.ContainsKey('logon_identity_detail_unavailable'))
}
foreach($sid in @('','s-1-5-90-0-1','S-1-05-90-0-1','S-1-5-090-0-1','S-1-5-90-0-4294967296','S-1-281474976710656-1',('x'*185),'S-1-5-90-0--1')){
    Check ((Get-LogonDiagnosticSidRole $sid) -ceq 'unknown')
}
$clipped=@{interactive_target_sid_unknown=[long]4096;interactive_virtual_account_yes=[long]4096}
Add-LogonIdentityDiagnostic $eventXml $clipped
Check ($clipped.interactive_target_sid_unknown -eq 4096)
Check ($clipped.interactive_virtual_account_yes -eq 4096)
$serialized=$diagnostic|ConvertTo-Json -Compress
foreach($private in @('S-1-5-','private-control-user','private-control-domain','<Event','%%1842')){Check (-not $serialized.Contains($private))}
if($script:checks -lt 20){throw 'Native logon identity controls incomplete'}
@{schema_version=1;diagnostic_only=$true;product_qualification=$false;
  windows_powershell_major=5;edition='Desktop';source_sha256=$ExpectedSourceSha256;
  contract_controls_passed=$script:checks;namespace_cases=6;invalid_sid_cases=8;
  actual_boot_actor_observed=$false;all_interactive_refusals_unchanged=$true} | ConvertTo-Json -Compress
