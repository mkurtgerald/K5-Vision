[CmdletBinding()]
param(
    [string]$PackageRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$InstallRoot = (Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'K5VisionOperator'),
    [string]$PythonExecutable = '',
    [string]$RuntimeRoot = (Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'K5VisionOperatorRuntime'),
    [string]$StateRoot = (Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'K5VisionOperatorState'),
    [ValidateRange(1024, 65535)][int]$Port = 8000,
    [ValidateRange(1, 4096)][int]$RecordingLimitGb = 20,
    [switch]$DesktopShortcut,
    [switch]$StartAtLogon
)

# Per-user deployment of the reviewed native pilot; no elevation, bundled
# runtime, service, scheduled task, credential transport or ACL repair.
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$stage = 'platform'
$script:pythonDiagnostic = 'unavailable'

function Invoke-K5InstallPython {
    param([string]$Code, [string[]]$Arguments)
    $encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($Code))
    $previous = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $output = @(& $PythonExecutable -I -B -c "exec(__import__('base64').b64decode('$encoded'))" @Arguments 2>&1)
        $code = $LASTEXITCODE
    } finally { $ErrorActionPreference = $previous }
    if ($code -ne 0) {
        foreach ($item in $output) {
            try {
                $detail = ([string]$item) | ConvertFrom-Json
                $phases = @('bootstrap', 'package-admission', 'root-separation',
                    'profile-admission', 'shortcut-admission', 'shortcut-slot-0', 'shortcut-slot-1', 'shortcut-slot-2', 'identity-admission',
                    'package-content', 'wheel-admission', 'install-admission',
                    'release-lock', 'runtime-lock', 'release-stage', 'release-commit')
                if ($detail.schema_version -ceq '1' -and $detail.phase -cin $phases -and
                    $detail.reason -cin @('private-boundary', 'foreign-access', 'file-inheritance', 'ownership', 'volume-alias', 'volume-type', 'acl-query', 'python-validation', 'file-operation', 'other') -and
                    $detail.line -is [int] -and $detail.line -ge 1 -and $detail.line -le 1000) {
                    $script:pythonDiagnostic = $detail.phase + ' public-line ' + [string]$detail.line + ' reason ' + $detail.reason
                }
            } catch { }
        }
        throw 'Installer admission failed.'
    }
    return ($output -join "`n").Trim()
}

function Set-K5Shortcut {
    param([object]$Shell, [string]$Path, [string]$Target, [string]$Arguments)
    if (Test-Path -LiteralPath $Path) {
        $existing = $Shell.CreateShortcut($Path)
        if ($existing.TargetPath -ine $Target -or $existing.Arguments -cne $Arguments) {
            throw 'Occupied shortcut refused.'
        }
    }
    $shortcut = $Shell.CreateShortcut($Path)
    $shortcut.TargetPath = $Target
    $shortcut.Arguments = $Arguments
    $shortcut.WorkingDirectory = $InstallRoot
    $shortcut.Description = 'K5 Vision native camera and recording operator'
    $shortcut.Save()
}

