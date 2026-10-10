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
$session = $null
$csrf = $null
$source = ''
$issued = $false
$failed = $false
$root = Join-Path $env:LOCALAPPDATA 'K5VisionAlpha'

# Only a fresh isolated hosted worker is permitted. This is not the physical
# host/storage acceptance lane and never admits a home or private camera.
if ($env:GITHUB_ACTIONS -cne 'true' -or $env:RUNNER_ENVIRONMENT -cne 'github-hosted' -or
    $env:GITHUB_REPOSITORY -cne 'mkurtgerald/K5-Vision') {
    throw 'Installed GUI witness requires the admitted isolated hosted worker.'
}
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes

function Read-K5ControlText($Element) {
    $pattern = $null
    if ($Element.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern, [ref]$pattern)) {
        return [string]$pattern.Current.Value
    }
    $pattern = $null
    if ($Element.TryGetCurrentPattern([System.Windows.Automation.TextPattern]::Pattern, [ref]$pattern)) {
        return [string]$pattern.DocumentRange.GetText(8192)
    }
    return ''
}
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
    $window = [System.Windows.Automation.AutomationElement]::FromHandle($Process.MainWindowHandle)
    if ($window.Current.ProcessId -ne $Process.Id) { throw 'GUI process identity mismatch.' }
    $condition = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::NameProperty, 'Run test')
    $button = $window.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $condition)
    if ($null -eq $button) { throw 'Run action unavailable.' }
    $all = $window.FindAll([System.Windows.Automation.TreeScope]::Descendants,
        [System.Windows.Automation.Condition]::TrueCondition)
    $sourcePattern = $null
    $status = $null
    foreach ($element in $all) {
        # Multiline WinForms edit controls expose TextPattern on some Windows
        # versions, rather than ValuePattern. Neither is assumed unconditionally.
        if ((Read-K5ControlText $element).Contains('Ready. Recording is disabled for these tests.')) {
            $status = $element
        }
        $pattern = $null
        if ($element.Current.ControlType -eq [System.Windows.Automation.ControlType]::Edit -and
            $element.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern, [ref]$pattern) -and
            -not $pattern.Current.IsReadOnly) {
            $sourcePattern = $pattern
        }
    }
    if ($null -eq $status -or $null -eq $sourcePattern) { throw 'GUI controls unavailable.' }
    return @{ Button = $button; Input = $sourcePattern; Status = $status }
}
function Request-K5Publisher([string]$Path, $Payload = $null) {
    $headers = @{ Accept = 'application/json'; Origin = 'https://rtsplink.com' }
    $arguments = @{
        Uri = ('https://rtsplink.com' + $Path)
        WebSession = $script:session
        UseBasicParsing = $true
        TimeoutSec = 15
        MaximumRedirection = 0
        Headers = $headers
        ErrorAction = 'Stop'
    }
    if ($null -ne $Payload) {
        $headers['X-CSRF-Token'] = $script:csrf
        $arguments['Method'] = 'POST'
        $arguments['ContentType'] = 'application/json'
        $arguments['Body'] = ($Payload | ConvertTo-Json -Compress -Depth 5)
    }
    $response = Invoke-WebRequest @arguments
    if ($response.StatusCode -ne 200 -or $response.Content.Length -gt 262144) {
        throw 'Publisher response refused.'
    }
    return ($response.Content | ConvertFrom-Json)
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
        $invoke = $controls.Button.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
        $invoke.Invoke()
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
            $stage = 'publisher-session'
            $session = New-Object Microsoft.PowerShell.Commands.WebRequestSession
            $handshake = Request-K5Publisher '/api/v1/session/active'
            $csrf = $handshake.csrfToken
            $nonce = $handshake.nonce
            if ($csrf -isnot [string] -or $nonce -isnot [string] -or
                $csrf.Length -lt 1 -or $csrf.Length -gt 2048 -or $csrf -match '[\r\n]' -or
                $nonce.Length -lt 1 -or $nonce.Length -gt 2048) { throw 'Publisher handshake refused.' }
            $stage = 'publisher-catalog'
            $catalog = Request-K5Publisher '/api/v1/streams/catalog'
            $people = @($catalog.streams | Where-Object { $_.id -ceq 'live/people' })
            if ($people.Count -ne 1) { throw 'Approved public library source unavailable.' }
            $stage = 'publisher-token'
            # Only this fresh anonymous session's public library token is created.
            # No signup, paid profile, account session, media download or local file.
            $issued = $true
            $token = Request-K5Publisher '/api/v1/token/generate' @{
                csrfToken = $csrf; nonce = $nonce; streamName = 'live/people'; forceNew = $false
                quality = @{ resolution = 'source'; fps = 'source'; allowCompatibilityUpscale = $false }
            }
            $source = $token.rtspUrl
            $uri = $null
            if ($source -isnot [string] -or $source.Length -gt 2048 -or
                -not [Uri]::TryCreate($source, [UriKind]::Absolute, [ref]$uri) -or
                $uri.Scheme -cne 'rtsp' -or $uri.Host -cne 'rtsp.rtsplink.com' -or
                $uri.UserInfo.Length -ne 0 -or $uri.Fragment.Length -ne 0) {
                throw 'Publisher media destination refused.'
            }
            Write-Host 'K5_PUBLIC_LIBRARY_SOURCE=issued_transiently'
        }
        $stage = 'gui-open'
        $gui = Start-Process -FilePath (Join-Path $root 'K5VisionAlpha.exe') -PassThru
        $stage = 'gui-controls'
        $controls = Get-K5GuiControls $gui
        if (-not $controls.Button.Current.IsEnabled) { throw 'Installed action disabled.' }
        $controls.Input.SetValue($source)
        $source = ''
        $stage = 'gui-invoke'
        $invoke = $controls.Button.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
        $invoke.Invoke()
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
    if ($issued) {
        try {
            $null = Request-K5Publisher '/api/v1/token/revoke' @{ csrfToken = $csrf }
            $state = Request-K5Publisher '/api/v1/session/active'
            if ($state.active -ne $false) { throw 'Session still active.' }
            Write-Host 'K5_PUBLIC_LIBRARY_TOKEN=revoked'
        } catch { $failed = $true; Write-Host 'K5_PUBLIC_LIBRARY_TOKEN=revocation_unconfirmed' }
    }
    $source = $csrf = $nonce = $token = $session = $handshake = $null
}
if ($failed) { throw 'Installed GUI witness failed; only fixed diagnostic codes are published.' }
