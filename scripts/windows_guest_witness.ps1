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
$systemSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
if ($systemSid -ne 'S-1-5-18' -or [Diagnostics.Process]::GetCurrentProcess().SessionId -ne 0) { exit 2 }

Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Security.Principal;
public static class K5GuestNative {
  [StructLayout(LayoutKind.Sequential)] struct Boot { public Guid Id; public int Firmware; public ulong Flags; }
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
  [DllImport("advapi32.dll",SetLastError=true)] static extern bool AuditQuerySystemPolicy(ref Guid subcategory,uint count,out IntPtr policy);
  [DllImport("advapi32.dll")] static extern void AuditFree(IntPtr buffer);
  public static string BootId() { var b=new Boot(); int n; if(NtQuerySystemInformation(90,ref b,Marshal.SizeOf(b),out n)<0 || b.Id==Guid.Empty) throw new InvalidOperationException(); return b.Id.ToString(); }
  public static bool LogonAuditEnabled() { var g=new Guid("0cce9215-69ae-11d9-bed3-505054503030"); IntPtr p; if(!AuditQuerySystemPolicy(ref g,1,out p)) throw new Win32Exception(); try{return (((Audit)Marshal.PtrToStructure(p,typeof(Audit))).Flags&1)!=0;} finally{AuditFree(p);} }
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

function Write-PrivateJson([string]$Name, $Value) {
    $path = Join-Path $root $Name
    if (Test-Path -LiteralPath $path) { throw 'Witness receipt already exists' }
    $temporary = Join-Path $root ([guid]::NewGuid().ToString('N') + '.tmp')
    [IO.File]::WriteAllText($temporary, ($Value | ConvertTo-Json -Depth 12 -Compress), [Text.UTF8Encoding]::new($false))
    [IO.File]::Move($temporary, $path)
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
function Interactive-LogonsSince([datetime]$Since) {
    $events = @()
    try { $events = @(Get-WinEvent -FilterHashtable @{LogName='Security'; Id=4624; StartTime=$Since} -ErrorAction Stop) }
    catch { if ($_.FullyQualifiedErrorId -notlike 'NoMatchingEventsFound*') { throw } }
    if ($events.Count -gt 4096) { throw 'Logon audit bound exceeded' }
    $count = 0
    foreach ($event in $events) {
        [xml]$xml = $event.ToXml()
        $kind = $xml.Event.EventData.Data | Where-Object { $_.Name -ceq 'LogonType' }
        if ([string]$kind.'#text' -in @('2','7','10','11')) { $count++ }
    }
    return $count
}

if ($Mode -eq 'Setup') {
    try {
        $setup = Join-Path $root 'K5VisionSetup.exe'
        if ((Get-FileHash -LiteralPath $setup -Algorithm SHA256).Hash.ToLowerInvariant() -cne $config.setup_sha256) { throw 'Setup identity differs' }
        $installed = Start-Process -FilePath $setup -ArgumentList @('/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART','/SP-') -PassThru
        if (-not $installed.WaitForExit(180000) -or $installed.ExitCode -ne 0) { throw 'Setup did not finish' }
        & (Join-Path $env:SystemRoot 'System32\auditpol.exe') /set '/subcategory:{0CCE9215-69AE-11D9-BED3-505054503030}' /success:enable /failure:enable | Out-Null
        if ($LASTEXITCODE -ne 0 -or -not [K5GuestNative]::LogonAuditEnabled()) { throw 'Logon audit unavailable' }
        Set-Service -Name vmickvpexchange -StartupType Automatic
        Start-Service -Name vmickvpexchange
        $adapter = @(Get-NetAdapter | Where-Object { $_.MacAddress.Replace('-','') -ceq $config.guest_mac })
        if ($adapter.Count -ne 1) { throw 'Owned guest adapter unavailable' }
        New-NetIPAddress -InterfaceIndex $adapter[0].ifIndex -IPAddress $config.guest_ip -PrefixLength 30 -AddressFamily IPv4 | Out-Null
        $observerAction = New-ScheduledTaskAction -Execute (Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe') -Argument '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File C:\K5GuestWitness\windows_guest_witness.ps1 -Mode Observe'
        $system = New-ScheduledTaskPrincipal -UserId SYSTEM -LogonType ServiceAccount -RunLevel Highest
        $observerName = 'K5GuestWitness-' + $transfer
        Register-ScheduledTask -TaskName $observerName -Action $observerAction -Trigger (New-ScheduledTaskTrigger -AtStartup) -Principal $system -Force | Out-Null
        $bootstrap = New-ScheduledTaskAction -Execute (Join-Path $root 'windows_guest_bootstrap.exe') -WorkingDirectory $root
        $admin = New-ScheduledTaskPrincipal -UserId Administrator -LogonType Interactive -RunLevel Highest
        Register-ScheduledTask -TaskName ('K5GuestBootstrap-' + $transfer) -Action $bootstrap -Trigger (New-ScheduledTaskTrigger -AtLogOn -User Administrator) -Principal $admin -Force | Out-Null
        Start-ScheduledTask -TaskName $observerName
        exit 0
    } catch {
        $failure = @{schema_version=1; phase='setup'; transfer_id=$transfer; status='failed'; error_kind=$_.Exception.GetType().Name; hresult=$_.Exception.HResult}
        $hash = Write-PrivateJson 'setup-failure.json' $failure
        Publish-Seal 'failed' ([K5GuestNative]::BootId()) $hash
        exit 1
    }
}

$phase = [string]$config.phase
$boot = [K5GuestNative]::BootId()
$receipt = [ordered]@{
    schema_version=1; phase=$phase; transfer_id=$transfer; status='failed'; boot_id=$boot;
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
        $baseline = @{schema_version=1; boot_id=$boot; transfer_id=$transfer; files=$closed}
        $receipt.baseline_sha256 = Write-PrivateJson 'baseline.json' $baseline
        $receipt.service = Recorder-Facts
        $receipt.recording_bytes = Recording-Bytes
        $receipt.closed_recording_count = $closed.Count
        $receipt.status = 'bootstrap_sealed'
    } elseif ($phase -eq 'reboot') {
        $observationStage='reboot_identity'
        Disable-TemporaryAutoLogon
        $receipt.cleanup_credentials_removed = $true
        $receipt.logon_audit_enabled = [K5GuestNative]::LogonAuditEnabled()
        if (-not $receipt.logon_audit_enabled) { throw 'Logon audit disabled' }
        $baseline = Get-Content -LiteralPath (Join-Path $root 'baseline.json') -Raw | ConvertFrom-Json
        if ($baseline.transfer_id -cne $transfer -or $baseline.boot_id -cne $config.previous_boot_id -or $boot -ceq $baseline.boot_id) { throw 'Actual reboot not established' }
        $bootTime = (Get-CimInstance Win32_OperatingSystem).LastBootUpTime
        $observationStage='service_before_login'
        $watch = [Diagnostics.Stopwatch]::StartNew()
        while ($true) {
            if (@([K5GuestNative]::InteractiveSessions()).Count -ne 0) { throw 'Interactive session preceded boot witness' }
            try { $service = Recorder-Facts; break } catch { if ($watch.Elapsed.TotalSeconds -gt 90) { throw }; Start-Sleep -Milliseconds 250 }
        }
        $before = Recording-Bytes
        $observationStage='prelogin_recording_growth'
        $watch.Restart()
        do {
            if (@([K5GuestNative]::InteractiveSessions()).Count -ne 0) { throw 'Interactive session during pre-login recording' }
            $service = Recorder-Facts
            Start-Sleep -Milliseconds 250
        } while ($watch.Elapsed.TotalSeconds -lt 20)
        $after = Recording-Bytes
        if ($after -le $before -or (Interactive-LogonsSince $bootTime) -ne 0) { throw 'Pre-login recording or logon audit failed' }
        $closed = @(Closed-Recordings)
        $observationStage='retained_closed_recordings'
        foreach ($saved in @($baseline.files)) {
            $matches = @($closed | Where-Object { $_.path -ceq $saved.path -and $_.bytes -eq $saved.bytes -and $_.sha256 -ceq $saved.sha256 })
            if ($matches.Count -ne 1) { throw 'Retained closed recording differs' }
        }
        $receipt.previous_boot_id = $baseline.boot_id
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
        if ($boot -cne $config.previous_boot_id) { throw 'Signout changed boot' }
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
        if ($after -le $before -or [K5GuestNative]::BootId() -cne $boot) { throw 'Recording did not survive same-boot signout' }
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
}
$hash = Write-PrivateJson ($phase + '-witness.json') $receipt
Publish-Seal $phase $boot $hash
if ($receipt.status -eq 'failed') { exit 1 }
