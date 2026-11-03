[CmdletBinding()]
param(
    [string]$InstallRoot = '',
    [string]$PythonExecutable = ''
)

# Deliberately preserves runtime, account identity, camera sources and recordings.
# Unknown/linked content or a running launcher causes refusal, never broad deletion.
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
if ([string]::IsNullOrWhiteSpace($InstallRoot)) {
    if (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'active.json')) { $InstallRoot = $PSScriptRoot }
    else { $InstallRoot = Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'K5VisionOperator' }
}
try {
    if ($env:OS -cne 'Windows_NT') { throw 'Native Windows required.' }
    if (-not (Test-Path -LiteralPath $InstallRoot)) {
        Write-Host 'K5 operator application is already uninstalled. Identity and recordings were preserved.'
        exit 0
    }
    # An installed copy embeds the previously admitted interpreter. The source
    # copy accepts its explicit path; install metadata never selects executable
    # code before its ancestry and private Windows boundary are inspected.
    if ([string]::IsNullOrWhiteSpace($PythonExecutable)) { throw 'Installed interpreter required.' }
    $python = $PythonExecutable
    if (-not [IO.Path]::IsPathRooted($python) -or -not (Test-Path -LiteralPath $python -PathType Leaf)) { throw 'Installed interpreter required.' }
    $paths = @(
        (Join-Path ([Environment]::GetFolderPath('Programs', 'DoNotVerify')) 'K5 Vision Operator.lnk'),
        (Join-Path ([Environment]::GetFolderPath('DesktopDirectory', 'DoNotVerify')) 'K5 Vision Operator.lnk'),
        (Join-Path ([Environment]::GetFolderPath('Startup', 'DoNotVerify')) 'K5 Vision Operator.lnk')
    )
    $powershell = Join-Path $env:SYSTEMROOT 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $arguments = '-NoProfile -ExecutionPolicy Bypass -File "' + (Join-Path $InstallRoot 'Start-K5Operator.ps1') + '"'
    $shell = New-Object -ComObject WScript.Shell
    $ownedShortcuts = @()
    foreach ($path in $paths) {
        if (Test-Path -LiteralPath $path) {
            $shortcut = $shell.CreateShortcut($path)
            $expected = $arguments
            if ($path -ceq $paths[2]) { $expected += ' -NoBrowser' }
            # Only this installation's exact entries are candidates for deletion.
            if ($shortcut.TargetPath -ieq $powershell -and $shortcut.Arguments -ceq $expected) { $ownedShortcuts += $path }
        }
    }
    $code = @'
import ctypes,hashlib,importlib.util,json,msvcrt,os,re,stat,sys
from pathlib import Path
root=Path(sys.argv[1]);shortcuts=list(map(Path,sys.argv[2:]));runtime_handle=None;lock=None
def ordinary(path,directory=False):
 if not path.is_absolute() or '..' in path.parts: raise ValueError('Absolute ordinary path required')
 for p in reversed((path,*path.parents)):
  info=p.lstat()
  if stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0)&0x400: raise ValueError('Linked path refused')
  if p==path and not directory:
   if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1: raise ValueError('Ordinary file required')
  elif not stat.S_ISDIR(info.st_mode): raise ValueError('Directory required')
helper=root/'windows_identity_security.py';ordinary(helper)
helper_bytes=helper.read_bytes()
helper_pins={'a5ca5fbdef60960de07384138d3290d6c6a1c7e8f688362f6cfd8c7976537ae9',
             '33493f384a73eb078a6d0481429e5254d20302408876248da0e43326234123f7'}
if hashlib.sha256(helper_bytes).hexdigest() not in helper_pins:
 raise ValueError('Bootstrap identity refused')
spec=importlib.util.spec_from_file_location('k5_uninstall_security',helper);security=importlib.util.module_from_spec(spec);sys.modules[spec.name]=security
exec(compile(helper_bytes,str(helper),'exec'),security.__dict__)
def admit(path,directory=False):
 ordinary(path,directory);security.check_windows_volume(path)
 for p in reversed((path,*path.parents)):
  leaf=p==path;private=leaf or p==path.parent
  security.check_windows_security(p,private=private,require_file_inheritance=private and (not leaf or directory))
def load(path):
 admit(path)
 if path.stat().st_size>65536: raise ValueError('Metadata budget exceeded')
 return json.loads(path.read_text(encoding='utf-8'))