try {
    if ($env:OS -cne 'Windows_NT' -or -not [Environment]::Is64BitOperatingSystem) {
        throw 'Native 64-bit Windows required.'
    }
    $stage = 'python'
    if ([string]::IsNullOrWhiteSpace($PythonExecutable)) {
        $py = Get-Command py.exe -ErrorAction SilentlyContinue
        if ($null -ne $py) {
            $PythonExecutable = $py.Source
            # The py selector is separate from the later fixed helper invocation.
            $value = @(& $PythonExecutable -3.12 -I -B -c 'import json,sys; print(json.dumps(sys.executable,ensure_ascii=True))' 2>$null)
            if ($LASTEXITCODE -ne 0) { throw 'Python 3.12 required.' }
            $PythonExecutable = ($value -join "`n") | ConvertFrom-Json
        } else { $PythonExecutable = (Get-Command python.exe -ErrorAction Stop).Source }
    }
    $probe = 'import json,struct,sys; assert sys.platform=="win32" and sys.version_info[:2]==(3,12) and sys.version_info.micro>=4 and struct.calcsize("P")==8; print(json.dumps(sys.executable,ensure_ascii=True))'
    $PythonExecutable = (Invoke-K5InstallPython -Code $probe -Arguments @()) | ConvertFrom-Json
    $stage = 'package-and-private-paths'
    $programs = Join-Path ([Environment]::GetFolderPath('Programs', 'DoNotVerify')) 'K5 Vision Operator.lnk'
    $desktop = Join-Path ([Environment]::GetFolderPath('DesktopDirectory', 'DoNotVerify')) 'K5 Vision Operator.lnk'
    $startup = Join-Path ([Environment]::GetFolderPath('Startup', 'DoNotVerify')) 'K5 Vision Operator.lnk'
    $powershell = Join-Path $env:SYSTEMROOT 'System32\WindowsPowerShell\v1.0\powershell.exe'
    # The helper is the unchanged pilot stdlib-only admission implementation.
    # New releases are copied into fresh ordinary private files and hashed before
    # the active pointer changes. User identity/recordings are never rewritten.
    $install = @'
import ctypes,hashlib,importlib.util,json,msvcrt,os,re,shutil,stat,sys,uuid,zipfile
from pathlib import Path
phase='bootstrap'
def diagnostic(kind,error,trace):
    line=0
    while trace is not None:
        if trace.tb_frame.f_code.co_filename=='<string>': line=trace.tb_lineno
        trace=trace.tb_next
    reason='other'
    if kind.__module__=='k5_install_security' and kind.__name__=='WindowsIdentitySecurityError':
        reason={
            'Windows identity path grants unsafe access':'foreign-access',
            'Windows identity directory lacks private file inheritance':'file-inheritance',
            'Windows identity path has unsupported ownership':'ownership',
            'Windows identity volume aliases are unsupported':'volume-alias',
            'Windows identity requires a fixed local volume':'volume-type',
            'Windows identity ACL cannot be established':'acl-query',
        }.get(str(error),'private-boundary')
    elif kind is ValueError: reason='python-validation'
    elif issubclass(kind,OSError): reason='file-operation'
    if 1<=line<=1000:
        print(json.dumps({'schema_version':'1','phase':phase,'line':line,'reason':reason}))
sys.excepthook=diagnostic
package,root,runtime,state=map(Path,sys.argv[1:5]);port=int(sys.argv[5]);logon=sys.argv[6]=='1';recording_limit_gb=int(sys.argv[7])
shortcuts=list(map(Path,sys.argv[8:]));lock=None;runtime_handle=None;candidate=None;committed=False;originals={}
def ordinary(path,kind):
    if not path.is_absolute() or '..' in path.parts or any(c in str(path) for c in '\r\n"'):
        raise ValueError('Ordinary absolute path required')
    for p in reversed((path,*path.parents)):
        info=p.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0)&0x400:
            raise ValueError('Linked path refused')
        if p==path and kind=='file':
            if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1: raise ValueError('Ordinary file required')
        elif not stat.S_ISDIR(info.st_mode): raise ValueError('Directory required')
def digest(path):
    with path.open('rb') as stream: return hashlib.file_digest(stream,'sha256').hexdigest()
helper=package/'src/k5vision/windows_identity_security.py';ordinary(helper,'file')
# Only these exact reviewed checkout byte representations may execute bootstrap
# code. Mutable wheel/release metadata never certifies the admission helper.
helper_bytes=helper.read_bytes()
helper_pins={'a5ca5fbdef60960de07384138d3290d6c6a1c7e8f688362f6cfd8c7976537ae9',
             '33493f384a73eb078a6d0481429e5254d20302408876248da0e43326234123f7'}
