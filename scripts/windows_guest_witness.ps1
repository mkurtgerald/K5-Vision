# Runs only inside the disposable evaluation guest as SYSTEM. It contains no
# Actions credential and never infers recording from SCM configuration alone.
param([ValidateSet('Setup', 'Observe')][string]$Mode = 'Observe')
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$root = 'C:\K5GuestWitness'
$config = Get-Content -LiteralPath (Join-Path $root 'public-config.json') -Raw | ConvertFrom-Json
if ($config.schema_version -ne 1 -or [guid]::Parse($config.transfer_id).ToString() -cne $config.transfer_id) { exit 2 }
$transfer = [string]$config.transfer_id
function Write-DurableNewFile([string]$Path,[string]$Text) {
    $bytes=[Text.UTF8Encoding]::new($false).GetBytes($Text)
    $stream=[IO.FileStream]::new($Path,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None,4096,[IO.FileOptions]::WriteThrough)
    try{$stream.Write($bytes,0,$bytes.Length);$stream.Flush($true)}finally{$stream.Dispose()}
}
function Flush-DurableExistingFile([string]$Path) {
    $stream=[IO.FileStream]::new($Path,[IO.FileMode]::Open,[IO.FileAccess]::ReadWrite,[IO.FileShare]::Read,4096,[IO.FileOptions]::WriteThrough)
    try{$stream.Flush($true)}finally{$stream.Dispose()}
}
function Write-EntryDiagnostic([string]$Name,[hashtable]$Value) {
    if($Name -cnotmatch '^(?:observer-(?:entry|stage|preflight-failure)-(?:bootstrap|reboot|signout)|setup-preflight-failure)\.json$'){throw 'Diagnostic name refused'}
    $path=Join-Path $root $Name;$temporary=Join-Path $root ([guid]::NewGuid().ToString('N')+'.tmp')
    Write-DurableNewFile $temporary ($Value|ConvertTo-Json -Depth 6 -Compress)
    if([IO.File]::Exists($path)){
        if(([IO.File]::GetAttributes($path) -band [IO.FileAttributes]::ReparsePoint) -ne 0){throw 'Diagnostic target refused'}
        [IO.File]::Replace($temporary,$path,[NullString]::Value)
    }else{[IO.File]::Move($temporary,$path)}
    Flush-DurableExistingFile $path
}
$systemSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$guestWitnessSession=[Diagnostics.Process]::GetCurrentProcess().SessionId
$guestWitnessAdmitted=($systemSid -ceq 'S-1-5-18' -and (($Mode -eq 'Setup' -and $guestWitnessSession -in @(0,1)) -or ($Mode -eq 'Observe' -and $guestWitnessSession -eq 0)))
if($Mode -eq 'Setup'){
    # Diagnostic context only, before admission/native compilation. Windows
    # Setup may use a different session; observer proof remains SYSTEM/session0.
    $entry=@{schema_version=1;phase='setup_entry';stage='setup_admission';status='context_captured';configuration_valid=$true;system_account_verified=($systemSid -ceq 'S-1-5-18');observer_session=$guestWitnessSession;setup_session_admitted=$guestWitnessAdmitted}
    [IO.File]::WriteAllText((Join-Path $root 'setup-entry.json'),($entry|ConvertTo-Json -Compress),[Text.UTF8Encoding]::new($false))
}
if($Mode -eq 'Observe' -and [string]$config.phase -in @('bootstrap','reboot','signout')){
    Write-EntryDiagnostic ('observer-entry-'+$config.phase+'.json') @{schema_version=1;phase=[string]$config.phase;status='observer_entered';stage='observer_admission';system_account_verified=($systemSid -ceq 'S-1-5-18');observer_session=$guestWitnessSession;observer_session_admitted=$guestWitnessAdmitted}
}
if (-not $guestWitnessAdmitted) { exit 2 }

