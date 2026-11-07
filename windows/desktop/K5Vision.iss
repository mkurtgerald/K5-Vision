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
; Favor fast, low-memory extraction for the offline Windows setup; all installed payload hashes remain verified.
Compression=zip/7
SolidCompression=yes
LicenseFile={#PayloadRoot}\licenses\INSTALLATION-LICENSE.txt

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"; Flags: checkedonce

[Files]
Source: "{#PayloadRoot}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; This admitted copy runs before any existing installation is trusted.
Source: "{#PayloadRoot}\K5Vision.exe"; DestName: "K5VisionInstallGuard.exe"; Flags: dontcopy
; Two-phase admission: each sealed file and its ACL is checked during
; extraction, then the entire installed tree is rechecked after uninstall log
; finalization and before Setup may display success.
Source: "{#PayloadRoot}\payload-manifest.json"; DestDir: "{tmp}"; DestName: "k5-install-seal.json"; Flags: deleteafterinstall; AfterInstall: AdmitExtractedPayload

[Icons]
Name: "{userprograms}\K5 Vision"; Filename: "{app}\K5Vision.exe"; WorkingDir: "{app}"
Name: "{userdesktop}\K5 Vision"; Filename: "{app}\K5Vision.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\K5Vision.exe"; Description: "Launch K5 Vision"; Flags: nowait postinstall skipifsilent; Check: InstallationVerified

[Code]
var
  InstallationMutex: THandle;
  InstallationLease: Boolean;
  FinalPayloadVerified: Boolean;

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
  Prefix := 'Global\K5VisionDesktop-S-1-';
  Result := (Length(Name) > Length(Prefix)) and (Length(Name) <= 224) and
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

procedure AdmitExtractedPayload();
var
  ExitCode: Integer;
begin
  // File extraction is complete, but the uninstaller log is not yet finalized.
  // Check all installed sealed bytes, owner and ACL before log commit.
  // Inno may still have in-flight uninstall files here. The final strict-tree
  // admission below cannot tolerate any unlisted installed file.
  if not Exec(ExpandConstant('{app}\K5Vision.exe'), '--installation-extraction-check', '',
    SW_HIDE, ewWaitUntilTerminated, ExitCode) or (ExitCode <> 0) then
    RaiseException('K5 Vision could not verify the installed application. Your account and recordings were preserved.');
end;

function InstallationVerified(): Boolean;
begin
  Result := FinalPayloadVerified;
end;

procedure CurStepChanged(Step: TSetupStep);
var
  ExitCode: Integer;
begin
  if Step = ssPostInstall then begin
    // Inno 6.4.3 catches exceptions from this callback and continues. Keep
    // failure latched before Exec, including if Exec itself raises.
    FinalPayloadVerified := False;
    try
      FinalPayloadVerified := Exec(ExpandConstant('{app}\K5Vision.exe'),
        '--installation-payload-check', '', SW_HIDE, ewWaitUntilTerminated, ExitCode);
      if FinalPayloadVerified then FinalPayloadVerified := ExitCode = 0;
    except
      FinalPayloadVerified := False;
    end;
    // Allow the normal verified launch; retain the lease on rejection.
    if FinalPayloadVerified then ReleaseInstallationLease();
  end;
end;

procedure CurPageChanged(PageID: Integer);
begin
  if (PageID = wpFinished) and not FinalPayloadVerified then begin
    // Inno writes its success labels AFTER ssPostInstall. Override them here.
    WizardForm.FinishedHeadingLabel.Caption := 'K5 Vision installation failed';
    WizardForm.FinishedLabel.Caption :=
      'K5 Vision could not verify the installed application. Do not use this installation. Your account and recordings were preserved.';
    WizardForm.RunList.Visible := False;
  end;
end;

function GetCustomSetupExitCode(): Integer;
begin
  Result := 0;
  if not FinalPayloadVerified then Result := 1;
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
