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
    [switch]$BootstrapObservation,
    [ValidateSet('hard-reset','planned-shutdown-start')][string]$RebootOperation='hard-reset'
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
    recording_after_actual_signout=$false; retained_media_decoded=$false; postboot_prelogin_media_decoded=$false;
    github_credentials_entered_guest=$false; cleanup_complete=$true; stage='host_guard';
    bootstrap_observation_only=$BootstrapObservation.IsPresent; reboot_operation=$RebootOperation; reboot_operation_executed=$false
    proof_scope=if($RebootOperation -ceq 'planned-shutdown-start'){'planned-shutdown-start-recording'}else{'hard-reset-recording'}
}
$root=$null; $rootIdentity=$null; $image=$null; $mounted=$false; $diskNumber=$null; $diskId=$null
$partitionNumber=$null; $guestPartitionNumber=$null; $accessPath=$null; $vmId=$null; $vmName=$null; $switchId=$null; $switchName=$null
$hostInterface=$null; $hostIPAddress=$null; $firewallName=$null; $direct=$null; $password=$null; $credential=$null
$mediaProcess=$null; $publisherProcess=$null; $plannedBoundaryMountIntent=$null
$processNotes=@{}
Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.ComponentModel;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
public sealed class K5GuestConfigSnapshot {
  public string Identity,SecuritySha256,ContentSha256,Text;
  public long Length;
}
public static class K5GuestFileIdentity {
  [StructLayout(LayoutKind.Sequential)] struct Information {
    public uint Attributes;
    public System.Runtime.InteropServices.ComTypes.FILETIME Creation,Access,Write;
    public uint VolumeSerial,SizeHigh,SizeLow,Links,IndexHigh,IndexLow;
  }
  [DllImport("kernel32.dll",CharSet=CharSet.Unicode,ExactSpelling=true,SetLastError=true)] static extern SafeFileHandle CreateFileW(string path,uint access,uint share,IntPtr security,uint creation,uint flags,IntPtr template);
  [DllImport("kernel32.dll",ExactSpelling=true,SetLastError=true)] [return:MarshalAs(UnmanagedType.Bool)] static extern bool GetFileInformationByHandle(SafeFileHandle handle,out Information information);
  [DllImport("advapi32.dll",ExactSpelling=true,SetLastError=true)] [return:MarshalAs(UnmanagedType.Bool)] static extern bool GetKernelObjectSecurity(SafeFileHandle handle,uint sections,[Out] byte[] descriptor,uint bytes,out uint needed);
  static string Identity(SafeFileHandle handle) {
    if(!Environment.Is64BitProcess || Marshal.SizeOf(typeof(Information))!=52 || handle.IsInvalid) throw new InvalidOperationException();
    Information information;if(!GetFileInformationByHandle(handle,out information)) throw new Win32Exception();
    if((information.Attributes&0x410)!=0 || information.Links!=1) throw new InvalidOperationException();
    return information.VolumeSerial.ToString("x8")+"-"+information.IndexHigh.ToString("x8")+information.IndexLow.ToString("x8");
  }
  static string Hash(byte[] bytes) {
    using(var sha=System.Security.Cryptography.SHA256.Create())return BitConverter.ToString(sha.ComputeHash(bytes)).Replace("-","").ToLowerInvariant();
  }
  static string PrivateSecurity(SafeFileHandle handle) {
    uint needed;bool initial=GetKernelObjectSecurity(handle,7,null,0,out needed);int error=Marshal.GetLastWin32Error();
    if(initial || error!=122 || needed<20 || needed>65536) throw new Win32Exception(error);
    var descriptor=new byte[needed];uint returned;
    if(!GetKernelObjectSecurity(handle,7,descriptor,(uint)descriptor.Length,out returned)) throw new Win32Exception();
    if(returned<20 || returned>descriptor.Length) throw new InvalidOperationException();
    if(returned!=descriptor.Length)Array.Resize(ref descriptor,(int)returned);
    var security=new System.Security.AccessControl.RawSecurityDescriptor(descriptor,0);
    var flags=System.Security.AccessControl.ControlFlags.DiscretionaryAclPresent|System.Security.AccessControl.ControlFlags.DiscretionaryAclProtected;
    if((security.ControlFlags&flags)!=flags || security.Owner==null || (security.Owner.Value!="S-1-5-18" && security.Owner.Value!="S-1-5-32-544") || security.DiscretionaryAcl==null || security.DiscretionaryAcl.Count!=2)throw new InvalidOperationException();
    var admitted=new HashSet<string>(StringComparer.Ordinal);
    foreach(System.Security.AccessControl.GenericAce generic in security.DiscretionaryAcl){
      var ace=generic as System.Security.AccessControl.CommonAce;
      if(ace==null || ace.AceQualifier!=System.Security.AccessControl.AceQualifier.AccessAllowed || ace.AceFlags!=System.Security.AccessControl.AceFlags.None || ace.AccessMask!=0x1f01ff || (ace.SecurityIdentifier.Value!="S-1-5-18" && ace.SecurityIdentifier.Value!="S-1-5-32-544") || !admitted.Add(ace.SecurityIdentifier.Value))throw new InvalidOperationException();
    }
    return Hash(descriptor);
  }
  public static string Read(string path) {
    using(var handle=CreateFileW(path,0x80,7,IntPtr.Zero,3,0x00200000,IntPtr.Zero))return Identity(handle);
  }
  public static K5GuestConfigSnapshot ReadConfig(string path) {
    using(var handle=CreateFileW(path,0x80020080,1,IntPtr.Zero,3,0x00200000,IntPtr.Zero)){
      var snapshot=new K5GuestConfigSnapshot();snapshot.Identity=Identity(handle);snapshot.SecuritySha256=PrivateSecurity(handle);
      using(var stream=new FileStream(handle,FileAccess.Read,4096,false)){
        snapshot.Length=stream.Length;if(snapshot.Length<2 || snapshot.Length>65536)throw new InvalidOperationException();
        var bytes=new byte[(int)snapshot.Length];int offset=0;
        while(offset<bytes.Length){int got=stream.Read(bytes,offset,bytes.Length-offset);if(got==0)throw new EndOfStreamException();offset+=got;}
        if(stream.ReadByte()!=-1)throw new InvalidOperationException();
        snapshot.ContentSha256=Hash(bytes);snapshot.Text=new UTF8Encoding(false,true).GetString(bytes);
      }
      return snapshot;
    }
  }
  public static void WriteConfig(string path,string expectedIdentity,string expectedSecurity,string expectedContent,long expectedLength,string text) {
    byte[] bytes=new UTF8Encoding(false,true).GetBytes(text);
    if(bytes.Length<2 || bytes.Length>65536 || expectedLength<2 || expectedLength>65536)throw new InvalidOperationException();
    // Acquire and admit this exact write handle before any truncation. ShareRead
    // denies other data writers and deletion; never follow a leaf reparse point.
    using(var handle=CreateFileW(path,0xc0020080,1,IntPtr.Zero,3,0x80200000,IntPtr.Zero)){
      if(Identity(handle)!=expectedIdentity || PrivateSecurity(handle)!=expectedSecurity)throw new InvalidOperationException();
      using(var stream=new FileStream(handle,FileAccess.ReadWrite,4096,false)){
        if(stream.Length!=expectedLength)throw new InvalidOperationException();
        string current;using(var sha=System.Security.Cryptography.SHA256.Create())current=BitConverter.ToString(sha.ComputeHash(stream)).Replace("-","").ToLowerInvariant();
        if(current!=expectedContent || stream.Position!=expectedLength)throw new InvalidOperationException();
        stream.Position=0;stream.SetLength(0);stream.Write(bytes,0,bytes.Length);stream.Flush(true);
        if(stream.Length!=bytes.Length || Identity(handle)!=expectedIdentity || PrivateSecurity(handle)!=expectedSecurity)throw new InvalidOperationException();
      }
    }
  }
}
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
function Planned-BoundaryGuard {
    if($script:plannedBoundaryWatch.ElapsedMilliseconds -ge 40000){throw [TimeoutException]::new('Planned offline boundary exceeded its bound')}
    $vm=Owned-VM
    if([string]$vm.State -cne 'Off'){throw 'Planned boundary requires exact owned Off'}
    if($script:plannedBoundaryWatch.ElapsedMilliseconds -ge 40000){throw [TimeoutException]::new('Planned offline ownership query exceeded its bound')}
}
function Offline-File([string]$Volume,[string]$Relative) {
    $current=$Volume
    foreach($part in $Relative.Split('\')){
        if($part -notmatch '^[A-Za-z0-9_. -]+$' -or $part -in @('.','..')){throw 'Offline path refused'}
        $current=Join-Path $current $part
        if(((Get-Item -LiteralPath $current -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0){throw 'Offline path reparse point refused'}
    }
    return $current
}
function File-Digest([string]$Path){return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()}
function Text-Digest([string]$Text){
    $sha=[Security.Cryptography.SHA256]::Create()
    try{return [BitConverter]::ToString($sha.ComputeHash([Text.UTF8Encoding]::new($false).GetBytes($Text))).Replace('-','').ToLowerInvariant()}finally{$sha.Dispose()}
}
function Config-Custody([string]$Volume){
    $directory=Offline-File $Volume 'K5GuestWitness'
    $path=Offline-File $Volume 'K5GuestWitness\public-config.json'
    foreach($item in @($directory,$path)){
        $acl=Get-Acl -LiteralPath $item
        if(-not $acl.AreAccessRulesProtected -or $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value -notin @('S-1-5-18','S-1-5-32-544')){throw 'Planned config private owner or DACL refused'}
        $rules=@($acl.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier]))
        if($rules.Count -ne 2 -or @($rules|ForEach-Object{$_.IdentityReference.Value}|Select-Object -Unique).Count -ne 2){throw 'Planned config ACL cardinality refused'}
        foreach($rule in $rules){
            if($rule.IdentityReference.Value -notin @('S-1-5-18','S-1-5-32-544') -or $rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow -or $rule.IsInherited -or $rule.FileSystemRights -ne [Security.AccessControl.FileSystemRights]::FullControl){throw 'Planned config foreign or nonprivate ACL refused'}
        }
    }
    $snapshot=[K5GuestFileIdentity]::ReadConfig($path)
    return @{path=$path;identity=$snapshot.Identity;acl_sha256=$snapshot.SecuritySha256;sha256=$snapshot.ContentSha256;text=$snapshot.Text;length=$snapshot.Length}
}
function Write-PlannedConfig([string]$Volume,[hashtable]$Expected,[string]$Text){
    $before=Config-Custody $Volume
    if($before.identity -cne $Expected.identity -or $before.acl_sha256 -cne $Expected.acl_sha256 -or $before.sha256 -cne $Expected.sha256){throw 'Planned config changed before write'}
    [K5GuestFileIdentity]::WriteConfig($before.path,$Expected.identity,$Expected.acl_sha256,$Expected.sha256,[long]$Expected.length,$Text)
    $after=Config-Custody $Volume
    if($after.identity -cne $before.identity -or $after.acl_sha256 -cne $before.acl_sha256 -or $after.text -cne $Text){throw 'Planned config file identity, DACL or durable text changed'}
    return $after
}
function Assert-PlannedCustodyContracts {
    $volume=Join-Path $root ('planned-native-custody-'+[guid]::NewGuid().ToString('N'))
    $null=Private-Directory $volume
    $directory=Join-Path $volume 'K5GuestWitness';$null=Private-Directory $directory $true
    $path=Join-Path $directory 'public-config.json';$alias=Join-Path $directory 'config-alias.json'
    try{
        [IO.File]::WriteAllText($path,'{"phase":"owned_smoke_before"}',[Text.UTF8Encoding]::new($false))
        $security=[Security.AccessControl.FileSecurity]::new();$security.SetOwner([Security.Principal.SecurityIdentifier]::new('S-1-5-32-544'));$security.SetAccessRuleProtection($true,$false)
        foreach($sid in @('S-1-5-18','S-1-5-32-544')){$security.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new([Security.Principal.SecurityIdentifier]::new($sid),'FullControl','Allow'))}
        Set-Acl -LiteralPath $path -AclObject $security
        $before=Config-Custody $volume;$after=Write-PlannedConfig $volume $before '{"phase":"owned_smoke_after"}'
        if($before.identity -cne $after.identity -or $before.acl_sha256 -cne $after.acl_sha256){throw 'Native planned custody smoke differs'}
        $controls=1
        foreach($case in @('identity','security','content','length','oversized_utf8','short_utf8')){
            $current=Config-Custody $volume;$identity=$current.identity;$aclHash=$current.acl_sha256;$contentHash=$current.sha256;$length=[long]$current.length;$text='{"phase":"must_never_write"}'
            switch($case){'identity'{$identity='00000000-0000000000000000'};'security'{$aclHash='0'*64};'content'{$contentHash='0'*64};'length'{$length++};'oversized_utf8'{$text=([string][char]0x20ac)*23000};'short_utf8'{$text='{'} }
            $refused=$false
            try{[K5GuestFileIdentity]::WriteConfig($path,$identity,$aclHash,$contentHash,$length,$text)}catch{if($_.Exception.GetBaseException() -is [InvalidOperationException]){$refused=$true}else{throw}}
            if(-not $refused -or (File-Digest $path) -cne $current.sha256){throw 'Native planned no-mutation control failed'};$controls++
        }
        # Replace the name with a distinct task-owned object having matching
        # content/ACL, between the saved custody and the native write acquisition.
        $current=Config-Custody $volume;$saved=Join-Path $directory 'saved-config.json'
        Move-Item -LiteralPath $path -Destination $saved
        try{
            [IO.File]::WriteAllText($path,$current.text,[Text.UTF8Encoding]::new($false))
            Set-Acl -LiteralPath $path -AclObject (Get-Acl -LiteralPath $saved)
            $replacement=Config-Custody $volume
            if($replacement.identity -ceq $current.identity -or $replacement.sha256 -cne $current.sha256 -or $replacement.acl_sha256 -cne $current.acl_sha256){throw 'Native replacement control fixture differs'}
            $refused=$false
            try{[K5GuestFileIdentity]::WriteConfig($path,$current.identity,$current.acl_sha256,$current.sha256,[long]$current.length,'{"phase":"must_never_write"}')}catch{if($_.Exception.GetBaseException() -is [InvalidOperationException]){$refused=$true}else{throw}}
            if(-not $refused -or (File-Digest $path) -cne $replacement.sha256 -or (File-Digest $saved) -cne $current.sha256){throw 'Native replacement wrote another object'};$controls++
        }finally{if(Test-Path -LiteralPath $path){Remove-Item -LiteralPath $path -Force};Move-Item -LiteralPath $saved -Destination $path}
        $current=Config-Custody $volume;$changed=$current.text.Replace('after','AFTER')
        if($changed -ceq $current.text){throw 'Native content control fixture did not change'}
        [IO.File]::WriteAllText($path,$changed,[Text.UTF8Encoding]::new($false))
        $changedHash=File-Digest $path;$refused=$false
        try{[K5GuestFileIdentity]::WriteConfig($path,$current.identity,$current.acl_sha256,$current.sha256,[long]$current.length,'{"phase":"must_never_write"}')}catch{if($_.Exception.GetBaseException() -is [InvalidOperationException]){$refused=$true}else{throw}}
        if(-not $refused -or (File-Digest $path) -cne $changedHash){throw 'Native same-object changed content was overwritten'};$controls++
        $modified=Config-Custody $volume;$null=Write-PlannedConfig $volume $modified $current.text
        New-Item -ItemType HardLink -Path $alias -Target $path|Out-Null
        $refused=$false;try{$null=[K5GuestFileIdentity]::Read($path)}catch{$refused=$true}
        if(-not $refused){throw 'Native planned hardlink smoke was accepted'}
        Remove-Item -LiteralPath $alias -Force
        if([K5GuestFileIdentity]::Read($path) -cne $before.identity){throw 'Native planned identity changed after alias removal'}
        $controls++
        $receipt.planned_native_config_custody_controls_passed=$true;$receipt.planned_native_config_custody_control_count=$controls
    }finally{
        if(Test-Path -LiteralPath $alias){Remove-Item -LiteralPath $alias -Force}
        Remove-Item -LiteralPath $volume -Recurse -Force
        if(Test-Path -LiteralPath $volume){throw 'Native planned custody smoke remains'}
    }
}
function Invoke-PlannedOffline([bool]$ReadOnly,[scriptblock]$Action){
    Planned-BoundaryGuard
    if((Get-VHD -Path $image).Attached){throw 'Planned disk already attached'}
    Planned-BoundaryGuard
    $mount=(Join-Path $root ('planned-boundary-'+[guid]::NewGuid().ToString('N')))+'\'
    New-Item -ItemType Directory -Path $mount|Out-Null
    $mountAttempted=$false;$partitionAttempted=$false
    try{
        Planned-BoundaryGuard
        # Record intent before native operations: a cmdlet can mutate then throw.
        $mountAttempted=$true
        $script:plannedBoundaryMountIntent=@{access_path=$mount;partition_number=$guestPartitionNumber}
        $attached=Mount-VHD -Path $image -ReadOnly:$ReadOnly -NoDriveLetter -PassThru
        $script:mounted=$true;$script:diskNumber=[int]$attached.DiskNumber;$script:accessPath=$mount;$script:partitionNumber=$null
        $disk=Get-Disk -Number $script:diskNumber
        if([string]$disk.UniqueId -cne $diskId -or [bool]$disk.IsReadOnly -ne $ReadOnly){throw 'Planned disk identity or access differs'}
        Planned-BoundaryGuard
        $partition=Get-Partition -DiskNumber $script:diskNumber -PartitionNumber $guestPartitionNumber
        if($partition.DiskNumber -ne $script:diskNumber -or $partition.PartitionNumber -ne $guestPartitionNumber){throw 'Planned Windows partition differs'}
        Planned-BoundaryGuard
        $partitionAttempted=$true;$script:partitionNumber=$guestPartitionNumber
        Add-PartitionAccessPath -DiskNumber $script:diskNumber -PartitionNumber $guestPartitionNumber -AccessPath $mount
        Planned-BoundaryGuard
        $value=&$Action $mount
        Planned-BoundaryGuard
        return $value
    }finally{
        if($mountAttempted){
            try{
                $cleanupVM=Owned-VM
                if([string]$cleanupVM.State -cne 'Off'){throw 'Planned cleanup requires exact owned Off'}
                $current=Get-VHD -Path $image
                if($current.Attached){
                    $number=[int]$current.DiskNumber
                    if([string](Get-Disk -Number $number).UniqueId -cne $diskId){throw 'Planned cleanup foreign disk refused'}
                    $script:mounted=$true;$script:diskNumber=$number;$script:accessPath=$mount
                    # Query the exact owned access path even when Add-* threw
                    # after creating it. Never remove another path or disk.
                    $partition=Get-Partition -DiskNumber $number -PartitionNumber $guestPartitionNumber
                    $ownedPath=@($partition.AccessPaths|Where-Object{[string]::Equals([string]$_,$mount,[StringComparison]::OrdinalIgnoreCase)})
                    if($ownedPath.Count -gt 1){throw 'Planned cleanup access path differs'}
                    $pathCleanupError=$null
                    if($ownedPath.Count -eq 1){
                        try{Remove-PartitionAccessPath -DiskNumber $number -PartitionNumber $guestPartitionNumber -AccessPath $mount}catch{$pathCleanupError=$_}
                        if(@((Get-Partition -DiskNumber $number -PartitionNumber $guestPartitionNumber).AccessPaths|Where-Object{[string]::Equals([string]$_,$mount,[StringComparison]::OrdinalIgnoreCase)}).Count){throw 'Planned access path remains'}
                    }
                    $script:partitionNumber=$null
                    Dismount-VHD -Path $image
                    if((Get-VHD -Path $image).Attached){throw 'Planned disk remains attached'}
                    $script:mounted=$false
                    if($null -ne $pathCleanupError){throw $pathCleanupError}
                }
                if(((Get-Item -LiteralPath $mount -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0){throw 'Planned offline mount path remains'}
                $script:plannedBoundaryMountIntent=$null
            }catch{$receipt.cleanup_complete=$false;$receipt.planned_boundary_cleanup_failed=$true;throw}
        }
    }
}
function Set-PlannedFinalOffBoundary($Initial){
    $script:plannedBoundaryWatch=[Diagnostics.Stopwatch]::StartNew()
    $snapshot=Invoke-PlannedOffline $true {
        param($volume)
        $custody=Config-Custody $volume;$configuration=$custody.text|ConvertFrom-Json
        if($configuration.transfer_id -cne $transfer -or $configuration.head_sha -cne $ProductHead -or $configuration.setup_sha256 -cne $SetupSha256 -or $configuration.phase -cne 'reboot' -or $configuration.reboot_operation -cne 'planned-shutdown-start' -or $configuration.previous_boot_id -cne $Initial.boot_id -or $configuration.previous_boot_sequence -ne $Initial.boot_sequence){throw 'Planned config scope differs'}
        $baselinePath=Offline-File $volume 'K5GuestWitness\baseline.json'
        if((Get-Item -LiteralPath $baselinePath -Force).Length -gt 65536 -or (File-Digest $baselinePath) -cne $Initial.baseline_sha256){throw 'Planned original baseline digest differs'}
        $baseline=[IO.File]::ReadAllText($baselinePath)|ConvertFrom-Json
        if($baseline.transfer_id -cne $transfer -or $baseline.boot_id -cne $Initial.boot_id -or $baseline.boot_sequence -ne $Initial.boot_sequence){throw 'Planned original baseline scope differs'}
        $original=$baseline.security_checkpoint
        $log=Offline-File $volume 'Windows\System32\winevt\Logs\Security.evtx'
        if((Get-Item -LiteralPath $log -Force).Length -gt 67108864){throw 'Planned Security log bound exceeded'}
        $logIdentity=[K5GuestFileIdentity]::Read($log);$logHash=File-Digest $log
        $oldest=@();$tail=@();$anchor=@();$range=@()
        try{
            $oldest=@(Get-WinEvent -Path $log -Oldest -MaxEvents 1 -ErrorAction Stop)
            $tail=@(Get-WinEvent -Path $log -MaxEvents 1 -ErrorAction Stop)
            if($oldest.Count -ne 1 -or $tail.Count -ne 1 -or [long]$oldest[0].RecordId -ne $original.oldest_record_id -or [long]$tail[0].RecordId -lt $original.record_id){throw 'Planned pre-Off log reset or wrap'}
            $anchor=@(Get-WinEvent -Path $log -FilterXPath ('*[System[EventRecordID={0}]]' -f [long]$original.record_id) -MaxEvents 2 -ErrorAction Stop)
            if($anchor.Count -ne 1 -or (Text-Digest $anchor[0].ToXml()) -cne $original.xml_sha256){throw 'Planned original checkpoint not retained'}
            [long]$count=[long]$tail[0].RecordId-[long]$original.record_id
            if($count -lt 0 -or $count -gt 4096){throw 'Planned pre-Off range bound exceeded'}
            if($count -gt 0){$range=@(Get-WinEvent -Path $log -FilterXPath ('*[System[EventRecordID>{0} and EventRecordID<={1}]]' -f [long]$original.record_id,[long]$tail[0].RecordId) -Oldest -MaxEvents 4097 -ErrorAction Stop)}
            if($range.Count -ne $count){throw 'Planned pre-Off range gap'}
            [long]$expected=[long]$original.record_id+1
            # These events were present while the exact owned VM was Off.
            # Record old-boot auth honestly; the strict new-boot window begins
            # after the separately retained final-Off tail, never at4608/UTC.
            [int]$preOffInteractive=0
            foreach($event in $range){
                Planned-BoundaryGuard
                if([long]$event.RecordId -ne $expected){throw 'Planned pre-Off record order changed'};$expected++
                if($event.Id -in @(1101,1102,1104,1105,4612,4719,4912)){throw 'Planned pre-Off audit continuity lost'}
                if($event.Id -eq 4624){
                    [xml]$xml=$event.ToXml();$types=@($xml.Event.EventData.Data|Where-Object{$_.Name -ceq 'LogonType'})
                    if($types.Count -ne 1 -or [string]$types[0].'#text' -notmatch '^\d{1,2}$'){throw 'Planned pre-Off logon type unavailable'}
                    if([string]$types[0].'#text' -in @('2','7','10','11','12','13')){$preOffInteractive++}
                }
            }
            $checkpoint=@{schema_version=1;phase='reboot';reboot_operation='planned-shutdown-start';harness_head=$configuration.harness_head;witness_sha256=$configuration.witness_sha256;image_sha256=$imageSha;owned_vm_id=$vmId.ToString();transfer_id=$transfer;head_sha=$ProductHead;setup_sha256=$SetupSha256;boot_id=$Initial.boot_id;boot_sequence=[long]$Initial.boot_sequence;baseline_sha256=$Initial.baseline_sha256;record_id=[long]$tail[0].RecordId;oldest_record_id=[long]$oldest[0].RecordId;xml_sha256=(Text-Digest $tail[0].ToXml());original_record_id=[long]$original.record_id;original_oldest_record_id=[long]$original.oldest_record_id;original_xml_sha256=$original.xml_sha256;pre_off_original_checkpoint_retained=$true;pre_off_range_contiguous=$true;pre_off_interactive_logons=$preOffInteractive;pre_off_loss_or_policy_events=0}
            if([K5GuestFileIdentity]::Read($log) -cne $logIdentity -or (File-Digest $log) -cne $logHash){throw 'Planned read-only Security log changed'}
            return @{custody=$custody;checkpoint=$checkpoint;log_identity=$logIdentity;log_sha256=$logHash}
        }finally{foreach($event in (@($oldest)+@($tail)+@($anchor)+@($range))){if($event -is [IDisposable]){$event.Dispose()}}}
    }
    Planned-BoundaryGuard
    $checkpointJson=$snapshot.checkpoint|ConvertTo-Json -Depth 6 -Compress;$checkpointHash=Text-Digest $checkpointJson
    $published=Invoke-PlannedOffline $false {
        param($volume)
        $before=Config-Custody $volume
        if($before.identity -cne $snapshot.custody.identity -or $before.acl_sha256 -cne $snapshot.custody.acl_sha256 -or $before.sha256 -cne $snapshot.custody.sha256){throw 'Planned config custody changed between mounts'}
        $log=Offline-File $volume 'Windows\System32\winevt\Logs\Security.evtx'
        if([K5GuestFileIdentity]::Read($log) -cne $snapshot.log_identity -or (File-Digest $log) -cne $snapshot.log_sha256){throw 'Planned Security boundary changed before publication'}
        $configuration=$before.text|ConvertFrom-Json
        $configuration|Add-Member NoteProperty planned_owned_vm_id $vmId.ToString()
        $configuration|Add-Member NoteProperty final_off_checkpoint_json $checkpointJson
        $configuration|Add-Member NoteProperty final_off_checkpoint_sha256 $checkpointHash
        $after=Write-PlannedConfig $volume $before ($configuration|ConvertTo-Json -Depth 8 -Compress)
        $verified=$after.text|ConvertFrom-Json
        if($after.identity -cne $before.identity -or $after.acl_sha256 -cne $before.acl_sha256 -or $verified.final_off_checkpoint_sha256 -cne $checkpointHash -or $verified.final_off_checkpoint_json -cne $checkpointJson -or $verified.transfer_id -cne $transfer -or $verified.phase -cne 'reboot'){throw 'Planned config durable publication differs'}
        if([K5GuestFileIdentity]::Read($log) -cne $snapshot.log_identity -or (File-Digest $log) -cne $snapshot.log_sha256){throw 'Planned Security boundary changed during publication'}
        return @{config_identity_sha256=(Text-Digest $after.identity);config_acl_sha256=$after.acl_sha256}
    }
    Planned-BoundaryGuard
    if((Get-VHD -Path $image).Attached){throw 'Planned disk still mounted before Start'}
    Planned-BoundaryGuard
    $receipt.planned_final_off_checkpoint=$snapshot.checkpoint;$receipt.planned_final_off_checkpoint_sha256=$checkpointHash
    $receipt.planned_final_off_security_evtx_sha256=$snapshot.log_sha256;$receipt.planned_final_off_security_identity_sha256=Text-Digest $snapshot.log_identity
    $receipt.planned_config_identity_sha256=$published.config_identity_sha256;$receipt.planned_config_acl_sha256=$published.config_acl_sha256
    $receipt.planned_final_off_boundary_verified=$true;$receipt.planned_boundary_dismounted_before_start=$true
    $receipt.planned_boundary_elapsed_ms=$script:plannedBoundaryWatch.ElapsedMilliseconds
}
function Restart-OwnedGuest($Initial) {
    $operation=[Diagnostics.Stopwatch]::StartNew()
    if($RebootOperation -ceq 'hard-reset'){
        # Preserve the existing hardware-reset operation and its proof gates.
        $receipt.reboot_operation_subphase='hard_reset'
        Bounded-Job (Restart-VM -VM (Owned-VM) -Force -AsJob -Confirm:$false) 40|Out-Null
    }elseif($RebootOperation -ceq 'planned-shutdown-start'){
        # Separately labelled planned cold start. Never convert a refused
        # shutdown into a force power-off or continue to Start after refusal.
        $receipt.reboot_operation_subphase='planned_shutdown'
        $stop=[Diagnostics.Stopwatch]::StartNew()
        Bounded-Job (Stop-VM -VM (Owned-VM) -AsJob -Confirm:$false) 40|Out-Null
        $off=Owned-VM
        if([string]$off.State -cne 'Off'){throw 'Planned shutdown did not reach owned Off'}
        $stopElapsed=$stop.ElapsedMilliseconds
        if($stopElapsed -ge 40000){throw [TimeoutException]::new('Planned shutdown exceeded its bound')}
        $receipt.planned_shutdown_off_observed=$true
        $receipt.planned_shutdown_elapsed_ms=$stopElapsed
        $receipt.reboot_operation_subphase='planned_final_off_boundary'
        Set-PlannedFinalOffBoundary $Initial
        Planned-BoundaryGuard
        if($receipt.planned_boundary_elapsed_ms -lt 0 -or $receipt.planned_boundary_elapsed_ms -ge 40000){throw [TimeoutException]::new('Planned boundary was not admitted within its bound')}
        $receipt.reboot_operation_subphase='planned_start'
        $start=[Diagnostics.Stopwatch]::StartNew()
        Bounded-Job (Start-VM -VM (Owned-VM) -AsJob) 40|Out-Null
        $running=Owned-VM
        if([string]$running.State -cne 'Running'){throw 'Planned start did not reach owned Running'}
        $startElapsed=$start.ElapsedMilliseconds
        if($startElapsed -ge 40000){throw [TimeoutException]::new('Planned start exceeded its bound')}
        $receipt.planned_start_running_observed=$true
        $receipt.planned_start_elapsed_ms=$startElapsed
    }else{throw 'Unknown owned reboot operation'}
    $receipt.reboot_operation_elapsed_ms=$operation.ElapsedMilliseconds
    $receipt.reboot_operation_executed=$true
    $receipt.reboot_operation_subphase='await_sealed_system_startup'
}
function Stop-OwnedDiagnosticVM {
    $vm=Owned-VM
    if([string]$vm.State -eq 'Off'){return}
    $receipt.diagnostic_shutdown_requested_after_interval=$true
    $job=$null
    try{
        $job=Stop-VM -VM $vm -AsJob -Confirm:$false -ErrorAction Stop
        if($null -ne (Wait-Job -Job $job -Timeout 20)){Receive-Job -Job $job -ErrorAction Stop|Out-Null}
    }catch{$receipt.graceful_shutdown_error_kind=$_.Exception.GetType().Name;$receipt.graceful_shutdown_hresult=$_.Exception.HResult}
    finally{
        if($null -ne $job){
            if($job.State -in @('Running','NotStarted','Blocked')){Stop-Job -Job $job -ErrorAction SilentlyContinue}
            Remove-Job -Job $job -Force -ErrorAction SilentlyContinue
        }
    }
    $vm=Owned-VM
    if([string]$vm.State -eq 'Off'){$receipt.diagnostic_shutdown_method='graceful';return}
    $receipt.diagnostic_shutdown_method='force_fallback'
    Bounded-Job (Stop-VM -VM $vm -TurnOff -Force -AsJob -Confirm:$false) 30|Out-Null
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
        if($watch.Elapsed.TotalSeconds -ge $Seconds){throw [TimeoutException]::new('Sealed guest witness unavailable')}
        $vm=Owned-VM
        if($watch.Elapsed.TotalSeconds -ge $Seconds){throw [TimeoutException]::new('Owned VM query exceeded witness budget')}
        Assert-Publisher
        if($watch.Elapsed.TotalSeconds -ge $Seconds){throw [TimeoutException]::new('Source query exceeded witness budget')}
        if([string]$vm.State -ne 'Running'){throw 'Owned VM stopped before witness'}
        $remaining=[int][Math]::Max(1,[Math]::Ceiling($Seconds-$watch.Elapsed.TotalSeconds))
        $computer=Get-CimInstance -Namespace root/virtualization/v2 -ClassName Msvm_ComputerSystem -Filter ("Name='"+$vmId.ToString()+"'") -OperationTimeoutSec $remaining
        if($watch.Elapsed.TotalSeconds -ge $Seconds){throw [TimeoutException]::new('Guest system query exceeded witness budget')}
        $remaining=[int][Math]::Max(1,[Math]::Ceiling($Seconds-$watch.Elapsed.TotalSeconds))
        $components=@(Get-CimAssociatedInstance -InputObject $computer -ResultClassName Msvm_KvpExchangeComponent -OperationTimeoutSec $remaining)
        if($watch.Elapsed.TotalSeconds -ge $Seconds){throw [TimeoutException]::new('Guest exchange query exceeded witness budget')}
        if($components.Count -ne 1){throw 'Owned KVP component unavailable'}
        foreach($raw in @($components[0].GuestExchangeItems)){
            if($watch.Elapsed.TotalSeconds -ge $Seconds){throw [TimeoutException]::new('Guest exchange parsing exceeded witness budget')}
            if($null -eq $raw -or $raw.Length -gt 8192){continue}
            [xml]$item=$raw
            $name=$item.SelectSingleNode('/INSTANCE/PROPERTY[@NAME="Name"]/VALUE')
            if($null -eq $name -or $name.InnerText -cne ('K5GuestWitness-'+$transfer)){continue}
            $value=$item.SelectSingleNode('/INSTANCE/PROPERTY[@NAME="Data"]/VALUE')
            if($null -eq $value){continue}
            $parts=$value.InnerText.Split('|')
            if($parts.Count -eq 3 -and $parts[0] -ceq $Phase -and $parts[1] -match '^[a-f0-9-]{36}$' -and $parts[2] -match '^[a-f0-9]{64}$'){
                if($watch.Elapsed.TotalSeconds -ge $Seconds){throw [TimeoutException]::new('Guest witness arrived after budget')}
                return @{phase=$parts[0];boot_id=$parts[1];sha256=$parts[2]}
            }
            if($parts.Count -eq 3 -and $parts[0] -ceq 'failed'){throw 'Guest setup witness failed'}
        }
        $sleep=[int][Math]::Min(1000,[Math]::Max(0,[Math]::Floor(($Seconds-$watch.Elapsed.TotalSeconds)*1000)))
        if($sleep -gt 0){Start-Sleep -Milliseconds $sleep}
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
    if($result.transfer_id -cne $transfer -or $result.head_sha -cne $ProductHead -or $result.installer_sha256 -cne $SetupSha256 -or $result.boot_id -cne $Seal.boot_id -or $result.reboot_operation -cne $RebootOperation -or $result.harness_head -cne $env:GITHUB_SHA -or $result.witness_sha256 -cne (File-Digest (Join-Path $PSScriptRoot 'windows_guest_witness.ps1'))){throw 'Sealed witness scope differs'}
    return $result
}
function Assert-GuestLogonCensus($Witness,[bool]$Planned) {
    $fields=@('successful_interactive_type_logons_since_checkpoint','human_or_unclassified_interactive_logons_since_checkpoint','reserved_os_virtual_logons_since_checkpoint','reserved_window_manager_logons_since_checkpoint','reserved_font_driver_logons_since_checkpoint')
    foreach($name in $fields){
        $property=$Witness.PSObject.Properties[$name]
        if($null -eq $property){throw 'Guest logon census field unavailable'}
        $value=$property.Value
        if(($value -isnot [int] -and $value -isnot [long]) -or $value -lt 0 -or $value -gt 4096){throw 'Guest logon census count differs'}
    }
    if($Witness.human_or_unclassified_interactive_logons_since_checkpoint -ne 0 -or
       $Witness.reserved_os_virtual_logons_since_checkpoint -ne ($Witness.reserved_window_manager_logons_since_checkpoint+$Witness.reserved_font_driver_logons_since_checkpoint) -or
       $Witness.successful_interactive_type_logons_since_checkpoint -ne ($Witness.human_or_unclassified_interactive_logons_since_checkpoint+$Witness.reserved_os_virtual_logons_since_checkpoint) -or
       (-not $Planned -and $Witness.reserved_os_virtual_logons_since_checkpoint -ne 0)) {throw 'Guest logon census classification differs'}
    foreach($oldClaim in @('interactive_logons_since_boot','startup_before_interactive_login','security_all_post_off_shutdown_loss_policy_and_interactive_events_refused')){
        if($null -ne $Witness.PSObject.Properties[$oldClaim]){throw 'Ambiguous previous guest logon claim retained'}
    }
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
function Read-OwnedTaskDiagnostics([string]$WindowsVolume) {
    $facts=@{tasks=@{};task_events=@();service_events=@{}}
    foreach($role in @('observer','bootstrap')){
        $name=if($role -ceq 'observer'){'K5GuestWitness-'+$transfer}else{'K5GuestBootstrap-'+$transfer}
        $path=Join-Path $WindowsVolume ('Windows\System32\Tasks\'+$name)
        $note=@{present=[bool](Test-Path -LiteralPath $path -PathType Leaf)};$facts.tasks[$role]=$note
        if(-not $note.present){continue}
        try{
            $info=Get-Item -LiteralPath $path -Force
            if($info.Length -gt 65536 -or ($info.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0){throw 'Task diagnostic bound refused'}
            [xml]$document=[IO.File]::ReadAllText($path)
            $ns=[Xml.XmlNamespaceManager]::new($document.NameTable);$ns.AddNamespace('t','http://schemas.microsoft.com/windows/2004/02/mit/task')
            $note.boot_trigger_present=($null -ne $document.SelectSingleNode('/t:Task/t:Triggers/t:BootTrigger',$ns))
            $note.logon_trigger_present=($null -ne $document.SelectSingleNode('/t:Task/t:Triggers/t:LogonTrigger',$ns))
            $principal=$document.SelectSingleNode('/t:Task/t:Principals/t:Principal/t:UserId',$ns)
            if($null -ne $principal){$note.system_principal=($principal.InnerText -in @('S-1-5-18','SYSTEM','NT AUTHORITY\SYSTEM'))}
            foreach($field in @('Enabled','DisallowStartIfOnBatteries','StopIfGoingOnBatteries','RunOnlyIfNetworkAvailable','RunOnlyIfIdle','StartWhenAvailable','AllowStartOnDemand')){
                $node=$document.SelectSingleNode('/t:Task/t:Settings/t:'+$field,$ns)
                if($null -ne $node -and $node.InnerText -in @('true','false')){$note[$field]=($node.InnerText -ieq 'true')}
            }
            $node=$document.SelectSingleNode('/t:Task/t:Settings/t:MultipleInstancesPolicy',$ns)
            if($null -ne $node){$note.multiple_instances=if($node.InnerText -in @('Parallel','Queue','IgnoreNew','StopExisting')){$node.InnerText}else{'other'}}
            $node=$document.SelectSingleNode('/t:Task/t:Actions/t:Exec/t:Arguments',$ns)
            if($role -ceq 'observer'){$note.expected_observer_action=($null -ne $node -and $node.InnerText -ceq '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File C:\K5GuestWitness\windows_guest_witness.ps1 -Mode Observe')}
        }catch{$note.failure_kind=$_.Exception.GetType().Name;$note.failure_hresult=$_.Exception.HResult}
    }
    foreach($channel in @('task','system')){
        $relative=if($channel -ceq 'task'){'Windows\System32\winevt\Logs\Microsoft-Windows-TaskScheduler%4Operational.evtx'}else{'Windows\System32\winevt\Logs\System.evtx'}
        $path=Join-Path $WindowsVolume $relative
        if(-not(Test-Path -LiteralPath $path -PathType Leaf)){continue}
        $events=@()
        try{
            $info=Get-Item -LiteralPath $path -Force
            if($info.Length -gt 67108864 -or ($info.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0){throw 'Event diagnostic bound refused'}
            $xpath=if($channel -ceq 'task'){
                '*[EventData[Data[@Name="TaskName"]="\K5GuestWitness-'+$transfer+'" or Data[@Name="TaskName"]="\K5GuestBootstrap-'+$transfer+'"]]'
            }else{'*[System[Provider[@Name="Service Control Manager"]]]'}
            try{$events=@(Get-WinEvent -Path $path -FilterXPath $xpath -MaxEvents 64 -ErrorAction Stop)}catch{if($_.FullyQualifiedErrorId -notlike 'NoMatchingEventsFound*'){throw}}
            $safeEvents=@()
            foreach($event in $events){
                    [xml]$xml=$event.ToXml();$ns=[Xml.XmlNamespaceManager]::new($xml.NameTable);$ns.AddNamespace('e','http://schemas.microsoft.com/win/2004/08/events/event')
                    $data=@($xml.SelectNodes('/e:Event/e:EventData/e:Data',$ns))
                    $entry=@{event_id=[int]$event.Id;record_id=[long]$event.RecordId}
                    if($channel -ceq 'task'){
                        $taskName=@($data|Where-Object{$_.GetAttribute('Name') -ceq 'TaskName'})
                        if($taskName.Count -ne 1){continue}
                        if($taskName[0].InnerText -ceq ('\K5GuestWitness-'+$transfer)){$entry.role='observer'}
                        elseif($taskName[0].InnerText -ceq ('\K5GuestBootstrap-'+$transfer)){$entry.role='bootstrap'}else{continue}
                        foreach($item in $data|Where-Object{$_.GetAttribute('Name') -in @('ResultCode','ErrorCode','ErrorValue')}){
                            $value=$item.InnerText
                            if($value -match '^0x[0-9a-fA-F]{1,8}$'){$entry.result_code=[Convert]::ToUInt32($value.Substring(2),16)}
                            elseif($value -match '^\d{1,10}$' -and [decimal]$value -le [uint32]::MaxValue){$entry.result_code=[long]$value}
                        }
                    }else{
                        if($event.Id -notin @(7000,7001,7009,7011,7023,7024,7031,7034,7036)){continue}
                        if(@($data|Where-Object{$_.InnerText -in @('K5VisionRecorder','K5 Vision Recorder')}).Count -eq 0){continue}
                        foreach($item in $data){if($item.InnerText -match '^%%(\d{1,10})$'){$entry.win32_code=[long]$Matches[1]}}
                    }
                    $safeEvents+=@($entry)
            }
            if($channel -ceq 'task'){$facts.task_events=$safeEvents}else{$facts.service_events=$safeEvents}
        }catch{$facts[($channel+'_event_failure')]=@{kind=$_.Exception.GetType().Name;hresult=$_.Exception.HResult}}
        finally{foreach($event in $events){if($event -is [IDisposable]){$event.Dispose()}}}
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
        $facts.subphase='owned_task_diagnostics'
        $facts.task_diagnostics=Read-OwnedTaskDiagnostics $diagnosticMount
        $facts.subphase='own_receipts'
        foreach($name in @('public-config.json','setup-entry.json','setup-failure.json','setup-preflight-failure.json','bootstrap-receipt.json','bootstrap-witness.json','reboot-witness.json','observer-entry-bootstrap.json','observer-stage-bootstrap.json','observer-preflight-failure-bootstrap.json','observer-entry-reboot.json','observer-stage-reboot.json','observer-preflight-failure-reboot.json')){
            $path=Join-Path $diagnosticMount ('K5GuestWitness\'+$name)
            if(-not(Test-Path -LiteralPath $path -PathType Leaf)){continue}
            $info=Get-Item -LiteralPath $path
            if($info.Length -gt 65536 -or ($info.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0){continue}
            try{
                $parsed=Get-Content -LiteralPath $path -Raw|ConvertFrom-Json;$safe=@{}
                foreach($field in @('status','phase','stage','failure_stage','error_kind','failure_type','hresult','failure_hresult','native_code','http_status','mfa_enabled_before_reboot','mfa_confirmed_via_normal_api','gui_admin_setup','camera_source_enrolled','service_running','gui_client_closed','cleanup_credentials_removed','configuration_valid','system_account_verified','observer_session','setup_session_admitted','observer_session_admitted','logon_audit_enabled','startup_audit_enabled','security_policy_coverage_verified','security_tail_stable','boot_id','previous_boot_id','boot_sequence','previous_boot_sequence','native_boot_sequence_verified','native_boot_sequence_increment_verified')){
                    $property=$parsed.PSObject.Properties[$field]
                    if($null -eq $property){continue};$value=$property.Value
                    if($value -is [bool] -or $value -is [int] -or $value -is [long]){$safe[$field]=$value}
                    elseif($value -is [string] -and (($field -in @('boot_id','previous_boot_id') -and $value -cmatch '^[a-f0-9-]{36}$') -or $value -match '^[A-Za-z][A-Za-z0-9_.-]{0,79}$')){$safe[$field]=$value}
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
    if($RebootOperation -ceq 'planned-shutdown-start'){Assert-PlannedCustodyContracts}
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
    $guestPartitionNumber=$partitionNumber
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
    $public=@{schema_version=1;transfer_id=$transfer;head_sha=$ProductHead;setup_sha256=$SetupSha256;phase='bootstrap';harness_head=$env:GITHUB_SHA;witness_sha256=(File-Digest $witness);reboot_operation=$RebootOperation;previous_boot_id='';previous_boot_sequence=-1;guest_mac=$mac;guest_ip=$guestIP;host_ip=$hostIPAddress}
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
    if($initial.status -cne 'bootstrap_sealed' -or -not $initial.cleanup_credentials_removed -or $initial.native_boot_sequence_verified -ne $true -or $initial.boot_sequence -lt 0 -or $initial.boot_sequence -ge 4294967295){throw 'Guest bootstrap not sealed'}
    $receipt.actual_guest_boot=$true
    $receipt.stage='real_guest_reboot'
    Invoke-Command -Session $direct -ScriptBlock {
        param($boot,$sequence)
        $path='C:\K5GuestWitness\public-config.json';$configuration=Get-Content -LiteralPath $path -Raw|ConvertFrom-Json
        $configuration.phase='reboot';$configuration.previous_boot_id=$boot;$configuration.previous_boot_sequence=$sequence
        if(([IO.File]::GetAttributes($path) -band [IO.FileAttributes]::ReparsePoint) -ne 0){throw 'Owned phase file refused'}
        $bytes=[Text.UTF8Encoding]::new($false).GetBytes(($configuration|ConvertTo-Json -Depth 5))
        $stream=[IO.FileStream]::new($path,[IO.FileMode]::Open,[IO.FileAccess]::Write,[IO.FileShare]::Read,4096,[IO.FileOptions]::WriteThrough)
        try{$stream.SetLength(0);$stream.Write($bytes,0,$bytes.Length);$stream.Flush($true)}finally{$stream.Dispose()}
        $verified=Get-Content -LiteralPath $path -Raw|ConvertFrom-Json
        if($verified.phase -cne 'reboot' -or $verified.previous_boot_id -cne $boot -or $verified.previous_boot_sequence -ne $sequence){throw 'Durable reboot phase verification failed'}
    } -ArgumentList $initial.boot_id,$initial.boot_sequence -ErrorAction Stop
    Remove-PSSession -Session $direct;$direct=$null
    Restart-OwnedGuest $initial
    $rebootSeal=Kvp-Seal 'reboot' 240
    $receipt.stage='reboot_receipt_read'
    $direct=Open-Direct
    $reboot=Read-GuestReceipt 'reboot' $rebootSeal;$receipt.reboot_witness=$reboot
    if($reboot.status -cne 'reboot_sealed' -or -not $reboot.startup_before_human_or_unclassified_interactive_login -or -not $reboot.recording_after_reboot -or -not $reboot.retained_closed_hashes_unchanged -or $reboot.boot_id -cne $initial.boot_id -or $reboot.native_boot_sequence_verified -ne $true -or $reboot.native_boot_sequence_increment_verified -ne $true -or $reboot.previous_boot_sequence -ne $initial.boot_sequence -or $reboot.boot_sequence -ne ($initial.boot_sequence+1)){throw 'Reboot recording witness failed'}
    Assert-GuestLogonCensus $reboot ($RebootOperation -ceq 'planned-shutdown-start')
    if(-not $reboot.security_policy_coverage_verified -or -not $reboot.security_tail_stable -or -not $reboot.startup_audit_enabled -or -not $reboot.startup_audit_4608_verified -or -not $reboot.security_audit_checkpoint_retained -or -not $reboot.security_no_clear_reset_wrap -or -not $reboot.postboot_clip_created_after_system_inventory -or $null -eq $reboot.postboot_closed_recording){throw 'Reboot audit or new closed recording witness unavailable'}
    if($RebootOperation -ceq 'planned-shutdown-start'){
        if(-not $receipt.planned_final_off_boundary_verified -or -not $receipt.planned_boundary_dismounted_before_start -or $reboot.planned_final_off_checkpoint_sha256 -cne $receipt.planned_final_off_checkpoint_sha256 -or $reboot.security_final_off_checkpoint_retained -ne $true -or $reboot.security_original_checkpoint_retained -ne $true -or $reboot.security_all_post_off_shutdown_loss_policy_and_human_or_unclassified_interactive_events_refused -ne $true){throw 'Planned sealed witness differs from exact final-Off boundary'}
    }
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
        param($boot,$sequence,$transfer)
        $path='C:\K5GuestWitness\public-config.json';$configuration=Get-Content -LiteralPath $path -Raw|ConvertFrom-Json
        $configuration.phase='signout';$configuration.previous_boot_id=$boot;$configuration.previous_boot_sequence=$sequence
        [IO.File]::WriteAllText($path,($configuration|ConvertTo-Json -Depth 5),[Text.UTF8Encoding]::new($false))
        Start-ScheduledTask -TaskName ('K5GuestWitness-'+$transfer)
    } -ArgumentList $reboot.boot_id,$reboot.boot_sequence,$transfer -ErrorAction Stop
    Remove-PSSession -Session $direct;$direct=$null
    $signoutSeal=Kvp-Seal 'signout' 120
    $direct=Open-Direct
    $signout=Read-GuestReceipt 'signout' $signoutSeal;$receipt.signout_witness=$signout
    if($signout.status -cne 'signout_sealed' -or -not $signout.recording_after_signout -or $signout.boot_id -cne $reboot.boot_id -or $signout.native_boot_sequence_verified -ne $true -or $signout.boot_sequence -ne $reboot.boot_sequence){throw 'Signout recording witness failed'}
    $receipt.recording_after_actual_signout=$true
    $receipt.stage='retained_media_decode'
    $preboot=Invoke-Command -Session $direct -ScriptBlock {
        $baseline=Get-Content -LiteralPath 'C:\K5GuestWitness\baseline.json' -Raw|ConvertFrom-Json
        $saved=$baseline.files[0]
        @{path=[string]$saved.path;bytes=[long]$saved.bytes;sha256=[string]$saved.sha256}
    } -ErrorAction Stop
    foreach($clip in @(@{role='retained_preboot';facts=$preboot},@{role='new_postboot_prelogin';facts=$reboot.postboot_closed_recording})){
        $relative=[string]$clip.facts.path
    if($relative.Length -gt 512 -or $relative -notmatch '^[A-Za-z0-9_./\\+\-]+\.mp4$' -or $relative.Contains('..') -or [IO.Path]::IsPathRooted($relative)){throw 'Retained path refused'}
    $retained=Join-Path $root ($clip.role+'.mp4')
    Copy-Item -FromSession $direct -LiteralPath ('C:\ProgramData\K5Vision\recordings\'+$relative) -Destination $retained -ErrorAction Stop
    if($clip.facts.bytes -lt 16 -or $clip.facts.bytes -gt 536870912 -or [string]$clip.facts.sha256 -cnotmatch '^[a-f0-9]{64}$' -or (Get-Item -LiteralPath $retained).Length -ne [long]$clip.facts.bytes -or (Get-FileHash -LiteralPath $retained -Algorithm SHA256).Hash.ToLowerInvariant() -cne [string]$clip.facts.sha256){throw 'Copied closed recording differs from sealed witness'}
    $probeInfo=[Diagnostics.ProcessStartInfo]::new($FFprobe,('-v error -select_streams v:0 -count_frames -show_entries stream=codec_name,nb_read_frames -of json '+$retained))
    $probeInfo.UseShellExecute=$false;$probeInfo.CreateNoWindow=$true;$probeInfo.RedirectStandardOutput=$true;$probeInfo.RedirectStandardError=$true
    $probe=[Diagnostics.Process]::Start($probeInfo);$probe.BeginErrorReadLine();$probeOutput=$probe.StandardOutput.ReadToEndAsync()
    try{
        if(-not $probe.WaitForExit(30000) -or $probe.ExitCode -ne 0){throw 'Retained frame inspection failed'}
        $probeJson=$probeOutput.GetAwaiter().GetResult()
        if($probeJson.Length -gt 65536){throw 'Retained inspection output too large'}
        $inspected=$probeJson|ConvertFrom-Json
        if(@($inspected.streams).Count -ne 1 -or [string]$inspected.streams[0].nb_read_frames -notmatch '^\d+$' -or [long]$inspected.streams[0].nb_read_frames -lt 10){throw 'Retained clip has insufficient decoded frames'}
        $receipt[($clip.role+'_frames_inspected')]=[long]$inspected.streams[0].nb_read_frames
    }finally{if(-not $probe.HasExited){$probe.Kill();$probe.WaitForExit(2000)|Out-Null};$probe.Dispose()}
    $decode=Start-OwnedProcess $FFmpeg ('-nostdin -v error -i '+$retained+' -frames:v 10 -f null -')
    try{if(-not $decode.WaitForExit(30000) -or $decode.ExitCode -ne 0){throw 'Retained media decode failed'}}finally{if(-not $decode.HasExited){$decode.Kill();$decode.WaitForExit(2000)|Out-Null};$decode.Dispose()}
    if($clip.role -ceq 'retained_preboot'){$receipt.retained_media_decoded=$true;$receipt.retained_decoded_frames=10}else{$receipt.postboot_prelogin_media_decoded=$true;$receipt.postboot_decoded_frames=10}
    }
    $receipt.outcome='installer_guest_recording_observed';$receipt.stage='complete'
    }
}catch{
    $receipt.failure_kind=$_.Exception.GetType().Name;$receipt.failure_hresult=$_.Exception.HResult
    # Optional failure-only metadata: never export a path, message or stack.
    $failureRecord=$_
    try{
        $info=$failureRecord.InvocationInfo
        $ownScript=[IO.Path]::GetFullPath($PSCommandPath)
        if($null -ne $info -and -not [string]::IsNullOrEmpty([string]$info.ScriptName) -and
           [IO.Path]::GetFileName($ownScript) -ceq 'probe_windows_guest_recording.ps1' -and
           [string]::Equals([IO.Path]::GetFullPath([string]$info.ScriptName),$ownScript,[StringComparison]::OrdinalIgnoreCase)){
            $line=[long]$info.ScriptLineNumber
            if($line -ge 1 -and $line -le [IO.File]::ReadAllLines($ownScript).Length){$receipt.failure_owned_source_line=[int]$line}
        }
    }catch{}
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
    $plannedReasons=@{
        'Native content control fixture did not change'='native_content_control_fixture_did_not_change'
        'Native planned custody smoke differs'='native_planned_custody_smoke_differs'
        'Native planned custody smoke remains'='native_planned_custody_smoke_remains'
        'Native planned hardlink smoke was accepted'='native_planned_hardlink_smoke_was_accepted'
        'Native planned identity changed after alias removal'='native_planned_identity_changed_after_alias_removal'
        'Native planned no-mutation control failed'='native_planned_no_mutation_control_failed'
        'Native replacement control fixture differs'='native_replacement_control_fixture_differs'
        'Native replacement wrote another object'='native_replacement_wrote_another_object'
        'Native same-object changed content was overwritten'='native_same_object_changed_content_was_overwritten'
        'Offline path refused'='offline_path_refused'
        'Offline path reparse point refused'='offline_path_reparse_point_refused'
        'Planned Security boundary changed before publication'='planned_security_boundary_changed_before_publication'
        'Planned Security boundary changed during publication'='planned_security_boundary_changed_during_publication'
        'Planned Security log bound exceeded'='planned_security_log_bound_exceeded'
        'Planned Windows partition differs'='planned_windows_partition_differs'
        'Planned access path remains'='planned_access_path_remains'
        'Planned boundary requires exact owned Off'='planned_boundary_requires_exact_owned_off'
        'Planned boundary was not admitted within its bound'='planned_boundary_was_not_admitted_within_its_bound'
        'Planned cleanup access path differs'='planned_cleanup_access_path_differs'
        'Planned cleanup foreign disk refused'='planned_cleanup_foreign_disk_refused'
        'Planned cleanup requires exact owned Off'='planned_cleanup_requires_exact_owned_off'
        'Planned config ACL cardinality refused'='planned_config_acl_cardinality_refused'
        'Planned config changed before write'='planned_config_changed_before_write'
        'Planned config custody changed between mounts'='planned_config_custody_changed_between_mounts'
        'Planned config durable publication differs'='planned_config_durable_publication_differs'
        'Planned config file identity, DACL or durable text changed'='planned_config_file_identity_dacl_or_durable_text_changed'
        'Planned config foreign or nonprivate ACL refused'='planned_config_foreign_or_nonprivate_acl_refused'
        'Planned config private owner or DACL refused'='planned_config_private_owner_or_dacl_refused'
        'Planned config scope differs'='planned_config_scope_differs'
        'Planned disk already attached'='planned_disk_already_attached'
        'Planned disk identity or access differs'='planned_disk_identity_or_access_differs'
        'Planned disk remains attached'='planned_disk_remains_attached'
        'Planned disk still mounted before Start'='planned_disk_still_mounted_before_start'
        'Planned offline boundary exceeded its bound'='planned_offline_boundary_exceeded_its_bound'
        'Planned offline mount path remains'='planned_offline_mount_path_remains'
        'Planned offline ownership query exceeded its bound'='planned_offline_ownership_query_exceeded_its_bound'
        'Planned original baseline digest differs'='planned_original_baseline_digest_differs'
        'Planned original baseline scope differs'='planned_original_baseline_scope_differs'
        'Planned original checkpoint not retained'='planned_original_checkpoint_not_retained'
        'Planned pre-Off audit continuity lost'='planned_pre_off_audit_continuity_lost'
        'Planned pre-Off log reset or wrap'='planned_pre_off_log_reset_or_wrap'
        'Planned pre-Off logon type unavailable'='planned_pre_off_logon_type_unavailable'
        'Planned pre-Off range bound exceeded'='planned_pre_off_range_bound_exceeded'
        'Planned pre-Off range gap'='planned_pre_off_range_gap'
        'Planned pre-Off record order changed'='planned_pre_off_record_order_changed'
        'Planned read-only Security log changed'='planned_read_only_security_log_changed'
        'Planned shutdown did not reach owned Off'='planned_shutdown_did_not_reach_owned_off'
        'Planned shutdown exceeded its bound'='planned_shutdown_exceeded_its_bound'
        'Planned start did not reach owned Running'='planned_start_did_not_reach_owned_running'
        'Planned start exceeded its bound'='planned_start_exceeded_its_bound'
        'Unknown owned reboot operation'='unknown_owned_reboot_operation'
    }
    foreach($message in $plannedReasons.Keys){$knownReasons[$message]=$plannedReasons[$message]}
    $receipt.failure_reason='other'
    if($knownReasons.ContainsKey($failureRecord.Exception.Message)){$receipt.failure_reason=$knownReasons[$failureRecord.Exception.Message]}
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
    }elseif($null -ne $vmId -and $receipt.stage -ceq 'real_guest_reboot'){
        # No keyboard login or PowerShell Direct connection has occurred in
        # this measured interval; capture only credential-free host-side data.
        try{$receipt.reboot_failure_live=Read-BootFailureLive}catch{$receipt.reboot_live_diagnostic_error=$_.Exception.GetType().Name}
    }
}finally{
    $password=$null
    if($null -ne $credential){$credential.Password.Dispose();$credential=$null}
    try{
        if($null -ne $direct){Remove-PSSession -Session $direct;$direct=$null}
        if($null -ne $vmId){
            $vm=Owned-VM
            if([string]$vm.State -ne 'Off'){
                if($receipt.stage -in @('bootstrap_observation','actual_guest_boot','real_guest_reboot')){Stop-OwnedDiagnosticVM}
                else{Bounded-Job (Stop-VM -VM $vm -TurnOff -Force -AsJob -Confirm:$false) 30|Out-Null}
            }
            $vm=Owned-VM;if([string]$vm.State -ne 'Off'){throw 'Owned VM did not stop'}
            Bounded-Job (Remove-VM -VM $vm -Force -AsJob -Confirm:$false) 30|Out-Null
            if($null -ne (Get-VM -Id $vmId -ErrorAction SilentlyContinue)){throw 'Owned VM remains'}
            if($mounted -or $null -ne $plannedBoundaryMountIntent){$receipt.offline_diagnostic_skipped_for_pending_cleanup=$true}
            elseif($receipt.stage -ceq 'bootstrap_observation'){
                try{$receipt.bootstrap_observation_offline=Read-BootFailureOffline}catch{$receipt.bootstrap_offline_diagnostic_error=$_.Exception.GetType().Name}
            }elseif($receipt.stage -ceq 'actual_guest_boot' -and -not $receipt.actual_guest_boot){
                try{$receipt.bootstrap_failure_offline=Read-BootFailureOffline}catch{$receipt.bootstrap_offline_diagnostic_error=$_.Exception.GetType().Name}
            }elseif($receipt.stage -ceq 'real_guest_reboot'){
                try{$receipt.reboot_failure_offline=Read-BootFailureOffline}catch{$receipt.reboot_offline_diagnostic_error=$_.Exception.GetType().Name}
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
        if($null -ne $plannedBoundaryMountIntent){
            $pending=Get-VHD -Path $image
            if($pending.Attached){
                $pendingNumber=[int]$pending.DiskNumber
                if([string](Get-Disk -Number $pendingNumber).UniqueId -cne $diskId){throw 'Pending planned cleanup disk differs'}
                $mounted=$true;$diskNumber=$pendingNumber;$accessPath=$plannedBoundaryMountIntent.access_path
                $paths=@((Get-Partition -DiskNumber $diskNumber -PartitionNumber $plannedBoundaryMountIntent.partition_number).AccessPaths|Where-Object{[string]::Equals([string]$_,$accessPath,[StringComparison]::OrdinalIgnoreCase)})
                $partitionNumber=if($paths.Count -eq 1){$plannedBoundaryMountIntent.partition_number}else{$null}
                if($paths.Count -gt 1){throw 'Pending planned cleanup path differs'}
            }else{$mounted=$false}
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
