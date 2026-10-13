; Native click-through installer. Inno Setup 6.4.3 is pinned in donors.json.
; Build helpers and PowerShell are never installed or shown to the operator.
#ifndef PayloadRoot
  #error PayloadRoot must name the exact verified desktop payload
#endif
#ifndef LauncherSha
  #error LauncherSha must pin the compiled graphical supervisor
#endif
#ifndef OutputRoot
  #error OutputRoot must name the owned installer build directory
#endif

[Setup]
AppId={{B495C557-C6B7-4DF6-B0B8-4D2A16E940F8}
AppName=K5 Vision
AppVersion=0.1 Beta Candidate
AppPublisher=K5 Vision
AppPublisherURL=https://github.com/mkurtgerald/K5-Vision
AppSupportURL=https://github.com/mkurtgerald/K5-Vision/issues
DefaultDirName={localappdata}\K5VisionDesktop
DefaultGroupName=K5 Vision
DisableProgramGroupPage=yes
DisableDirPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
WizardStyle=modern
SetupLogging=no
CloseApplications=no
RestartApplications=no
UninstallDisplayIcon={app}\K5Vision.exe
OutputDir={#OutputRoot}
OutputBaseFilename=K5VisionSetup
Compression=lzma2/max
SolidCompression=yes
LicenseFile={#PayloadRoot}\licenses\INSTALLATION-LICENSE.txt

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"; Flags: checkedonce

[Files]
Source: "{#PayloadRoot}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; This admitted copy runs before any existing installation is trusted.
Source: "{#PayloadRoot}\K5Vision.exe"; DestName: "K5VisionInstallGuard.exe"; Flags: dontcopy

[Icons]
Name: "{userprograms}\K5 Vision"; Filename: "{app}\K5Vision.exe"; WorkingDir: "{app}"
Name: "{userdesktop}\K5 Vision"; Filename: "{app}\K5Vision.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\K5Vision.exe"; Description: "Launch K5 Vision"; Flags: nowait postinstall skipifsilent

[Code]
var
  InstallationMutex: THandle;
  InstallationLease: Boolean;

function CreateMutex(Security: Integer; InitialOwner: Integer; Name: String): THandle;
  external 'CreateMutexW@kernel32.dll stdcall';
function WaitForSingleObject(Handle: THandle; Milliseconds: LongWord): LongWord;
  external 'WaitForSingleObject@kernel32.dll stdcall';
function ReleaseMutex(Handle: THandle): Boolean;
  external 'ReleaseMutex@kernel32.dll stdcall';
function CloseHandle(Handle: THandle): Boolean;
  external 'CloseHandle@kernel32.dll stdcall';

procedure ReleaseInstallationLease();
begin
  if InstallationLease then begin
    ReleaseMutex(InstallationMutex);
    InstallationLease := False;
  end;
  if InstallationMutex <> 0 then begin
    CloseHandle(InstallationMutex);
    InstallationMutex := 0;
  end;
end;

function ValidMutexName(const Name: String): Boolean;
var
  Prefix: String;
  I: Integer;
begin
  Prefix := 'Global\K5VisionDesktop-S-1-5-';
  Result := (Length(Name) > Length(Prefix)) and (Length(Name) <= 160) and
    (Copy(Name, 1, Length(Prefix)) = Prefix);
  if not Result then exit;
  for I := Length(Prefix) + 1 to Length(Name) do begin
    if ((Name[I] < '0') or (Name[I] > '9')) and (Name[I] <> '-') then begin
      Result := False;
      exit;
    end;
  end;
end;

function AdmittedGuard(const Filename: String): Boolean;
var
  ExitCode: Integer;
  IdentityFile, Name: String;
  Identity: AnsiString;
  WaitResult: LongWord;
begin
  Result := False;
  if CompareText(GetSHA256OfFile(Filename), '{#LauncherSha}') <> 0 then begin
    MsgBox('The installed application could not be verified. Your account and recordings have been preserved.', mbError, MB_OK);
    exit;
  end;
  IdentityFile := ExpandConstant('{tmp}\k5-desktop-mutex-name.txt');
  if FileExists(IdentityFile) then begin
    MsgBox('K5 Vision could not prepare a safe installation check. No application files have been changed.', mbError, MB_OK);
    exit;
  end;
  if not Exec(Filename, '--installation-identity "' + IdentityFile + '"', '', SW_HIDE, ewWaitUntilTerminated, ExitCode) then begin
    MsgBox('K5 Vision could not check whether recording is running. No application files have been changed.', mbError, MB_OK);
    exit;
  end;
  if ExitCode <> 0 then begin
    MsgBox('K5 Vision could not verify the installation owner. No application files have been changed.', mbError, MB_OK);
    exit;
  end;
  if not LoadStringFromFile(IdentityFile, Identity) then exit;
  DeleteFile(IdentityFile);
  Name := String(Identity);
  if not ValidMutexName(Name) then exit;
  InstallationMutex := CreateMutex(0, 0, Name);
  if InstallationMutex = 0 then exit;
  WaitResult := WaitForSingleObject(InstallationMutex, 0);
  // WAIT_OBJECT_0 or WAIT_ABANDONED grants exclusive ownership. Keep this
  // lease throughout every file copy/removal and release it only on exit.
  if (WaitResult <> 0) and (WaitResult <> $80) then begin
    ReleaseInstallationLease();
    MsgBox('K5 Vision is still running. Choose Stop and Exit in K5 Vision before installing or uninstalling.', mbInformation, MB_OK);
    exit;
  end;
  InstallationLease := True;
  Result := True;
end;

function InitializeSetup(): Boolean;
begin
  ExtractTemporaryFile('K5VisionInstallGuard.exe');
  Result := AdmittedGuard(ExpandConstant('{tmp}\K5VisionInstallGuard.exe'));
end;

procedure CurStepChanged(Step: TSetupStep);
begin
  // All payload, shortcut and installer-registry writes have finished. The
  // normal Finish-page Launch choice can now start the verified application.
  if Step = ssPostInstall then ReleaseInstallationLease();
end;

procedure DeinitializeSetup();
begin
  ReleaseInstallationLease();
end;

procedure DeinitializeUninstall();
begin
  ReleaseInstallationLease();
end;

function InitializeUninstall(): Boolean;
var
  Installed, Captured: String;
begin
  Result := False;
  Installed := ExpandConstant('{app}\K5Vision.exe');
  if not FileExists(Installed) then begin
    MsgBox('The application could not be verified for removal. Your account and recordings have been preserved.', mbError, MB_OK);
    exit;
  end;
  if CompareText(GetSHA256OfFile(Installed), '{#LauncherSha}') <> 0 then begin
    MsgBox('The application has changed and could not be verified for removal. Your account and recordings have been preserved.', mbError, MB_OK);
    exit;
  end;
  // Capture the admitted guard into the uninstaller-owned temporary directory.
  // Never execute an unknown program supplied from an existing installation.
  Captured := ExpandConstant('{tmp}\K5VisionUninstallGuard.exe');
  if not FileCopy(Installed, Captured, False) then begin
    MsgBox('The application could not prepare safe removal. Your account and recordings have been preserved.', mbError, MB_OK);
    exit;
  end;
  try
    Result := AdmittedGuard(Captured);
  finally
    DeleteFile(Captured);
  end;
end;
