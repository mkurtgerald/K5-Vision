# Install the native Windows operator

This per-user installer turns the extracted pilot into a reusable Start menu
application. It needs no administrator access, Docker, WSL or machine-wide
changes. It keeps the existing native MediaMTX/Caddy supervisor and security
checks. It does not install a Windows service or a signed MSI/EXE.

Use Windows 10/11, installed **64-bit CPython 3.12.4 or newer within 3.12.x**, a
current Edge/Chrome browser and H.264 RTSP camera streams. First application
launch needs Internet access for Python dependencies and pinned native workers.
Later launches try the installed dependencies without a package index before
requesting online setup, so a complete cached runtime can start without Internet.
The package includes donor licenses/notices; it does not bundle native binaries,
Python, models or FFmpeg. The existing pilot distribution/license limits apply.

Extract the entire qualified operator ZIP. From PowerShell in its root, run:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\Install-K5Operator.ps1 -DesktopShortcut
```

If Python is not found automatically, append `-PythonExecutable` and the full
path to the installed `python.exe`. Use `-Port 8001` if port 8000 is occupied.
Use `-RecordingLimitGb 50` to select a 50 GiB rolling recording allowance; the
default is 20 GiB. This setting is saved and reused by the installed starter.
Open **K5 Vision Operator** from Start or its optional Desktop shortcut. The
first launch prompts for the password and confirmation for **recovery-admin**,
then opens `http://127.0.0.1:8000/operator/`. Sign in and add a camera using its
numeric IP and actual RTSP URL. Person detection remains unconfigured.

The application remains a foreground supervisor. Closing the browser keeps
recording; closing its PowerShell console or signing out of Windows stops the
application. Keep the console open. Ctrl+C stops the operator and owned workers.

## Repeat installation and upgrades

Stop the running operator with Ctrl+C before installing another package. Extract
the new qualified ZIP elsewhere and run the same install command. Each package
is copied into an immutable release directory named by its SHA256 file manifest.
The active release changes after all copied files are checked. Repeating an
installation reuses matching files; changed or linked installation files are
refused. Prior release directories are retained. The installed starter verifies
the active release and every recorded file hash on every launch.

Default locations for this Windows account are:

- Application: `%LOCALAPPDATA%\K5VisionOperator`
- Runtime/dependencies/workers: `%LOCALAPPDATA%\K5VisionOperatorRuntime`
- Accounts, camera sources and recordings: `%LOCALAPPDATA%\K5VisionOperatorState`

The extracted download can be moved or removed after installation. Runtime and
state stay outside releases and survive upgrade. `-InstallRoot`, `-RuntimeRoot`
and `-StateRoot` support separate private absolute directories. Repeat installs
must retain the same runtime and state paths. Existing permissions are inspected
and never widened or repaired. A conflicting Start/Desktop/Startup shortcut is
refused, so another installation's entry is not overwritten.
Start/Desktop/Startup shortcuts contain public launch arguments and may allow
other local users to read them. Foreign writes to those shortcuts are refused;
application releases, runtime, accounts and recordings retain their private
directory requirements.

## Optional start when this user signs in

Complete the first interactive account setup, stop the operator, then rerun the
installer from its extracted package with:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\Install-K5Operator.ps1 -StartAtLogon
```

This creates a shortcut in this account's Startup folder. It starts the native
foreground operator at the next Windows sign-in without opening a browser; open
the Start shortcut only when the operator is stopped, or visit the local URL to
use the running application. Startup contains no password, runs as this user,
and preserves the same account/profile. It requires the initial identity
directory to exist; incomplete/unsafe identity still causes the starter to
refuse, rather than reset it. Repeating installation without `-StartAtLogon`
does not silently remove an existing startup entry. To disable startup, remove
**K5 Vision Operator** from this user's Startup folder (open `shell:startup`).

This starts **after sign-in**, not at system boot. Recording does not continue
after Windows signs this user out, and the console must remain open. Long
duration operation and a true unattended Windows service remain separate beta
qualification work.

## Uninstall the application

Stop the operator. Then run the installed uninstaller:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$env:LOCALAPPDATA\K5VisionOperator\Uninstall-K5Operator.ps1"
```

It removes this installation's application releases and exact matching
Start/Desktop/Startup entries. It **preserves runtime, accounts, camera sources
and recordings**, so reinstalling uses the existing profile. A running operator,
linked paths or unknown application files cause refusal before deletion. There
is no automatic data deletion option. Keep private profile directories out of
repositories and shared evidence.

Native installer acceptance checks actual Windows PowerShell 5.1, shortcuts,
repeat install, installed-wheel startup/login, refusal while recording is owned,
stopped upgrade, corruption refusal and profile preservation on uninstall.
These do not replace physical-camera, target Windows browser, sustained capacity
or commercial distribution qualification.
