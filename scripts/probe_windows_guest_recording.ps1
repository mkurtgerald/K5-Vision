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
    [Parameter(Mandatory=$true)][string]$FFprobe
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
    github_credentials_entered_guest=$false; cleanup_complete=$true; stage='host_guard'
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
        $settings=@($computer.GetRelated('Msvm_VirtualSystemSettingData')|Where-Object{$_.VirtualSystemType -ceq 'Microsoft:Hyper-V:System:Realized'})
        $manager=Get-WmiObject -Namespace root\virtualization\v2 -Class Msvm_VirtualSystemManagementService
        if($settings.Count -ne 1){throw 'Actual VM settings unavailable'}
        $arguments=$manager.GetMethodParameters('GetVirtualSystemThumbnailImage')
        $arguments['TargetSystem']=$settings[0].Path.Path;$arguments['WidthPixels']=[uint16]640;$arguments['HeightPixels']=[uint16]480
        $thumbnail=$manager.InvokeMethod('GetVirtualSystemThumbnailImage',$arguments,$null)
        $facts.thumbnail_return_code=[int]$thumbnail.ReturnValue
        if($thumbnail.ReturnValue -eq 0){
            $pixels=[byte[]]$thumbnail.ImageData
            if($pixels.Length -ne 640*480*2){throw 'Thumbnail byte geometry differs'}
            Add-Type -AssemblyName System.Drawing
            $bitmap=[Drawing.Bitmap]::new(640,480,[Drawing.Imaging.PixelFormat]::Format16bppRgb565)
            $buffer=$null;$png=[IO.MemoryStream]::new()
            try{
                $buffer=$bitmap.LockBits([Drawing.Rectangle]::new(0,0,640,480),[Drawing.Imaging.ImageLockMode]::WriteOnly,[Drawing.Imaging.PixelFormat]::Format16bppRgb565)
                if($buffer.Stride -ne 1280){throw 'Thumbnail stride differs'}
                [Runtime.InteropServices.Marshal]::Copy($pixels,0,$buffer.Scan0,$pixels.Length)
                $bitmap.UnlockBits($buffer);$buffer=$null
                $bitmap.Save($png,[Drawing.Imaging.ImageFormat]::Png)
                if($png.Length -gt 1048576){throw 'Thumbnail PNG bound exceeded'}
                $facts.thumbnail_png_base64=[Convert]::ToBase64String($png.ToArray())
            }finally{if($null -ne $buffer){$bitmap.UnlockBits($buffer)};$bitmap.Dispose();$png.Dispose()}
        }
    }catch{$facts.thumbnail_failure_kind=$_.Exception.GetType().Name;$facts.thumbnail_failure_hresult=$_.Exception.HResult}
    return $facts
}
function Read-BootFailureOffline {
    Assert-Root
    $facts=@{read_only_mount=$false;known_files=@{};own_receipts=@{};setup_logs=@{}}
    $attached=$null;$diagnosticPartition=$null;$diagnosticMount=(Join-Path $root 'failure-offline')+'\'
    try{
        New-Item -ItemType Directory -Path $diagnosticMount|Out-Null
        $attached=Mount-VHD -Path $image -ReadOnly -NoDriveLetter -PassThru
        $diagnosticDisk=[int]$attached.DiskNumber
        $disk=Get-Disk -Number $diagnosticDisk
        if(-not $disk.IsReadOnly -or [string]$disk.UniqueId -cne $diskId){throw 'Owned read-only diagnostic disk differs'}
        $facts.read_only_mount=$true
        foreach($partition in @(Get-Partition -DiskNumber $diagnosticDisk|Where-Object{$_.Size -gt 1GB})){
            $diagnosticPartition=[int]$partition.PartitionNumber
            Add-PartitionAccessPath -DiskNumber $diagnosticDisk -PartitionNumber $diagnosticPartition -AccessPath $diagnosticMount
            if(Test-Path -LiteralPath (Join-Path $diagnosticMount 'Windows\System32\config\SOFTWARE')){break}
            Remove-PartitionAccessPath -DiskNumber $diagnosticDisk -PartitionNumber $diagnosticPartition -AccessPath $diagnosticMount;$diagnosticPartition=$null
        }
        if($null -eq $diagnosticPartition){throw 'Diagnostic Windows volume unavailable'}
        $known=@{
          installed_launcher='Program Files\K5 Vision\K5Vision.exe';identity='ProgramData\K5Vision\identity\identity.json';
          answer_file='Windows\Panther\Unattend.xml';setup_complete='Windows\Setup\Scripts\SetupComplete.cmd';
          setup_task=('Windows\System32\Tasks\K5GuestWitness-'+$transfer);bootstrap_task=('Windows\System32\Tasks\K5GuestBootstrap-'+$transfer)
        }
        foreach($entry in $known.GetEnumerator()){$facts.known_files[$entry.Key]=[bool](Test-Path -LiteralPath (Join-Path $diagnosticMount $entry.Value) -PathType Leaf)}
        foreach($name in @('setup-failure.json','bootstrap-receipt.json','bootstrap-witness.json')){
            $path=Join-Path $diagnosticMount ('K5GuestWitness\'+$name)
            if(-not(Test-Path -LiteralPath $path -PathType Leaf)){continue}
            $info=Get-Item -LiteralPath $path
            if($info.Length -gt 65536 -or ($info.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0){continue}
            try{
                $parsed=Get-Content -LiteralPath $path -Raw|ConvertFrom-Json;$safe=@{}
                foreach($field in @('status','phase','stage','failure_stage','error_kind','failure_type','hresult','failure_hresult','http_status','mfa_enabled_before_reboot','mfa_confirmed_via_normal_api','gui_admin_setup','camera_source_enrolled','service_running','gui_client_closed','cleanup_credentials_removed')){
                    $property=$parsed.PSObject.Properties[$field]
                    if($null -eq $property){continue};$value=$property.Value
                    if($value -is [bool] -or $value -is [int] -or $value -is [long]){$safe[$field]=$value}
                    elseif($value -is [string] -and $value -match '^[A-Za-z][A-Za-z0-9_.-]{0,79}$'){$safe[$field]=$value}
                }
                $facts.own_receipts[$name]=$safe
            }catch{$facts.own_receipts[$name]=@{parse_failed=$true}}
        }
        foreach($relative in @('Windows\Panther\setupact.log','Windows\Panther\setuperr.log','Windows\Panther\UnattendGC\setupact.log','Windows\Panther\UnattendGC\setuperr.log')){
            $path=Join-Path $diagnosticMount $relative
            if(-not(Test-Path -LiteralPath $path -PathType Leaf)){continue}
            $info=Get-Item -LiteralPath $path
            if($info.Length -gt 16777216 -or ($info.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0){$facts.setup_logs[$relative]=@{bounded_read_refused=$true};continue}
            $text=[IO.File]::ReadAllText($path)
            $flags=@{bytes=$info.Length;unattend_error=($text -match '(?is)(?:could not parse|failed[^\r\n]{0,120}unattend|unattend[^\r\n]{0,120}(?:error|fail))');oobe_seen=($text -match '(?i)(?:oobeSystem|windeploy|oobe)');setup_complete_seen=($text -match '(?i)SetupComplete\.cmd');setup_complete_error=($text -match '(?i)(?:SetupComplete[^\r\n]{0,120}(?:fail|error)|(?:fail|error)[^\r\n]{0,120}SetupComplete)')}
            $codes=@([regex]::Matches($text,'(?i)(?:HRESULT|\bhr|error(?:\s+code)?)\s*[:= ]\s*(0x[0-9a-f]{8})')|ForEach-Object{$_.Groups[1].Value.ToLowerInvariant()}|Select-Object -Unique -First 16)
            $flags.error_codes=$codes;$facts.setup_logs[$relative]=$flags;$text=$null
        }
    }finally{
        if($null -ne $attached){
            $admitted=Get-VHD -Path $image
            if(-not $admitted.Attached -or [int]$admitted.DiskNumber -ne [int]$attached.DiskNumber){throw 'Diagnostic disk attachment changed'}
            if($null -ne $diagnosticPartition){Remove-PartitionAccessPath -DiskNumber ([int]$attached.DiskNumber) -PartitionNumber $diagnosticPartition -AccessPath $diagnosticMount}
            Dismount-VHD -Path $image
            if((Get-VHD -Path $image).Attached){throw 'Diagnostic disk remains mounted'}
        }
    }
    return $facts
}
try{
    if($env:GITHUB_ACTIONS -ne 'true' -or $env:RUNNER_ENVIRONMENT -ne 'github-hosted' -or $env:RUNNER_OS -ne 'Windows'){throw 'Disposable Windows host required'}
    Assert-CopyContracts
    $receipt.compiled_copy_contracts_passed=$true
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
    $handler=[Net.Http.HttpClientHandler]::new();$handler.AllowAutoRedirect=$false
    $client=[Net.Http.HttpClient]::new($handler);$client.Timeout=[TimeSpan]::FromMinutes(12)
    $cancel=[Threading.CancellationTokenSource]::new(720000)
    $response=$null;$input=$null;$output=$null;$copier=$null
    try{
        $url='https://software-static.download.prss.microsoft.com/dbazure/888969d5-f34g-4e03-ac9d-1f9786c66749/26100.1742.amd64fre.ge_release_svc_refresh.240906-0331_server_serverdatacentereval_en-us.vhdx'
        $response=$client.GetAsync($url,[Net.Http.HttpCompletionOption]::ResponseHeadersRead,$cancel.Token).GetAwaiter().GetResult()
        $receipt.download_response_status=[int]$response.StatusCode
        $receipt.download_content_length=[long]$response.Content.Headers.ContentLength
        if([int]$response.StatusCode -ne 200 -or $response.Content.Headers.ContentLength -ne $imageBytes){throw 'Official image response differs'}
        $receipt.download_subphase='body_stream'
        $input=$response.Content.ReadAsStreamAsync().GetAwaiter().GetResult()
        $output=[IO.FileStream]::new($image,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None,1048576,([IO.FileOptions]::Asynchronous -bor [IO.FileOptions]::SequentialScan))
        $copier=[K5GuestBoundedCopy]::new()
        # Keep TLS-fragment read/write dispatch inside compiled .NET rather
        # than invoking the PowerShell binder once per network fragment.
        [void]$copier.Copy($input,$output,$imageBytes,$cancel.Token).GetAwaiter().GetResult()
        $downloaded=$copier.Received
        $receipt.download_subphase='durable_flush'
        $output.Flush($true)
        $receipt.download_copy_complete=$true
    }finally{
        if($null -ne $copier){$downloaded=$copier.Received}
        $receipt.downloaded_bytes=$downloaded
        $receipt.download_elapsed_seconds=[Math]::Round($downloadWatch.Elapsed.TotalSeconds,3)
        $receipt.download_cancellation_requested=$cancel.IsCancellationRequested
        if($null -ne $output){$output.Dispose()};if($null -ne $input){$input.Dispose()};if($null -ne $response){$response.Dispose()}
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
<unattend xmlns="urn:schemas-microsoft-com:unattend">
 <settings pass="specialize"><component name="Microsoft-Windows-Shell-Setup" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS"><ComputerName>K5GUEST</ComputerName><TimeZone>UTC</TimeZone></component></settings>
 <settings pass="oobeSystem"><component name="Microsoft-Windows-Shell-Setup" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS">
 <OOBE><HideEULAPage>true</HideEULAPage><HideOnlineAccountScreens>true</HideOnlineAccountScreens><HideWirelessSetupInOOBE>true</HideWirelessSetupInOOBE><ProtectYourPC>3</ProtectYourPC></OOBE>
 <UserAccounts><AdministratorPassword><Value>$escaped</Value><PlainText>true</PlainText></AdministratorPassword></UserAccounts>
 <AutoLogon><Password><Value>$escaped</Value><PlainText>true</PlainText></Password><Enabled>true</Enabled><LogonCount>1</LogonCount><Username>Administrator</Username><Domain>.</Domain></AutoLogon>
 </component></settings>
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
    $setupScripts=Join-Path $windows 'Windows\Setup\Scripts';if(-not(Test-Path -LiteralPath $setupScripts)){New-Item -ItemType Directory -Path $setupScripts|Out-Null}
    $complete=Join-Path $setupScripts 'SetupComplete.cmd';$prior=''
    if(Test-Path -LiteralPath $complete){Copy-Item -LiteralPath $complete -Destination (Join-Path $guestRoot 'SetupComplete.original.cmd');$prior='call C:\K5GuestWitness\SetupComplete.original.cmd'+"`r`n"}
    [IO.File]::WriteAllText($complete,('@echo off'+"`r`n"+$prior+'%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File C:\K5GuestWitness\windows_guest_witness.ps1 -Mode Setup'+"`r`n"+'exit /b %errorlevel%'+"`r`n"),[Text.ASCIIEncoding]::new())
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
            if($receipt.stage -ceq 'actual_guest_boot' -and -not $receipt.actual_guest_boot){
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
