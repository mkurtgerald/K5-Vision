# Windows and camera time administration

The Administration time panel manages the signed-in PC's Windows Time
configuration and enrolled ONVIF cameras. Only a currently authenticated K5
administrator can read these controls or request a change. Time changes are
explicit actions; sign-in, camera discovery and ordinary recording do not
silently modify either clock.

## Windows Time

The panel reads Windows Time service state, source mode and configured manual
NTP peers. An administrator can select manual peers or domain time and request
Windows Time resynchronization. Changes use the installed native supervisor's
fixed time-administration operation. Windows displays its normal UAC consent
dialog; the operator runs no terminal or separate script.

The elevated operation is restricted to Windows Time configuration/service
actions and a fixed system `w32tm.exe` invocation. It does not accept arbitrary
programs, arguments or registry paths. Requests use private files with a fresh
nonce; source admission, file identity and a matching response bind the result.
Each private request also binds the live issuing backend's PID and exact native
creation `FILETIME` as integer 100-nanosecond units. The elevated helper pins
that kernel process object, verifies its SID and creation identity, and checks
that it remains alive before every privileged action. A surviving request file
cannot authorize a delayed UAC approval after a hard-killed backend, or a reused
PID with a different creation identity. This binding does not use wall-clock age
or floating-point process timestamps.
Different-account UAC elevation cannot bypass the original profile's custody.
Cancellation removes the pending admission request, kills the owned consent
broker and drains work before the profile lease is released.

Configuration is read back after a native operation. A successful request or a
running service alone does not establish synchronization. For manual peers, a
separate bounded SNTP probe compares UTC against the first configured peer.
The reply must match a fresh originate nonce, the connected peer, a synchronized
server leap state, a valid stratum and consistent timestamps. Offset,
round-trip uncertainty and root dispersion are displayed. A simultaneous host
clock step invalidates the measurement. DNS and UDP work run in a killable
owned process with a five-second deadline.

`reference.sync_verified` means this comparison is within two seconds after
including the reported uncertainty. Standard SNTP is unauthenticated; this is
not NTS or a certification of absolute UTC accuracy. Domain mode does not
misrepresent the dormant manual-peer registry value as the domain controller's
selected source. Domain resynchronization can complete while independent
reference verification remains unavailable.

## ONVIF cameras

The camera panel uses standard ONVIF `GetSystemDateAndTime`, `GetNTP`, `SetNTP`
and `SetSystemDateAndTime`. Direct RTSP streams have no ONVIF management
authority and are reported as unsupported. A camera can also refuse time
administration when the account lacks the required device permissions.

Some cameras issue a delegated streaming account in their media URI. The panel
therefore accepts optional management username/password fields for a single
operation. These values cross only the bounded private ONVIF request path;
they are not returned, audited, saved or retained for the next operation.
Without an override the existing private connection credentials are tried.

An administrator can configure DHCP-provided or manual NTP peers and switch the
camera to NTP mode. Read-back must agree before the change is reported as
configured. The camera's reported NTP mode is not proof that it has reached a
reference server or achieved absolute UTC accuracy.

The separate **Copy PC UTC to camera** action writes current host UTC in manual
mode. It preserves the camera's existing timezone and daylight-saving setting,
then reads the clock again and reports the measured relative offset. This
explicitly switches from NTP to manual mode. The result can verify alignment
with the PC, while `absolute_sync_verified` remains false.

## Qualification boundary

Unit/API qualification covers source validation, SNTP wire validation, fresh
nonces, host-step rejection, source-era rollover, bounded process cleanup,
configuration read-back, refused/ignored camera writes, management credential
separation, authorization, origin checks, stale sessions, camera changes and
cancellation retaining mutation authority until physical work ends. The native
Windows preflight additionally rejects dead issuing processes, exact creation
time mismatches and live-PID/old-birth substitutions without changing service or
source configuration.

Actual Windows Time configuration/resynchronization needs a disposable Windows
qualification VM with restoration of its original service and source settings.
Existing +/-48-hour resilience tests establish preservation of recordings and
event UTC through host-clock changes; they do not replace this source/sync
qualification or physical-camera accuracy checks.
