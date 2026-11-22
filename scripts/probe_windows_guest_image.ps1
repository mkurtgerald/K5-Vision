# Official-image discovery only. This does not boot Windows or qualify K5.
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$VerbosePreference = 'SilentlyContinue'
$receipt = [ordered]@{
    schema_version=1; status='diagnostic_complete'; diagnostic_only=$true;
    product_qualification=$false; windows_boot_qualified=$false;
    outcome='unavailable'; stage='host_guard'; cleanup_complete=$true;
    source_page='https://www.microsoft.com/en-us/evalcenter/download-windows-server-2025';
    source_host='software-static.download.prss.microsoft.com';
    source_bytes=11686379520; downloaded_bytes=0; image_sha256=$null;
    eligible_fixed_drive_count=0; selected_available_bytes=0; free_memory_bytes=0;
    read_only_mount_verified=$false; offline_installation_type=$null;
    desktop_experience_verified=$false; kernel_signature_verified=$false;
    failure_kind=$null; failure_hresult=0; failure_native_code=0
}
$ownedRoot=$null; $ownedRootIdentity=$null; $image=$null; $mounted=$false
$accessPath=$null; $diskNumber=$null; $partitionNumber=$null; $hiveName=$null; $hiveLoaded=$false
function Set-PrivateDirectory([string]$Path) {
    $directory = New-Item -ItemType Directory -Path $Path
    $security = [Security.AccessControl.DirectorySecurity]::new()
    $security.SetAccessRuleProtection($true,$false)
    foreach ($sid in @([Security.Principal.WindowsIdentity]::GetCurrent().User.Value,'S-1-5-18','S-1-5-32-544') | Select-Object -Unique) {
        $rule = [Security.AccessControl.FileSystemAccessRule]::new([Security.Principal.SecurityIdentifier]::new($sid),'FullControl','ContainerInherit,ObjectInherit','None','Allow')
        $security.AddAccessRule($rule)
    }
    Set-Acl -LiteralPath $Path -AclObject $security
    return $directory
}
function Assert-OwnedRoot {
    $current = Get-Item -LiteralPath $ownedRoot -Force
    if ($current.FullName -cne $ownedRootIdentity.FullName -or $current.CreationTimeUtc.Ticks -ne $ownedRootIdentity.CreationTimeUtc.Ticks -or
        ($current.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or $current.Name -cnotmatch '^K5GuestImage-[a-f0-9]{32}$') { throw 'Owned image directory changed' }
}
try {
    if ($env:GITHUB_ACTIONS -ne 'true' -or $env:RUNNER_ENVIRONMENT -ne 'github-hosted' -or $env:RUNNER_OS -ne 'Windows' -or $PSVersionTable.PSEdition -ne 'Core') { throw 'Disposable native host required' }
    $os = Get-CimInstance Win32_OperatingSystem
    $receipt.free_memory_bytes = [long]$os.FreePhysicalMemory * 1024
    $drives = @([IO.DriveInfo]::GetDrives() | Where-Object { $_.DriveType -eq [IO.DriveType]::Fixed -and $_.IsReady -and $_.AvailableFreeSpace -ge 24GB } | Sort-Object AvailableFreeSpace -Descending)
    $receipt.eligible_fixed_drive_count = $drives.Count
    $receipt.stage='resources'
    if ($receipt.free_memory_bytes -lt 5GB -or $drives.Count -eq 0) { throw 'Guest resources unavailable' }
    $receipt.selected_available_bytes = [long]$drives[0].AvailableFreeSpace
    $ownedRoot = Join-Path $drives[0].RootDirectory.FullName ('K5GuestImage-' + [guid]::NewGuid().ToString('N'))
    $ownedRootIdentity = Set-PrivateDirectory $ownedRoot
    $image = Join-Path $ownedRoot 'microsoft-evaluation.vhdx'
    $receipt.stage='official_download'
    $url='https://software-static.download.prss.microsoft.com/dbazure/888969d5-f34g-4e03-ac9d-1f9786c66749/26100.1742.amd64fre.ge_release_svc_refresh.240906-0331_server_serverdatacentereval_en-us.vhdx'
    $handler=[Net.Http.HttpClientHandler]::new(); $handler.AllowAutoRedirect=$false
    $client=[Net.Http.HttpClient]::new($handler); $client.Timeout=[TimeSpan]::FromMinutes(12)
    $cancellation=[Threading.CancellationTokenSource]::new(720000)
    $response=$null; $input=$null; $output=$null
    try {
        $response=$client.GetAsync($url,[Net.Http.HttpCompletionOption]::ResponseHeadersRead,$cancellation.Token).GetAwaiter().GetResult()
        if ([int]$response.StatusCode -ne 200 -or $response.RequestMessage.RequestUri.Host -cne $receipt.source_host -or $response.Content.Headers.ContentLength -ne $receipt.source_bytes) { throw 'Official image identity differs' }
        $input=$response.Content.ReadAsStreamAsync($cancellation.Token).GetAwaiter().GetResult()
        $output=[IO.File]::Open($image,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
        $buffer=[byte[]]::new(1048576)
        while (($count=$input.ReadAsync($buffer,0,$buffer.Length,$cancellation.Token).GetAwaiter().GetResult()) -gt 0) {
            $receipt.downloaded_bytes += $count
            if ($receipt.downloaded_bytes -gt $receipt.source_bytes) { throw 'Official image length exceeded' }
            $output.Write($buffer,0,$count)
        }
        $output.Flush($true)
    } finally {
        if ($null -ne $output) { $output.Dispose() }; if ($null -ne $input) { $input.Dispose() }
        if ($null -ne $response) { $response.Dispose() }; $cancellation.Dispose(); $client.Dispose(); $handler.Dispose()
    }
    if ($receipt.downloaded_bytes -ne $receipt.source_bytes -or (Get-Item -LiteralPath $image).Length -ne $receipt.source_bytes) { throw 'Official image incomplete' }
    $receipt.stage='full_byte_hash'
    $receipt.image_sha256=(Get-FileHash -LiteralPath $image -Algorithm SHA256).Hash.ToLowerInvariant()
    Assert-OwnedRoot
    $receipt.stage='read_only_mount'
    $attached=Mount-VHD -Path $image -ReadOnly -NoDriveLetter -PassThru
    $mounted=$true; $diskNumber=[int]$attached.DiskNumber
    $admitted=Get-VHD -Path $image
    if (-not $admitted.Attached -or [int]$admitted.DiskNumber -ne $diskNumber -or -not (Get-Disk -Number $diskNumber).IsReadOnly) { throw 'Read-only owned image mount differs' }
    $receipt.read_only_mount_verified=$true
    $accessPath=(Join-Path $ownedRoot 'offline') + '\'
    New-Item -ItemType Directory -Path $accessPath | Out-Null
    $partitions=@(Get-Partition -DiskNumber $diskNumber | Where-Object { $_.Size -gt 1GB })
    if ($partitions.Count -gt 8) { throw 'Image partition count exceeded' }
    $windows=$null
    foreach ($partition in $partitions) {
        $partitionNumber=[int]$partition.PartitionNumber
        Add-PartitionAccessPath -DiskNumber $diskNumber -PartitionNumber $partitionNumber -AccessPath $accessPath
        if (Test-Path -LiteralPath (Join-Path $accessPath 'Windows\System32\config\SOFTWARE') -PathType Leaf) { $windows=$accessPath; break }
        Remove-PartitionAccessPath -DiskNumber $diskNumber -PartitionNumber $partitionNumber -AccessPath $accessPath
        $partitionNumber=$null
    }
    if ($null -eq $windows) { throw 'Windows installation not found' }
    $receipt.stage='offline_windows_identity'
    $kernel=Join-Path $windows 'Windows\System32\ntoskrnl.exe'
    $signature=Get-AuthenticodeSignature -LiteralPath $kernel
    if ($signature.Status -ne 'Valid' -or $null -eq $signature.SignerCertificate -or $signature.SignerCertificate.Subject -notmatch 'O=Microsoft Corporation') { throw 'Microsoft kernel signature unavailable' }
    $receipt.kernel_signature_verified=$true
    $receipt.kernel_sha256=(Get-FileHash -LiteralPath $kernel -Algorithm SHA256).Hash.ToLowerInvariant()
    # Registry load/recovery is restricted to a private copy. The official VHDX
    # remains read-only, including its original SOFTWARE hive and sidecars.
    $hiveCopy=Join-Path $ownedRoot 'software-copy.hiv'
    Copy-Item -LiteralPath (Join-Path $windows 'Windows\System32\config\SOFTWARE') -Destination $hiveCopy
    $hiveName='K5GuestImage_' + [guid]::NewGuid().ToString('N')
    & (Join-Path $env:SystemRoot 'System32\reg.exe') load ('HKLM\' + $hiveName) $hiveCopy | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Offline image registry unavailable' }
    $hiveLoaded=$true
    $key=[Microsoft.Win32.Registry]::LocalMachine.OpenSubKey($hiveName + '\Microsoft\Windows NT\CurrentVersion')
    try {
        if ($null -eq $key) { throw 'Offline Windows version unavailable' }
        $type=[string]$key.GetValue('InstallationType')
        $build=[string]$key.GetValue('CurrentBuild')
        $product=[string]$key.GetValue('ProductName')
        if ($type.Length -gt 64 -or $build -notmatch '^\d{4,6}$' -or $product.Length -gt 256) { throw 'Offline Windows identity refused' }
        $receipt.offline_installation_type=$type; $receipt.offline_windows_build=$build; $receipt.offline_product_name=$product
        $receipt.desktop_experience_verified=($type -ceq 'Server' -and (Test-Path -LiteralPath (Join-Path $windows 'Windows\explorer.exe') -PathType Leaf))
    } finally { if ($null -ne $key) { $key.Dispose() } }
    $receipt.outcome=if ($receipt.desktop_experience_verified) { 'official_desktop_image_inspected' } else { 'image_requires_desktop_experience' }
    $receipt.stage='complete'
} catch {
    $receipt.failure_kind=$_.Exception.GetType().Name; $receipt.failure_hresult=$_.Exception.HResult
    if ($_.Exception -is [ComponentModel.Win32Exception]) { $receipt.failure_native_code=$_.Exception.NativeErrorCode }
} finally {
    try {
        if ($hiveLoaded) {
            & (Join-Path $env:SystemRoot 'System32\reg.exe') unload ('HKLM\' + $hiveName) | Out-Null
            if ($LASTEXITCODE -ne 0) { throw 'Owned image hive remains loaded' }; $hiveLoaded=$false
        }
        if ($mounted) {
            $admitted=Get-VHD -Path $image
            if (-not $admitted.Attached -or [int]$admitted.DiskNumber -ne $diskNumber) { throw 'Owned image attachment changed' }
            if ($null -ne $partitionNumber) { Remove-PartitionAccessPath -DiskNumber $diskNumber -PartitionNumber $partitionNumber -AccessPath $accessPath }
            Dismount-VHD -Path $image
            if ((Get-VHD -Path $image).Attached) { throw 'Owned image remains attached' }; $mounted=$false
        }
        if ($null -ne $ownedRoot) {
            Assert-OwnedRoot
            $entries=@(Get-ChildItem -LiteralPath $ownedRoot -Force -Recurse)
            if (@($entries | Where-Object { ($_.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 }).Count -ne 0) { throw 'Owned image tree contains a reparse point' }
            Remove-Item -LiteralPath $ownedRoot -Recurse -Force
            if (Test-Path -LiteralPath $ownedRoot) { throw 'Owned image tree remains' }
        }
    } catch { $receipt.cleanup_complete=$false; $receipt.cleanup_hresult=$_.Exception.HResult }
}
$receipt | ConvertTo-Json -Depth 5 -Compress
if (-not $receipt.cleanup_complete) { exit 2 }
exit 0