try{
Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Security.Principal;
public static class K5GuestNative {
  [StructLayout(LayoutKind.Sequential)] struct Boot { public Guid Id; public int Firmware; public ulong Flags; }
  // Exact Microsoft WDK 10.0.26100.0 ntddk.h layout, independently
  // range-extracted from Microsoft.Windows.WDK.x64 10.0.26100.1:
  // ntddk.h SHA256 2d82908d3c5cfda7214a1d299dc730fd4863c090df7d146a62b0f7fb080320f7.
  // AlternativeArchitecture C_ASSERT 0x2C0; ULONG BootId; next C_ASSERT 0x2C8.
  // SDK 10.0.26100.1742 ksamd64.inc defines user VA 0x7FFE0000; SHA256
  // 832f26c59686cf6864da83681f97fc6fee3a7006399c48efa777d4330a499efb.
  [StructLayout(LayoutKind.Explicit, Size=0x2D0)] struct SharedBootLayout {
    [FieldOffset(0x2C0)] public uint AlternativeArchitecture;
    [FieldOffset(0x2C4)] public uint BootSequence;
    [FieldOffset(0x2C8)] public long SystemExpirationDate;
  }
  [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)] struct Version {
    public uint Size, Major, Minor, Build, Platform;
    [MarshalAs(UnmanagedType.ByValTStr, SizeConst=128)] public string ServicePack;
    public ushort ServicePackMajor, ServicePackMinor, SuiteMask;
    public byte ProductType, Reserved;
  }
  [DllImport("kernel32.dll")] static extern IntPtr GetCurrentProcess();
  [DllImport("kernel32.dll", SetLastError=true)] [return:MarshalAs(UnmanagedType.Bool)] static extern bool IsWow64Process2(IntPtr process,out ushort processMachine,out ushort nativeMachine);
  [DllImport("kernel32.dll", SetLastError=true)] [return:MarshalAs(UnmanagedType.Bool)] static extern bool ReadProcessMemory(IntPtr process,IntPtr address,[Out] byte[] buffer,UIntPtr size,out UIntPtr bytesRead);
  [DllImport("ntdll.dll", ExactSpelling=true)] static extern int RtlGetVersion(ref Version version);
  [StructLayout(LayoutKind.Sequential)] struct Session { public int Id; public IntPtr Name; public int State; }
  [StructLayout(LayoutKind.Sequential)] struct Audit { public Guid Subcategory; public uint Flags; public Guid Category; }
  [DllImport("ntdll.dll")] static extern int NtQuerySystemInformation(int kind, ref Boot boot, int size, out int length);
  [DllImport("wtsapi32.dll", SetLastError=true)] static extern bool WTSEnumerateSessionsW(IntPtr server,int reserved,int version,out IntPtr buffer,out int count);
  [DllImport("wtsapi32.dll", SetLastError=true)] static extern bool WTSQuerySessionInformationW(IntPtr server,int session,int kind,out IntPtr buffer,out int count);
  [DllImport("wtsapi32.dll")] static extern void WTSFreeMemory(IntPtr memory);
  [DllImport("wtsapi32.dll", SetLastError=true)] static extern bool WTSLogoffSession(IntPtr server,int session,bool wait);
  [DllImport("kernel32.dll", SetLastError=true)] static extern IntPtr OpenProcess(uint access,bool inherit,int pid);
  [DllImport("kernel32.dll", SetLastError=true)] static extern bool ProcessIdToSessionId(int pid,out int session);
  [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);
  [DllImport("advapi32.dll",SetLastError=true)] static extern bool OpenProcessToken(IntPtr process,uint access,out IntPtr token);
  [DllImport("advapi32.dll",SetLastError=true)] static extern bool GetTokenInformation(IntPtr token,int kind,IntPtr buffer,int length,out int needed);
  [DllImport("advapi32.dll",SetLastError=true)] [return:MarshalAs(UnmanagedType.U1)] static extern bool AuditQuerySystemPolicy(ref Guid subcategory,uint count,out IntPtr policy);
  [DllImport("advapi32.dll")] static extern void AuditFree(IntPtr buffer);
  public static string BootId() { var b=new Boot(); int n; if(NtQuerySystemInformation(90,ref b,Marshal.SizeOf(b),out n)<0 || b.Id==Guid.Empty) throw new InvalidOperationException(); return b.Id.ToString(); }
  public static uint BootSequence() {
    ushort processMachine,nativeMachine;
    if(!IsWow64Process2(GetCurrentProcess(),out processMachine,out nativeMachine)) throw new Win32Exception();
    var version=new Version(); version.Size=(uint)Marshal.SizeOf(typeof(Version));
    int status=RtlGetVersion(ref version);
    AssertBootSequenceContext(Environment.Is64BitProcess,Environment.Is64BitOperatingSystem,IntPtr.Size,processMachine,nativeMachine,version.Size,status,version.Major,version.Minor,version.Build);
    // This checks our managed ABI only. The vendor-header derivation of 0x2C4
    // is separately pinned; this manually explicit struct is not a WDK compile.
    if(Marshal.SizeOf(typeof(SharedBootLayout))!=0x2D0 || Marshal.OffsetOf(typeof(SharedBootLayout),"AlternativeArchitecture").ToInt32()!=0x2C0 || Marshal.OffsetOf(typeof(SharedBootLayout),"BootSequence").ToInt32()!=0x2C4 || Marshal.OffsetOf(typeof(SharedBootLayout),"SystemExpirationDate").ToInt32()!=0x2C8) throw new InvalidOperationException("Unsupported native layout");
    string entry=BootId(); uint first=ReadBootSequence();
    System.Threading.Thread.Sleep(10);
    if(ReadBootSequence()!=first || BootId()!=entry) throw new InvalidOperationException("Unstable native boot sequence");
    return first;
  }
  static void AssertBootSequenceContext(bool process64,bool operatingSystem64,int pointerBytes,ushort processMachine,ushort nativeMachine,uint versionSize,int status,uint major,uint minor,uint build) {
    if(!process64 || !operatingSystem64 || pointerBytes!=8 || processMachine!=0 || nativeMachine!=0x8664) throw new InvalidOperationException("Unsupported native architecture");
    if(versionSize!=284 || status!=0 || major!=10 || minor!=0 || build!=26100) throw new InvalidOperationException("Unsupported native version");
  }
  static uint DecodeBootSequence(bool success,UIntPtr received,byte[] bytes) {
    if(!success) throw new Win32Exception();
    if(received.ToUInt64()!=4 || bytes==null || bytes.Length!=4) throw new InvalidOperationException("Native boot sequence length differs");
    return BitConverter.ToUInt32(bytes,0);
  }
  static uint ReadBootSequence() {
    var bytes=new byte[4]; UIntPtr received;
    bool success=ReadProcessMemory(GetCurrentProcess(),new IntPtr(0x7FFE02C4L),bytes,new UIntPtr(4u),out received);
    return DecodeBootSequence(success,received,bytes);
  }
  static bool AuditSuccess(string category) { var g=new Guid(category); IntPtr p; if(!AuditQuerySystemPolicy(ref g,1,out p)) throw new Win32Exception(); try{return (((Audit)Marshal.PtrToStructure(p,typeof(Audit))).Flags&1)!=0;} finally{AuditFree(p);} }
  public static bool LogonAuditEnabled() { return AuditSuccess("0cce9215-69ae-11d9-bed3-505054503030"); }
  public static bool StartupAuditEnabled() { return AuditSuccess("0cce9210-69ae-11d9-bed3-505054503030"); }
  public static int[] InteractiveSessions() {
    IntPtr p; int count; if(!WTSEnumerateSessionsW(IntPtr.Zero,0,1,out p,out count)) throw new Win32Exception();
    try { if(count<0 || count>64) throw new InvalidOperationException(); var found=new List<int>();
      for(int i=0;i<count;i++){var s=(Session)Marshal.PtrToStructure(IntPtr.Add(p,i*Marshal.SizeOf(typeof(Session))),typeof(Session));
        if(s.Id<=0)continue; IntPtr u; int bytes;
        if(!WTSQuerySessionInformationW(IntPtr.Zero,s.Id,5,out u,out bytes))throw new Win32Exception();
        try{if(bytes>2 && !String.IsNullOrEmpty(Marshal.PtrToStringUni(u)))found.Add(s.Id);}finally{WTSFreeMemory(u);}}
      return found.ToArray(); }finally{WTSFreeMemory(p);}
  }
  public static string TokenUser(int pid) {
    IntPtr p=OpenProcess(0x1000,false,pid); if(p==IntPtr.Zero)throw new Win32Exception();
    try{IntPtr t;if(!OpenProcessToken(p,8,out t))throw new Win32Exception();try{
      int n;GetTokenInformation(t,1,IntPtr.Zero,0,out n);if(n<1||n>65536)throw new InvalidOperationException();IntPtr b=Marshal.AllocHGlobal(n);
      try{if(!GetTokenInformation(t,1,b,n,out n))throw new Win32Exception();return new SecurityIdentifier(Marshal.ReadIntPtr(b)).Value;}finally{Marshal.FreeHGlobal(b);}
    }finally{CloseHandle(t);}}finally{CloseHandle(p);}
  }
  public static int SessionId(int pid){int id;if(!ProcessIdToSessionId(pid,out id))throw new Win32Exception();return id;}
  public static void Logoff(int id){if(id<=0 || !WTSLogoffSession(IntPtr.Zero,id,false))throw new Win32Exception();}
}
'@
}catch{
    $name=if($Mode -eq 'Setup'){'setup-preflight-failure.json'}else{'observer-preflight-failure-'+$config.phase+'.json'}
    Write-EntryDiagnostic $name @{schema_version=1;phase=[string]$config.phase;stage='native_compile';status='preflight_failed';error_kind=$_.Exception.GetType().Name;hresult=$_.Exception.HResult;observer_session=$guestWitnessSession}
    exit 1
}

