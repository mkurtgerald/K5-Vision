# Diagnostic installer-specific guest proof. The outer runner never reboots.
# Only public fixture data and a one-time guest setup credential enter the VM;
# Actions credentials remain exclusively in the outer runner's environment.
param(
    [Parameter(Mandatory=$true)][string]$SetupPath,
    [Parameter(Mandatory=$true)][ValidatePattern('^[a-f0-9]{40}$')][string]$ProductHead,
    [Parameter(Mandatory=$true)][ValidatePattern('^[a-f0-9]{64}$')][string]$SetupSha256,
    [Parameter(Mandatory=$true)][string]$BootstrapExe,
    [Parameter(Mandatory=$true)][string]$MediaMTX,
    [Parameter(Mandatory=$true)][string]$FFmpeg,
    [Parameter(Mandatory=$true)][string]$FFprobe,
    [switch]$BootstrapObservation
)
Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'
$VerbosePreference='SilentlyContinue'
$WarningPreference='SilentlyContinue'
$InformationPreference='SilentlyContinue'
$imageSha='2d175924c8e647969a82e36f931b22397108bd94a030e0e947b7e66e47e0be9a'
$imageBytes=[long]11686379520
$transfer=[guid]::NewGuid().ToString()
$receipt=[ordered]@{
    schema_version=1; diagnostic_only=$true; product_qualification=$false;
    status='diagnostic_complete'; outcome='unavailable'; transfer_id=$transfer;
    product_head=$ProductHead; installer_sha256=$SetupSha256; image_sha256=$imageSha;
    actual_guest_boot=$false; recording_after_reboot_before_login=$false;
    recording_after_actual_signout=$false; retained_media_decoded=$false;
    github_credentials_entered_guest=$false; cleanup_complete=$true; stage='host_guard';
    bootstrap_observation_only=$BootstrapObservation.IsPresent
}
$root=$null; $rootIdentity=$null; $image=$null; $mounted=$false; $diskNumber=$null; $diskId=$null
$partitionNumber=$null; $accessPath=$null; $vmId=$null; $vmName=$null; $switchId=$null; $switchName=$null
$hostInterface=$null; $hostIPAddress=$null; $firewallName=$null; $direct=$null; $password=$null; $credential=$null
$mediaProcess=$null; $publisherProcess=$null
$processNotes=@{}
Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
public sealed class K5GuestProcessNotes {
  readonly object gate=new object(); readonly StringBuilder text=new StringBuilder();
  public K5GuestProcessNotes(Process process){process.OutputDataReceived+=Receive;process.ErrorDataReceived+=Receive;}
  void Receive(object sender,DataReceivedEventArgs data){
    if(data.Data==null)return;
    lock(gate){int count=Math.Min(data.Data.Length,65536-text.Length);if(count>0)text.Append(data.Data,0,count);if(text.Length<65536)text.Append('\n');}
  }
  public int CapturedCharacters {get{lock(gate){return text.Length;}}}
  public string[] Flags(){
    string value;lock(gate){value=text.ToString().ToLowerInvariant();}
    var flags=new List<string>();
    if(value.Contains("bind:")||value.Contains("listen tcp"))flags.Add("socket_bind_error");
    if(value.Contains("cannot assign requested address")||value.Contains("address is not valid in its context"))flags.Add("bind_address_unavailable");
    if(value.Contains("address already in use")||value.Contains("only one usage of each socket address"))flags.Add("bind_address_in_use");
    if(value.Contains("forbidden by its access permissions")||value.Contains("permission denied")||value.Contains("access is denied"))flags.Add("access_refused");
    if(value.Contains("unknown field"))flags.Add("unknown_configuration_field");
    if(value.Contains("unmarshal")||value.Contains("yaml:")||value.Contains("invalid configuration"))flags.Add("configuration_parse_refused");
    if(value.Contains("no such file")||value.Contains("cannot find the file")||value.Contains("cannot find the path"))flags.Add("configuration_file_missing");
    return flags.ToArray();
  }
}
public sealed class K5GuestBoundedCopy {
  long received;
  public long Received {get{return Interlocked.Read(ref received);}}
  public async Task CopyAndCancel(Stream source,Stream destination,long expected,CancellationTokenSource cancellation){
    try{await Copy(source,destination,expected,cancellation.Token).ConfigureAwait(false);}
    catch{cancellation.Cancel();throw;}
  }
  public async Task Copy(Stream source,Stream destination,long expected,CancellationToken cancellation){
    byte[] buffer=new byte[1048576];
    while(true){
      cancellation.ThrowIfCancellationRequested();
      int count=await source.ReadAsync(buffer,0,buffer.Length,cancellation).ConfigureAwait(false);
      if(count==0)break;
      if(Interlocked.Add(ref received,count)>expected)throw new InvalidDataException("Bounded image length exceeded");
      await destination.WriteAsync(buffer,0,count,cancellation).ConfigureAwait(false);
    }
    if(Received!=expected)throw new EndOfStreamException("Bounded image incomplete");
    await destination.FlushAsync(cancellation).ConfigureAwait(false);
  }
}
'@
function Assert-ImageRangeResponse($Response,[long]$Start,[long]$End,[long]$Total,[string]$ETag) {
    $range=$Response.Content.Headers.ContentRange
    if([int]$Response.StatusCode -ne 206 -or $null -eq $range -or -not $range.HasRange -or -not $range.HasLength -or
       $range.Unit -cne 'bytes' -or $range.From -ne $Start -or $range.To -ne $End -or $range.Length -ne $Total -or
       $Response.Content.Headers.ContentLength -ne ($End-$Start+1)){throw 'Official image range response differs'}
    $current=[string]$Response.Headers.ETag
    if($current.Length -gt 256 -or ($ETag.Length -gt 0 -and $current.Length -gt 0 -and $current -cne $ETag)){throw 'Official image range ETag differs'}
    return $current
}
function Assert-RangeContracts {
    $header=@{StatusCode=206;Content=@{Headers=@{ContentLength=[long]3;ContentRange=@{HasRange=$true;HasLength=$true;Unit='bytes';From=[long]2921594880;To=[long]2921594882;Length=[long]11686379520}}};Headers=@{ETag='"fixture"'}}
    $null=Assert-ImageRangeResponse $header 2921594880 2921594882 11686379520 '"fixture"'
    foreach($case in @('status','from','to','total','length','missing_range','etag')){
        $copy=$header|ConvertTo-Json -Depth 8|ConvertFrom-Json
        switch($case){'status'{$copy.StatusCode=200};'from'{$copy.Content.Headers.ContentRange.From=0};'to'{$copy.Content.Headers.ContentRange.To=2921594883};'total'{$copy.Content.Headers.ContentRange.Length=11686379521};'length'{$copy.Content.Headers.ContentLength=4};'missing_range'{$copy.Content.Headers.ContentRange.HasRange=$false};'etag'{$copy.Headers.ETag='"different"'}}
        $refused=$false;try{$null=Assert-ImageRangeResponse $copy 2921594880 2921594882 11686379520 '"fixture"'}catch{$refused=$true}
        if(-not $refused){throw 'Official range header negative contract failed'}
    }
    foreach($case in @('truncated','oversized','cancelled')){
        $cancelRange=[Threading.CancellationTokenSource]::new();$sourceRange=[IO.MemoryStream]::new([byte[]]@(1,2,3));$destinationRange=[IO.MemoryStream]::new()
        try{
            $expected=if($case -ceq 'truncated'){4}elseif($case -ceq 'oversized'){2}else{3}
            if($case -ceq 'cancelled'){$cancelRange.Cancel()}
            $refused=$false
            try{[void][K5GuestBoundedCopy]::new().CopyAndCancel($sourceRange,$destinationRange,$expected,$cancelRange).GetAwaiter().GetResult()}catch{$refused=$true}
            if(-not $refused -or -not $cancelRange.IsCancellationRequested -or $destinationRange.Length -gt $expected){throw 'Official range bounded cancellation contract failed'}
        }finally{$sourceRange.Dispose();$destinationRange.Dispose();$cancelRange.Dispose()}
    }
}
function Private-Directory([string]$Path,[bool]$Guest=$false) {
    $directory=New-Item -ItemType Directory -Path $Path
    $security=[Security.AccessControl.DirectorySecurity]::new()
    $security.SetAccessRuleProtection($true,$false)
    $security.SetOwner([Security.Principal.SecurityIdentifier]::new('S-1-5-32-544'))
    $sids=@('S-1-5-18','S-1-5-32-544')
    if(-not $Guest){$sids += [Security.Principal.WindowsIdentity]::GetCurrent().User.Value}
    foreach($sid in @($sids|Select-Object -Unique)){
        $security.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new([Security.Principal.SecurityIdentifier]::new($sid),'FullControl','ContainerInherit,ObjectInherit','None','Allow'))
    }
    Set-Acl -LiteralPath $Path -AclObject $security
    return $directory
}
function Assert-Root {
    $current=Get-Item -LiteralPath $root -Force
    if($current.FullName -cne $rootIdentity.FullName -or $current.CreationTimeUtc.Ticks -ne $rootIdentity.CreationTimeUtc.Ticks -or
       ($current.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or $current.Name -cnotmatch '^K5GuestRecording-[a-f0-9]{32}$'){throw 'Owned guest root changed'}
}
function Owned-VM {
    Assert-Root
    $found=Get-VM -Id $vmId
    if($found.Id -ne $vmId -or $found.Name -cne $vmName -or -not [IO.Path]::GetFullPath($found.ConfigurationLocation).StartsWith($root+'\',[StringComparison]::OrdinalIgnoreCase)){throw 'Owned VM changed'}
    return $found
}
function Bounded-Job($Job,[int]$Seconds){
    try{
        if($null -eq (Wait-Job -Job $Job -Timeout $Seconds)){throw [TimeoutException]::new('Owned VM operation expired')}
        Receive-Job -Job $Job -ErrorAction Stop
    }finally{
        if($Job.State -in @('Running','NotStarted','Blocked')){Stop-Job -Job $Job -ErrorAction SilentlyContinue}
        Remove-Job -Job $Job -Force -ErrorAction SilentlyContinue
    }
}
function Start-OwnedProcess([string]$Executable,[string]$Arguments){
    $info=[Diagnostics.ProcessStartInfo]::new($Executable,$Arguments)
    $info.UseShellExecute=$false; $info.CreateNoWindow=$true
    $info.RedirectStandardOutput=$true; $info.RedirectStandardError=$true
    $process=[Diagnostics.Process]::Start($info)
    $processNotes[$process]=[K5GuestProcessNotes]::new($process)
    $process.BeginOutputReadLine(); $process.BeginErrorReadLine()
    return $process
}
function Owned-AddressState {
    $addresses=@(Get-NetIPAddress -InterfaceIndex $hostInterface -IPAddress $hostIPAddress -AddressFamily IPv4 -ErrorAction Stop)
    if($addresses.Count -ne 1){return 'missing'}
    $state=[string]$addresses[0].AddressState
    if($state -in @('Invalid','Tentative','Duplicate','Deprecated','Preferred')){return $state}
    return 'other'
}
function Assert-Publisher {
    if($null -eq $mediaProcess -or $null -eq $publisherProcess -or $mediaProcess.HasExited -or $publisherProcess.HasExited){throw 'Owned generated source ended'}
}
function Kvp-Seal([string]$Phase,[int]$Seconds){
    $watch=[Diagnostics.Stopwatch]::StartNew()
    do{
        $vm=Owned-VM; Assert-Publisher
        if([string]$vm.State -ne 'Running'){throw 'Owned VM stopped before witness'}
        $computer=Get-CimInstance -Namespace root/virtualization/v2 -ClassName Msvm_ComputerSystem -Filter ("Name='"+$vmId.ToString()+"'")
        $components=@(Get-CimAssociatedInstance -InputObject $computer -ResultClassName Msvm_KvpExchangeComponent)
        if($components.Count -ne 1){throw 'Owned KVP component unavailable'}
        foreach($raw in @($components[0].GuestExchangeItems)){
            if($null -eq $raw -or $raw.Length -gt 8192){continue}
            [xml]$item=$raw
            $name=$item.SelectSingleNode('/INSTANCE/PROPERTY[@NAME="Name"]/VALUE')
            if($null -eq $name -or $name.InnerText -cne ('K5GuestWitness-'+$transfer)){continue}
            $value=$item.SelectSingleNode('/INSTANCE/PROPERTY[@NAME="Data"]/VALUE')
            if($null -eq $value){continue}
            $parts=$value.InnerText.Split('|')
            if($parts.Count -eq 3 -and $parts[0] -ceq $Phase -and $parts[1] -match '^[a-f0-9-]{36}$' -and $parts[2] -match '^[a-f0-9]{64}$'){
                return @{phase=$parts[0];boot_id=$parts[1];sha256=$parts[2]}
            }
            if($parts.Count -eq 3 -and $parts[0] -ceq 'failed'){throw 'Guest setup witness failed'}
        }
        Start-Sleep -Seconds 1
    }while($watch.Elapsed.TotalSeconds -lt $Seconds)
    throw [TimeoutException]::new('Sealed guest witness unavailable')
}
function Open-Direct {
    # Called only AFTER the credential-free SYSTEM witness publishes its seal.
    # No connection, logon or endpoint probe is attempted in the measured interval.
    return New-PSSession -VMId $vmId -Credential $credential -ErrorAction Stop
}
function Read-GuestReceipt([string]$Phase,$Seal){
    $raw=Invoke-Command -Session $direct -ScriptBlock {
        param($phase)
        if($phase -notin @('bootstrap','reboot','signout')){throw 'Unadmitted receipt'}
        $path='C:\K5GuestWitness\'+$phase+'-witness.json'
        if((Get-Item -LiteralPath $path).Length -gt 65536){throw 'Receipt length exceeded'}
        [Convert]::ToBase64String([IO.File]::ReadAllBytes($path))
    } -ArgumentList $Phase -ErrorAction Stop
    $bytes=[Convert]::FromBase64String([string]$raw)
    $sha=[Security.Cryptography.SHA256]::Create()
    try{$digest=[BitConverter]::ToString($sha.ComputeHash($bytes)).Replace('-','').ToLowerInvariant()}finally{$sha.Dispose()}
    if($digest -cne $Seal.sha256){throw 'Sealed witness bytes differ'}
    $result=[Text.Encoding]::UTF8.GetString($bytes)|ConvertFrom-Json
    if($result.transfer_id -cne $transfer -or $result.head_sha -cne $ProductHead -or $result.installer_sha256 -cne $SetupSha256 -or $result.boot_id -cne $Seal.boot_id){throw 'Sealed witness scope differs'}
    return $result
}
function IPv4-Number([string]$Address){
    $bytes=[Net.IPAddress]::Parse($Address).GetAddressBytes()
    if($bytes.Length -ne 4){throw 'IPv4 required'}
    if([BitConverter]::IsLittleEndian){[Array]::Reverse($bytes)}
    return [BitConverter]::ToUInt32($bytes,0)
}
function Assert-CopyContracts {
    foreach($case in @(
        @{bytes=512;expected=512;failure='none';cancel=$false},
        @{bytes=512;expected=1024;failure='EndOfStreamException';cancel=$false},
        @{bytes=1024;expected=512;failure='InvalidDataException';cancel=$false},
        @{bytes=512;expected=512;failure='cancel';cancel=$true}
    )){
        $source=[IO.MemoryStream]::new([byte[]]::new($case.bytes));$destination=[IO.MemoryStream]::new()
        $cancellation=[Threading.CancellationTokenSource]::new();$copy=[K5GuestBoundedCopy]::new();$observed='none'
        try{
            if($case.cancel){$cancellation.Cancel()}
            try{[void]$copy.Copy($source,$destination,$case.expected,$cancellation.Token).GetAwaiter().GetResult()}
            catch{$observed=$_.Exception.GetBaseException().GetType().Name}
            if($case.failure -ceq 'none'){
                if($observed -cne 'none' -or $copy.Received -ne $case.expected -or $destination.Length -ne $case.expected){throw 'Compiled copy exact-byte contract failed'}
            }elseif($case.failure -ceq 'cancel'){
                if($observed -notin @('OperationCanceledException','TaskCanceledException') -or $destination.Length -ne 0){throw 'Compiled copy cancellation contract failed'}
            }elseif($observed -cne $case.failure -or $destination.Length -gt $case.expected){throw 'Compiled copy length-refusal contract failed'}
        }finally{$source.Dispose();$destination.Dispose();$cancellation.Dispose()}
    }
}
function Read-BootFailureLive {
    $vm=Owned-VM
    $facts=@{vm_state=[string]$vm.State;uptime_seconds=[Math]::Round($vm.Uptime.TotalSeconds,3);cpu_usage=[int]$vm.CPUUsage;memory_assigned_bytes=[long]$vm.MemoryAssigned;intrinsic=@{};own_seal_present=$false}
    $computer=Get-WmiObject -Namespace root\virtualization\v2 -Class Msvm_ComputerSystem -Filter ("Name='"+$vmId.ToString()+"'")
    $components=@($computer.GetRelated('Msvm_KvpExchangeComponent'))
    if($components.Count -eq 1){
        foreach($raw in @($components[0].GuestIntrinsicExchangeItems)){
            if($null -eq $raw -or $raw.Length -gt 8192){continue};[xml]$item=$raw
            $name=$item.SelectSingleNode('/INSTANCE/PROPERTY[@NAME="Name"]/VALUE');$value=$item.SelectSingleNode('/INSTANCE/PROPERTY[@NAME="Data"]/VALUE')
            if($null -ne $name -and $null -ne $value -and $name.InnerText -in @('OSName','OSVersion','IntegrationServicesVersion','ProcessorArchitecture') -and $value.InnerText.Length -le 256){$facts.intrinsic[$name.InnerText]=$value.InnerText}
        }
        foreach($raw in @($components[0].GuestExchangeItems)){
            if($null -eq $raw -or $raw.Length -gt 8192){continue};[xml]$item=$raw
            $name=$item.SelectSingleNode('/INSTANCE/PROPERTY[@NAME="Name"]/VALUE');$value=$item.SelectSingleNode('/INSTANCE/PROPERTY[@NAME="Data"]/VALUE')
            if($null -ne $name -and $null -ne $value -and $name.InnerText -ceq ('K5GuestWitness-'+$transfer)){
                $facts.own_seal_present=$true
                if($value.InnerText -match '^(bootstrap|reboot|signout|failed)\|[a-f0-9-]{36}\|[a-f0-9]{64}$'){$facts.own_seal_value=$value.InnerText}
            }
        }
    }
    # Initial-bootstrap failure only: no keyboard password has been sent and
    # the first-admin fields use native password controls. No raw log is copied.
    try{
        $facts.thumbnail_subphase='native_request'
        $settings=@($computer.GetRelated('Msvm_VirtualSystemSettingData')|Where-Object{$_.VirtualSystemType -ceq 'Microsoft:Hyper-V:System:Realized'})
        $manager=Get-WmiObject -Namespace root\virtualization\v2 -Class Msvm_VirtualSystemManagementService
        if($settings.Count -ne 1){throw 'Actual VM settings unavailable'}
        $arguments=$manager.GetMethodParameters('GetVirtualSystemThumbnailImage')
        $arguments['TargetSystem']=$settings[0].Path.Path;$arguments['WidthPixels']=[uint16]640;$arguments['HeightPixels']=[uint16]480
        $thumbnail=$manager.InvokeMethod('GetVirtualSystemThumbnailImage',$arguments,$null)
        $facts.thumbnail_return_code=[int]$thumbnail.ReturnValue
        if($thumbnail.ReturnValue -eq 0){
            $facts.thumbnail_subphase='byte_geometry'
            $pixels=[byte[]]$thumbnail.ImageData
            $facts.thumbnail_byte_count=[int]$pixels.Length
            if($pixels.Length -ne 640*480*2){
                if($pixels.Length -gt 0 -and $pixels.Length -le 614416){
                    # Preserve the exact bounded native output for offline
                    # diagnosis; do not guess whether extra bytes are a header.
                    $facts.thumbnail_rgb565_base64=[Convert]::ToBase64String($pixels)
                }
                throw 'Thumbnail byte geometry differs'
            }
            $facts.thumbnail_subphase='bitmap_allocation'
            Add-Type -AssemblyName System.Drawing
            $bitmap=[Drawing.Bitmap]::new(640,480,[Drawing.Imaging.PixelFormat]::Format16bppRgb565)
            $buffer=$null;$png=[IO.MemoryStream]::new()
            try{
                $facts.thumbnail_subphase='bitmap_lock'
                $buffer=$bitmap.LockBits([Drawing.Rectangle]::new(0,0,640,480),[Drawing.Imaging.ImageLockMode]::WriteOnly,[Drawing.Imaging.PixelFormat]::Format16bppRgb565)
                $facts.thumbnail_stride=[int]$buffer.Stride
                if($buffer.Stride -ne 1280){throw 'Thumbnail stride differs'}
                [Runtime.InteropServices.Marshal]::Copy($pixels,0,$buffer.Scan0,$pixels.Length)
                $bitmap.UnlockBits($buffer);$buffer=$null
                $facts.thumbnail_subphase='png_encode'
                $bitmap.Save($png,[Drawing.Imaging.ImageFormat]::Png)
                if($png.Length -gt 1048576){throw 'Thumbnail PNG bound exceeded'}
                $facts.thumbnail_png_base64=[Convert]::ToBase64String($png.ToArray())
                $facts.thumbnail_subphase='complete'
            }finally{if($null -ne $buffer){$bitmap.UnlockBits($buffer)};$bitmap.Dispose();$png.Dispose()}
        }
    }catch{$facts.thumbnail_failure_kind=$_.Exception.GetType().Name;$facts.thumbnail_failure_hresult=$_.Exception.HResult}
    return $facts
}
function Read-SetupState([string]$WindowsVolume) {
    # Only allowlisted values leave copied hives. Recovery never writes to the
    # mounted source hive, and passwords/answer-file contents are never emitted.
    Assert-Root
    $facts=@{hives_unloaded=$true;inspection_complete=$true;answer_candidates=@{};locale=@{};winlogon=@{};hive_diagnostics=@{}}
    foreach($entry in @(@{name='panther_cached';path='Windows\Panther\Unattend.xml'},@{name='panther_explicit';path='Windows\Panther\Unattend\Unattend.xml'},@{name='sysprep';path='Windows\System32\Sysprep\Unattend.xml'})){
        $facts.answer_candidates[$entry.name]=[bool](Test-Path -LiteralPath (Join-Path $WindowsVolume $entry.path) -PathType Leaf)
    }
    foreach($kind in @('SOFTWARE','SYSTEM')){
        $hiveName='K5GuestSetup_'+[guid]::NewGuid().ToString('N');$loaded=$false
        $note=@{subphase='snapshot_metadata';suffix='main';inspection_complete=$false}
        $facts.hive_diagnostics[$kind]=$note
        $source=Join-Path $WindowsVolume ('Windows\System32\config\'+$kind)
        $copy=Join-Path $root ($hiveName+'.hiv')
        try{
            foreach($suffix in @('','.LOG1','.LOG2')){
                $note.subphase='snapshot_metadata';$note.suffix=if($suffix -ceq ''){'main'}elseif($suffix -ceq '.LOG1'){'log1'}else{'log2'}
                try{
                    $candidate=$source+$suffix
                    if(-not(Test-Path -LiteralPath $candidate -PathType Leaf)){continue}
                    $info=Get-Item -LiteralPath $candidate -Force
                    if($info.Length -gt 268435456 -or ($info.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0){throw 'Setup hive copy bound refused'}
                    $note.subphase='snapshot_copy';Copy-Item -LiteralPath $candidate -Destination ($copy+$suffix) -Force
                }catch{
                    if($suffix -ceq ''){throw}
                    if(-not $note.ContainsKey('sidecar_errors')){$note.sidecar_errors=@()}
                    $note.sidecar_errors+=@{subphase=$note.subphase;suffix=$note.suffix;failure_kind=$_.Exception.GetType().Name;hresult=$_.Exception.HResult}
                }
            }
            $note.subphase='hive_load';$note.suffix='none'
            & (Join-Path $env:SystemRoot 'System32\reg.exe') load ('HKLM\'+$hiveName) $copy 2>&1|Out-Null
            $note.load_exit_code=$LASTEXITCODE
            if($LASTEXITCODE -ne 0){throw 'Setup hive copy unavailable'};$loaded=$true
            if($kind -ceq 'SOFTWARE'){
                $note.subphase='image_state_open'
                $key=[Microsoft.Win32.Registry]::LocalMachine.OpenSubKey($hiveName+'\Microsoft\Windows\CurrentVersion\Setup\State')
                try{
                    $note.subphase='image_state_read'
                    if($null -ne $key){
                        $value=[string]$key.GetValue('ImageState','')
                        $facts.image_state=if($value -in @('IMAGE_STATE_COMPLETE','IMAGE_STATE_UNDEPLOYABLE','IMAGE_STATE_GENERALIZE_RESEAL_TO_AUDIT','IMAGE_STATE_GENERALIZE_RESEAL_TO_OOBE','IMAGE_STATE_SPECIALIZE_RESEAL_TO_AUDIT','IMAGE_STATE_SPECIALIZE_RESEAL_TO_OOBE')){$value}elseif($value.Length -eq 0){'absent'}else{'other'}
                    }
                }finally{if($null -ne $key){$key.Dispose()}}
                $note.subphase='winlogon_open';$key=[Microsoft.Win32.Registry]::LocalMachine.OpenSubKey($hiveName+'\Microsoft\Windows NT\CurrentVersion\Winlogon')
                try{
                    $note.subphase='winlogon_read'
                    if($null -ne $key){
                        $auto=[string]$key.GetValue('AutoAdminLogon','')
                        $facts.winlogon.auto_admin_logon=if($auto -in @('0','1')){$auto}elseif($auto.Length -eq 0){'absent'}else{'other'}
                        $facts.winlogon.default_password_present=($key.GetValueNames() -contains 'DefaultPassword')
                        $facts.winlogon.administrator_selected=([string]$key.GetValue('DefaultUserName','') -ieq 'Administrator')
                    }
                }finally{if($null -ne $key){$key.Dispose()}}
            }else{
                $note.subphase='setup_open'
                $key=[Microsoft.Win32.Registry]::LocalMachine.OpenSubKey($hiveName+'\Setup')
                try{
                    $note.subphase='setup_read'
                    if($null -ne $key){
                        foreach($name in @('OOBEInProgress','SystemSetupInProgress','SetupType','SetupPhase')){
                            $value=$key.GetValue($name,$null)
                            if($value -is [int] -and $value -ge 0 -and $value -le 255){$facts[$name]=[int]$value}
                        }
                        $value=[string]$key.GetValue('UnattendFile','')
                        $facts.unattend_pointer=if($value.Length -eq 0){'absent'}elseif($value -imatch '^(?:C:|%SystemDrive%)\\Windows\\Panther\\Unattend\.xml$' -or $value -imatch '^%windir%\\Panther\\Unattend\.xml$'){'our_cached_path'}elseif($value -imatch '\\Panther\\Unattend\\Unattend\.xml$'){'higher_precedence_panther'}elseif($value -imatch '\\Sysprep\\Unattend\.xml$'){'sysprep'}else{'other'}
                        $value=[string]$key.GetValue('CmdLine','')
                        $facts.setup_command=if($value.Length -eq 0){'empty'}elseif($value -imatch '(?:windeploy|msoobe|oobe)'){'oobe'}else{'other'}
                    }
                }finally{if($null -ne $key){$key.Dispose()}}
                $note.subphase='select_open';$select=[Microsoft.Win32.Registry]::LocalMachine.OpenSubKey($hiveName+'\Select')
                try{$note.subphase='select_read';$control=if($null -ne $select){$select.GetValue('Current',1)}else{1}}finally{if($null -ne $select){$select.Dispose()}}
                if($control -isnot [int] -or $control -lt 1 -or $control -gt 999){throw 'Setup control set refused'}
                $note.subphase='language_open';$key=[Microsoft.Win32.Registry]::LocalMachine.OpenSubKey($hiveName+('\ControlSet{0:D3}\Control\Nls\Language' -f $control))
                try{$note.subphase='language_read';if($null -ne $key){foreach($name in @('Default','InstallLanguage')){$value=[string]$key.GetValue($name,'');$facts.locale[$name]=if($value -match '^[0-9a-fA-F]{4}$'){$value.ToLowerInvariant()}elseif($value.Length -eq 0){'absent'}else{'other'}}}}finally{if($null -ne $key){$key.Dispose()}}
            }
            $note.inspection_complete=$true
        }catch{
            $facts.inspection_complete=$false
            $note.failure_subphase=$note.subphase;$note.failure_suffix=$note.suffix
            $note.failure_kind=$_.Exception.GetType().Name;$note.failure_hresult=$_.Exception.HResult
            $inner=$_.Exception
            for($depth=0;$null -ne $inner -and $depth -lt 8;$depth++){
                if($inner -is [ComponentModel.Win32Exception]){$note.failure_native_code=$inner.NativeErrorCode;break};$inner=$inner.InnerException
            }
        }finally{
            if($loaded){
                $note.subphase='hive_unload'
                try{& (Join-Path $env:SystemRoot 'System32\reg.exe') unload ('HKLM\'+$hiveName) 2>&1|Out-Null}
                catch{$receipt.cleanup_complete=$false;$receipt.registry_cleanup_failure=@{hive_kind=$kind;subphase='hive_unload';failure_kind=$_.Exception.GetType().Name;hresult=$_.Exception.HResult};throw}
                $note.unload_exit_code=$LASTEXITCODE
                if($LASTEXITCODE -ne 0){$facts.hives_unloaded=$false;$receipt.cleanup_complete=$false;$receipt.registry_cleanup_failure=@{hive_kind=$kind;subphase='hive_unload';native_exit_code=$LASTEXITCODE};throw 'Owned setup hive remains loaded'}
            }
            # Copies remain private until the outer exact-owned-tree cleanup;
            # only successfully unloaded hives permit VM observation to proceed.
            $note.subphase='complete';$note.private_copy_cleanup='deferred_to_owned_root'
        }
    }
    return $facts
}
function Read-BootFailureOffline {
    Assert-Root
    $facts=@{read_only_mount=$false;known_files=@{};own_receipts=@{};setup_logs=@{};subphase='create_access_path';inspection_complete=$false}
    $attached=$null;$diagnosticPartition=$null;$diagnosticMount=(Join-Path $root 'failure-offline')+'\'
    try{
        New-Item -ItemType Directory -Path $diagnosticMount|Out-Null
        $facts.subphase='mount_readonly'
        $attached=Mount-VHD -Path $image -ReadOnly -NoDriveLetter -PassThru
        $diagnosticDisk=[int]$attached.DiskNumber
        $facts.subphase='verify_disk'
        $disk=Get-Disk -Number $diagnosticDisk
        $facts.disk_is_readonly=[bool]$disk.IsReadOnly;$facts.disk_unique_id_matches=([string]$disk.UniqueId -ceq $diskId)
        $observedId=[string]$disk.UniqueId
        if($observedId -match '^[A-Za-z0-9_-]{1,128}$'){$facts.observed_disk_unique_id=$observedId}
        if(-not $disk.IsReadOnly -or [string]$disk.UniqueId -cne $diskId){throw 'Owned read-only diagnostic disk differs'}
        $facts.read_only_mount=$true
        $facts.subphase='find_windows_volume'
        foreach($partition in @(Get-Partition -DiskNumber $diagnosticDisk|Where-Object{$_.Size -gt 1GB})){
            $diagnosticPartition=[int]$partition.PartitionNumber
            Add-PartitionAccessPath -DiskNumber $diagnosticDisk -PartitionNumber $diagnosticPartition -AccessPath $diagnosticMount
            if(Test-Path -LiteralPath (Join-Path $diagnosticMount 'Windows\System32\config\SOFTWARE')){break}
            Remove-PartitionAccessPath -DiskNumber $diagnosticDisk -PartitionNumber $diagnosticPartition -AccessPath $diagnosticMount;$diagnosticPartition=$null
        }
        if($null -eq $diagnosticPartition){throw 'Diagnostic Windows volume unavailable'}
        $facts.subphase='setup_state'
        $facts.setup_state=Read-SetupState $diagnosticMount
        $facts.subphase='known_payload_files'
        $known=@{
          installed_launcher='Program Files\K5 Vision\K5Vision.exe';identity='ProgramData\K5Vision\identity\identity.json';
          answer_file='Windows\Panther\Unattend.xml';setup_complete='Windows\Setup\Scripts\SetupComplete.cmd';
          setup_task=('Windows\System32\Tasks\K5GuestWitness-'+$transfer);bootstrap_task=('Windows\System32\Tasks\K5GuestBootstrap-'+$transfer)
        }
        foreach($entry in $known.GetEnumerator()){$facts.known_files[$entry.Key]=[bool](Test-Path -LiteralPath (Join-Path $diagnosticMount $entry.Value) -PathType Leaf)}
        $facts.subphase='own_receipts'
        foreach($name in @('setup-entry.json','setup-failure.json','bootstrap-receipt.json','bootstrap-witness.json')){
            $path=Join-Path $diagnosticMount ('K5GuestWitness\'+$name)
            if(-not(Test-Path -LiteralPath $path -PathType Leaf)){continue}
            $info=Get-Item -LiteralPath $path
            if($info.Length -gt 65536 -or ($info.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0){continue}
            try{
                $parsed=Get-Content -LiteralPath $path -Raw|ConvertFrom-Json;$safe=@{}
                foreach($field in @('status','phase','stage','failure_stage','error_kind','failure_type','hresult','failure_hresult','native_code','http_status','mfa_enabled_before_reboot','mfa_confirmed_via_normal_api','gui_admin_setup','camera_source_enrolled','service_running','gui_client_closed','cleanup_credentials_removed','configuration_valid','system_account_verified','observer_session','setup_session_admitted')){
                    $property=$parsed.PSObject.Properties[$field]
                    if($null -eq $property){continue};$value=$property.Value
                    if($value -is [bool] -or $value -is [int] -or $value -is [long]){$safe[$field]=$value}
                    elseif($value -is [string] -and $value -match '^[A-Za-z][A-Za-z0-9_.-]{0,79}$'){$safe[$field]=$value}
                }
                $facts.own_receipts[$name]=$safe
            }catch{$facts.own_receipts[$name]=@{parse_failed=$true}}
        }
        $facts.subphase='setup_logs'
        foreach($relative in @('Windows\Panther\setupact.log','Windows\Panther\setuperr.log','Windows\Panther\UnattendGC\setupact.log','Windows\Panther\UnattendGC\setuperr.log')){
            $path=Join-Path $diagnosticMount $relative
            if(-not(Test-Path -LiteralPath $path -PathType Leaf)){continue}
            $info=Get-Item -LiteralPath $path
            if($info.Length -gt 16777216 -or ($info.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0){$facts.setup_logs[$relative]=@{bounded_read_refused=$true};continue}
            $text=[IO.File]::ReadAllText($path)
            $flags=@{bytes=$info.Length;unattend_error=($text -match '(?is)(?:could not parse|failed[^\r\n]{0,120}unattend|unattend[^\r\n]{0,120}(?:error|fail))');oobe_seen=($text -match '(?i)(?:oobeSystem|windeploy|oobe)');setup_complete_seen=($text -match '(?i)SetupComplete\.cmd');setup_complete_error=($text -match '(?i)(?:SetupComplete[^\r\n]{0,120}(?:fail|error)|(?:fail|error)[^\r\n]{0,120}SetupComplete)')}
            $codes=@([regex]::Matches($text,'(?i)(?:HRESULT|\bhr|error(?:\s+code)?)\s*[:= ]\s*(0x[0-9a-f]{8})')|ForEach-Object{$_.Groups[1].Value.ToLowerInvariant()}|Select-Object -Unique -First 16)
            $flags.specialize_seen=($text -match '(?i)\bspecialize\b');$flags.oobe_system_seen=($text -match '(?i)\boobeSystem\b')
            $flags.our_computer_name_seen=($text -match '(?i)\bK5GUEST\b');$flags.our_cached_answer_path_seen=($text -match '(?i)\\Panther\\Unattend\.xml\b')
            $flags.our_hook_referenced=($text -match '(?i)windows_guest_witness\.ps1')
            $flags.near_hook_return_codes=@([regex]::Matches($text,'(?is)windows_guest_witness\.ps1.{0,2048}?\b(?:exit(?:\s+code)?|returned(?:\s+code)?|result(?:\s+code)?)\s*(?:=|:|is|with)?\s*(0x[0-9a-f]{1,8}|\d{1,10})')|ForEach-Object{$_.Groups[1].Value.ToLowerInvariant()}|Select-Object -Unique -First 16)
            $flags.error_codes=$codes;$facts.setup_logs[$relative]=$flags;$text=$null
        }
        $facts.inspection_complete=$true;$facts.subphase='complete'
    }catch{
        $facts.failure_subphase=$facts.subphase;$facts.failure_kind=$_.Exception.GetType().Name;$facts.failure_hresult=$_.Exception.HResult
        $facts.failure_reason='other'
        if($_.Exception.Message -ceq 'Owned read-only diagnostic disk differs'){$facts.failure_reason='owned_readonly_disk_refused'}
        elseif($_.Exception.Message -ceq 'Diagnostic Windows volume unavailable'){$facts.failure_reason='windows_volume_unavailable'}
        $inner=$_.Exception
        for($depth=0;$null -ne $inner -and $depth -lt 8;$depth++){
            if($inner -is [ComponentModel.Win32Exception]){$facts.failure_native_code=$inner.NativeErrorCode;break};$inner=$inner.InnerException
        }
    }finally{
        if($null -ne $attached){
            try{
                $admitted=Get-VHD -Path $image
                if(-not $admitted.Attached -or [int]$admitted.DiskNumber -ne [int]$attached.DiskNumber){throw 'Diagnostic disk attachment changed'}
                if($null -ne $diagnosticPartition){Remove-PartitionAccessPath -DiskNumber ([int]$attached.DiskNumber) -PartitionNumber $diagnosticPartition -AccessPath $diagnosticMount}
                Dismount-VHD -Path $image
                if((Get-VHD -Path $image).Attached){throw 'Diagnostic disk remains mounted'}
            }catch{$receipt.cleanup_complete=$false;$receipt.diagnostic_disk_cleanup_failed=$true;throw}
        }
    }
    return $facts
}
try{
    if($env:GITHUB_ACTIONS -ne 'true' -or $env:RUNNER_ENVIRONMENT -ne 'github-hosted' -or $env:RUNNER_OS -ne 'Windows'){throw 'Disposable Windows host required'}
    Assert-CopyContracts
    $receipt.compiled_copy_contracts_passed=$true
    Assert-RangeContracts
    $receipt.compiled_range_contracts_passed=$true
    foreach($file in @($SetupPath,$BootstrapExe,$MediaMTX,$FFmpeg,$FFprobe,(Join-Path $PSScriptRoot 'windows_guest_witness.ps1'))){
        if(-not(Test-Path -LiteralPath $file -PathType Leaf) -or ((Get-Item -LiteralPath $file).Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0){throw 'Required pinned fixture unavailable'}
    }
    if((Get-FileHash -LiteralPath $SetupPath -Algorithm SHA256).Hash.ToLowerInvariant() -cne $SetupSha256){throw 'Installer bytes differ'}
    $receipt.stage='resources'
    $drives=@([IO.DriveInfo]::GetDrives()|Where-Object{$_.DriveType -eq 'Fixed' -and $_.IsReady -and $_.AvailableFreeSpace -ge 24GB}|Sort-Object AvailableFreeSpace -Descending)
    if($drives.Count -eq 0 -or [long](Get-CimInstance Win32_OperatingSystem).FreePhysicalMemory*1024 -lt 5GB){throw 'Guest resources unavailable'}
    $receipt.available_bytes=[long]$drives[0].AvailableFreeSpace
    $root=Join-Path $drives[0].RootDirectory.FullName ('K5GuestRecording-'+[guid]::NewGuid().ToString('N'))
    $rootIdentity=Private-Directory $root
    $image=Join-Path $root 'official-evaluation.vhdx'
    $receipt.stage='isolated_camera_network'
    $receipt.network_subphase='select_conflict_free_subnet'
    $receipt.subnet_candidates_tested=0
    $receipt.overlapping_fixture_prefixes=@()
    $prefix=$null
    foreach($octet in (Get-Random -InputObject (20..230) -Count 211)){
        $receipt.subnet_candidates_tested++
        $candidate='172.29.'+$octet+'.0';$number=IPv4-Number $candidate;$conflict=$false
        foreach($route in @(Get-NetRoute -AddressFamily IPv4|Where-Object{$_.DestinationPrefix -ne '0.0.0.0/0'})){
            $parts=$route.DestinationPrefix.Split('/');$length=[Math]::Min(30,[int]$parts[1]);$mask=[uint32](([uint64]4294967295 -shl (32-$length)) -band [uint64]4294967295)
            if(($number -band $mask) -eq ((IPv4-Number $parts[0]) -band $mask)){
                $conflict=$true
                if($receipt.overlapping_fixture_prefixes.Count -lt 16 -and $route.DestinationPrefix -notin $receipt.overlapping_fixture_prefixes){$receipt.overlapping_fixture_prefixes+=([string]$route.DestinationPrefix)}
                break
            }
        }
        if(-not $conflict){$prefix='172.29.'+$octet;break}
    }
    if($null -eq $prefix){throw 'Isolated fixture subnet unavailable'}
    $hostIPAddress=$prefix+'.1';$guestIP=$prefix+'.2'
    $switchName='K5Guest-'+$transfer.Substring(0,8)
    $receipt.network_subphase='create_owned_internal_switch'
    $switch=New-VMSwitch -Name $switchName -SwitchType Internal;$switchId=$switch.Id
    $receipt.network_subphase='find_owned_internal_adapter'
    $hostAdapters=@(Get-NetAdapter -Name ('vEthernet ('+$switchName+')'))
    if($hostAdapters.Count -ne 1){throw 'Owned internal adapter unavailable'};$hostInterface=[int]$hostAdapters[0].ifIndex
    $receipt.network_subphase='assign_private_host_address'
    New-NetIPAddress -InterfaceIndex $hostInterface -IPAddress $hostIPAddress -PrefixLength 30 -AddressFamily IPv4|Out-Null
    $firewallName='K5GuestCamera-'+$transfer
    $receipt.network_subphase='create_narrow_owned_firewall_rule'
    New-NetFirewallRule -Name $firewallName -DisplayName $firewallName -Direction Inbound -Action Allow -Protocol TCP -LocalAddress $hostIPAddress -RemoteAddress $guestIP -LocalPort 18554 -Program $MediaMTX|Out-Null
    $configPath=Join-Path $root 'generated-camera.json'
    $mediaConfig=@{logLevel='warn';rtspAddress=($hostIPAddress+':18554');rtspTransports=@('tcp');api=$false;rtmp=$false;hls=$false;webrtc=$false;srt=$false;paths=@{generated=@{source='publisher'}}}
    [IO.File]::WriteAllText($configPath,($mediaConfig|ConvertTo-Json -Depth 8),[Text.UTF8Encoding]::new($false))
    $receipt.network_subphase='await_owned_address_preferred'
    $addressWatch=[Diagnostics.Stopwatch]::StartNew()
    $receipt.owned_address_state_edges=@()
    do{
        $addressState=Owned-AddressState
        if($receipt.owned_address_state_edges.Count -eq 0 -or $receipt.owned_address_state_edges[-1] -cne $addressState){$receipt.owned_address_state_edges+=$addressState}
        if($addressState -ceq 'Preferred'){break}
        if($addressState -cne 'Tentative' -or $addressWatch.Elapsed.TotalSeconds -ge 5){throw [TimeoutException]::new('Owned address did not become preferred')}
        Start-Sleep -Milliseconds 100
    }while($true)
    $receipt.owned_address_observation_seconds=[Math]::Round($addressWatch.Elapsed.TotalSeconds,3)
    $receipt.network_subphase='start_generated_relay'
    $receipt.host_address_state_before_relay=$addressState
    $mediaProcess=Start-OwnedProcess $MediaMTX $configPath
    Start-Sleep -Seconds 1
    $source='rtsp://'+$hostIPAddress+':18554/generated'
    $receipt.network_subphase='start_generated_publisher'
    $publisherProcess=Start-OwnedProcess $FFmpeg ('-nostdin -hide_banner -loglevel error -re -f lavfi -i testsrc2=size=320x240:rate=10 -an -c:v libx264 -threads 1 -preset ultrafast -tune zerolatency -pix_fmt yuv420p -g 10 -f rtsp -rtsp_transport tcp '+$source)
    Start-Sleep -Seconds 1
    $receipt.network_subphase='verify_owned_source_processes'
    Assert-Publisher
    $receipt.network_preflight_complete=$true
    $receipt.generated_source_isolated=$true
    $receipt.stage='official_pinned_image_download'
    $receipt.download_subphase='response_headers'
    $receipt.download_response_status=0;$receipt.download_content_length=0
    $receipt.download_copy_complete=$false
    $downloadWatch=[Diagnostics.Stopwatch]::StartNew()
    [long]$downloaded=0
    Add-Type -AssemblyName System.Net.Http
    $handler=[Net.Http.HttpClientHandler]::new();$handler.AllowAutoRedirect=$false;$handler.MaxConnectionsPerServer=4
    $client=[Net.Http.HttpClient]::new($handler);$client.Timeout=[TimeSpan]::FromMinutes(12)
    $cancel=[Threading.CancellationTokenSource]::new(720000)
    $parts=@();$output=$null;$etag=''
    try{
        $url='https://software-static.download.prss.microsoft.com/dbazure/888969d5-f34g-4e03-ac9d-1f9786c66749/26100.1742.amd64fre.ge_release_svc_refresh.240906-0331_server_serverdatacentereval_en-us.vhdx'
        $output=[IO.FileStream]::new($image,[IO.FileMode]::CreateNew,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None)
        $output.SetLength($imageBytes);$output.Dispose();$output=$null
        [long]$slice=[Math]::Floor($imageBytes/4)
        for($index=0;$index -lt 4;$index++){
            [long]$start=$index*$slice;[long]$end=if($index -eq 3){$imageBytes-1}else{($index+1)*$slice-1}
            $request=[Net.Http.HttpRequestMessage]::new([Net.Http.HttpMethod]::Get,$url)
            $request.Headers.Range=[Net.Http.Headers.RangeHeaderValue]::new($start,$end)
            $request.Headers.AcceptEncoding.ParseAdd('identity')
            $part=@{index=$index;start=$start;end=$end;expected=($end-$start+1);request=$request;response=$null;input=$null;output=$null;copier=$null;copy_task=$null;headers_observed=$false}
            $part.header_task=$client.SendAsync($request,[Net.Http.HttpCompletionOption]::ResponseHeadersRead,$cancel.Token)
            $parts+=@($part)
        }
        $receipt.download_range_count=4
        while(@($parts|Where-Object{-not $_.headers_observed}).Count -gt 0){
            $pending=@($parts|Where-Object{-not $_.headers_observed})
            $ready=[Threading.Tasks.Task]::WhenAny([Threading.Tasks.Task[]]@($pending|ForEach-Object{$_.header_task})).GetAwaiter().GetResult()
            $part=@($pending|Where-Object{[object]::ReferenceEquals($_.header_task,$ready)})[0]
            $receipt.download_active_range=$part.index;$receipt.download_subphase='range_headers'
            $part.response=$part.header_task.GetAwaiter().GetResult();$part.headers_observed=$true
            $part.response_status=[int]$part.response.StatusCode;$receipt.download_response_status=$part.response_status
            $part.content_length=[long]$part.response.Content.Headers.ContentLength
            $tag=Assert-ImageRangeResponse $part.response $part.start $part.end $imageBytes $etag
            $part.content_range_verified=$true;$part.etag_present=($tag.Length -gt 0)
            if($etag.Length -eq 0 -and $tag.Length -gt 0){$etag=$tag}
            $receipt.download_content_length+=$part.content_length
            $part.input=$part.response.Content.ReadAsStreamAsync().GetAwaiter().GetResult()
            $part.output=[IO.FileStream]::new($image,[IO.FileMode]::Open,[IO.FileAccess]::Write,[IO.FileShare]::ReadWrite,1048576,([IO.FileOptions]::Asynchronous -bor [IO.FileOptions]::RandomAccess))
            $part.output.Position=[long]$part.start;$part.copier=[K5GuestBoundedCopy]::new()
            $part.copy_task=$part.copier.CopyAndCancel($part.input,$part.output,[long]$part.expected,$cancel)
        }
        $receipt.download_subphase='parallel_body_stream'
        [void][Threading.Tasks.Task]::WhenAll([Threading.Tasks.Task[]]@($parts|ForEach-Object{$_.copy_task})).GetAwaiter().GetResult()
        foreach($part in $parts){if($part.copier.Received -ne $part.expected){throw 'Official range copy length differs'};$part.output.Dispose();$part.output=$null}
        if($receipt.download_content_length -ne $imageBytes){throw 'Official aggregate image length differs'}
        $receipt.download_subphase='durable_flush'
        $output=[IO.FileStream]::new($image,[IO.FileMode]::Open,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None)
        $output.Flush($true);$output.Dispose();$output=$null
        $receipt.download_copy_complete=$true
    }catch{$cancel.Cancel();throw
    }finally{
        # Retire every pending header/body task before any shared file handle,
        # HTTP message, or cancellation source is disposed, including faults.
        if(-not $receipt.download_copy_complete){$cancel.Cancel()}
        foreach($field in @('header_task','copy_task')){
            $tasks=@($parts|Where-Object{$null -ne $_[$field]}|ForEach-Object{$_[$field]})
            if($tasks.Count -gt 0){try{[void][Threading.Tasks.Task]::WhenAll([Threading.Tasks.Task[]]$tasks).GetAwaiter().GetResult()}catch{}}
        }
        $receipt.download_ranges=@()
        foreach($part in $parts){
            $note=@{index=$part.index;start=[long]$part.start;end=[long]$part.end;expected=[long]$part.expected;received=[long]0;header_state=[string]$part.header_task.Status}
            if($null -ne $part.copier){$note.received=$part.copier.Received;$downloaded+=$note.received}
            foreach($field in @('response_status','content_length','content_range_verified','etag_present')){if($part.ContainsKey($field)){$note[$field]=$part[$field]}}
            if($null -ne $part.copy_task){$note.copy_state=[string]$part.copy_task.Status;if($part.copy_task.IsFaulted){$fault=$part.copy_task.Exception.GetBaseException();$note.copy_failure_kind=$fault.GetType().Name;$note.copy_failure_hresult=$fault.HResult}}
            $receipt.download_ranges+=@($note)
            if($null -eq $part.response -and $part.header_task.Status -eq [Threading.Tasks.TaskStatus]::RanToCompletion){$part.response=$part.header_task.GetAwaiter().GetResult()}
            foreach($field in @('output','input','response','request')){if($null -ne $part[$field]){$part[$field].Dispose()}}
        }
        $receipt.downloaded_bytes=$downloaded
        $receipt.download_elapsed_seconds=[Math]::Round($downloadWatch.Elapsed.TotalSeconds,3)
        $receipt.download_cancellation_requested=$cancel.IsCancellationRequested
        if($null -ne $output){$output.Dispose()}
        $cancel.Dispose();$client.Dispose();$handler.Dispose()
    }
    if((Get-Item -LiteralPath $image).Length -ne $imageBytes -or (Get-FileHash -LiteralPath $image -Algorithm SHA256).Hash.ToLowerInvariant() -cne $imageSha){throw 'Pinned official image bytes differ'}
    $receipt.stage='offline_image_and_generation'
    $attached=Mount-VHD -Path $image -NoDriveLetter -PassThru;$mounted=$true;$diskNumber=[int]$attached.DiskNumber
    $disk=Get-Disk -Number $diskNumber;$diskId=[string]$disk.UniqueId
    $partitions=@(Get-Partition -DiskNumber $diskNumber)
    $efi=@($partitions|Where-Object{$_.GptType -eq '{c12a7328-f81f-11d2-ba4b-00a0c93ec93b}'})
    if([string]$disk.PartitionStyle -eq 'GPT' -and $efi.Count -eq 1){$generation=2}
    elseif([string]$disk.PartitionStyle -eq 'MBR' -and @($partitions|Where-Object{$_.IsActive}).Count -eq 1){$generation=1}
    else{throw 'Official boot partition layout refused'}
    $receipt.vm_generation=$generation;$receipt.disk_partition_style=[string]$disk.PartitionStyle;$receipt.disk_unique_id=$diskId
    $accessPath=(Join-Path $root 'offline')+'\';New-Item -ItemType Directory -Path $accessPath|Out-Null
    $windows=$null
    foreach($partition in @($partitions|Where-Object{$_.Size -gt 1GB})){
        $partitionNumber=[int]$partition.PartitionNumber
        Add-PartitionAccessPath -DiskNumber $diskNumber -PartitionNumber $partitionNumber -AccessPath $accessPath
        if(Test-Path -LiteralPath (Join-Path $accessPath 'Windows\explorer.exe') -PathType Leaf){$windows=$accessPath;break}
        Remove-PartitionAccessPath -DiskNumber $diskNumber -PartitionNumber $partitionNumber -AccessPath $accessPath;$partitionNumber=$null
    }
    if($null -eq $windows){throw 'Pinned Desktop Experience image unavailable'}
    $kernel=Get-AuthenticodeSignature -LiteralPath (Join-Path $windows 'Windows\System32\ntoskrnl.exe')
    if($kernel.Status -ne 'Valid' -or $kernel.SignerCertificate.Subject -notmatch 'O=Microsoft Corporation'){throw 'Pinned Windows kernel signature unavailable'}
    $receipt.microsoft_kernel_verified=$true
    if($BootstrapObservation){$receipt.original_image_setup_state=Read-SetupState $windows}
    $receipt.stage='offline_guest_payload'
    $guestRoot=Join-Path $windows 'K5GuestWitness';Private-Directory $guestRoot $true|Out-Null
    $witness=Join-Path $PSScriptRoot 'windows_guest_witness.ps1'
    Copy-Item -LiteralPath $witness -Destination (Join-Path $guestRoot 'windows_guest_witness.ps1')
    Copy-Item -LiteralPath $BootstrapExe -Destination (Join-Path $guestRoot 'windows_guest_bootstrap.exe')
    Copy-Item -LiteralPath $SetupPath -Destination (Join-Path $guestRoot 'K5VisionSetup.exe')
    $mac='00155D'+([guid]::NewGuid().ToString('N').Substring(0,6)).ToUpperInvariant()
    $public=@{schema_version=1;transfer_id=$transfer;head_sha=$ProductHead;setup_sha256=$SetupSha256;phase='bootstrap';previous_boot_id='';guest_mac=$mac;guest_ip=$guestIP;host_ip=$hostIPAddress}
    $request=@{schema_version=1;head_sha=$ProductHead;setup_sha256=$SetupSha256;source_uri=$source;transfer_id=$transfer;receipt_path='C:\K5GuestWitness\bootstrap-receipt.json'}
    [IO.File]::WriteAllText((Join-Path $guestRoot 'public-config.json'),($public|ConvertTo-Json -Depth 5),[Text.UTF8Encoding]::new($false))
    [IO.File]::WriteAllText((Join-Path $guestRoot 'bootstrap-request.json'),($request|ConvertTo-Json -Depth 5),[Text.UTF8Encoding]::new($false))
    $entropy=[byte[]]::new(32);$rng=[Security.Cryptography.RandomNumberGenerator]::Create();try{$rng.GetBytes($entropy)}finally{$rng.Dispose()}
    $password='K5g!'+[Convert]::ToBase64String($entropy);[Array]::Clear($entropy,0,$entropy.Length)
    $credential=[Management.Automation.PSCredential]::new('Administrator',(ConvertTo-SecureString $password -AsPlainText -Force))
    $escaped=[Security.SecurityElement]::Escape($password)
    $unattend=@"
<?xml version="1.0" encoding="utf-8"?>
<unattend xmlns="urn:schemas-microsoft-com:unattend" xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State">
 <settings pass="specialize">
 <component name="Microsoft-Windows-Shell-Setup" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS"><ComputerName>K5GUEST</ComputerName><TimeZone>UTC</TimeZone></component>
 <component name="Microsoft-Windows-Deployment" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS"><RunSynchronous><RunSynchronousCommand wcm:action="add"><Order>1</Order><Path>C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File C:\K5GuestWitness\windows_guest_witness.ps1 -Mode Setup</Path><WillReboot>Never</WillReboot></RunSynchronousCommand></RunSynchronous></component>
 </settings>
 <settings pass="oobeSystem"><component name="Microsoft-Windows-Shell-Setup" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS">
 <OOBE><HideEULAPage>true</HideEULAPage><HideOnlineAccountScreens>true</HideOnlineAccountScreens><HideWirelessSetupInOOBE>true</HideWirelessSetupInOOBE><ProtectYourPC>3</ProtectYourPC></OOBE>
 <UserAccounts><AdministratorPassword><Value>$escaped</Value><PlainText>true</PlainText></AdministratorPassword></UserAccounts>
 <AutoLogon><Password><Value>$escaped</Value><PlainText>true</PlainText></Password><Enabled>true</Enabled><LogonCount>1</LogonCount><Username>Administrator</Username><Domain>.</Domain></AutoLogon>
 </component>
 <component name="Microsoft-Windows-International-Core" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS"><InputLocale>0409:00000409</InputLocale><SystemLocale>en-US</SystemLocale><UILanguage>en-US</UILanguage><UserLocale>en-US</UserLocale></component></settings>
</unattend>
"@
    $panther=Join-Path $windows 'Windows\Panther';if(-not(Test-Path -LiteralPath $panther)){New-Item -ItemType Directory -Path $panther|Out-Null}
    $answerPath=Join-Path $panther 'Unattend.xml'
    if(-not(Test-Path -LiteralPath $answerPath)){New-Item -ItemType File -Path $answerPath|Out-Null}
    if(((Get-Item -LiteralPath $answerPath).Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0){throw 'Guest answer file is a reparse point'}
    $answerAcl=[Security.AccessControl.FileSecurity]::new();$answerAcl.SetOwner([Security.Principal.SecurityIdentifier]::new('S-1-5-32-544'));$answerAcl.SetAccessRuleProtection($true,$false)
    foreach($sid in @('S-1-5-18','S-1-5-32-544')){$answerAcl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new([Security.Principal.SecurityIdentifier]::new($sid),'FullControl','Allow'))}
    Set-Acl -LiteralPath $answerPath -AclObject $answerAcl
    [IO.File]::WriteAllText($answerPath,$unattend,[Text.UTF8Encoding]::new($false));$unattend=$null;$escaped=$null
    $security=(Get-Acl -LiteralPath $guestRoot)
    foreach($file in @(Get-ChildItem -LiteralPath $guestRoot -File)){
        $acl=[Security.AccessControl.FileSecurity]::new();$acl.SetOwner([Security.Principal.SecurityIdentifier]::new('S-1-5-32-544'));$acl.SetAccessRuleProtection($true,$false)
        foreach($sid in @('S-1-5-18','S-1-5-32-544')){$acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new([Security.Principal.SecurityIdentifier]::new($sid),'FullControl','Allow'))}
        Set-Acl -LiteralPath $file.FullName -AclObject $acl
    }
    if([string](Get-Disk -Number $diskNumber).UniqueId -cne $diskId){throw 'Owned disk identity changed'}
    Remove-PartitionAccessPath -DiskNumber $diskNumber -PartitionNumber $partitionNumber -AccessPath $accessPath;$partitionNumber=$null
    Dismount-VHD -Path $image;$mounted=$false
    $receipt.stage='actual_guest_boot'
    $vmName='K5Guest-'+$transfer.Substring(0,8)
    $vm=New-VM -Name $vmName -Generation $generation -MemoryStartupBytes 4GB -Path (Join-Path $root 'vm') -VHDPath $image -SwitchName $switchName
    $vmId=$vm.Id;Set-VMProcessor -VM $vm -Count 2;Set-VMMemory -VM $vm -DynamicMemoryEnabled $false
    Set-VM -VM $vm -AutomaticCheckpointsEnabled $false
    Set-VMNetworkAdapter -VM $vm -StaticMacAddress $mac
    if($generation -eq 2){Set-VMFirmware -VM $vm -EnableSecureBoot On -SecureBootTemplate MicrosoftWindows}
    $vmSid=([Security.Principal.NTAccount]::new('NT VIRTUAL MACHINE',$vmId.ToString())).Translate([Security.Principal.SecurityIdentifier])
    # Hyper-V assigns the exact VM SID to its VHD/configuration itself. That VM
    # may traverse only this private parent; it receives no inherited read
    # access to the outer runner's files or credentials.
    $rootAcl=Get-Acl -LiteralPath $root
    $rootAcl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($vmSid,'Traverse','ContainerInherit','None','Allow'))
    Set-Acl -LiteralPath $root -AclObject $rootAcl
    Bounded-Job (Start-VM -VM (Owned-VM) -AsJob) 40|Out-Null
    if($BootstrapObservation){
        # Separate infrastructure diagnosis: no bootstrap seal is accepted, no
        # credential is typed, and no PowerShell Direct session is opened.
        $receipt.stage='bootstrap_observation'
        $receipt.bootstrap_observation_limit_seconds=240
        $observe=[Diagnostics.Stopwatch]::StartNew()
        do{
            $observedVM=Owned-VM;Assert-Publisher
            if([string]$observedVM.State -ne 'Running'){throw 'Owned VM stopped during initialization observation'}
            if($observedVM.Uptime.TotalSeconds -ge 240 -or $observe.Elapsed.TotalSeconds -ge 240){break}
            Start-Sleep -Milliseconds 250
        }while($true)
        $receipt.bootstrap_observation_elapsed_seconds=[Math]::Round($observe.Elapsed.TotalSeconds,3)
        $receipt.bootstrap_observation_live=Read-BootFailureLive
        $receipt.outcome='guest_initialization_diagnostic_observed'
    }else{
    $firstSeal=Kvp-Seal 'bootstrap' 720
    $direct=Open-Direct
    $initial=Read-GuestReceipt 'bootstrap' $firstSeal
    $receipt.bootstrap_witness=$initial
    if($initial.status -cne 'bootstrap_sealed' -or -not $initial.cleanup_credentials_removed){throw 'Guest bootstrap not sealed'}
    $receipt.actual_guest_boot=$true
    $receipt.stage='real_guest_reboot'
    Invoke-Command -Session $direct -ScriptBlock {
        param($boot)
        $path='C:\K5GuestWitness\public-config.json';$configuration=Get-Content -LiteralPath $path -Raw|ConvertFrom-Json
        $configuration.phase='reboot';$configuration.previous_boot_id=$boot
        [IO.File]::WriteAllText($path,($configuration|ConvertTo-Json -Depth 5),[Text.UTF8Encoding]::new($false))
    } -ArgumentList $initial.boot_id -ErrorAction Stop
    Remove-PSSession -Session $direct;$direct=$null
    Bounded-Job (Restart-VM -VM (Owned-VM) -Force -AsJob -Confirm:$false) 40|Out-Null
    $rebootSeal=Kvp-Seal 'reboot' 240
    $direct=Open-Direct
    $reboot=Read-GuestReceipt 'reboot' $rebootSeal;$receipt.reboot_witness=$reboot
    if($reboot.status -cne 'reboot_sealed' -or -not $reboot.startup_before_interactive_login -or -not $reboot.recording_after_reboot -or -not $reboot.retained_closed_hashes_unchanged -or $reboot.boot_id -ceq $initial.boot_id){throw 'Reboot recording witness failed'}
    $receipt.recording_after_reboot_before_login=$true
    $receipt.stage='actual_console_logon'
    # After the pre-login receipt has been sealed and verified, simulate an
    # ordinary secure-attention/password login on the isolated guest keyboard.
    # The password travels only as an in-memory CIM method argument.
    $keyboard=@(Get-CimInstance -Namespace root/virtualization/v2 -ClassName Msvm_Keyboard -Filter ("SystemName='"+$vmId.ToString()+"'"))
    if($keyboard.Count -ne 1){throw 'Owned guest keyboard unavailable'}
    if((Invoke-CimMethod -InputObject $keyboard[0] -MethodName TypeCtrlAltDel).ReturnValue -ne 0){throw 'Guest secure attention failed'}
    Start-Sleep -Seconds 2
    if((Invoke-CimMethod -InputObject $keyboard[0] -MethodName TypeText -Arguments @{asciiText=$password}).ReturnValue -ne 0){throw 'Guest keyboard input failed'}
    if((Invoke-CimMethod -InputObject $keyboard[0] -MethodName TypeKey -Arguments @{keyCode=[uint32]13}).ReturnValue -ne 0){throw 'Guest keyboard confirmation failed'}
    $password=$null
    $logonWatch=[Diagnostics.Stopwatch]::StartNew();$interactive=0
    do{
        $interactive=Invoke-Command -Session $direct -ScriptBlock {
            @(Get-CimInstance Win32_Process -Filter "Name='explorer.exe'"|Where-Object{$_.SessionId -gt 0}).Count
        } -ErrorAction Stop
        if($interactive -gt 0){break};Start-Sleep -Seconds 1
    }while($logonWatch.Elapsed.TotalSeconds -lt 60)
    if($interactive -lt 1){throw 'Actual interactive guest login not observed'}
    $receipt.stage='actual_guest_signout'
    Invoke-Command -Session $direct -ScriptBlock {
        param($boot,$transfer)
        $path='C:\K5GuestWitness\public-config.json';$configuration=Get-Content -LiteralPath $path -Raw|ConvertFrom-Json
        $configuration.phase='signout';$configuration.previous_boot_id=$boot
        [IO.File]::WriteAllText($path,($configuration|ConvertTo-Json -Depth 5),[Text.UTF8Encoding]::new($false))
        Start-ScheduledTask -TaskName ('K5GuestWitness-'+$transfer)
    } -ArgumentList $reboot.boot_id,$transfer -ErrorAction Stop
    Remove-PSSession -Session $direct;$direct=$null
    $signoutSeal=Kvp-Seal 'signout' 120
    $direct=Open-Direct
    $signout=Read-GuestReceipt 'signout' $signoutSeal;$receipt.signout_witness=$signout
    if($signout.status -cne 'signout_sealed' -or -not $signout.recording_after_signout -or $signout.boot_id -cne $reboot.boot_id){throw 'Signout recording witness failed'}
    $receipt.recording_after_actual_signout=$true
    $receipt.stage='retained_media_decode'
    $relative=Invoke-Command -Session $direct -ScriptBlock {
        $baseline=Get-Content -LiteralPath 'C:\K5GuestWitness\baseline.json' -Raw|ConvertFrom-Json
        [string]$baseline.files[0].path
    } -ErrorAction Stop
    if($relative.Length -gt 512 -or $relative -notmatch '^[A-Za-z0-9_./\\+\-]+\.mp4$' -or $relative.Contains('..') -or [IO.Path]::IsPathRooted($relative)){throw 'Retained path refused'}
    $retained=Join-Path $root 'retained.mp4'
    Copy-Item -FromSession $direct -LiteralPath ('C:\ProgramData\K5Vision\recordings\'+$relative) -Destination $retained -ErrorAction Stop
    $probeInfo=[Diagnostics.ProcessStartInfo]::new($FFprobe,('-v error -select_streams v:0 -count_frames -show_entries stream=codec_name,nb_read_frames -of json '+$retained))
    $probeInfo.UseShellExecute=$false;$probeInfo.CreateNoWindow=$true;$probeInfo.RedirectStandardOutput=$true;$probeInfo.RedirectStandardError=$true
    $probe=[Diagnostics.Process]::Start($probeInfo);$probe.BeginErrorReadLine();$probeOutput=$probe.StandardOutput.ReadToEndAsync()
    try{
        if(-not $probe.WaitForExit(30000) -or $probe.ExitCode -ne 0){throw 'Retained frame inspection failed'}
        $probeJson=$probeOutput.GetAwaiter().GetResult()
        if($probeJson.Length -gt 65536){throw 'Retained inspection output too large'}
        $inspected=$probeJson|ConvertFrom-Json
        if(@($inspected.streams).Count -ne 1 -or [string]$inspected.streams[0].nb_read_frames -notmatch '^\d+$' -or [long]$inspected.streams[0].nb_read_frames -lt 10){throw 'Retained clip has insufficient decoded frames'}
        $receipt.retained_frames_inspected=[long]$inspected.streams[0].nb_read_frames
    }finally{if(-not $probe.HasExited){$probe.Kill();$probe.WaitForExit(2000)|Out-Null};$probe.Dispose()}
    $decode=Start-OwnedProcess $FFmpeg ('-nostdin -v error -i '+$retained+' -frames:v 10 -f null -')
    try{if(-not $decode.WaitForExit(30000) -or $decode.ExitCode -ne 0){throw 'Retained media decode failed'}}finally{if(-not $decode.HasExited){$decode.Kill();$decode.WaitForExit(2000)|Out-Null};$decode.Dispose()}
    $receipt.retained_media_decoded=$true;$receipt.retained_decoded_frames=10
    $receipt.outcome='installer_guest_recording_observed';$receipt.stage='complete'
    }
}catch{
    $receipt.failure_kind=$_.Exception.GetType().Name;$receipt.failure_hresult=$_.Exception.HResult
    $receipt.failure_exception_chain=@()
    $inner=$_.Exception
    for($depth=0;$null -ne $inner -and $depth -lt 8;$depth++){
        $kind=$inner.GetType().Name
        if($kind -notmatch '^[A-Za-z][A-Za-z0-9_]{0,63}$'){$kind='Other'}
        $entry=@{kind=$kind;hresult=$inner.HResult}
        if($inner -is [ComponentModel.Win32Exception]){$entry.native_code=$inner.NativeErrorCode}
        if($inner -is [Net.WebException]){$entry.transport_status=[string]$inner.Status}
        $receipt.failure_exception_chain+=$entry
        $inner=$inner.InnerException
    }
    $knownReasons=@{'Isolated fixture subnet unavailable'='fixture_subnet_unavailable';'Owned internal adapter unavailable'='owned_internal_adapter_unavailable';'Owned generated source ended'='owned_generated_source_ended';'IPv4 required'='invalid_route_address_family';'Owned address did not become preferred'='owned_address_not_preferred'}
    $receipt.failure_reason='other'
    if($knownReasons.ContainsKey($_.Exception.Message)){$receipt.failure_reason=$knownReasons[$_.Exception.Message]}
    foreach($entry in @(@{name='relay';process=$mediaProcess},@{name='publisher';process=$publisherProcess})){
        if($null -ne $entry.process){
            $exited=$entry.process.HasExited;$receipt[($entry.name+'_exited_at_failure')]=$exited
            if($exited){$receipt[($entry.name+'_exit_code_at_failure')]=$entry.process.ExitCode}
            if($processNotes.ContainsKey($entry.process)){
                $notes=$processNotes[$entry.process]
                $receipt[($entry.name+'_output_flags')]=@($notes.Flags())
                $receipt[($entry.name+'_captured_characters')]=$notes.CapturedCharacters
            }
        }
    }
    if($null -ne $hostInterface -and $null -ne $hostIPAddress){
        try{$receipt.host_address_state_at_failure=Owned-AddressState}catch{$receipt.host_address_observation_failed=$true}
    }
    if($_.Exception -is [ComponentModel.Win32Exception]){$receipt.failure_native_code=$_.Exception.NativeErrorCode}
    if($null -ne $vmId -and $receipt.stage -ceq 'actual_guest_boot' -and -not $receipt.actual_guest_boot){
        try{$receipt.bootstrap_failure_live=Read-BootFailureLive}catch{$receipt.bootstrap_live_diagnostic_error=$_.Exception.GetType().Name}
    }
}finally{
    $password=$null
    if($null -ne $credential){$credential.Password.Dispose();$credential=$null}
    try{
        if($null -ne $direct){Remove-PSSession -Session $direct;$direct=$null}
        if($null -ne $vmId){
            $vm=Owned-VM
            if([string]$vm.State -ne 'Off'){Bounded-Job (Stop-VM -VM $vm -TurnOff -Force -AsJob -Confirm:$false) 30|Out-Null}
            $vm=Owned-VM;if([string]$vm.State -ne 'Off'){throw 'Owned VM did not stop'}
            Bounded-Job (Remove-VM -VM $vm -Force -AsJob -Confirm:$false) 30|Out-Null
            if($null -ne (Get-VM -Id $vmId -ErrorAction SilentlyContinue)){throw 'Owned VM remains'}
            if($receipt.stage -ceq 'bootstrap_observation'){
                try{$receipt.bootstrap_observation_offline=Read-BootFailureOffline}catch{$receipt.bootstrap_offline_diagnostic_error=$_.Exception.GetType().Name}
            }elseif($receipt.stage -ceq 'actual_guest_boot' -and -not $receipt.actual_guest_boot){
                try{$receipt.bootstrap_failure_offline=Read-BootFailureOffline}catch{$receipt.bootstrap_offline_diagnostic_error=$_.Exception.GetType().Name}
            }
        }
        foreach($process in @($publisherProcess,$mediaProcess)){
            if($null -ne $process){if(-not $process.HasExited){$process.Kill();if(-not $process.WaitForExit(5000)){throw 'Owned source process remains'}};$process.Dispose()}
        }
        if($null -ne $firewallName){Remove-NetFirewallRule -Name $firewallName -ErrorAction Stop}
        if($null -ne $switchId){
            $switch=Get-VMSwitch -Id $switchId
            if($switch.Name -cne $switchName -or [string]$switch.SwitchType -ne 'Internal'){throw 'Owned internal switch changed'}
            if($null -ne $hostInterface){Get-NetIPAddress -InterfaceIndex $hostInterface -IPAddress $hostIPAddress -ErrorAction SilentlyContinue|Remove-NetIPAddress -Confirm:$false}
            Remove-VMSwitch -VMSwitch $switch -Force -Confirm:$false
        }
        if($mounted){
            $attached=Get-VHD -Path $image
            if(-not $attached.Attached -or [int]$attached.DiskNumber -ne $diskNumber -or [string](Get-Disk -Number $diskNumber).UniqueId -cne $diskId){throw 'Owned disk mount changed'}
            if($null -ne $partitionNumber){Remove-PartitionAccessPath -DiskNumber $diskNumber -PartitionNumber $partitionNumber -AccessPath $accessPath}
            Dismount-VHD -Path $image;$mounted=$false
        }
        if($null -ne $root){
            Assert-Root
            if(@(Get-ChildItem -LiteralPath $root -Recurse -Force|Where-Object{($_.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0}).Count){throw 'Owned guest tree has reparse point'}
            Remove-Item -LiteralPath $root -Recurse -Force
            if(Test-Path -LiteralPath $root){throw 'Owned guest tree remains'}
        }
    }catch{$receipt.cleanup_complete=$false;$receipt.cleanup_hresult=$_.Exception.HResult}
}
$receipt|ConvertTo-Json -Depth 15 -Compress
if(-not $receipt.cleanup_complete){exit 2}
exit 0
