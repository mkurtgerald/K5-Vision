[CmdletBinding()]
param(
    [ValidateSet('synthetic', 'public')]
    [string]$Mode = 'synthetic',
    [switch]$ControllerSelfTest
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$stage = 'host-admission'
$gui = $null
$source = ''
$failed = $false
$root = Join-Path $env:LOCALAPPDATA 'K5VisionAlpha'

# Only a fresh isolated hosted worker is permitted. This is not the physical
# host/storage acceptance lane and never admits a home or private camera.
if ($env:GITHUB_ACTIONS -cne 'true' -or $env:RUNNER_ENVIRONMENT -cne 'github-hosted' -or
    $env:GITHUB_REPOSITORY -cne 'mkurtgerald/K5-Vision') {
    throw 'Installed GUI witness requires the admitted isolated hosted worker.'
}
# WinForms accessibility providers vary across hosted Windows images. Use bounded
# native messages directed only at children of the exact process we launched.
$nativeControls = @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;
public sealed class K5WitnessControl {
    internal IntPtr Handle, Root;
    internal uint Pid;
    internal bool ReadOnly;
    public bool IsEnabled { get { Validate(); return K5WitnessNative.IsWindowEnabled(Handle); } }
    internal void Validate() {
        uint owner;
        K5WitnessNative.GetWindowThreadProcessId(Handle, out owner);
        if (owner != Pid || !K5WitnessNative.IsChild(Root, Handle))
            throw new InvalidOperationException("Control identity changed.");
    }
    public string Read() {
        Validate();
        var text = new StringBuilder(8192);
        UIntPtr length;
        if (K5WitnessNative.ReadMessage(Handle, 0x000D, new UIntPtr(8192), text, 2, 2000, out length) == IntPtr.Zero)
            throw new InvalidOperationException("Control read timed out.");
        return text.ToString();
    }
    public void SetValue(string value) {
        Validate();
        if (ReadOnly || value == null || value.Length > 2048 || !IsEnabled)
            throw new InvalidOperationException("Input refused.");
        UIntPtr result;
        if (K5WitnessNative.WriteMessage(Handle, 0x000C, UIntPtr.Zero, value, 2, 2000, out result) == IntPtr.Zero ||
            result == UIntPtr.Zero) throw new InvalidOperationException("Input failed.");
    }
    public void Invoke() {
        Validate();
        if (!IsEnabled) throw new InvalidOperationException("Button disabled.");
        K5WitnessNative.SetForegroundWindow(Root);
        UIntPtr result;
        if (K5WitnessNative.ClickMessage(Handle, 0x00F5, UIntPtr.Zero, IntPtr.Zero, 2, 5000, out result) == IntPtr.Zero)
            throw new InvalidOperationException("Button invocation timed out.");
    }
}
public sealed class K5WitnessControls {
    public K5WitnessControl Input, Status, Button;
}
public static class K5WitnessNative {
    public delegate bool ChildCallback(IntPtr handle, IntPtr parameter);
    [DllImport("user32.dll")] public static extern bool EnumChildWindows(IntPtr parent, ChildCallback callback, IntPtr parameter);
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr handle, out uint process);
    [DllImport("user32.dll")] public static extern bool IsChild(IntPtr parent, IntPtr child);
    [DllImport("user32.dll")] public static extern bool IsWindowEnabled(IntPtr handle);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr handle);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetClassName(IntPtr handle, StringBuilder name, int count);
    [DllImport("user32.dll", EntryPoint="GetWindowLongW")] public static extern int GetStyle(IntPtr handle, int index);
    [DllImport("user32.dll", EntryPoint="SendMessageTimeoutW", CharSet=CharSet.Unicode)]
    public static extern IntPtr ReadMessage(IntPtr handle, uint message, UIntPtr count, StringBuilder text, uint flags, uint timeout, out UIntPtr result);
    [DllImport("user32.dll", EntryPoint="SendMessageTimeoutW", CharSet=CharSet.Unicode)]
    public static extern IntPtr WriteMessage(IntPtr handle, uint message, UIntPtr count, string text, uint flags, uint timeout, out UIntPtr result);
    [DllImport("user32.dll", EntryPoint="SendMessageTimeoutW")]
    public static extern IntPtr ClickMessage(IntPtr handle, uint message, UIntPtr count, IntPtr value, uint flags, uint timeout, out UIntPtr result);
    public static K5WitnessControls Discover(IntPtr root, uint pid) {
        uint owner;
        GetWindowThreadProcessId(root, out owner);
        if (owner != pid) throw new InvalidOperationException("Window identity mismatch.");
        var handles = new List<IntPtr>();
        ChildCallback collect = delegate(IntPtr h, IntPtr p) { handles.Add(h); return true; };
        EnumChildWindows(root, collect, IntPtr.Zero);
        GC.KeepAlive(collect);
        var controls = new K5WitnessControls();
        int inputs = 0, statuses = 0, buttons = 0;
        foreach (IntPtr handle in handles) {
            GetWindowThreadProcessId(handle, out owner);
            if (owner != pid || !IsChild(root, handle)) continue;
            var className = new StringBuilder(256);
            GetClassName(handle, className, className.Capacity);
            string kind = className.ToString().ToUpperInvariant();
            var control = new K5WitnessControl { Handle=handle, Root=root, Pid=pid };
            if (kind.Contains(".EDIT.") || kind == "EDIT") {
                control.ReadOnly = (GetStyle(handle, -16) & 0x0800) != 0;
                if (control.ReadOnly) { statuses++; controls.Status = control; }
                else { inputs++; controls.Input = control; }
            } else if ((kind.Contains(".BUTTON.") || kind == "BUTTON") && control.Read() == "Run test") {
                buttons++; controls.Button = control;
            }
        }
        if (inputs != 1 || statuses != 1 || buttons != 1)
            throw new InvalidOperationException("Native control discovery failed.");
        return controls;
    }
}
'@
Add-Type -TypeDefinition $nativeControls
function Read-K5ControlText($Element) { return [string]$Element.Read() }
function Get-K5GuiControls($Process) {
    $deadline = [DateTime]::UtcNow.AddSeconds(20)
    do {
        Start-Sleep -Milliseconds 250
        $Process.Refresh()
    } until ($Process.HasExited -or $Process.MainWindowHandle -ne [IntPtr]::Zero -or
        [DateTime]::UtcNow -gt $deadline)
    if ($Process.HasExited -or $Process.MainWindowHandle -eq [IntPtr]::Zero) {
        throw 'Owned GUI did not open.'
    }
    $controls = [K5WitnessNative]::Discover($Process.MainWindowHandle, [uint32]$Process.Id)
    if (-not $controls.Status.Read().Contains('Ready. Recording is disabled for these tests.')) {
        throw 'Fresh GUI status mismatch.'
    }
    return $controls
}
function Resolve-K5VendorPublicSource {
    # Use only the stream Wowza explicitly publishes for developer testing.
    # Its live DESCRIBE/H.264 preflight passed independently. No auth bypass,
    # account, cookies, downloads of media, or literal RTSP URI is retained.
    $request = [Net.WebRequest]::CreateHttp('https://www.wowza.com/developer/rtsp-stream-test')
    $request.Method = 'GET'
    $request.UserAgent = 'K5-Engineering-TestSourceReview/1.0'
    $request.AllowAutoRedirect = $false
    $request.Timeout = 15000
    $request.ReadWriteTimeout = 5000
    $response = $null
    $stream = $null
    $memory = New-Object IO.MemoryStream
    try {
        $response = $request.GetResponse()
        if ([int]$response.StatusCode -ne 200) { throw 'Publisher document unavailable.' }
        $stream = $response.GetResponseStream()
        $buffer = New-Object byte[] 8192
        while (($count = $stream.Read($buffer, 0, $buffer.Length)) -gt 0) {
            if ($memory.Length + $count -gt 1048576) { throw 'Publisher document exceeds bound.' }
            $memory.Write($buffer, 0, $count)
        }
        $encoding = New-Object Text.UTF8Encoding($false, $true)
        $document = [Net.WebUtility]::HtmlDecode($encoding.GetString($memory.ToArray()))
    } finally {
        if ($null -ne $stream) { $stream.Dispose() }
        if ($null -ne $response) { $response.Dispose() }
        $memory.Dispose()
    }
    $approved = New-Object 'System.Collections.Generic.HashSet[string]'
    foreach ($match in [regex]::Matches($document, 'rtsp://[^\s<>"'']+')) {
        $value = $match.Value
        [Uri]$candidate = $null
        if ($value.Length -le 2048 -and [Uri]::TryCreate($value, [UriKind]::Absolute, [ref]$candidate) -and
            $candidate.Scheme -ceq 'rtsp' -and
            $candidate.Host -ceq '9627b0bf2a7b.entrypoint.cloud.wowza.com' -and
            $candidate.Port -eq 1935 -and $candidate.UserInfo.Length -eq 0 -and
            $candidate.Fragment.Length -eq 0) { [void]$approved.Add($value) }
    }
    if ($approved.Count -ne 1) { throw 'Unique approved public source unavailable.' }
    # The installed launcher independently resolves and pins globally routable
    # DNS before touching RTSP. Never replace that product admission boundary.
    foreach ($value in $approved) { return [string]$value }
}
try {
    if ($ControllerSelfTest) {
        # A separate generated WinForms fixture validates only this UI witness.
        # It is never installed as K5 and cannot count as product acceptance.
        $stage = 'controller-fixture'
        $fixture = Join-Path $env:RUNNER_TEMP 'K5GuiWitnessFixture.exe'
        $fixtureSource = @'
using System;
using System.Drawing;
using System.Windows.Forms;
internal static class WitnessFixture {
    [STAThread] public static void Main() {
        var form = new Form { Text = "K5 witness fixture", ClientSize = new Size(600, 300) };
        var input = new TextBox { Location = new Point(10,10), Width = 550 };
        var status = new TextBox { Multiline = true, ReadOnly = true,
            Location = new Point(10,80), Size = new Size(550,180),
            Text = "Ready. Recording is disabled for these tests." };
        var button = new Button { Text = "Run test", Location = new Point(10,40), Width = 100 };
        button.Click += delegate { status.Text = input.Text == "fixture-only" ? "WITNESS_OK" : "WITNESS_BAD"; };
        form.Controls.AddRange(new Control[] { input, status, button });
        Application.Run(form);
    }
}
'@
        Add-Type -TypeDefinition $fixtureSource -ReferencedAssemblies System.Windows.Forms,System.Drawing -OutputAssembly $fixture -OutputType WindowsApplication
        $gui = Start-Process -FilePath $fixture -PassThru
        $controls = Get-K5GuiControls $gui
        $controls.Input.SetValue('fixture-only')
        $controls.Button.Invoke()
        $deadline = [DateTime]::UtcNow.AddSeconds(5)
        do {
            Start-Sleep -Milliseconds 100
            $text = Read-K5ControlText $controls.Status
        } until ($text -ceq 'WITNESS_OK' -or [DateTime]::UtcNow -gt $deadline)
        if ($text -cne 'WITNESS_OK') { throw 'UI controller fixture failed.' }
        Write-Host 'K5_GUI_WITNESS_CONTROLLER=passed'
    } else {
        $stage = 'installed-identity'
        if (([IO.File]::ReadAllText((Join-Path $root 'k5-revision.txt'))).Trim() -cne
            '887051738890ca2c0e34431bd707fe302654747e') { throw 'Installed revision mismatch.' }
        if ($Mode -ceq 'public') {
            $stage = 'publisher-document'
            $source = Resolve-K5VendorPublicSource
            Write-Host 'K5_PUBLIC_LIBRARY_SOURCE=vendor_published_transiently'
        }
        $stage = 'gui-open'
        $gui = Start-Process -FilePath (Join-Path $root 'K5VisionAlpha.exe') -PassThru
        $stage = 'gui-controls'
        $controls = Get-K5GuiControls $gui
        if (-not $controls.Button.IsEnabled) { throw 'Installed action disabled.' }
        $controls.Input.SetValue($source)
        $source = ''
        $stage = 'gui-invoke'
        $controls.Button.Invoke()
        $stage = 'runtime-receipt'
        $expected = 'Synthetic video-only smoke passed; public RTSP analytics NOT qualified.'
        if ($Mode -ceq 'public') {
            $expected = 'Stage-One receipt passed: analytics, boxes and operator presentation confirmed.'
        }
        $deadline = [DateTime]::UtcNow.AddMinutes(5)
        do {
            Start-Sleep -Milliseconds 500
            $text = Read-K5ControlText $controls.Status
            if ($text.Contains($expected) -or $text.Contains('Test did not pass:') -or
                $text.Contains('Stage-One NOT qualified:')) { break }
        } while ([DateTime]::UtcNow -lt $deadline -and -not $gui.HasExited)
        $health = $text.Contains('K5 local service health passed.')
        $presentation = $text.Contains('Positive operator frames and presentations confirmed.')
        $privacy = $text.Contains('No test media was retained.')
        $analytics = $text.Contains('Positive analytics submissions and completions; zero failures.')
        $boxes = $text.Contains('Positive rendered detection-box count confirmed.')
        Write-Host ("K5_INSTALLED_GUI_${Mode}_HEALTH=" + [int]$health)
        Write-Host ("K5_INSTALLED_GUI_${Mode}_PRESENTATION=" + [int]$presentation)
        Write-Host ("K5_INSTALLED_GUI_${Mode}_PRIVACY=" + [int]$privacy)
        Write-Host ("K5_INSTALLED_GUI_${Mode}_ANALYTICS=" + [int]$analytics)
        Write-Host ("K5_INSTALLED_GUI_${Mode}_BOXES=" + [int]$boxes)
        if (-not ($text.Contains($expected) -and $health -and $presentation -and $privacy) -or
            ($Mode -ceq 'public' -and -not ($analytics -and $boxes))) {
            $diagnostic = 'no-terminal-status'
            if ($text.Contains('Analytics did not meet')) { $diagnostic = 'analytics-gate' }
            elseif ($text.Contains('GStreamer runtime validation failed')) { $diagnostic = 'gstreamer' }
            elseif ($text.Contains('RTSP test source could not')) { $diagnostic = 'source-decode' }
            elseif ($text.Contains('local K5 service did not')) { $diagnostic = 'service-health' }
            elseif ($text.Contains('Stage-One NOT qualified:')) { $diagnostic = 'receipt-incomplete' }
            elseif ($text.Contains('Test did not pass:')) { $diagnostic = 'runtime-failed' }
            Write-Host ("K5_INSTALLED_GUI_${Mode}_DIAGNOSTIC=" + $diagnostic)
            throw 'Installed operator receipt incomplete.'
        }
        Write-Host ("K5_INSTALLED_GUI_${Mode}_RECEIPT=passed")
    }
} catch {
    $failed = $true
    # Never print exception text, response bodies, URLs, cookies or GUI contents.
    Write-Host ("K5_INSTALLED_GUI_FAILURE_STAGE=" + $stage)
    Write-Host ('K5_GUI_WITNESS_FAILURE_LINE=' + [int]$_.InvocationInfo.ScriptLineNumber)
    if ($ControllerSelfTest) {
        $reason = 'unclassified'
        $detail = [string]$_.Exception.Message
        if ($detail -match 'null-valued') { $reason = 'null-value' }
        elseif ($detail -match 'overload') { $reason = 'overload' }
        elseif ($detail -match 'pattern') { $reason = 'pattern' }
        elseif ($detail -match 'controls unavailable') { $reason = 'control-discovery' }
        elseif ($detail -match 'Run action unavailable') { $reason = 'button-discovery' }
        elseif ($detail -match 'did not open') { $reason = 'window-discovery' }
        elseif ($detail -match 'compile|compilation|error CS') { $reason = 'fixture-compilation' }
        elseif ($detail -match 'conversion|convert') { $reason = 'type-conversion' }
        Write-Host ('K5_GUI_WITNESS_FIXTURE_DIAGNOSTIC=' + $reason)
    }
} finally {
    if ($null -ne $gui) {
        try {
            if (-not $gui.HasExited) {
                if (-not $gui.CloseMainWindow() -or -not $gui.WaitForExit(10000)) {
                    $failed = $true
                    Write-Host 'K5_INSTALLED_GUI_CLOSE=not_completed'
                }
            }
        } catch { $failed = $true; Write-Host 'K5_INSTALLED_GUI_CLOSE=failed' }
        $gui.Dispose()
    }
    $source = $null
}
if ($failed) { throw 'Installed GUI witness failed; only fixed diagnostic codes are published.' }