def admit_shortcut(path):
 ordinary(path);security.check_windows_volume(path)
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
admit(helper);admit(root,True)
active=load(root/'active.json')
if active.get('schema_version')!='1' or active.get('python')!=sys.executable or not re.fullmatch('[0-9a-f]{64}',active.get('release','')):
 raise ValueError('Install metadata refused')
manifest=load(root/'releases'/active['release']/'release.json')
if hashlib.sha256(json.dumps(manifest,sort_keys=True,separators=(',',':')).encode()).hexdigest()!=active['release']:
 raise ValueError('Release identity refused')
runtime,state=Path(active['runtime']),Path(active['state'])
for other in (runtime,state):
 if not other.is_absolute() or '..' in other.parts or root.is_relative_to(other) or other.is_relative_to(root):
  raise ValueError('Separate profile required')
 if other.exists(): admit(other,True)
allowed={'releases','active.json','Start-K5Operator.ps1','Uninstall-K5Operator.ps1','windows_identity_security.py','.install.lock'}
if {p.name for p in root.iterdir()}!=allowed: raise ValueError('Unknown install content refused')
lock_path=root/'.install.lock';admit(lock_path);lock=lock_path.open('r+b');msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
files=[];directories=[]
try:
 if runtime.exists():
  runtime_lock=runtime/'operator-launcher.lock'
  if runtime_lock.exists(): admit(runtime_lock)
  kernel=ctypes.WinDLL('kernel32',use_last_error=True)
  kernel.CreateFileW.argtypes=(ctypes.c_wchar_p,ctypes.c_uint32,ctypes.c_uint32,ctypes.c_void_p,ctypes.c_uint32,ctypes.c_uint32,ctypes.c_void_p)
  kernel.CreateFileW.restype=ctypes.c_void_p;kernel.CloseHandle.argtypes=(ctypes.c_void_p,)
  runtime_handle=kernel.CreateFileW(str(runtime_lock),0xC0000000,0,None,4,0x80,None)
  if runtime_handle==ctypes.c_void_p(-1).value: runtime_handle=None;raise ValueError('Stop running operator before uninstall')
 releases=root/'releases';admit(releases,True)
 for release in releases.iterdir():
  admit(release,True)
  if not re.fullmatch('[0-9a-f]{64}',release.name): raise ValueError('Unknown release refused')
  document=load(release/'release.json');admit(release/'release.json')
  if hashlib.sha256(json.dumps(document,sort_keys=True,separators=(',',':')).encode()).hexdigest()!=release.name:
   raise ValueError('Release manifest refused')
  expected=set(document['files'])|{'release.json'}
  actual=set()
  for path in release.rglob('*'):
   info=path.lstat();directory=stat.S_ISDIR(info.st_mode);admit(path,directory)
   if directory: directories.append(path)
   else: actual.add(path.relative_to(release).as_posix());files.append(path)
  if actual!=expected: raise ValueError('Unknown release content refused')
  directories.append(release)
 for path in root.iterdir():
  if path.name!='releases': admit(path)
 for path in shortcuts: admit_shortcut(path)
 # Admission of all deletions finishes before deleting any file. Profiles remain.
 for path in shortcuts: path.unlink()
 for path in files: path.unlink()
 for path in sorted(directories,key=lambda p:len(p.parts),reverse=True): path.rmdir()
 releases.rmdir()
 for name in allowed-{'releases','.install.lock'}: (root/name).unlink()
finally:
 if runtime_handle is not None: kernel.CloseHandle(runtime_handle)
 if lock is not None: lock.close()
lock_path.unlink();root.rmdir()
'@
    $encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($code))
    $previous = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $null = @(& $python -I -B -c "exec(__import__('base64').b64decode('$encoded'))" $InstallRoot @ownedShortcuts 2>&1)
        $result = $LASTEXITCODE
    } finally { $ErrorActionPreference = $previous }
    if ($result -ne 0) { throw 'Uninstall admission failed.' }
    Write-Host 'K5 operator application uninstalled. Runtime, identity, camera sources and recordings were preserved.'
    exit 0
} catch {
    Write-Host 'K5 operator uninstall refused. Stop the running operator and use an intact installation. Identity and recordings were preserved.'
    exit 1
}
