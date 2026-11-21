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
DefaultDirName={autopf}\K5 Vision
UsePreviousAppDir=no
DefaultGroupName=K5 Vision
DisableProgramGroupPage=yes
DisableDirPage=yes
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
WizardStyle=modern
SetupLogging=no
CloseApplications=yes
CloseApplicationsFilter=K5Vision.exe
RestartApplications=no
UninstallDisplayIcon={app}\K5Vision.exe
OutputDir={#OutputRoot}
OutputBaseFilename=K5VisionSetup
; The native guard is extracted by InitializeSetup *before* the first wizard page.
; Independent ZIP blocks avoid inflating the entire 200+ MB payload just to
; reach that guard during an upgrade/reversal. The same sealed-file SHA/ACL
; admission still runs before install success; no integrity check is skipped.
Compression=zip/7
SolidCompression=no
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
Name: "{commonprograms}\K5 Vision"; Filename: "{app}\K5Vision.exe"; WorkingDir: "{app}"
Name: "{commondesktop}\K5 Vision"; Filename: "{app}\K5Vision.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\K5Vision.exe"; Description: "Launch K5 Vision"; Flags: nowait postinstall skipifsilent runasoriginaluser; Check: InstallationVerified

[Code]
type
  TMutexSecurity = record
    Length: LongWord;
    Descriptor: THandle;
    Inherit: Integer;
  end;

var
  InstallationMutex: THandle;
  InstallationLease: Boolean;
  FinalPayloadVerified: Boolean;

function CreateMutex(var Security: TMutexSecurity; InitialOwner: Integer; Name: String): THandle;
  external 'CreateMutexW@kernel32.dll stdcall';
function ConvertSecurityDescriptor(Text: String; Revision: LongWord; var Descriptor: THandle; Size: Integer): Boolean;
  external 'ConvertStringSecurityDescriptorToSecurityDescriptorW@advapi32.dll stdcall';
function LocalFree(Handle: THandle): THandle;
  external 'LocalFree@kernel32.dll stdcall';
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
begin
  Result := Name = 'Global\K5VisionMachineInstallation';
end;

function AdmittedGuard(const Filename: String): Boolean;
var
  ExitCode: Integer;
  IdentityFile, Name: String;
  Identity: AnsiString;
  WaitResult: LongWord;
  Security: TMutexSecurity;
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
  Security.Length := SizeOf(Security);
  Security.Descriptor := 0;
  Security.Inherit := 0;
  if not ConvertSecurityDescriptor('D:P(A;;GA;;;SY)(A;;GA;;;BA)', 1, Security.Descriptor, 0) then exit;
  try
    InstallationMutex := CreateMutex(Security, 0, Name);
  finally
    LocalFree(Security.Descriptor);
  end;
  if InstallationMutex = 0 then exit;
  WaitResult := WaitForSingleObject(InstallationMutex, 0);
  // WAIT_OBJECT_0 or WAIT_ABANDONED grants exclusive ownership. Keep this
  // lease throughout every file copy/removal and release it only on exit.
  if (WaitResult <> 0) and (WaitResult <> $80) then begin
    ReleaseInstallationLease();
    MsgBox('Another K5 Vision installation is running. Close that installer before continuing.', mbInformation, MB_OK);
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

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ExitCode: Integer;
begin
  Result := '';
  if not Exec(ExpandConstant('{tmp}\K5VisionInstallGuard.exe'),
    '--installation-service-legacy-check', '', SW_HIDE, ewWaitUntilTerminated, ExitCode) or (ExitCode <> 0) then begin
    Result := 'An existing K5 user site needs verified migration before installing this service candidate. No application files or recordings were changed. Keep using the previous installation until a migration-qualified upgrade is available.';
    exit;
  end;
  // Stop only after the operator chooses Install. Never replace live recorder
  // code, and never execute a prior installation to control an unknown service.
  if not Exec(ExpandConstant('{tmp}\K5VisionInstallGuard.exe'),
    '--installation-service-stop', '', SW_HIDE, ewWaitUntilTerminated, ExitCode) or (ExitCode <> 0) then
    Result := 'K5 Vision could not stop and verify its recording service. No application files were replaced. Your account and recordings are preserved.';
  if Result = '' then
    if not Exec(ExpandConstant('{tmp}\K5VisionInstallGuard.exe'),
      '--installation-service-prepare', '', SW_HIDE, ewWaitUntilTerminated, ExitCode) or (ExitCode <> 0) then
      Result := 'K5 Vision could not secure its application folder. No application files were replaced. Your account and recordings are preserved.';
end;

procedure AdmitExtractedPayload();
var
  ExitCode: Integer;
begin
  // File extraction is complete, but the uninstaller log is not yet finalized.
  // Check all installed sealed bytes, owner and ACL before log commit.
  // Inno may still have in-flight uninstall files here. The final strict-tree
  // admission below cannot tolerate any unlisted installed file.
  if not Exec(ExpandConstant('{tmp}\K5VisionInstallGuard.exe'), '--installation-machine-extraction-check', '',
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
      FinalPayloadVerified := Exec(ExpandConstant('{tmp}\K5VisionInstallGuard.exe'),
        '--installation-service-seal', '', SW_HIDE, ewWaitUntilTerminated, ExitCode);
      if FinalPayloadVerified then FinalPayloadVerified := ExitCode = 0;
      // The launcher is excluded from its own payload manifest. Pin its bytes
      // after private code sealing and before its first elevated execution.
      if FinalPayloadVerified then
        FinalPayloadVerified := CompareText(GetSHA256OfFile(ExpandConstant('{app}\K5Vision.exe')), '{#LauncherSha}') = 0;
      if FinalPayloadVerified then begin
        FinalPayloadVerified := Exec(ExpandConstant('{app}\K5Vision.exe'),
          '--installation-payload-check', '', SW_HIDE, ewWaitUntilTerminated, ExitCode);
        if FinalPayloadVerified then FinalPayloadVerified := ExitCode = 0;
      end;
      if FinalPayloadVerified then begin
        FinalPayloadVerified := Exec(ExpandConstant('{app}\K5Vision.exe'),
          '--installation-service-install', '', SW_HIDE, ewWaitUntilTerminated, ExitCode);
        if FinalPayloadVerified then FinalPayloadVerified := ExitCode = 0;
      end;
      if FinalPayloadVerified then begin
        FinalPayloadVerified := Exec(ExpandConstant('{app}\K5Vision.exe'),
          '--installation-service-start', '', SW_HIDE, ewWaitUntilTerminated, ExitCode);
        if FinalPayloadVerified then FinalPayloadVerified := ExitCode = 0;
      end;
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
      'K5 Vision could not verify or start the recording service. Run Setup again to repair this installation. Your account and recordings were preserved.';
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
  ExitCode: Integer;
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
    if Result then
      Result := Exec(Captured, '--installation-service-remove', '', SW_HIDE,
        ewWaitUntilTerminated, ExitCode) and (ExitCode = 0);
    if not Result then
      MsgBox('K5 Vision could not safely remove its recording service. Your account and recordings were preserved.', mbError, MB_OK);
  finally
    DeleteFile(Captured);
  end;
end;
