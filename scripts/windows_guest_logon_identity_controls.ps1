param(
    [Parameter(Mandatory=$true)][string]$Source,
    [Parameter(Mandatory=$true)][string]$ExpectedSourceSha256,
    [Parameter(Mandatory=$true)][string]$ControllerSource,
    [Parameter(Mandatory=$true)][string]$ExpectedControllerSha256
)
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest
if($PSVersionTable.PSVersion.Major -ne 5 -or $PSVersionTable.PSEdition -cne 'Desktop'){throw 'Windows PowerShell 5 Desktop required'}
if($ExpectedSourceSha256 -cnotmatch '^[a-f0-9]{64}$' -or (Get-FileHash -LiteralPath $Source -Algorithm SHA256).Hash.ToLowerInvariant() -cne $ExpectedSourceSha256){throw 'Logon controls source differs'}
$tokens=$null;$errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile($Source,[ref]$tokens,[ref]$errors)
if($errors.Count){throw 'Logon controls source did not parse'}
foreach($name in @('Get-LogonDiagnosticSidRole','Add-LogonIdentityDiagnostic','Get-ReservedOsVirtualLogonRole')){
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
function Fixture([string]$Sid,[string]$Type='2',[string]$Virtual='%%1842',[bool]$Namespaced=$true){
    $ns=if($Namespaced){' xmlns="http://schemas.microsoft.com/win/2004/08/events/event"'}else{''}
    return [xml]('<Event'+$ns+'><System><EventID>4624</EventID></System><EventData><Data Name="LogonType">'+$Type+'</Data><Data Name="TargetUserSid">'+$Sid+'</Data><Data Name="VirtualAccount">'+$Virtual+'</Data><Data Name="TargetUserName">DWM-1</Data></EventData></Event>')
}
$classificationStart=$script:checks
foreach($ns in @($true,$false)){
    foreach($rid in @('0','1','4294967295')){
        Check ((Get-ReservedOsVirtualLogonRole (Fixture ('S-1-5-90-0-'+$rid) '2' '%%1842' $ns)) -ceq 'window_manager_namespace')
        Check ((Get-ReservedOsVirtualLogonRole (Fixture ('S-1-5-96-0-'+$rid) '2' '%%1842' $ns)) -ceq 'font_driver_namespace')
    }
}
foreach($sid in @('','S-1-5-90-1','S-1-5-96-1','S-1-5-21-100-200-300-500','S-1-5-18','S-1-5-80-1','S-1-5-90-1-1','S-1-5-96-1-1','S-1-5-90-0-01','S-1-5-96-0-01','S-1-5-90-0-4294967296','S-1-5-96-0-4294967296','S-1-5-90-0-1-1','s-1-5-90-0-1','S-1-05-90-0-1','S-1-5-090-0-1',' S-1-5-90-0-1','S-1-5-90-0-1 ',('x'*185))){
    Check ((Get-ReservedOsVirtualLogonRole (Fixture $sid)) -ceq 'unclassified')
}
foreach($type in @('7','10','11','12','13','3','4','5','8','9','02','+2',' 2','2 ','','x')){
    foreach($sid in @('S-1-5-90-0-1','S-1-5-96-0-1')){Check ((Get-ReservedOsVirtualLogonRole (Fixture $sid $type)) -ceq 'unclassified')}
}
foreach($virtual in @('%%1843','%%1841','true','yes','1','',' %%1842','%%1842 ')){
    foreach($sid in @('S-1-5-90-0-1','S-1-5-96-0-1')){Check ((Get-ReservedOsVirtualLogonRole (Fixture $sid '2' $virtual)) -ceq 'unclassified')}
}
foreach($field in @('LogonType','TargetUserSid','VirtualAccount')){
    foreach($mutation in @('missing','duplicate','nested','comment')){
        $xml=Fixture 'S-1-5-90-0-1'
        $node=@($xml.Event.EventData.Data|Where-Object{$_.Name -ceq $field})[0]
        if($mutation -ceq 'missing'){$null=$node.ParentNode.RemoveChild($node)}
        elseif($mutation -ceq 'duplicate'){$null=$node.ParentNode.AppendChild($node.CloneNode($true))}
        elseif($mutation -ceq 'nested'){$node.InnerXml='<nested>'+[Security.SecurityElement]::Escape($node.InnerText)+'</nested>'}
        else{$null=$node.AppendChild($xml.CreateComment('private-control-comment'))}
        Check ((Get-ReservedOsVirtualLogonRole $xml) -ceq 'unclassified')
    }
}
Check ((Get-ReservedOsVirtualLogonRole $null) -ceq 'unclassified')
$classificationCases=$script:checks-$classificationStart
if($ExpectedControllerSha256 -cnotmatch '^[a-f0-9]{64}$' -or (Get-FileHash -LiteralPath $ControllerSource -Algorithm SHA256).Hash.ToLowerInvariant() -cne $ExpectedControllerSha256){throw 'Logon census controller source differs'}
$controllerAst=[Management.Automation.Language.Parser]::ParseFile($ControllerSource,[ref]$tokens,[ref]$errors)
if($errors.Count){throw 'Logon census controller did not parse'}
$nodes=@($controllerAst.FindAll({param($n)$n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -ceq 'Assert-GuestLogonCensus'},$true))
if($nodes.Count -ne 1){throw 'Logon census helper ambiguous'}
. ([scriptblock]::Create($nodes[0].Extent.Text))
function Census {return [pscustomobject]@{successful_interactive_type_logons_since_checkpoint=4;human_or_unclassified_interactive_logons_since_checkpoint=0;reserved_os_virtual_logons_since_checkpoint=4;reserved_window_manager_logons_since_checkpoint=2;reserved_font_driver_logons_since_checkpoint=2}}
function Refused($Value,[bool]$Planned=$true){$denied=$false;try{Assert-GuestLogonCensus $Value $Planned}catch{$denied=$true};Check $denied}
$censusStart=$script:checks
$census=Census;Assert-GuestLogonCensus $census $true;Check $true
Refused (Census) $false
$census=Census;foreach($name in @($census.PSObject.Properties.Name)){$census.$name=0};Assert-GuestLogonCensus $census $false;Check $true
foreach($field in @((Census).PSObject.Properties.Name)){
    foreach($invalid in @(-1,4097,1.5,'0',$true,$null)){$census=Census;$census.$field=$invalid;Refused $census}
    $census=Census;$census.PSObject.Properties.Remove($field);Refused $census
}
foreach($field in @('human_or_unclassified_interactive_logons_since_checkpoint','reserved_os_virtual_logons_since_checkpoint','reserved_window_manager_logons_since_checkpoint','reserved_font_driver_logons_since_checkpoint','successful_interactive_type_logons_since_checkpoint')){
    $census=Census;$census.$field=1;Refused $census
}
foreach($old in @('interactive_logons_since_boot','startup_before_interactive_login','security_all_post_off_shutdown_loss_policy_and_interactive_events_refused')){
    $census=Census;$census|Add-Member -NotePropertyName $old -NotePropertyValue $false;Refused $census
}
$censusCases=$script:checks-$censusStart
if($script:checks -ne 165 -or $classificationCases -ne 92 -or $censusCases -ne 46){throw 'Native logon identity controls incomplete'}
@{schema_version=1;diagnostic_only=$true;product_qualification=$false;
  windows_powershell_major=5;edition='Desktop';source_sha256=$ExpectedSourceSha256;
  contract_controls_passed=$script:checks;namespace_cases=6;invalid_sid_cases=8;
  reserved_os_classification_cases=$classificationCases;host_census_cases=$censusCases;
  controller_source_sha256=$ExpectedControllerSha256;actual_boot_actor_observed=$false;
  human_or_unclassified_interactive_refusals_preserved=$true;
  planned_reserved_os_type2_classification_controls_passed=$true;
  hard_reset_exemption_enabled=$false} | ConvertTo-Json -Compress
