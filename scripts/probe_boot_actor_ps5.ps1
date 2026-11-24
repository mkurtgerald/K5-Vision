param(
    [Parameter(Mandatory=$true)][string]$WitnessPath,
    [Parameter(Mandatory=$true)][string]$OutputPath
)
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest
$expected='52a7c3fd246993b85d21f3698b4f21ec7d05432855e93cd398e78b963164a0e5'
if($PSVersionTable.PSVersion.Major -ne 5 -or $PSVersionTable.PSEdition -cne 'Desktop'){throw 'Exact Windows PowerShell 5 Desktop required'}
if((Get-FileHash -LiteralPath $WitnessPath -Algorithm SHA256).Hash.ToLowerInvariant() -cne $expected){throw 'Exact witness Git blob required'}
$tokens=$null;$errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile($WitnessPath,[ref]$tokens,[ref]$errors)
if($errors.Count){throw 'Witness parser refused'}
$functions=@{}
foreach($name in @('Get-LogonDiagnosticSidRole','Add-LogonIdentityDiagnostic')){
    $nodes=@($ast.FindAll({param($n)$n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -ceq $name},$true))
    if($nodes.Count -ne 1){throw 'Witness helper selection ambiguous'}
    $functions[$name]=$nodes[0].Extent.Text
    . ([scriptblock]::Create($functions[$name]))
}
# Fake fixtures only: an otherwise identical clone exposes the caught failure's
# finite class/relative line. The unchanged production helper runs first.
$original=[string]$functions['Add-LogonIdentityDiagnostic']
$needle='}catch{$Diagnostic.logon_identity_detail_unavailable=$true}'
if(($original.Length-$original.Replace($needle,'').Length) -ne $needle.Length){throw 'Exact helper containment catch unavailable'}
$diagnostic=$original.Replace('function Add-LogonIdentityDiagnostic','function Invoke-ControlledLogonIdentity').Replace($needle,'}catch{throw}')
. ([scriptblock]::Create($diagnostic))
$originalRole=[string]$functions['Get-LogonDiagnosticSidRole']
if(($originalRole.Length-$originalRole.Replace('[ulong]','').Length) -ne 14){throw 'Expected two unsigned aliases unavailable'}
$canonicalRole=$originalRole.Replace('function Get-LogonDiagnosticSidRole','function Get-CanonicalLogonDiagnosticSidRole').Replace('[ulong]','[uint64]')
$canonical=$original.Replace('function Add-LogonIdentityDiagnostic','function Invoke-CanonicalLogonIdentity').Replace('Get-LogonDiagnosticSidRole','Get-CanonicalLogonDiagnosticSidRole').Replace($needle,'}catch{throw}')
. ([scriptblock]::Create($canonicalRole))
. ([scriptblock]::Create($canonical))
function Finite-Error($ErrorRecord){
    $kind=$ErrorRecord.Exception.GetType().Name
    if($kind -notin @('RuntimeException','MethodException','MethodInvocationException','PropertyNotFoundException','PSInvalidOperationException','InvalidOperationException','ArgumentException','ParameterBindingException')){$kind='Other'}
    $result=@{kind=$kind;hresult=[int]$ErrorRecord.Exception.HResult}
    $line=[int]$ErrorRecord.InvocationInfo.ScriptLineNumber
    if($line -ge 1 -and $line -le 128){$result.relative_line=$line}
    $inner=$ErrorRecord.Exception.InnerException
    if($null -ne $inner){
        $innerKind=$inner.GetType().Name
        if($innerKind -notin @('InvalidOperationException','ArgumentException','FormatException','OverflowException','MissingMethodException','NotSupportedException')){$innerKind='Other'}
        $result.inner_kind=$innerKind;$result.inner_hresult=[int]$inner.HResult
    }
    return $result
}
function Public-Counts([hashtable]$Value){
    $copy=@{}
    foreach($name in @('interactive_target_sid_window_manager_namespace','interactive_target_sid_font_driver_namespace','interactive_target_sid_account_domain_namespace','interactive_target_sid_local_system','interactive_target_sid_other','interactive_target_sid_unknown','interactive_virtual_account_yes','interactive_virtual_account_no','interactive_virtual_account_unknown')){
        if($Value.ContainsKey($name)){
            $number=$Value[$name]
            if(($number -is [int] -or $number -is [long]) -and $number -ge 0 -and $number -le 4096){$copy[$name]=[long]$number}
        }
    }
    if($Value.ContainsKey('logon_identity_detail_unavailable') -and $Value.logon_identity_detail_unavailable -is [bool]){$copy.logon_identity_detail_unavailable=$Value.logon_identity_detail_unavailable}
    return $copy
}
$fixtures=@(
    @{name='window_manager';sid='S-1-5-90-0-1';virtual='%%1842';namespaced=$false},
    @{name='window_manager_namespaced';sid='S-1-5-90-0-1';virtual='%%1842';namespaced=$true},
    @{name='font_driver';sid='S-1-5-96-0-1';virtual='%%1842';namespaced=$true},
    @{name='account_domain';sid='S-1-5-21-100-200-300-500';virtual='%%1843';namespaced=$true},
    @{name='local_system';sid='S-1-5-18';virtual='%%1843';namespaced=$true},
    @{name='unknown';sid='not-a-valid-sid';virtual='not-a-virtual-flag';namespaced=$true}
)
$cases=@()
foreach($fixture in $fixtures){
    $namespace=if($fixture.namespaced){' xmlns="http://schemas.microsoft.com/win/2004/08/events/event"'}else{''}
    [xml]$eventXml=('<Event'+$namespace+'><System><EventID>4624</EventID><EventRecordID>1</EventRecordID></System><EventData><Data Name="LogonType">2</Data><Data Name="TargetUserSid">'+$fixture.sid+'</Data><Data Name="VirtualAccount">'+$fixture.virtual+'</Data><Data Name="TargetUserName">fixture-private-user</Data><Data Name="TargetDomainName">fixture-private-domain</Data></EventData></Event>')
    $result=@{fixture=$fixture.name}
    try{
        $role=Get-LogonDiagnosticSidRole ([string]$fixture.sid)
        $result.sid_role=if($role -in @('window_manager_namespace','font_driver_namespace','account_domain_namespace','local_system','other','unknown')){$role}else{'invalid_projection'}
    }catch{$result.sid_role_error=Finite-Error $_}
    $untouched=@{}
    Add-LogonIdentityDiagnostic $eventXml $untouched
    $result.original=Public-Counts $untouched
    $controlled=@{}
    try{Invoke-ControlledLogonIdentity $eventXml $controlled;$result.controlled_succeeded=$true}catch{$result.controlled_succeeded=$false;$result.controlled_error=Finite-Error $_}
    $result.controlled=Public-Counts $controlled
    $canonicalCounts=@{}
    try{Invoke-CanonicalLogonIdentity $eventXml $canonicalCounts;$result.canonical_succeeded=$true}catch{$result.canonical_succeeded=$false;$result.canonical_error=Finite-Error $_}
    $result.canonical=Public-Counts $canonicalCounts
    $cases+=@($result)
}
$receipt=@{
    schema_version=1;diagnostic_only=$true;product_qualification=$false
    status='controlled_helper_observed';windows_powershell_major=5;edition='Desktop'
    witness_sha256=$expected;source_head='a300b80755f46a9f0dafd24e09a39af08296fc99'
    harness_head=$env:GITHUB_SHA;case_count=$cases.Count;cases=$cases
    unsigned_alias_available=($null -ne ('ulong' -as [type]))
    canonical_uint64_available=($null -ne ('uint64' -as [type]))
    virtual_machine_created=$false;actual_boot_actor_observed=$false
}
$serialized=$receipt|ConvertTo-Json -Depth 8 -Compress
foreach($secret in @('S-1-5-','fixture-private-user','fixture-private-domain','not-a-valid-sid','<Event','%%1842','%%1843')){
    if($serialized.Contains($secret)){throw 'Controlled diagnostic privacy projection failed'}
}
if($cases.Count -ne 6){throw 'Controlled fixture count differs'}
[IO.File]::WriteAllText($OutputPath,$serialized,[Text.UTF8Encoding]::new($false))
Write-Output $serialized
