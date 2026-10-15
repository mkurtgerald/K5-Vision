#ifndef SourceRoot
  #error SourceRoot is required.
#endif
#ifndef K5Revision
  #error Exact K5Revision is required.
#endif
#ifndef OfflineWheelhouseDigest
  #error Exact offline wheelhouse digest is required.
#endif

[Setup]
AppId={{72241A0E-7DB0-4A48-BEDA-C594A66AC358}
AppName=K5 Vision Alpha
AppVersion=0.1.0
AppPublisher=K5 Vision
DefaultDirName={localappdata}\K5VisionAlpha
DefaultGroupName=K5 Vision Alpha
DisableDirPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
CloseApplications=no
RestartApplications=no
UninstallDisplayIcon={app}\K5VisionAlpha.exe
OutputDir={#SourceRoot}\dist\owner-installer
OutputBaseFilename=K5VisionAlpha-Setup-{#K5Revision}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern

[Files]
Source: "{#SourceRoot}\build\owner-installer\K5VisionAlpha.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourceRoot}\build\owner-installer\python-3.12.10-amd64.exe"; DestDir: "{app}\payload"; Flags: ignoreversion
Source: "{#SourceRoot}\build\owner-installer\mediamtx\mediamtx_v1.21.1_windows_amd64.zip"; DestDir: "{app}\payload\mediamtx"; Flags: ignoreversion
Source: "{#SourceRoot}\docs\licenses\MediaMTX-MIT.txt"; DestDir: "{app}\payload\licenses"; Flags: ignoreversion
Source: "{#SourceRoot}\build\owner-installer\gstreamer\gstreamer-1.0-msvc-x86_64-1.28.7.exe"; DestDir: "{app}\payload\gstreamer"; Flags: ignoreversion
Source: "{#SourceRoot}\scripts\windows-alpha\Install-K5VisionAlpha.ps1"; DestDir: "{app}\payload\windows-alpha"; Flags: ignoreversion
Source: "{#SourceRoot}\scripts\windows-alpha\Start-K5VisionAlpha.ps1"; DestDir: "{app}\payload\windows-alpha"; Flags: ignoreversion
Source: "{#SourceRoot}\scripts\windows-alpha\Test-K5VisionAlpha.ps1"; DestDir: "{app}\payload\windows-alpha"; Flags: ignoreversion
Source: "{#SourceRoot}\scripts\windows-alpha\Run-K5VisionAlpha.ps1"; DestDir: "{app}\payload\windows-alpha"; Flags: ignoreversion
Source: "{#SourceRoot}\scripts\windows-alpha\install_transaction.py"; DestDir: "{app}\payload\windows-alpha"; Flags: ignoreversion
Source: "{#SourceRoot}\scripts\windows-alpha\owner_analytics_bundle.py"; DestDir: "{app}\payload\windows-alpha"; Flags: ignoreversion
Source: "{#SourceRoot}\build\owner-installer\analytics\manifest.json"; DestDir: "{app}\payload\analytics"; Flags: ignoreversion
Source: "{#SourceRoot}\build\owner-installer\analytics\wheels\*.whl"; DestDir: "{app}\payload\analytics\wheels"; Flags: ignoreversion
Source: "{#SourceRoot}\build\owner-installer\analytics\models\*"; DestDir: "{app}\payload\analytics\models"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#SourceRoot}\build\owner-installer\analytics\Analytics-Lab-LICENSE"; DestDir: "{app}\payload\analytics"; Flags: ignoreversion
Source: "{#SourceRoot}\build\owner-installer\analytics\Analytics-Lab-THIRD_PARTY.md"; DestDir: "{app}\payload\analytics"; Flags: ignoreversion
Source: "{#SourceRoot}\scripts\windows-alpha\runtime-requirements.txt"; DestDir: "{app}\payload\windows-alpha"; Flags: ignoreversion
Source: "{#SourceRoot}\scripts\provision-stage03-gstreamer.ps1"; DestDir: "{app}\payload"; Flags: ignoreversion
Source: "{#SourceRoot}\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourceRoot}\THIRD_PARTY_NOTICES.md"; DestDir: "{app}"; Flags: ignoreversion
; This closed candidate is staged outside {app}: path/root non-overlap is enforced.
Source: "{#SourceRoot}\build\owner-installer\offline\manifest.json"; DestDir: "{tmp}\k5-closed\manifest"; Flags: ignoreversion
Source: "{#SourceRoot}\build\owner-installer\offline\wheelhouse\*.whl"; DestDir: "{tmp}\k5-closed\wheelhouse"; Flags: ignoreversion
Source: "{#SourceRoot}\scripts\windows-alpha\Install-K5VisionAlpha.ps1"; DestDir: "{tmp}\k5-closed\scripts\windows-alpha"; Flags: ignoreversion
Source: "{#SourceRoot}\scripts\windows-alpha\Start-K5VisionAlpha.ps1"; DestDir: "{tmp}\k5-closed\scripts\windows-alpha"; Flags: ignoreversion
Source: "{#SourceRoot}\scripts\windows-alpha\Test-K5VisionAlpha.ps1"; DestDir: "{tmp}\k5-closed\scripts\windows-alpha"; Flags: ignoreversion
Source: "{#SourceRoot}\scripts\windows-alpha\Run-K5VisionAlpha.ps1"; DestDir: "{tmp}\k5-closed\scripts\windows-alpha"; Flags: ignoreversion
Source: "{#SourceRoot}\scripts\windows-alpha\install_transaction.py"; DestDir: "{tmp}\k5-closed\scripts\windows-alpha"; Flags: ignoreversion
Source: "{#SourceRoot}\scripts\windows-alpha\owner_analytics_bundle.py"; DestDir: "{tmp}\k5-closed\scripts\windows-alpha"; Flags: ignoreversion
Source: "{#SourceRoot}\scripts\windows-alpha\runtime-requirements.txt"; DestDir: "{tmp}\k5-closed\scripts\windows-alpha"; Flags: ignoreversion
Source: "{#SourceRoot}\scripts\provision-stage03-gstreamer.ps1"; DestDir: "{tmp}\k5-closed\scripts"; Flags: ignoreversion
Source: "{#SourceRoot}\build\owner-installer\analytics\manifest.json"; DestDir: "{tmp}\k5-closed\scripts\analytics"; Flags: ignoreversion
Source: "{#SourceRoot}\build\owner-installer\analytics\wheels\*.whl"; DestDir: "{tmp}\k5-closed\scripts\analytics\wheels"; Flags: ignoreversion
Source: "{#SourceRoot}\build\owner-installer\analytics\models\*"; DestDir: "{tmp}\k5-closed\scripts\analytics\models"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#SourceRoot}\build\owner-installer\analytics\Analytics-Lab-LICENSE"; DestDir: "{tmp}\k5-closed\scripts\analytics"; Flags: ignoreversion
Source: "{#SourceRoot}\build\owner-installer\analytics\Analytics-Lab-THIRD_PARTY.md"; DestDir: "{tmp}\k5-closed\scripts\analytics"; Flags: ignoreversion


[Icons]
Name: "{autoprograms}\K5 Vision Alpha"; Filename: "{app}\K5VisionAlpha.exe"; WorkingDir: "{app}"
Name: "{userdesktop}\K5 Vision Alpha"; Filename: "{app}\K5VisionAlpha.exe"; WorkingDir: "{app}"

[UninstallDelete]
; Only owned files. Never delete camera/user data or another product's paths.
Type: filesandordirs; Name: "{app}\.venv"
Type: filesandordirs; Name: "{app}\python312"
Type: filesandordirs; Name: "{app}\analytics-models"
Type: files; Name: "{app}\analytics-config.json"
Type: files; Name: "{app}\Start-K5VisionAlpha.ps1"
Type: files; Name: "{app}\Test-K5VisionAlpha.ps1"
Type: files; Name: "{app}\Run-K5VisionAlpha.ps1"
Type: files; Name: "{app}\k5-revision.txt"
Type: files; Name: "{app}\gstreamer-version.txt"
Type: files; Name: "{app}\payload\owner-install-stage.txt"

[Code]
var
  K5PostInstallEntered: Boolean;
  K5PostInstallVerified: Boolean;
  K5PostInstallPhase: Integer;

function GetCustomSetupExitCode: Integer;
begin
  { Even if a suppressed post-install error would otherwise return success,
    never advertise an owner-installable runtime without positive verification. }
  if not K5PostInstallEntered then
    Result := 41
  else if not K5PostInstallVerified then
    Result := 41 + K5PostInstallPhase
  else
    Result := 0;
end;

function Q(Value: String): String;
begin
  Result := '"' + Value + '"';
end;

function OwnerFailureStage: String;
var
  Raw: AnsiString;
  Stage: String;
begin
  Result := 'not-reported';
  if not LoadStringFromFile(ExpandConstant('{tmp}\k5-closed\scripts\owner-install-stage.txt'), Raw) then
    Exit;
  Stage := Trim(String(Raw));
  { Accept fixed identifiers only. Never project source, exception, log, or path data. }
  if Pos('|' + Stage + '|',
    '|gstreamer|wheel-build-base|wheel-build-k5|wheel-copy-analytics|' +
    'wheel-hashes|stage-venv|stage-runtime|stage-preflight|shortcut|' +
    'verify-wheels|activation-prepare|activation-venv|activation-runtime|' +
    'activation-preflight|commit|complete|' +
    'stage-venv-create|activation-venv-create|' +
    'stage-pip-install|activation-pip-install|' +
    'stage-pip-check|activation-pip-check|' +
    'stage-cli|activation-cli|stage-probe|activation-probe|' +
    'stage-model-copy|activation-model-copy|' +
    'stage-analytics-preflight|activation-analytics-preflight|' +
    'stage-analytics-package|activation-analytics-package|' +
    'stage-analytics-versions|activation-analytics-versions|' +
    'stage-analytics-path|activation-analytics-path|' +
    'stage-analytics-model|activation-analytics-model|') > 0 then
    Result := Stage;
end;

procedure RunOrFail(const Executable, Parameters, StageName: String);
var
  ExitCode: Integer;
begin
  if not Exec(Executable, Parameters, '', SW_HIDE, ewWaitUntilTerminated, ExitCode) then
    RaiseException('K5 ' + StageName + ' could not start. No camera was contacted.');
  if ExitCode <> 0 then
  begin
    if StageName = 'transactional runtime installation' then
      RaiseException('K5 runtime installation failed at verified stage ' +
        OwnerFailureStage() + ' (code ' + IntToStr(ExitCode) + '). ' +
        'The installation was not accepted; no camera was contacted.');
    RaiseException('K5 ' + StageName + ' did not complete (code ' +
      IntToStr(ExitCode) + '). Installation cannot be accepted.');
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  AppRoot, PythonRoot, PythonExe, PySetup, Bootstrap, Parameters: String;
  OfflineBase, ReviewedManifest, ReviewedWheels, GstProvisioner: String;
begin
  if CurStep <> ssPostInstall then
    Exit;
  K5PostInstallEntered := True;
  K5PostInstallPhase := 1;
  AppRoot := ExpandConstant('{app}');
  PythonRoot := AppRoot + '\python312';
  PythonExe := PythonRoot + '\python.exe';
  PySetup := AppRoot + '\payload\python-3.12.10-amd64.exe';
  OfflineBase := ExpandConstant('{tmp}\k5-closed');
  Bootstrap := OfflineBase + '\scripts\windows-alpha\Install-K5VisionAlpha.ps1';
  ReviewedManifest := OfflineBase + '\manifest\manifest.json';
  ReviewedWheels := OfflineBase + '\wheelhouse';
  GstProvisioner := OfflineBase + '\scripts\provision-stage03-gstreamer.ps1';
  if not FileExists(AppRoot + '\payload\analytics\manifest.json') or
     not FileExists(OfflineBase + '\scripts\analytics\manifest.json') then
    RaiseException('Verified owner analytics payload is missing.');
  if not FileExists(Bootstrap) or not FileExists(ReviewedManifest) or
     not DirExists(ReviewedWheels) or not FileExists(GstProvisioner) then
    RaiseException('Closed offline owner wheelhouse payload is missing.');
  if not FileExists(PySetup) or not FileExists(Bootstrap) or
     not FileExists(AppRoot + '\payload\mediamtx\mediamtx_v1.21.1_windows_amd64.zip') or
     not FileExists(AppRoot + '\payload\licenses\MediaMTX-MIT.txt') or
     not FileExists(AppRoot + '\payload\gstreamer\gstreamer-1.0-msvc-x86_64-1.28.7.exe') then
    RaiseException('K5 installer payload is incomplete.');
  K5PostInstallPhase := 2;
  if not FileExists(PythonExe) then
  begin
    Parameters := '/quiet InstallAllUsers=0 Include_pip=1 Include_test=0 ' +
      'Include_launcher=0 PrependPath=0 Shortcuts=0 TargetDir=' + Q(PythonRoot);
    RunOrFail(PySetup, Parameters, 'private Python setup');
  end;
  if not FileExists(PythonExe) then
    RaiseException('Private Python runtime is unavailable.');
  K5PostInstallPhase := 3;
  { The offline transactional verifier never mutates shared GStreamer.
    A fresh GUI setup uses the existing hash-guarded provisioner; upgrades
    refuse shared-runtime changes and rely on preflight admission. }
  if (not DirExists(AppRoot + '\.venv')) and
     (not FileExists(AppRoot + '\k5-revision.txt')) then
  begin
    Parameters := '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File ' +
      Q(GstProvisioner) + ' -Version 1.28.7 -OfflineInstaller ' +
      Q(AppRoot + '\payload\gstreamer\gstreamer-1.0-msvc-x86_64-1.28.7.exe');
    RunOrFail(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
      Parameters, 'offline GStreamer admission');
  end;
  Parameters := '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File ' +
    Q(Bootstrap) + ' -InstallRoot ' + Q(AppRoot) +
    ' -PythonExecutable ' + Q(PythonExe) +
    ' -K5Revision {#K5Revision} -SkipDesktopShortcut' +
    ' -Wheelhouse ' + Q(ReviewedWheels) +
    ' -WheelhouseManifest ' + Q(ReviewedManifest) +
    ' -WheelhouseManifestSha256 {#OfflineWheelhouseDigest}';
  RunOrFail(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
    Parameters, 'transactional runtime installation');
  K5PostInstallPhase := 4;
  if not FileExists(AppRoot + '\.venv\Scripts\python.exe') or
     not FileExists(AppRoot + '\Start-K5VisionAlpha.ps1') or
     not FileExists(AppRoot + '\k5-revision.txt') or
     not FileExists(AppRoot + '\analytics-config.json') or
     not DirExists(AppRoot + '\analytics-models') then
    RaiseException('The installed K5 runtime is incomplete.');
  { The installer cannot report success unless the SAME installed GUI
    executable passes the runtime check exercised by hosted Windows CI.
    Component codes disclose no local source, credential, or media data. }
  K5PostInstallPhase := 5;
  RunOrFail(AppRoot + '\K5VisionAlpha.exe', '--self-check',
    'installed graphical runtime verification');
  K5PostInstallVerified := True;
end;