if hashlib.sha256(helper_bytes).hexdigest() not in helper_pins: raise ValueError('Unreviewed bootstrap refused')
spec=importlib.util.spec_from_file_location('k5_install_security',helper)
security=importlib.util.module_from_spec(spec);sys.modules[spec.name]=security
exec(compile(helper_bytes,str(helper),'exec'),security.__dict__)
def admit(path,kind,private=True):
    ordinary(path,kind);security.check_windows_volume(path)
    for p in reversed((path,*path.parents)):
        leaf=p==path;parent=p==path.parent
        secure=private and (leaf or parent)
        security.check_windows_security(p,private=secure,require_file_inheritance=secure and (not leaf or kind=='dir'))
def directory(path):
    if not path.exists():
        directory(path.parent);path.mkdir(mode=0o700)
    admit(path,'dir')
def admit_shortcut(path,kind):
    # Shortcuts contain only public dispatch arguments. Foreign reads are safe;
    # foreign writes to their bytes or future inherited files remain refused.
    ordinary(path,kind);security.check_windows_volume(path)
    native=security._NativeSecurity();current=native.current_user()
    trusted={current,security._ADMINISTRATORS,security._SYSTEM}
    if native.trusted_installer(): trusted.add(security._TRUSTED_INSTALLER)
    for component in reversed((path,*path.parents)):
        security.check_windows_security(component,private=False)
        if component not in (path,path.parent): continue
        owner,entries=native.descriptor(str(component))
        for entry in entries:
            if entry.kind!=0: continue
            sid=entry.sid
            if sid==security._OWNER_RIGHTS: sid=owner
            elif sid==security._CREATOR_OWNER and entry.flags&0x08: sid=current
            if sid not in trusted and security._expanded_mask(entry.mask)&(security._ANCESTOR_MUTATION|0x06):
                raise ValueError('Foreign shortcut write refused')
def shortcut_directory(path):
    if not path.exists(): shortcut_directory(path.parent);path.mkdir(mode=0o700)
    admit_shortcut(path,'dir')
def read_json(path):
    admit(path,'file')
    if path.stat().st_size>65536: raise ValueError('Metadata budget exceeded')
    return json.loads(path.read_text(encoding='utf-8'))
def write_new(path,data):
    with path.open('xb') as stream: stream.write(data);stream.flush();os.fsync(stream.fileno())
    admit(path,'file')
def replace_file(path,data):
    if path.exists(): admit(path,'file')
    temporary=path.parent/('pending-'+uuid.uuid4().hex)
    try: write_new(temporary,data);os.replace(temporary,path)
    finally:
        if temporary.exists(): temporary.unlink()
phase='package-admission'
admit(helper,'file')
for path in (package,): admit(path,'dir')
phase='root-separation'
for path in (root,runtime,state):
    if not path.is_absolute() or '..' in path.parts: raise ValueError('Absolute roots required')
for a,b in ((root,package),(root,runtime),(root,state),(state,runtime),(state,package),(runtime,package)):
    if a.is_relative_to(b) or b.is_relative_to(a): raise ValueError('Separate roots required')
phase='profile-admission'
for path in (runtime,state):
    if path.exists(): admit(path,'dir')
    else: admit(path.parent,'dir')
phase='shortcut-admission'
for index,path in enumerate(shortcuts):
    phase='shortcut-slot-'+str(index)
    shortcut_directory(path.parent)
    if path.exists(): admit_shortcut(path,'file')
if logon:
    phase='identity-admission'
    # Initial account creation belongs to an interactive first launch. Startup
    # never carries a password or invents/reset an interrupted identity.
    admit(state/'identity','dir')
phase='package-content'
files=['scripts/Start-K5Operator.ps1','scripts/Install-K5Operator.ps1','scripts/Uninstall-K5Operator.ps1',
       'src/k5vision/windows_identity_security.py','README.md','docs/WINDOWS_OPERATOR_INSTALL.md',
       'LICENSE','THIRD_PARTY_NOTICES.md','third_party/licenses/MediaMTX-MIT.txt','third_party/licenses/Caddy-Apache-2.0.txt']
