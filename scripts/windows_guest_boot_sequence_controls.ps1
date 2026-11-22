# QA-only exact-source controls. ContractOnly runs portable ABI/negative checks;
# the workflow requires NativeWindows before any installer or image download.
param(
    [Parameter(Mandatory=$true)][string]$Source,
    [ValidateSet('NativeWindows','ContractOnly')][string]$Mode='NativeWindows'
)
Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
$tokens=$null;$parseErrors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile((Resolve-Path -LiteralPath $Source).Path,[ref]$tokens,[ref]$parseErrors)
if(@($parseErrors).Count -ne 0){throw 'Witness source parse failed'}
$definitions=@($ast.FindAll({param($node)
    $node -is [Management.Automation.Language.CommandAst] -and $node.GetCommandName() -ceq 'Add-Type'
},$true))
if($definitions.Count -ne 1){throw 'Native witness definition count differs'}
$native=@($definitions[0].CommandElements | Where-Object {
    $_ -is [Management.Automation.Language.StringConstantExpressionAst] -and
    $_.StringConstantType -eq [Management.Automation.Language.StringConstantType]::SingleQuotedHereString -and
    $_.Value.Contains('public static class K5GuestNative')
})
if($native.Count -ne 1){throw 'Literal native witness definition unavailable'}
Add-Type -TypeDefinition $native[0].Value
$functions=@($ast.FindAll({param($node)
    $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -ceq 'Assert-NextBootSequence'
},$true))
if($functions.Count -ne 1){throw 'Native sequence guard unavailable'}
Invoke-Expression $functions[0].Extent.Text
$flags=[Reflection.BindingFlags]'Static,NonPublic'
$context=[K5GuestNative].GetMethod('AssertBootSequenceContext',$flags)
$decode=[K5GuestNative].GetMethod('DecodeBootSequence',$flags)
$read=[K5GuestNative].GetMethod('ReadProcessMemory',$flags)
if($null -eq $context -or $null -eq $decode -or $null -eq $read){throw 'Exact native ABI methods unavailable'}
$readParameters=$read.GetParameters()
$readAttribute=$read.GetCustomAttributes([Runtime.InteropServices.DllImportAttribute],$false)
$returnAttribute=$read.ReturnParameter.GetCustomAttributes([Runtime.InteropServices.MarshalAsAttribute],$false)
if($read.ReturnType -ne [bool] -or $readParameters.Count -ne 5 -or
    $readParameters[0].ParameterType -ne [IntPtr] -or $readParameters[1].ParameterType -ne [IntPtr] -or
    $readParameters[2].ParameterType -ne [byte[]] -or $readParameters[3].ParameterType -ne [UIntPtr] -or
    $readParameters[4].ParameterType -ne [UIntPtr].MakeByRefType() -or -not $readParameters[4].IsOut -or
    $readAttribute.Count -ne 1 -or -not $readAttribute[0].SetLastError -or
    $returnAttribute.Count -ne 1 -or $returnAttribute[0].Value -ne [Runtime.InteropServices.UnmanagedType]::Bool -or
    [K5GuestNative].GetMethod('BootSequence').ReturnType -ne [uint32]){throw 'Native boot read ABI differs'}
$count=0
function Must-Refuse([scriptblock]$Action,[type]$Expected) {
    $refused=$false
    try{& $Action | Out-Null}catch{
        $base=$_.Exception.GetBaseException()
        if($base.GetType() -ne $Expected){throw 'Contract refused for an unexpected reason'}
        $refused=$true
    }
    if(-not $refused){throw 'Negative native boot control was accepted'}
    $script:count++
}
$valid=[object[]]@($true,$true,[int]8,[uint16]0,[uint16]0x8664,[uint32]284,[int]0,[uint32]10,[uint32]0,[uint32]26100)
$context.Invoke($null,$valid) | Out-Null;$count++
foreach($case in @(
    @{index=0;value=$false},@{index=1;value=$false},@{index=2;value=[int]4},
    @{index=3;value=[uint16]0x14c},@{index=4;value=[uint16]0xaa64},
    @{index=5;value=[uint32]280},@{index=6;value=[int]-1},
    @{index=7;value=[uint32]6},@{index=8;value=[uint32]1},@{index=9;value=[uint32]26000}
)){
    $arguments=[object[]]$valid.Clone();$arguments[$case.index]=$case.value
    Must-Refuse {$context.Invoke($null,$arguments)} ([InvalidOperationException])
}
foreach($value in @([uint32]0,[uint32]1,[uint32]2147483647,[uint32]2147483648,[uint32]4294967295)){
    $actual=$decode.Invoke($null,[object[]]@($true,[UIntPtr]::new([uint32]4),[BitConverter]::GetBytes($value)))
    if($actual -isnot [uint32] -or $actual -ne $value){throw 'Native unsigned decode differs'};$count++
}
Must-Refuse {$decode.Invoke($null,[object[]]@($false,[UIntPtr]::new([uint32]4),[byte[]]::new(4)))} ([ComponentModel.Win32Exception])
foreach($length in @(0,3,5)){
    Must-Refuse {$decode.Invoke($null,[object[]]@($true,[UIntPtr]::new([uint32]$length),[byte[]]::new(4)))} ([InvalidOperationException])
}
foreach($bytes in @(@{value=$null},@{value=[byte[]]::new(3)},@{value=[byte[]]::new(5)})){
    Must-Refuse {$decode.Invoke($null,[object[]]@($true,[UIntPtr]::new([uint32]4),$bytes.value))} ([InvalidOperationException])
}
foreach($case in @(@{before=0L;after=1L},@{before=19L;after=20L},@{before=2147483647L;after=2147483648L},@{before=4294967294L;after=4294967295L})){
    Assert-NextBootSequence $case.before $case.after;$count++
}
foreach($case in @(@{before=19L;after=19L},@{before=19L;after=0L},@{before=4294967295L;after=0L},@{before=19L;after=21L},@{before=-1L;after=0L},@{before=4294967295L;after=4294967296L},@{before=0L;after=-1L})){
    Must-Refuse {Assert-NextBootSequence $case.before $case.after} ([Management.Automation.RuntimeException])
}
$nativeObserved=$false;$first=$null;$second=$null
if($Mode -ceq 'NativeWindows'){
    $watch=[Diagnostics.Stopwatch]::StartNew()
    $first=[K5GuestNative]::BootSequence();$second=[K5GuestNative]::BootSequence()
    if($watch.Elapsed.TotalSeconds -ge 2 -or $first -isnot [uint32] -or $first -ne $second){throw 'Native same-boot read smoke failed'}
    $nativeObserved=$true
}
@{schema_version=1;diagnostic_only=$true;product_qualification=$false;contract_controls_passed=$count;
    source_sha256=(Get-FileHash -LiteralPath $Source -Algorithm SHA256).Hash.ToLowerInvariant();
    vendor_wdk_header_sha256='2d82908d3c5cfda7214a1d299dc730fd4863c090df7d146a62b0f7fb080320f7';
    vendor_sdk_mapping_header_sha256='832f26c59686cf6864da83681f97fc6fee3a7006399c48efa777d4330a499efb';
    native_same_boot_read_observed=$nativeObserved;boot_sequence_first=$first;boot_sequence_second=$second;
    actual_guest_reboot_observed=$false} | ConvertTo-Json -Compress