function Write-PrivateJson([string]$Name, $Value) {
    $path = Join-Path $root $Name
    if (Test-Path -LiteralPath $path) { throw 'Witness receipt already exists' }
    $temporary = Join-Path $root ([guid]::NewGuid().ToString('N') + '.tmp')
    Write-DurableNewFile $temporary ($Value | ConvertTo-Json -Depth 12 -Compress)
    [IO.File]::Move($temporary, $path)
    Flush-DurableExistingFile $path
    return (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
}
function Publish-Seal([string]$Phase, [string]$Boot, [string]$Hash) {
    $kvp = 'HKLM:\SOFTWARE\Microsoft\Virtual Machine\Guest'
    New-Item -Path $kvp -Force | Out-Null
    New-ItemProperty -Path $kvp -Name ('K5GuestWitness-' + $transfer) -Value ($Phase + '|' + $Boot + '|' + $Hash) -PropertyType String -Force | Out-Null
}
function Disable-TemporaryAutoLogon {
    $key = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'
    Set-ItemProperty -Path $key -Name AutoAdminLogon -Value '0'
    Set-ItemProperty -Path $key -Name AutoLogonCount -Value 0 -Type DWord
    Remove-ItemProperty -Path $key -Name DefaultPassword -ErrorAction SilentlyContinue
    $winlogon=[Microsoft.Win32.Registry]::LocalMachine.OpenSubKey('SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon',$true)
    try{if($null -eq $winlogon){throw 'Temporary autologon key unavailable'};$winlogon.Flush()}finally{if($null -ne $winlogon){$winlogon.Dispose()}}
    foreach ($path in @('C:\Windows\Panther\Unattend.xml','C:\Windows\Panther\Unattend\Unattend.xml','C:\K5GuestWitness\Unattend.xml')) {
        if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Force }
    }
    $state = Get-ItemProperty -Path $key
    if ($state.AutoAdminLogon -ne '0' -or $state.PSObject.Properties.Name -contains 'DefaultPassword') { throw 'Temporary auto logon remains' }
}
function Recording-Files {
    $recordingRoot = Join-Path ([Environment]::GetFolderPath('CommonApplicationData')) 'K5Vision\recordings'
    if (-not (Test-Path -LiteralPath $recordingRoot -PathType Container)) { return @() }
    if (((Get-Item -LiteralPath $recordingRoot).Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'Recording root is a reparse point' }
    $all = @(Get-ChildItem -LiteralPath $recordingRoot -Recurse -Force)
    if ($all.Count -gt 8192 -or @($all | Where-Object { ($_.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 }).Count) { throw 'Recording inventory refused' }
    return @($all | Where-Object { -not $_.PSIsContainer -and $_.Extension -ceq '.mp4' })
}
function Recording-Bytes {
    [long]$total = 0
    foreach ($file in @(Recording-Files)) { $total += $file.Length }
    return $total
}
function Closed-Recordings {
    $result = @()
    $recordingRoot = Join-Path ([Environment]::GetFolderPath('CommonApplicationData')) 'K5Vision\recordings'
    foreach ($file in @(Recording-Files)) {
        $stream = $null
        try {
            # Deny writer/delete sharing: a currently-written MP4 cannot be
            # called a retained closed segment merely because its size paused.
            $stream = [IO.File]::Open($file.FullName,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::Read)
            if ($stream.Length -lt 16 -or $stream.Length -gt 536870912) { continue }
            $sha = [Security.Cryptography.SHA256]::Create()
            try { $digest = [BitConverter]::ToString($sha.ComputeHash($stream)).Replace('-','').ToLowerInvariant() } finally { $sha.Dispose() }
            $result += @{ path=$file.FullName.Substring($recordingRoot.Length+1); bytes=$stream.Length; sha256=$digest }
        } catch [IO.IOException] { }
        finally { if ($null -ne $stream) { $stream.Dispose() } }
    }
    return $result
}
function Recorder-Facts {
    $service = Get-CimInstance -ClassName Win32_Service -Filter "Name='K5VisionRecorder'"
    if ($null -eq $service -or $service.State -ne 'Running' -or $service.ProcessId -le 0 -or $service.StartName -cne 'NT SERVICE\K5VisionRecorder') { throw 'Recorder service unavailable' }
    $expected = ([Security.Principal.NTAccount]::new('NT SERVICE','K5VisionRecorder')).Translate([Security.Principal.SecurityIdentifier]).Value
    $actual = [K5GuestNative]::TokenUser([int]$service.ProcessId)
    $session = [K5GuestNative]::SessionId([int]$service.ProcessId)
    if ($actual -cne $expected -or $session -ne 0) { throw 'Recorder identity unavailable' }
    $request=[Net.HttpWebRequest]::CreateHttp('http://127.0.0.1:8000/api/v1/health')
    $request.Method='GET';$request.Proxy=$null;$request.Timeout=1500;$request.ReadWriteTimeout=1500
    $response=$null
    try{$response=$request.GetResponse();if([int]$response.StatusCode -ne 200){throw 'Recorder HTTP readiness unavailable'}}finally{if($null -ne $response){$response.Dispose()}}
    return @{ state='running'; sid_role='exact_recorder_virtual_account'; token_user_verified=$true; session_id=$session; process_id=[int]$service.ProcessId; health_http_status=200 }
}
function Security-EventHash($Event) {
    $bytes=[Text.Encoding]::UTF8.GetBytes($Event.ToXml())
    $hash=[Security.Cryptography.SHA256]::Create()
    try{return [BitConverter]::ToString($hash.ComputeHash($bytes)).Replace('-','').ToLowerInvariant()}finally{$hash.Dispose()}
}
function Assert-NextBootSequence([long]$Previous,[long]$Current) {
    # The documented ULONG loader sequence is a per-boot marker. Refuse wrap,
    # reset, multiple boot attempts and unchanged values in this exact VM.
    if($Previous -lt 0 -or $Previous -ge 4294967295 -or $Current -ne ($Previous+1)){throw 'Native reboot sequence differs'}
}
function Security-Checkpoint([string]$Boot,[long]$Sequence) {
    $newest=@();$oldest=@()
    try{
        $newest=@(Get-WinEvent -LogName Security -MaxEvents 1 -ErrorAction Stop)
        $oldest=@(Get-WinEvent -LogName Security -MaxEvents 1 -Oldest -ErrorAction Stop)
        if($newest.Count -ne 1 -or $oldest.Count -ne 1 -or $newest[0].RecordId -lt $oldest[0].RecordId -or $oldest[0].RecordId -lt 1){throw 'Security checkpoint unavailable'}
        return @{boot_id=$Boot;boot_sequence=$Sequence;record_id=[long]$newest[0].RecordId;oldest_record_id=[long]$oldest[0].RecordId;xml_sha256=(Security-EventHash $newest[0])}
    }finally{foreach($event in (@($newest)+@($oldest))){if($event -is [IDisposable]){$event.Dispose()}}}
}
function Add-SecurityPolicyDiagnostic($Event,[hashtable]$Diagnostic) {
    # Optional details on an event that already causes refusal. Export only
    # native audit-family category IDs and recognized enable/disable bit roles.
    try {
        [xml]$policy=$Event.ToXml()
        $guidFields=@($policy.Event.EventData.Data|Where-Object{$_.Name -ieq 'SubcategoryGuid'})
        if($guidFields.Count -eq 1){
            $guid=[guid]::Empty
            if([guid]::TryParse([string]$guidFields[0].'#text',[ref]$guid)){
                $value=$guid.ToString()
                if($value -cmatch '^0cce92[1-7][0-9a-f]-69ae-11d9-bed3-505054503030$'){$Diagnostic.policy_subcategory_guid=$value}
            }
        }
        $changes=@($policy.Event.EventData.Data|Where-Object{$_.Name -ceq 'AuditPolicyChanges'})
        if($changes.Count -eq 1){
            $parts=@(([string]$changes[0].'#text') -split '[,\s]+'|Where-Object{$_ -ne ''})
            if($parts.Count -ge 1 -and $parts.Count -le 4 -and @($parts|Where-Object{$_ -cnotmatch '^%%(?:8448|8449|8450|8451)$'}).Count -eq 0){
                $Diagnostic.policy_change_tokens_recognized=$true
                $Diagnostic.policy_success_removed=('%%8448' -cin $parts);$Diagnostic.policy_success_added=('%%8449' -cin $parts)
                $Diagnostic.policy_failure_removed=('%%8450' -cin $parts);$Diagnostic.policy_failure_added=('%%8451' -cin $parts)
            }else{$Diagnostic.policy_change_tokens_recognized=$false}
        }
    }catch{$Diagnostic.policy_detail_unavailable=$true}
}
function Save-SecurityAuditFailure([hashtable]$Diagnostic,$Failure) {
    $allowedStages=@('checkpoint_admission','oldest_newest_query','checkpoint_query','event_range_query','event_range_validation','startup_validation','logon_validation','retained_anchor_query','native_policy_wts_context','native_logon_policy','native_startup_policy','native_wts_sessions','tail_query','tail_hash','boundary_anchor_query','boundary_tail_query','boundary_tail_hash','tail_stability')
    $allowedReasons=@('unspecified','sequence_gate','checkpoint_identity','oldest_reset_or_wrap','anchor_not_retained','range_bound','range_gap','record_order','audit_continuity_event','startup_count','logging_shutdown_after_startup','logon_type_unavailable','interactive_logon','anchor_changed','logon_policy_disabled','startup_policy_disabled','interactive_wts_session','stability_deadline','tail_reset','tail_id_reused','context_anchor_changed','final_tail_reset','final_boundary_deadline','final_tail_id_reused','final_hash_deadline','tail_never_stable','event_query_no_matches','native_call_failed')
    if($null -ne $Failure){
        $Diagnostic.error_kind=$Failure.Exception.GetType().Name;$Diagnostic.hresult=[long]$Failure.Exception.HResult
        if($Diagnostic.reason -ceq 'unspecified'){$Diagnostic.reason=if($Failure.FullyQualifiedErrorId -like 'NoMatchingEventsFound,*'){'event_query_no_matches'}else{'native_call_failed'}}
        $native=$Failure.Exception
        for($depth=0;$null -ne $native -and $depth -lt 8;$depth++){
            if($native -is [ComponentModel.Win32Exception]){$Diagnostic.native_code=[long]$native.NativeErrorCode;break}
            if($native.GetType().FullName -like 'System.Diagnostics.Eventing.Reader.*' -and $native.PSObject.Properties.Name -contains 'ErrorCode'){$Diagnostic.native_code=[long]$native.ErrorCode;break}
            $native=$native.InnerException
        }
    }
    $copy=@{schema_version=1;stage='checkpoint_admission';reason='unspecified'}
    if($Diagnostic.stage -cin $allowedStages){$copy.stage=$Diagnostic.stage}
    if($Diagnostic.reason -cin $allowedReasons){$copy.reason=$Diagnostic.reason}
    foreach($name in @('stability_attempt','elapsed_ms','checkpoint_record_id','checkpoint_oldest_record_id','oldest_record_id','newest_record_id','range_expected','range_actual','startup_events','startup_record_id','offending_event_id','offending_record_id','offending_logon_type','interactive_logons','interactive_sessions','tail_record_id','hresult','native_code')){
        if($Diagnostic.ContainsKey($name)){
            $value=$Diagnostic[$name]
            if($value -is [long] -or $value -is [int] -or $value -is [uint32] -or $value -is [uint16]){$copy[$name]=[long]$value}
        }
    }
    foreach($name in @('startup_scan_complete','logon_policy_enabled','startup_policy_enabled','policy_detail_unavailable','policy_change_tokens_recognized','policy_success_removed','policy_success_added','policy_failure_removed','policy_failure_added')){
        if($Diagnostic.ContainsKey($name) -and $Diagnostic[$name] -is [bool]){$copy[$name]=$Diagnostic[$name]}
    }
    if($Diagnostic.ContainsKey('policy_subcategory_guid') -and $Diagnostic.policy_subcategory_guid -cmatch '^0cce92[1-7][0-9a-f]-69ae-11d9-bed3-505054503030$'){$copy.policy_subcategory_guid=$Diagnostic.policy_subcategory_guid}
    if($Diagnostic.ContainsKey('error_kind') -and $Diagnostic.error_kind -cmatch '^[A-Za-z][A-Za-z0-9]{0,63}$'){$copy.error_kind=$Diagnostic.error_kind}
    $first=Get-Variable -Name securityAuditFirstFailure -Scope Script -ErrorAction SilentlyContinue
    if($null -eq $first -or $null -eq $first.Value){$script:securityAuditFirstFailure=$copy.Clone()}
    $script:securityAuditLastFailure=$copy.Clone()
}
function Assert-SecurityAuditContext {
    $script:securityAuditActiveDiagnostic.stage='native_logon_policy'
    $script:securityAuditActiveDiagnostic.logon_policy_enabled=[K5GuestNative]::LogonAuditEnabled()
    if(-not $script:securityAuditActiveDiagnostic.logon_policy_enabled){$script:securityAuditActiveDiagnostic.reason='logon_policy_disabled';throw 'Native audit policy coverage unavailable'}
    $script:securityAuditActiveDiagnostic.stage='native_startup_policy'
    $script:securityAuditActiveDiagnostic.startup_policy_enabled=[K5GuestNative]::StartupAuditEnabled()
    if(-not $script:securityAuditActiveDiagnostic.startup_policy_enabled){$script:securityAuditActiveDiagnostic.reason='startup_policy_disabled';throw 'Native audit policy coverage unavailable'}
    $script:securityAuditActiveDiagnostic.stage='native_wts_sessions'
    $script:securityAuditActiveDiagnostic.interactive_sessions=@([K5GuestNative]::InteractiveSessions()).Count
    if($script:securityAuditActiveDiagnostic.interactive_sessions -ne 0){$script:securityAuditActiveDiagnostic.reason='interactive_wts_session';throw 'Interactive session during audit boundary'}
}
function Security-RebootAudit($Checkpoint,[string]$PreviousBoot,[string]$CurrentBoot,[long]$PreviousSequence,[long]$CurrentSequence) {
    $stability=[Diagnostics.Stopwatch]::StartNew()
    for($attempt=0;$attempt -lt 8;$attempt++){
        $oldest=@();$newest=@();$anchor=@();$events=@();$oldestAfter=@();$anchorAfter=@();$tailAfter=@();$boundaryOldest=@();$boundaryAnchor=@();$boundaryTail=@()
        $diagnostic=@{stage='checkpoint_admission';reason='sequence_gate';stability_attempt=($attempt+1);startup_events=0;startup_scan_complete=$false}
        $script:securityAuditActiveDiagnostic=$diagnostic
        try{
            Assert-NextBootSequence $PreviousSequence $CurrentSequence
            $diagnostic.reason='unspecified'
            if($Checkpoint.boot_id -cne $PreviousBoot -or $CurrentBoot -cne $PreviousBoot -or $Checkpoint.boot_sequence -ne $PreviousSequence -or $Checkpoint.record_id -lt 1 -or $Checkpoint.oldest_record_id -lt 1 -or $Checkpoint.xml_sha256 -cnotmatch '^[a-f0-9]{64}$'){$diagnostic.reason='checkpoint_identity';throw 'Security reboot checkpoint differs'}
            $diagnostic.checkpoint_record_id=[long]$Checkpoint.record_id;$diagnostic.checkpoint_oldest_record_id=[long]$Checkpoint.oldest_record_id
            $diagnostic.stage='oldest_newest_query'
            $oldest=@(Get-WinEvent -LogName Security -MaxEvents 1 -Oldest -ErrorAction Stop)
            $newest=@(Get-WinEvent -LogName Security -MaxEvents 1 -ErrorAction Stop)
            if($oldest.Count -eq 1){$diagnostic.oldest_record_id=[long]$oldest[0].RecordId};if($newest.Count -eq 1){$diagnostic.newest_record_id=[long]$newest[0].RecordId}
            if($oldest.Count -ne 1 -or $newest.Count -ne 1 -or $oldest[0].RecordId -ne $Checkpoint.oldest_record_id -or $newest[0].RecordId -le $Checkpoint.record_id){$diagnostic.reason='oldest_reset_or_wrap';throw 'Security log cleared reset or wrapped'}
            $diagnostic.stage='checkpoint_query'
            $anchor=@(Get-WinEvent -LogName Security -FilterXPath ('*[System[EventRecordID={0}]]' -f [long]$Checkpoint.record_id) -MaxEvents 2 -ErrorAction Stop)
            if($anchor.Count -ne 1 -or (Security-EventHash $anchor[0]) -cne $Checkpoint.xml_sha256){$diagnostic.reason='anchor_not_retained';throw 'Security checkpoint was not retained'}
            $through=[long]$newest[0].RecordId;$count=$through-[long]$Checkpoint.record_id;$diagnostic.range_expected=$count
            if($count -lt 1 -or $count -gt 4096){$diagnostic.reason='range_bound';throw 'Security event range bound exceeded'}
            $xpath='*[System[EventRecordID>{0} and EventRecordID<={1}]]' -f [long]$Checkpoint.record_id,$through
            $diagnostic.stage='event_range_query'
            $events=@(Get-WinEvent -LogName Security -FilterXPath $xpath -Oldest -MaxEvents 4097 -ErrorAction Stop)
            $diagnostic.range_actual=$events.Count
            if($events.Count -ne $count){$diagnostic.reason='range_gap';throw 'Security event record range has a gap'}
            $diagnostic.stage='event_range_validation'
            $expected=[long]$Checkpoint.record_id+1;$startup=@()
            foreach($event in $events){
                if([long]$event.RecordId -ne $expected){$diagnostic.offending_event_id=[int]$event.Id;$diagnostic.offending_record_id=[long]$event.RecordId;$diagnostic.reason='record_order';throw 'Security event record range changed'};$expected++
                if($event.Id -in @(1101,1102,1104,1105,4612,4719,4912)){$diagnostic.offending_event_id=[int]$event.Id;$diagnostic.offending_record_id=[long]$event.RecordId;Add-SecurityPolicyDiagnostic $event $diagnostic;$diagnostic.reason='audit_continuity_event';throw 'Security log or audit policy continuity changed'}
                if($event.Id -eq 4608){$startup+=@($event);$diagnostic.startup_events=$startup.Count;if($startup.Count -eq 1){$diagnostic.startup_record_id=[long]$event.RecordId}}
            }
            $diagnostic.startup_events=$startup.Count;$diagnostic.startup_scan_complete=$true
            if($startup.Count -ne 1){$diagnostic.reason='startup_count';throw 'Exact new startup audit event unavailable'}
            $diagnostic.stage='startup_validation'
            [long]$startupId=$startup[0].RecordId;$interactive=0;$diagnostic.startup_record_id=$startupId;$diagnostic.stage='logon_validation'
            foreach($event in $events){
                if($event.Id -eq 1100 -and [long]$event.RecordId -gt $startupId){$diagnostic.offending_event_id=[int]$event.Id;$diagnostic.offending_record_id=[long]$event.RecordId;$diagnostic.reason='logging_shutdown_after_startup';throw 'Event logging stopped after startup'}
                if($event.Id -ne 4624 -or [long]$event.RecordId -le $startupId){continue}
                [xml]$xml=$event.ToXml();$types=@($xml.Event.EventData.Data|Where-Object{$_.Name -ceq 'LogonType'})
                if($types.Count -ne 1 -or [string]$types[0].'#text' -notmatch '^\d{1,2}$'){$diagnostic.offending_event_id=[int]$event.Id;$diagnostic.offending_record_id=[long]$event.RecordId;$diagnostic.reason='logon_type_unavailable';throw 'Logon audit type unavailable'}
                if([string]$types[0].'#text' -in @('2','7','10','11','12','13')){$interactive++;if(-not $diagnostic.ContainsKey('offending_event_id')){$diagnostic.offending_event_id=[int]$event.Id;$diagnostic.offending_record_id=[long]$event.RecordId;$diagnostic.offending_logon_type=[int]([string]$types[0].'#text')}}
            }
            $diagnostic.interactive_logons=$interactive
            if($interactive -ne 0){$diagnostic.reason='interactive_logon';throw 'Interactive logon preceded boot witness'}
            $diagnostic.stage='retained_anchor_query'
            $oldestAfter=@(Get-WinEvent -LogName Security -MaxEvents 1 -Oldest -ErrorAction Stop)
            $anchorAfter=@(Get-WinEvent -LogName Security -FilterXPath ('*[System[EventRecordID={0}]]' -f [long]$Checkpoint.record_id) -MaxEvents 2 -ErrorAction Stop)
            if($oldestAfter.Count -ne 1 -or $oldestAfter[0].RecordId -ne $Checkpoint.oldest_record_id -or $anchorAfter.Count -ne 1 -or (Security-EventHash $anchorAfter[0]) -cne $Checkpoint.xml_sha256){$diagnostic.reason='anchor_changed';throw 'Security log changed during observation'}
            # Policy and WTS checks are native in production. Then extend the
            # record range if anything arrived while that boundary was sampled.
            $diagnostic.stage='native_policy_wts_context'
            Assert-SecurityAuditContext
            $diagnostic.stage='tail_query'
            $tailAfter=@(Get-WinEvent -LogName Security -MaxEvents 1 -ErrorAction Stop)
            if($stability.Elapsed.TotalSeconds -ge 5){$diagnostic.reason='stability_deadline';throw [TimeoutException]::new('Security audit stability budget expired')}
            if($tailAfter.Count -eq 1){$diagnostic.tail_record_id=[long]$tailAfter[0].RecordId}
            if($tailAfter.Count -ne 1 -or $tailAfter[0].RecordId -lt $through){$diagnostic.reason='tail_reset';throw 'Security tail reset during observation'}
            if($tailAfter[0].RecordId -gt $through){continue}
            $diagnostic.stage='tail_hash'
            $tailHash=Security-EventHash $newest[0]
            if((Security-EventHash $tailAfter[0]) -cne $tailHash){$diagnostic.reason='tail_id_reused';throw 'Security tail record ID was reused'}
            $diagnostic.stage='boundary_anchor_query'
            $boundaryOldest=@(Get-WinEvent -LogName Security -MaxEvents 1 -Oldest -ErrorAction Stop)
            $boundaryAnchor=@(Get-WinEvent -LogName Security -FilterXPath ('*[System[EventRecordID={0}]]' -f [long]$Checkpoint.record_id) -MaxEvents 2 -ErrorAction Stop)
            if($boundaryOldest.Count -ne 1 -or $boundaryOldest[0].RecordId -ne $Checkpoint.oldest_record_id -or $boundaryAnchor.Count -ne 1 -or (Security-EventHash $boundaryAnchor[0]) -cne $Checkpoint.xml_sha256){$diagnostic.reason='context_anchor_changed';throw 'Security log reset during native context'}
            $diagnostic.stage='boundary_tail_query'
            $boundaryTail=@(Get-WinEvent -LogName Security -MaxEvents 1 -ErrorAction Stop)
            if($boundaryTail.Count -eq 1){$diagnostic.tail_record_id=[long]$boundaryTail[0].RecordId}
            if($boundaryTail.Count -ne 1 -or $boundaryTail[0].RecordId -lt $through){$diagnostic.reason='final_tail_reset';throw 'Security final tail reset'}
            if($stability.Elapsed.TotalSeconds -ge 5){$diagnostic.reason='final_boundary_deadline';throw [TimeoutException]::new('Security audit final boundary expired')}
            if($boundaryTail[0].RecordId -gt $through){continue}
            $diagnostic.stage='boundary_tail_hash'
            if((Security-EventHash $boundaryTail[0]) -cne $tailHash){$diagnostic.reason='final_tail_id_reused';throw 'Security final tail ID was reused'}
            if($stability.Elapsed.TotalSeconds -ge 5){$diagnostic.reason='final_hash_deadline';throw [TimeoutException]::new('Security audit final hash exceeded budget')}
            return @{checkpoint_retained=$true;no_clear_reset_wrap=$true;policy_coverage_verified=$true;tail_stable=$true;startup_event_id=4608;startup_record_id=$startupId;through_record_id=$through;interactive_logons=0;stability_attempts=($attempt+1)}
        }catch{$diagnostic.elapsed_ms=$stability.ElapsedMilliseconds;Save-SecurityAuditFailure $diagnostic $_;throw}finally{foreach($event in (@($oldest)+@($newest)+@($anchor)+@($events)+@($oldestAfter)+@($anchorAfter)+@($tailAfter)+@($boundaryOldest)+@($boundaryAnchor)+@($boundaryTail))){if($event -is [IDisposable]){$event.Dispose()}}}
    }
    $diagnostic.stage='tail_stability';$diagnostic.reason='tail_never_stable';$diagnostic.elapsed_ms=$stability.ElapsedMilliseconds;$diagnostic.error_kind='TimeoutException'
    Save-SecurityAuditFailure $diagnostic $null
    throw [TimeoutException]::new('Security audit tail did not stabilize')
}

if ($Mode -eq 'Setup') {
    $setupStage='installer'
    try {
        $setup = Join-Path $root 'K5VisionSetup.exe'
        if ((Get-FileHash -LiteralPath $setup -Algorithm SHA256).Hash.ToLowerInvariant() -cne $config.setup_sha256) { throw 'Setup identity differs' }
        $installed = Start-Process -FilePath $setup -ArgumentList @('/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART','/SP-') -PassThru
        if (-not $installed.WaitForExit(180000) -or $installed.ExitCode -ne 0) { throw 'Setup did not finish' }
        $setupStage='logon_audit'
        & (Join-Path $env:SystemRoot 'System32\auditpol.exe') /set '/subcategory:{0CCE9215-69AE-11D9-BED3-505054503030}' /success:enable /failure:enable | Out-Null
        if ($LASTEXITCODE -ne 0 -or -not [K5GuestNative]::LogonAuditEnabled()) { throw 'Logon audit unavailable' }
        $setupStage='startup_audit'
        & (Join-Path $env:SystemRoot 'System32\auditpol.exe') /set '/subcategory:{0CCE9210-69AE-11D9-BED3-505054503030}' /success:enable | Out-Null
        if ($LASTEXITCODE -ne 0 -or -not [K5GuestNative]::StartupAuditEnabled()) { throw 'Startup audit unavailable' }
        $setupStage='kvp_service'
        Set-Service -Name vmickvpexchange -StartupType Automatic
        Start-Service -Name vmickvpexchange
        $setupStage='guest_network'
        $adapter = @(Get-NetAdapter | Where-Object { $_.MacAddress.Replace('-','') -ceq $config.guest_mac })
        if ($adapter.Count -ne 1) { throw 'Owned guest adapter unavailable' }
        New-NetIPAddress -InterfaceIndex $adapter[0].ifIndex -IPAddress $config.guest_ip -PrefixLength 30 -AddressFamily IPv4 | Out-Null
        $setupStage='register_observer'
        # Task history is diagnostic-only; no output or credential is exported.
        try{& (Join-Path $env:SystemRoot 'System32\wevtutil.exe') sl 'Microsoft-Windows-TaskScheduler/Operational' /e:true 2>&1|Out-Null}catch{}
        $observerAction = New-ScheduledTaskAction -Execute (Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe') -Argument '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File C:\K5GuestWitness\windows_guest_witness.ps1 -Mode Observe'
        $system = New-ScheduledTaskPrincipal -UserId SYSTEM -LogonType ServiceAccount -RunLevel Highest
        $observerName = 'K5GuestWitness-' + $transfer
        Register-ScheduledTask -TaskName $observerName -Action $observerAction -Trigger (New-ScheduledTaskTrigger -AtStartup) -Principal $system -Force | Out-Null
        $setupStage='register_bootstrap'
        $bootstrap = New-ScheduledTaskAction -Execute (Join-Path $root 'windows_guest_bootstrap.exe') -WorkingDirectory $root
        $admin = New-ScheduledTaskPrincipal -UserId Administrator -LogonType Interactive -RunLevel Highest
        Register-ScheduledTask -TaskName ('K5GuestBootstrap-' + $transfer) -Action $bootstrap -Trigger (New-ScheduledTaskTrigger -AtLogOn -User Administrator) -Principal $admin -Force | Out-Null
        $setupStage='start_observer'
        Start-ScheduledTask -TaskName $observerName
        exit 0
    } catch {
        $failure = @{schema_version=1; phase='setup'; transfer_id=$transfer; status='failed';failure_stage=$setupStage; error_kind=$_.Exception.GetType().Name; hresult=$_.Exception.HResult}
        $setupError=$_.Exception
        for($depth=0;$null -ne $setupError -and $depth -lt 8;$depth++){
            if($setupError -is [ComponentModel.Win32Exception]){$failure.native_code=$setupError.NativeErrorCode;break};$setupError=$setupError.InnerException
        }
        $hash = Write-PrivateJson 'setup-failure.json' $failure
        Publish-Seal 'failed' ([K5GuestNative]::BootId()) $hash
        exit 1
    }
}

$phase = [string]$config.phase
try{
    $boot = [K5GuestNative]::BootId()
    $bootSequence = [K5GuestNative]::BootSequence()
    Write-EntryDiagnostic ('observer-stage-'+$phase+'.json') @{schema_version=1;phase=$phase;status='native_ready';stage='native_boot_id';boot_id=$boot;boot_sequence=$bootSequence;observer_session=$guestWitnessSession}
}catch{
    Write-EntryDiagnostic ('observer-preflight-failure-'+$phase+'.json') @{schema_version=1;phase=$phase;stage='native_boot_id';status='preflight_failed';error_kind=$_.Exception.GetType().Name;hresult=$_.Exception.HResult;observer_session=$guestWitnessSession}
    exit 1
}
$receipt = [ordered]@{
    schema_version=1; phase=$phase; transfer_id=$transfer; status='failed'; boot_id=$boot; boot_sequence=$bootSequence; native_boot_sequence_verified=$true;
    head_sha=$config.head_sha; installer_sha256=$config.setup_sha256; observer_sid='SYSTEM'; observer_session=0;
    startup_before_interactive_login=$false; recording_after_reboot=$false; recording_after_signout=$false;
    retained_closed_hashes_unchanged=$false; logon_audit_enabled=$false; cleanup_credentials_removed=$false
}
$observationStage='phase_admission'
try {
    if ($phase -eq 'bootstrap') {
        $observationStage='bootstrap_wait'
        $watch = [Diagnostics.Stopwatch]::StartNew()
        $bootstrapPath = Join-Path $root 'bootstrap-receipt.json'
        while (-not (Test-Path -LiteralPath $bootstrapPath)) {
            if ($watch.Elapsed.TotalSeconds -gt 360) { throw 'Bootstrap receipt unavailable' }
            Start-Sleep -Milliseconds 500
        }
        if ((Get-Item -LiteralPath $bootstrapPath).Length -gt 65536) { throw 'Bootstrap receipt too large' }
        $bootstrap = Get-Content -LiteralPath $bootstrapPath -Raw | ConvertFrom-Json
        $receipt.bootstrap=$bootstrap
        if ($bootstrap.status -cne 'bootstrap_complete' -or $bootstrap.transfer_id -cne $transfer -or $bootstrap.head_sha -cne $config.head_sha -or $bootstrap.setup_sha256 -cne $config.setup_sha256) { throw 'Bootstrap scope differs' }
        if($bootstrap.mfa_enabled_before_reboot -ne $true -or $bootstrap.mfa_confirmed_via_normal_api -ne $true){throw 'Bootstrap MFA was not established'}
        $receipt.mfa_enabled_before_reboot=$true
        Disable-TemporaryAutoLogon
        $receipt.cleanup_credentials_removed = $true
        $observationStage='closed_recording_baseline'
        $watch.Restart()
        do {
            $closed = @(Closed-Recordings)
            if ($closed.Count -gt 0) { break }
            if ($watch.Elapsed.TotalSeconds -gt 90) { throw 'No closed recording before reboot' }
            Start-Sleep -Seconds 1
        } while ($true)
        if(-not [K5GuestNative]::LogonAuditEnabled() -or -not [K5GuestNative]::StartupAuditEnabled()){throw 'Audit coverage disabled before reboot'}
        $baselineSequence=[K5GuestNative]::BootSequence()
        if($baselineSequence -ne $bootSequence){throw 'Bootstrap native sequence changed'}
        $checkpoint=Security-Checkpoint $boot $bootSequence
        $baseline = @{schema_version=1; boot_id=$boot; boot_sequence=$bootSequence; transfer_id=$transfer; files=$closed; security_checkpoint=$checkpoint}
        $receipt.startup_audit_enabled=$true
        $receipt.baseline_sha256 = Write-PrivateJson 'baseline.json' $baseline
        $receipt.service = Recorder-Facts
        $receipt.recording_bytes = Recording-Bytes
        $receipt.closed_recording_count = $closed.Count
        $receipt.status = 'bootstrap_sealed'
    } elseif ($phase -eq 'reboot') {
        $script:securityAuditFirstFailure=$null;$script:securityAuditLastFailure=$null
        $observationStage='reboot_identity'
        Write-EntryDiagnostic 'observer-stage-reboot.json' @{schema_version=1;phase=$phase;status='observing';stage=$observationStage;boot_id=$boot;observer_session=$guestWitnessSession}
        $rebootWatch=[Diagnostics.Stopwatch]::StartNew()
        $recordingRoot=Join-Path ([Environment]::GetFolderPath('CommonApplicationData')) 'K5Vision\recordings'
        $startupPaths=@(Recording-Files|ForEach-Object{$_.FullName})
        $receipt.startup_recording_inventory_count=$startupPaths.Count
        Disable-TemporaryAutoLogon
        $receipt.cleanup_credentials_removed = $true
        $receipt.logon_audit_enabled = [K5GuestNative]::LogonAuditEnabled()
        if (-not $receipt.logon_audit_enabled) { throw 'Logon audit disabled' }
        $baseline = Get-Content -LiteralPath (Join-Path $root 'baseline.json') -Raw | ConvertFrom-Json
        if ($baseline.transfer_id -cne $transfer -or $baseline.boot_id -cne $config.previous_boot_id -or $boot -cne $baseline.boot_id -or $baseline.boot_sequence -ne $config.previous_boot_sequence) { throw 'Reboot source identity differs' }
        Assert-NextBootSequence $baseline.boot_sequence $bootSequence
        $receipt.startup_audit_enabled=[K5GuestNative]::StartupAuditEnabled()
        if(-not $receipt.startup_audit_enabled){throw 'Startup audit disabled'}
        $observationStage='service_before_login'
        Write-EntryDiagnostic 'observer-stage-reboot.json' @{schema_version=1;phase=$phase;status='observing';stage=$observationStage;boot_id=$boot;observer_session=$guestWitnessSession}
        $watch = [Diagnostics.Stopwatch]::StartNew()
        while ($true) {
            if (@([K5GuestNative]::InteractiveSessions()).Count -ne 0) { throw 'Interactive session preceded boot witness' }
            try { $observationStage='boot_security_audit';$audit=Security-RebootAudit $baseline.security_checkpoint $baseline.boot_id $boot $baseline.boot_sequence $bootSequence;$observationStage='service_before_login';$service = Recorder-Facts; break } catch { if ($watch.Elapsed.TotalSeconds -gt 90) { throw }; Start-Sleep -Milliseconds 250 }
        }
        $before = Recording-Bytes
        $observationStage='prelogin_recording_growth'
        Write-EntryDiagnostic 'observer-stage-reboot.json' @{schema_version=1;phase=$phase;status='observing';stage=$observationStage;boot_id=$boot;observer_session=$guestWitnessSession}
        $watch.Restart()
        do {
            if (@([K5GuestNative]::InteractiveSessions()).Count -ne 0) { throw 'Interactive session during pre-login recording' }
            $service = Recorder-Facts
            Start-Sleep -Milliseconds 250
        } while ($watch.Elapsed.TotalSeconds -lt 20)
        $after = Recording-Bytes
        if ($after -le $before) { throw 'Pre-login recording growth failed' }
        $closed = @(Closed-Recordings)
        $observationStage='retained_closed_recordings'
        foreach ($saved in @($baseline.files)) {
            $matches = @($closed | Where-Object { $_.path -ceq $saved.path -and $_.bytes -eq $saved.bytes -and $_.sha256 -ceq $saved.sha256 })
            if ($matches.Count -ne 1) { throw 'Retained closed recording differs' }
        }
        $observationStage='new_closed_postboot_recording'
        Write-EntryDiagnostic 'observer-stage-reboot.json' @{schema_version=1;phase=$phase;status='observing';stage=$observationStage;boot_id=$boot;observer_session=$guestWitnessSession}
        $auditWatch=[Diagnostics.Stopwatch]::StartNew();$postboot=@()
        do{
            if(@([K5GuestNative]::InteractiveSessions()).Count -ne 0){throw 'Interactive session before new recording closed'}
            $service=Recorder-Facts
            $postboot=@(Closed-Recordings|Where-Object{(Join-Path $recordingRoot $_.path) -notin $startupPaths})
            if($postboot.Count -gt 0){break}
            if($rebootWatch.Elapsed.TotalSeconds -ge 200){throw 'New closed postboot recording unavailable'}
            if($auditWatch.Elapsed.TotalSeconds -ge 1){$audit=Security-RebootAudit $baseline.security_checkpoint $baseline.boot_id $boot $baseline.boot_sequence $bootSequence;$auditWatch.Restart()}
            Start-Sleep -Milliseconds 250
        }while($true)
        $receipt.postboot_closed_recording=$postboot[0]
        $receipt.postboot_clip_created_after_system_inventory=$true
        $receipt.reboot_observation_total_seconds=[Math]::Round($rebootWatch.Elapsed.TotalSeconds,3)
        $observationStage='final_boot_security_audit'
        Write-EntryDiagnostic 'observer-stage-reboot.json' @{schema_version=1;phase=$phase;status='observing';stage=$observationStage;boot_id=$boot;observer_session=$guestWitnessSession}
        $audit=Security-RebootAudit $baseline.security_checkpoint $baseline.boot_id $boot $baseline.boot_sequence $bootSequence
        $receipt.security_audit_checkpoint_retained=$audit.checkpoint_retained
        $receipt.security_no_clear_reset_wrap=$audit.no_clear_reset_wrap
        $receipt.security_policy_coverage_verified=$audit.policy_coverage_verified
        $receipt.security_tail_stable=$audit.tail_stable
        $receipt.startup_audit_4608_verified=($audit.startup_event_id -eq 4608)
        $receipt.startup_audit_record_id=$audit.startup_record_id
        $receipt.security_observed_through_record_id=$audit.through_record_id
        $receipt.previous_boot_id = $baseline.boot_id
        $receipt.previous_boot_sequence = $baseline.boot_sequence
        $receipt.native_boot_sequence_increment_verified = $true
        $receipt.startup_before_interactive_login = $true
        $receipt.recording_after_reboot = $true
        $receipt.retained_closed_hashes_unchanged = $true
        $receipt.recording_bytes_before = $before
        $receipt.recording_bytes_after = $after
        $receipt.observed_seconds = [Math]::Round($watch.Elapsed.TotalSeconds,3)
        $receipt.interactive_sessions_observed = 0
        $receipt.interactive_logons_since_boot = 0
        $receipt.service = $service
        $receipt.status = 'reboot_sealed'
    } elseif ($phase -eq 'signout') {
        $observationStage='interactive_session_admission'
        Disable-TemporaryAutoLogon
        $receipt.cleanup_credentials_removed = $true
        if ($boot -cne $config.previous_boot_id -or $bootSequence -ne $config.previous_boot_sequence) { throw 'Signout changed boot' }
        $sessions = @([K5GuestNative]::InteractiveSessions())
        if ($sessions.Count -ne 1 -or $sessions[0] -le 0) { throw 'Exact interactive session unavailable' }
        $service = Recorder-Facts
        $before = Recording-Bytes
        [K5GuestNative]::Logoff($sessions[0])
        $observationStage='actual_session_logoff'
        $watch = [Diagnostics.Stopwatch]::StartNew()
        while (@([K5GuestNative]::InteractiveSessions()).Count -ne 0) {
            if ($watch.Elapsed.TotalSeconds -gt 20) { throw 'Actual session did not sign out' }
            Start-Sleep -Milliseconds 250
        }
        $watch.Restart()
        $observationStage='post_logoff_recording_growth'
        do {
            if (@([K5GuestNative]::InteractiveSessions()).Count -ne 0) { throw 'Interactive session returned' }
            $service = Recorder-Facts
            Start-Sleep -Milliseconds 250
        } while ($watch.Elapsed.TotalSeconds -lt 20)
        $after = Recording-Bytes
        if ($after -le $before -or [K5GuestNative]::BootId() -cne $boot -or [K5GuestNative]::BootSequence() -ne $bootSequence) { throw 'Recording did not survive same-boot signout' }
        $receipt.signed_out_session_id = $sessions[0]
        $receipt.recording_after_signout = $true
        $receipt.recording_bytes_before = $before
        $receipt.recording_bytes_after = $after
        $receipt.observed_seconds = [Math]::Round($watch.Elapsed.TotalSeconds,3)
        $receipt.service = $service
        $receipt.status = 'signout_sealed'
    } else { throw 'Unadmitted phase' }
} catch {
    $receipt.error_kind = $_.Exception.GetType().Name
    $receipt.hresult = $_.Exception.HResult
    $receipt.failure_stage=$observationStage
    if($phase -ceq 'reboot' -and $null -ne $script:securityAuditLastFailure){$receipt.security_audit_diagnostic=@{first_failure=$script:securityAuditFirstFailure;last_failure=$script:securityAuditLastFailure}}
}
$hash = Write-PrivateJson ($phase + '-witness.json') $receipt
Publish-Seal $phase $boot $hash
if ($receipt.status -eq 'failed') { exit 1 }