admit(package/'dist','dir')
wheels=list((package/'dist').glob('k5_vision-*-py3-none-any.whl'))
if len(wheels)!=1 or not re.fullmatch(r'k5_vision-[0-9A-Za-z_.+]+-py3-none-any\.whl',wheels[0].name):
    raise ValueError('One local operator wheel required')
wheel=wheels[0];files.append('dist/'+wheel.name)
for name in files: admit(package/name,'file')
phase='wheel-admission'
with zipfile.ZipFile(wheel) as archive:
    if len(archive.infolist())>4096 or len(set(archive.namelist()))!=len(archive.namelist()):
        raise ValueError('Wheel contents refused')
    required=('cli.py','operator_product.py','operator_product_service.py','data/mediamtx-donor-manifest.json','data/caddy-donor-manifest.json')
    if any('k5vision/'+name not in archive.namelist() for name in required): raise ValueError('Operator wheel required')
    if archive.read('k5vision/windows_identity_security.py').replace(b'\r\n',b'\n')!=helper.read_bytes().replace(b'\r\n',b'\n'):
        raise ValueError('Wheel bootstrap helper mismatch')
manifest={'schema_version':'1','files':{name:digest(package/name) for name in sorted(files)}}
manifest_bytes=json.dumps(manifest,sort_keys=True,separators=(',',':')).encode()
release=hashlib.sha256(manifest_bytes).hexdigest()
phase='install-admission'
directory(root)
allowed={'releases','active.json','Start-K5Operator.ps1','Uninstall-K5Operator.ps1','windows_identity_security.py','.install.lock'}
if any(path.name not in allowed for path in root.iterdir()): raise ValueError('Unowned installation content refused')
phase='release-lock'
lock_path=root/'.install.lock'
if lock_path.exists(): admit(lock_path,'file')
lock=lock_path.open('a+b')
if lock.seek(0,2)==0: lock.write(b'1');lock.flush()
lock.seek(0);msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
try:
    phase='runtime-lock'
    if runtime.exists():
        runtime_lock=runtime/'operator-launcher.lock'
        if runtime_lock.exists(): admit(runtime_lock,'file')
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.CreateFileW.argtypes=(ctypes.c_wchar_p,ctypes.c_uint32,ctypes.c_uint32,ctypes.c_void_p,ctypes.c_uint32,ctypes.c_uint32,ctypes.c_void_p)
        kernel.CreateFileW.restype=ctypes.c_void_p;kernel.CloseHandle.argtypes=(ctypes.c_void_p,)
        runtime_handle=kernel.CreateFileW(str(runtime_lock),0xC0000000,0,None,4,0x80,None)
        if runtime_handle==ctypes.c_void_p(-1).value: runtime_handle=None;raise ValueError('Stop running operator before install')
    phase='release-stage'
    directory(root/'releases')
    if (root/'active.json').exists():
        prior=read_json(root/'active.json')
        if prior.get('schema_version')!='1' or not re.fullmatch('[0-9a-f]{64}',prior.get('release','')):
            raise ValueError('Existing installation metadata refused')
        if prior.get('runtime')!=str(runtime) or prior.get('state')!=str(state):
            raise ValueError('Existing installation profile must be preserved')
    target=root/'releases'/release
    if target.exists():
        admit(target,'dir')
        if read_json(target/'release.json')!=manifest: raise ValueError('Release metadata mismatch')
        existing={p.relative_to(target).as_posix() for p in target.rglob('*') if p.is_file()}
        if existing!=set(files)|{'release.json'}: raise ValueError('Release content mismatch')
        for name,value in manifest['files'].items():
            admit(target/name,'file')
            if digest(target/name)!=value: raise ValueError('Installed release changed')
    else:
        candidate=root/'releases'/('pending-'+uuid.uuid4().hex);directory(candidate)
        for name in files:
            destination=candidate/name
            for parent in reversed(destination.parent.relative_to(candidate).parents):
                if str(parent)!='.': directory(candidate/parent)
            directory(destination.parent)
            with (package/name).open('rb') as incoming,destination.open('xb') as outgoing:
                shutil.copyfileobj(incoming,outgoing,length=1024**2);outgoing.flush();os.fsync(outgoing.fileno())
            admit(destination,'file')
            if digest(destination)!=manifest['files'][name]: raise ValueError('Copied file changed')
        write_new(candidate/'release.json',manifest_bytes)
        os.rename(candidate,target);candidate=None
    # Installed wrapper checks its private metadata and every immutable release
    # file on each start; it dispatches fixed script/argv with no Invoke-Expression.
    verifier=r'''import hashlib,importlib.util,json,re,stat,sys
from pathlib import Path
root=Path(sys.argv[1]);helper=root/'windows_identity_security.py';phase='bootstrap';private_ready=False
diagnostic_name='' if sys.argv[3]=='none' else sys.argv[3]
if diagnostic_name and not re.fullmatch(r'installed-launcher-diagnostic-[0-9a-f]{32}\.json',diagnostic_name): raise ValueError('Diagnostic basename refused')
def diagnostic(kind,error,trace):
 line=0
 while trace is not None:
  if trace.tb_frame.f_code.co_filename=='<string>': line=trace.tb_lineno
  trace=trace.tb_next
 reason='other'
 if kind.__module__=='k5_installed_security' and kind.__name__=='WindowsIdentitySecurityError': reason='private-boundary'
 elif kind is ValueError: reason='python-validation'
 elif issubclass(kind,OSError): reason='file-operation'
 if private_ready and diagnostic_name and 1<=line<=1000:
  try:
   with (root/diagnostic_name).open('xb') as stream:
    stream.write(json.dumps({'schema_version':'1','stage':phase,'line':line,'reason':reason}).encode())
  except Exception: pass
sys.excepthook=diagnostic
for p in reversed((helper,*helper.parents)):
 i=p.lstat()
 if stat.S_ISLNK(i.st_mode) or getattr(i,'st_file_attributes',0)&0x400: raise ValueError('Linked install refused')
helper_bytes=helper.read_bytes()
if not stat.S_ISREG(helper.stat().st_mode) or helper.stat().st_nlink!=1 or hashlib.sha256(helper_bytes).hexdigest()!=sys.argv[2]: raise ValueError('Bootstrap identity refused')
spec=importlib.util.spec_from_file_location('k5_installed_security',helper);s=importlib.util.module_from_spec(spec);sys.modules[spec.name]=s;exec(compile(helper_bytes,str(helper),'exec'),s.__dict__)
def admit(path,directory=False):
 s.check_windows_volume(path)
 for p in reversed((path,*path.parents)):
  i=p.lstat();leaf=p==path;private=leaf or p==path.parent
  if stat.S_ISLNK(i.st_mode) or getattr(i,'st_file_attributes',0)&0x400: raise ValueError('Linked install refused')
  if leaf and not directory:
   if not stat.S_ISREG(i.st_mode) or i.st_nlink!=1: raise ValueError('Ordinary install file required')
  elif not stat.S_ISDIR(i.st_mode): raise ValueError('Install directory required')
  s.check_windows_security(p,private=private,require_file_inheritance=private and (not leaf or directory))
def document(path):
 admit(path)
 if path.stat().st_size>65536: raise ValueError('Metadata budget exceeded')
 return json.loads(path.read_text(encoding='utf-8'))
phase='private-bootstrap'
admit(helper);private_ready=True
phase='active-settings'
active=document(root/'active.json')
if set(active)!={'schema_version','release','python','runtime','state','port','recording_limit_gb'} or active['schema_version']!='1' or not re.fullmatch('[0-9a-f]{64}',active['release']) or active['python']!=sys.executable or type(active['port']) is not int or not 1024<=active['port']<=65535 or type(active['recording_limit_gb']) is not int or not 1<=active['recording_limit_gb']<=4096: raise ValueError('Installed settings refused')
phase='release-manifest'
release=root/'releases'/active['release'];manifest=document(release/'release.json')
if hashlib.sha256(json.dumps(manifest,sort_keys=True,separators=(',',':')).encode()).hexdigest()!=active['release'] or set(manifest)!={'schema_version','files'} or manifest['schema_version']!='1': raise ValueError('Release identity refused')
phase='release-files'
for name,value in manifest['files'].items():
 path=Path(name)
 if path.is_absolute() or '..' in path.parts or not re.fullmatch('[0-9a-f]{64}',value): raise ValueError('Manifest path refused')
 admitted=release/path;admit(admitted)
 with admitted.open('rb') as stream:
  if hashlib.file_digest(stream,'sha256').hexdigest()!=value: raise ValueError('Release file identity refused')
phase='wheel-admission'
wheels=[name for name in manifest['files'] if re.fullmatch(r'dist/k5_vision-[0-9A-Za-z_.+]+-py3-none-any\.whl',name)]
if len(wheels)!=1: raise ValueError('Installed wheel identity refused')
active['starter']=str(release/'scripts/Start-K5Operator.ps1');active['wheel']=str(release/wheels[0]);print(json.dumps(active,ensure_ascii=True))'''
    import base64
    encoded=base64.b64encode(verifier.encode()).decode();python_literal=sys.executable.replace("'","''")
    helper_hash=manifest['files']['src/k5vision/windows_identity_security.py']
    wrapper=r"""[CmdletBinding()]
param([switch]$NoBrowser, [string]$DiagnosticName = '')
$ErrorActionPreference = 'Stop'
$stage = 'verification'
$privateReady = $false
try {
    $encoded = '%s'
    $python = '%s'
    $diagnosticArgument = 'none'
    if ($DiagnosticName) { $diagnosticArgument = $DiagnosticName }
    $previous = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $output = @(& $python -I -B -c "exec(__import__('base64').b64decode('$encoded'))" $PSScriptRoot '%s' $diagnosticArgument 2>&1)
        $result = $LASTEXITCODE
    } finally { $ErrorActionPreference = $previous }
    if ($result -ne 0) { throw 'Installed package admission failed.' }
    $privateReady = $true
    $stage = 'settings-parsing'
    $settings = ($output -join "`n") | ConvertFrom-Json
    # This is another PowerShell script, so bind named parameters with a
    # hashtable. Native-executable argv arrays would bind these as positionals.
    $arguments = @{PackagePath=[string]$settings.wheel; PythonExecutable=$python; RuntimeRoot=[string]$settings.runtime; StateRoot=[string]$settings.state; Port=[int]$settings.port; RecordingLimitGb=[int]$settings.recording_limit_gb}
    if ($NoBrowser) { $arguments['NoBrowser'] = $true }
    if ($DiagnosticName) {
        if ($DiagnosticName -cnotmatch '^installed-launcher-diagnostic-[0-9a-f]{32}\.json$') { throw 'Diagnostic basename refused.' }
        $arguments['DiagnosticName'] = 'launcher-diagnostic-' + $DiagnosticName.Substring(30)
    }
    $stage = 'starter-dispatch'
    & $settings.starter @arguments
    exit $LASTEXITCODE
} catch {
    if ($privateReady -and $DiagnosticName -cmatch '^installed-launcher-diagnostic-[0-9a-f]{32}\.json$') {
        $stream = $null
        try {
            $line = $_.InvocationInfo.ScriptLineNumber
            if ($line -lt 1 -or $line -gt 1000) { $line = 0 }
            $document = @{schema_version='1'; stage=$stage; line=$line; reason='powershell-operation'}
            $bytes = [Text.Encoding]::UTF8.GetBytes(($document | ConvertTo-Json -Compress))
            $stream = [IO.File]::Open((Join-Path $PSScriptRoot $DiagnosticName),[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
            $stream.Write($bytes,0,$bytes.Length)
        } catch { } finally { if ($null -ne $stream) { $stream.Dispose() } }
    }
    Write-Host 'K5 operator could not start. Reinstall the reviewed package; existing identity and recordings were preserved.'
    exit 1
}
"""%(encoded,python_literal,helper_hash)
    phase='release-commit'
    for name in ('windows_identity_security.py','Start-K5Operator.ps1','Uninstall-K5Operator.ps1','active.json'):
        path=root/name
        if path.exists(): admit(path,'file');originals[name]=path.read_bytes()
        else: originals[name]=None
    replace_file(root/'windows_identity_security.py',helper.read_bytes())
    # Windows PowerShell 5.1 needs a BOM for literal Unicode interpreter paths.
    replace_file(root/'Start-K5Operator.ps1',wrapper.encode('utf-8-sig'))
    uninstaller=(package/'scripts/Uninstall-K5Operator.ps1').read_text(encoding='utf-8')
    uninstaller=uninstaller.replace("[string]$PythonExecutable = ''", "[string]$PythonExecutable = '"+python_literal+"'",1)
    replace_file(root/'Uninstall-K5Operator.ps1',uninstaller.encode('utf-8-sig'))
    active={'schema_version':'1','release':release,'python':sys.executable,'runtime':str(runtime),'state':str(state),'port':port,'recording_limit_gb':recording_limit_gb}
    replace_file(root/'active.json',json.dumps(active,sort_keys=True,separators=(',',':')).encode())
    committed=True
    print(json.dumps({'install_root':str(root),'release':release},ensure_ascii=True))
finally:
    if not committed:
        for name,previous in originals.items():
            path=root/name
            if previous is not None: replace_file(path,previous)
            elif path.exists(): admit(path,'file');path.unlink()
    if runtime_handle is not None: kernel.CloseHandle(runtime_handle)
    if candidate is not None and candidate.exists(): shutil.rmtree(candidate)
    if lock is not None: lock.close()
'@
    $shortcutPaths = @($programs)
    if ($DesktopShortcut) { $shortcutPaths += $desktop }
    if ($StartAtLogon) { $shortcutPaths += $startup }
    # Refuse foreign shortcuts before modifying an existing active release.
    $shell = New-Object -ComObject WScript.Shell
    $arguments = '-NoProfile -ExecutionPolicy Bypass -File "' + (Join-Path $InstallRoot 'Start-K5Operator.ps1') + '"'
    foreach ($path in $shortcutPaths) {
        if (Test-Path -LiteralPath $path) {
            $shortcut = $shell.CreateShortcut($path)
            $expected = $arguments
            if ($path -ceq $startup) { $expected += ' -NoBrowser' }
            if ($shortcut.TargetPath -ine $powershell -or $shortcut.Arguments -cne $expected) { throw 'Occupied shortcut refused.' }
        }
    }
    $settings = (Invoke-K5InstallPython -Code $install -Arguments (@($PackageRoot, $InstallRoot, $RuntimeRoot, $StateRoot, [string]$Port, $(if ($StartAtLogon) { '1' } else { '0' }), [string]$RecordingLimitGb) + $shortcutPaths)) | ConvertFrom-Json
    $InstallRoot = $settings.install_root
    $stage = 'shortcuts'
    Set-K5Shortcut -Shell $shell -Path $programs -Target $powershell -Arguments $arguments
    if ($DesktopShortcut) { Set-K5Shortcut -Shell $shell -Path $desktop -Target $powershell -Arguments $arguments }
    if ($StartAtLogon) { Set-K5Shortcut -Shell $shell -Path $startup -Target $powershell -Arguments ($arguments + ' -NoBrowser') }
    Write-Host 'K5 Vision Operator installed for this Windows account. Open K5 Vision Operator from Start.'
    if ($StartAtLogon) { Write-Host 'Logon startup enabled. Keep the console open and remain signed in to record.' }
    exit 0
} catch {
    Write-Host ('K5 operator installation failed at ' + $stage + '. Stop any running operator before retrying. Existing identity and recordings were preserved.')
    Write-Host ('K5 install diagnostic: ' + $script:pythonDiagnostic)
    exit 1
}
