// Diagnostic guest bootstrap only. Compile x64 /target:winexe with Framework
// System.Core, System.Web.Extensions and System.ServiceProcess. No dev Python,
// browser driver, credential arguments, credential environment or private IPC.
using System;
using System.Collections;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Net;
using System.Runtime.InteropServices;
using System.Security.AccessControl;
using System.Security.Cryptography;
using System.Security.Principal;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Web.Script.Serialization;
using System.ServiceProcess;

internal static class WindowsGuestBootstrap
{
    private const string Root = @"C:\K5GuestWitness";
    private const string ReceiptPath = Root + @"\bootstrap-receipt.json";
    private const string Origin = "http://127.0.0.1:8000";
    private static readonly JavaScriptSerializer Json = new JavaScriptSerializer
        { MaxJsonLength = 65536, RecursionLimit = 16 };
    private static readonly Stopwatch Elapsed = Stopwatch.StartNew();
    private static readonly Dictionary<string, object> Receipt = new Dictionary<string, object>
        { { "schema_version", 1 }, { "diagnostic_only", true },
          { "actual_reboot_before_login", false }, { "actual_windows_logoff", false } };
    private static string Stage = "input";
    private static int HttpStatus;
    private static bool WritingFailure;
    [StructLayout(LayoutKind.Sequential)]
    private struct ServiceStatus
    {
        internal uint Type, State, Controls, Win32Exit, ServiceExit, Checkpoint, WaitHint, Pid, Flags;
    }
    [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern IntPtr OpenSCManagerW(string machine, string database, uint access);
    [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern IntPtr OpenServiceW(IntPtr manager, string name, uint access);
    [DllImport("advapi32.dll", SetLastError = true)]
    private static extern bool QueryServiceStatusEx(IntPtr service, int level, out ServiceStatus status, int size, out int needed);
    [DllImport("advapi32.dll")] private static extern bool CloseServiceHandle(IntPtr handle);
    private delegate bool EnumWindow(IntPtr window, IntPtr value);
    [DllImport("user32.dll")] private static extern bool EnumWindows(EnumWindow callback, IntPtr value);
    [DllImport("user32.dll")] private static extern bool EnumChildWindows(IntPtr parent, EnumWindow callback, IntPtr value);
    [DllImport("user32.dll")] private static extern uint GetWindowThreadProcessId(IntPtr window, out uint pid);
    [DllImport("user32.dll")] private static extern int GetDlgCtrlID(IntPtr window);
    [DllImport("user32.dll")] private static extern bool IsWindowVisible(IntPtr window);
    [DllImport("user32.dll")] private static extern bool IsWindowEnabled(IntPtr window);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] private static extern int GetWindowTextLengthW(IntPtr window);
    [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern IntPtr SendMessageTimeoutW(IntPtr window, uint message, IntPtr word,
        string value, uint flags, uint milliseconds, out IntPtr result);
    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool PostMessageW(IntPtr window, uint message, IntPtr word, IntPtr value);

    private static void Require(bool condition)
    {
        if (!condition) throw new InvalidOperationException();
        if (!WritingFailure && Elapsed.ElapsedMilliseconds >= 240000) throw new System.TimeoutException();
    }

    private static void Wait(Func<bool> condition, int milliseconds)
    {
        long deadline = Elapsed.ElapsedMilliseconds + milliseconds;
        while (Elapsed.ElapsedMilliseconds < deadline)
        {
            Require(true);
            if (condition()) return;
            Thread.Sleep(100);
        }
        throw new System.TimeoutException();
    }

    private static string Text(Dictionary<string, object> value, string key)
    {
        object item;
        Require(value.TryGetValue(key, out item) && item is string);
        return (string)item;
    }

    private static void AdmitPrivateDirectory()
    {
        Require(String.Equals(Path.GetFullPath(AppDomain.CurrentDomain.BaseDirectory).TrimEnd('\\'),
            Root, StringComparison.OrdinalIgnoreCase));
        Require((File.GetAttributes(Root) & FileAttributes.ReparsePoint) == 0);
        DirectorySecurity security = Directory.GetAccessControl(Root,
            AccessControlSections.Owner | AccessControlSections.Access);
        string owner = security.GetOwner(typeof(SecurityIdentifier)).Value;
        Require(owner == "S-1-5-18" || owner == "S-1-5-32-544");
        Require(security.AreAccessRulesProtected);
        foreach (FileSystemAccessRule rule in security.GetAccessRules(true, true, typeof(SecurityIdentifier)))
        {
            if (rule.AccessControlType != AccessControlType.Allow) continue;
            string sid = rule.IdentityReference.Value;
            Require(sid == "S-1-5-18" || sid == "S-1-5-32-544");
        }
    }

    private static string HashFile(string path)
    {
        Require((File.GetAttributes(path) & (FileAttributes.Directory | FileAttributes.ReparsePoint)) == 0);
        using (SHA256 sha = SHA256.Create())
        using (FileStream stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read))
            return BitConverter.ToString(sha.ComputeHash(stream)).Replace("-", "").ToLowerInvariant();
    }

    private static IntPtr Form(int processId)
    {
        IntPtr result = IntPtr.Zero;
        EnumWindows(delegate(IntPtr window, IntPtr unused)
        {
            uint pid; GetWindowThreadProcessId(window, out pid);
            if (pid == processId && IsWindowVisible(window) && Control(window, 1008) != IntPtr.Zero)
            { result = window; return false; }
            return true;
        }, IntPtr.Zero);
        return result;
    }

    private static IntPtr Control(IntPtr form, int id)
    {
        IntPtr result = IntPtr.Zero;
        EnumChildWindows(form, delegate(IntPtr window, IntPtr unused)
        {
            if (GetDlgCtrlID(window) == id) { result = window; return false; }
            return true;
        }, IntPtr.Zero);
        return result;
    }

    private static void SetText(IntPtr form, int id, string value)
    {
        IntPtr control = Control(form, id), result;
        Require(control != IntPtr.Zero && IsWindowVisible(control) && IsWindowEnabled(control));
        Require(SendMessageTimeoutW(control, 0x000C, IntPtr.Zero, value, 2, 1000, out result) != IntPtr.Zero);
    }

    private static void Click(IntPtr form, int id)
    {
        IntPtr control = Control(form, id);
        Require(control != IntPtr.Zero && IsWindowVisible(control) && IsWindowEnabled(control));
        Require(PostMessageW(control, 0x00F5, IntPtr.Zero, IntPtr.Zero));
    }

    private static Dictionary<string, object> Http(string route, object payload, string token, int expected, int timeout)
    {
        Require(route.StartsWith("/api/v1/", StringComparison.Ordinal));
        HttpStatus = 0;
        HttpWebRequest request = (HttpWebRequest)WebRequest.Create(Origin + route);
        request.Proxy = null; request.AllowAutoRedirect = false; request.Timeout = timeout;
        request.ReadWriteTimeout = timeout; request.Method = payload == null ? "GET" : "POST";
        request.Headers["Origin"] = Origin;
        if (token != null) request.Headers["Authorization"] = "Bearer " + token;
        if (payload != null)
        {
            byte[] body = Encoding.UTF8.GetBytes(Json.Serialize(payload));
            try
            {
                request.ContentType = "application/json"; request.ContentLength = body.Length;
                using (Stream stream = request.GetRequestStream()) stream.Write(body, 0, body.Length);
            }
            finally { Array.Clear(body, 0, body.Length); }
        }
        HttpWebResponse received;
        try { received = (HttpWebResponse)request.GetResponse(); }
        catch (WebException error)
        {
            using (HttpWebResponse failed = error.Response as HttpWebResponse)
                if (failed != null) HttpStatus = (int)failed.StatusCode;
            throw;
        }
        using (HttpWebResponse response = received)
        {
            HttpStatus = (int)response.StatusCode;
            Require(HttpStatus == expected);
            using (Stream stream = response.GetResponseStream())
            using (MemoryStream bytes = new MemoryStream())
            {
                byte[] buffer = new byte[4096]; int count;
                while ((count = stream.Read(buffer, 0, buffer.Length)) > 0)
                { Require(bytes.Length + count <= 65536); bytes.Write(buffer, 0, count); }
                object document = Json.DeserializeObject(Encoding.UTF8.GetString(bytes.ToArray()));
                Require(document is Dictionary<string, object>);
                return (Dictionary<string, object>)document;
            }
        }
    }

    private static bool Healthy()
    {
        try { return Text(Http("/api/v1/health", null, null, 200, 3000), "status") == "ok"; }
        catch (WebException) { return false; }
    }

    private static string Totp(string secret)
    {
        Require(Regex.IsMatch(secret, "^[A-Z2-7]{32}$"));
        const string alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
        byte[] seed = new byte[20], message = new byte[8], digest = null;
        try
        {
            int value = 0, bits = 0, position = 0;
            foreach (char symbol in secret)
            {
                value = (value << 5) | alphabet.IndexOf(symbol); bits += 5;
                if (bits >= 8)
                {
                    bits -= 8; seed[position++] = (byte)(value >> bits);
                    value &= (1 << bits) - 1;
                }
            }
            Require(position == seed.Length && bits == 0);
            long ticks = (DateTime.UtcNow - new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc)).Ticks;
            Require(ticks >= 0); ulong counter = (ulong)(ticks / 300000000L);
            for (int index = 7; index >= 0; index--) { message[index] = (byte)(counter & 255); counter >>= 8; }
            using (HMACSHA1 hmac = new HMACSHA1(seed)) digest = hmac.ComputeHash(message);
            int offset = digest[digest.Length - 1] & 15;
            uint number = ((uint)(digest[offset] & 127) << 24) | ((uint)digest[offset + 1] << 16)
                | ((uint)digest[offset + 2] << 8) | digest[offset + 3];
            return (number % 1000000).ToString("D6", System.Globalization.CultureInfo.InvariantCulture);
        }
        finally
        {
            Array.Clear(seed, 0, seed.Length); Array.Clear(message, 0, message.Length);
            if (digest != null) Array.Clear(digest, 0, digest.Length);
        }
    }

    private static void EnableMfa(string token, string password)
    {
        Dictionary<string, object> enrollment = null, confirmed = null;
        try
        {
            Stage = "mfa_enrollment";
            enrollment = Http("/api/v1/operator/security/mfa/enroll",
                new Dictionary<string, object> { { "current_password", password } }, token, 200, 10000);
            string seed = Text(enrollment, "secret");
            Stage = "mfa_confirmation";
            confirmed = Http("/api/v1/operator/security/mfa/confirm", new Dictionary<string, object> {
                { "enrollment_token", Text(enrollment, "enrollment_token") }, { "code", Totp(seed) }
            }, token, 200, 10000);
            seed = null;
            Require(confirmed.ContainsKey("mfa_enabled") && confirmed["mfa_enabled"] is bool && (bool)confirmed["mfa_enabled"]);
            Require(confirmed.ContainsKey("reauthentication_required") && confirmed["reauthentication_required"] is bool && (bool)confirmed["reauthentication_required"]);
            // Recovery codes are intentionally neither inspected nor persisted.
            Receipt["mfa_enabled_before_reboot"] = true;
            Receipt["mfa_confirmed_via_normal_api"] = true;
        }
        finally
        {
            if (enrollment != null) enrollment.Clear();
            if (confirmed != null) confirmed.Clear();
        }
    }

    private static long RecordingBytes(string path)
    {
        if (!Directory.Exists(path)) return 0;
        long bytes = 0; int files = 0, directories = 0;
        Queue<string> pending = new Queue<string>(); pending.Enqueue(path);
        while (pending.Count != 0)
        {
            string directory = pending.Dequeue(); Require(++directories <= 64);
            Require((File.GetAttributes(directory) & FileAttributes.ReparsePoint) == 0);
            foreach (string child in Directory.GetDirectories(directory)) pending.Enqueue(child);
            foreach (string file in Directory.GetFiles(directory, "*.mp4"))
            {
                Require(++files <= 1024);
                Require((File.GetAttributes(file) & FileAttributes.ReparsePoint) == 0);
                bytes = checked(bytes + new FileInfo(file).Length);
            }
        }
        Require(bytes >= 0 && bytes < 10L * 1024 * 1024 * 1024);
        return bytes;
    }

    private static bool ServiceRunning()
    {
        using (ServiceController service = new ServiceController("K5VisionRecorder"))
        { service.Refresh(); return service.Status == ServiceControllerStatus.Running; }
    }

    private static uint ServicePid()
    {
        IntPtr manager = OpenSCManagerW(null, null, 1);
        Require(manager != IntPtr.Zero);
        IntPtr service = IntPtr.Zero;
        try
        {
            service = OpenServiceW(manager, "K5VisionRecorder", 4);
            Require(service != IntPtr.Zero);
            ServiceStatus status; int needed;
            Require(QueryServiceStatusEx(service, 0, out status, Marshal.SizeOf(typeof(ServiceStatus)), out needed));
            Require(status.State == 4 && status.Pid > 0);
            return status.Pid;
        }
        finally
        {
            if (service != IntPtr.Zero) CloseServiceHandle(service);
            CloseServiceHandle(manager);
        }
    }

    private static void SaveReceipt()
    {
        AdmitPrivateDirectory();
        string temporary = Root + @"\bootstrap-receipt.pending";
        Require(!File.Exists(ReceiptPath) && !File.Exists(temporary));
        byte[] bytes = new UTF8Encoding(false).GetBytes(Json.Serialize(Receipt));
        using (FileStream stream = new FileStream(temporary, FileMode.CreateNew, FileAccess.Write, FileShare.None))
        { stream.Write(bytes, 0, bytes.Length); stream.Flush(true); }
        File.Move(temporary, ReceiptPath);
    }

    [STAThread]
    private static int Main(string[] arguments)
    {
        Process client = null; bool admitted = false; string password = null, token = null;
        try
        {
            Require(arguments.Length == 0 && Environment.Is64BitProcess);
            AdmitPrivateDirectory(); admitted = true;
            Require(!File.Exists(ReceiptPath) && !File.Exists(Root + @"\bootstrap-receipt.pending"));
            string input = Root + @"\bootstrap-request.json";
            Require(new FileInfo(input).Length <= 8192 && (File.GetAttributes(input) & FileAttributes.ReparsePoint) == 0);
            Dictionary<string, object> request = Json.DeserializeObject(File.ReadAllText(input)) as Dictionary<string, object>;
            Require(request != null && request.Count == 6 && request.ContainsKey("schema_version") && request["schema_version"] is int && (int)request["schema_version"] == 1);
            string head = Text(request, "head_sha"), setupHash = Text(request, "setup_sha256");
            string source = Text(request, "source_uri"), transfer = Text(request, "transfer_id");
            Require(Regex.IsMatch(head, "^[a-f0-9]{40}$") && Regex.IsMatch(setupHash, "^[a-f0-9]{64}$"));
            Require(Regex.IsMatch(transfer, "^[A-Za-z0-9_-]{16,80}$") && Text(request, "receipt_path") == ReceiptPath);
            Uri camera; IPAddress address;
            Require(Uri.TryCreate(source, UriKind.Absolute, out camera) && camera.Scheme == "rtsp" && camera.Port == 18554 && camera.UserInfo == "" && camera.Query == "" && camera.Fragment == "");
            Require(IPAddress.TryParse(camera.Host, out address) && address.AddressFamily == System.Net.Sockets.AddressFamily.InterNetwork);
            byte[] octets = address.GetAddressBytes(); Require(octets[0] == 172 && octets[1] == 29 && octets[3] == 1);
            Receipt["head_sha"] = head; Receipt["setup_sha256"] = setupHash; Receipt["transfer_id"] = transfer;
            Stage = "interactive_token";
            using (WindowsIdentity identity = WindowsIdentity.GetCurrent())
                Require(new WindowsPrincipal(identity).IsInRole(WindowsBuiltInRole.Administrator));
            using (Process current = Process.GetCurrentProcess())
            { Require(current.SessionId > 0); Receipt["bootstrap_session_id"] = current.SessionId; }
            Receipt["interactive_administrator"] = true;
            foreach (string name in new[] { "GITHUB_TOKEN", "GH_TOKEN", "ACTIONS_RUNTIME_TOKEN", "ACTIONS_ID_TOKEN_REQUEST_TOKEN" })
                Require(String.IsNullOrEmpty(Environment.GetEnvironmentVariable(name)));
            Stage = "product_binding";
            Require(HashFile(Root + @"\K5VisionSetup.exe") == setupHash);
            string installed = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles), "K5 Vision");
            string profile = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData), "K5Vision");
            Require(!File.Exists(Path.Combine(profile, @"identity\identity.json")));
            string manifestPath = Path.Combine(installed, "payload-manifest.json");
            Require(new FileInfo(manifestPath).Length <= 1024 * 1024);
            JavaScriptSerializer manifestJson = new JavaScriptSerializer { MaxJsonLength = 1024 * 1024, RecursionLimit = 16 };
            Dictionary<string, object> manifest = manifestJson.DeserializeObject(File.ReadAllText(manifestPath)) as Dictionary<string, object>;
            Require(manifest != null && Text(manifest, "head_sha") == head);
            string recordingRoot = Path.Combine(profile, "recordings");
            byte[] random = new byte[32];
            using (RandomNumberGenerator generator = RandomNumberGenerator.Create()) generator.GetBytes(random);
            password = Convert.ToBase64String(random); Array.Clear(random, 0, random.Length);
            string username = "guest-bootstrap-admin";
            Stage = "gui_start";
            client = Process.Start(new ProcessStartInfo(Path.Combine(installed, "K5Vision.exe"), "--no-browser")
                { UseShellExecute = false, CreateNoWindow = true, WorkingDirectory = installed });
            Require(client != null && client.Handle != IntPtr.Zero);
            IntPtr form = IntPtr.Zero;
            Wait(delegate { Require(!client.HasExited); form = Form(client.Id); return form != IntPtr.Zero; }, 30000);
            Stage = "gui_setup";
            Wait(delegate { return IsWindowEnabled(Control(form, 1003)); }, 60000);
            SetText(form, 1008, username); SetText(form, 1001, password); SetText(form, 1002, password); Click(form, 1003);
            Stage = "health"; Wait(Healthy, 60000);
            Wait(delegate { return GetWindowTextLengthW(Control(form, 1001)) == 0 && GetWindowTextLengthW(Control(form, 1002)) == 0; }, 10000);
            Receipt["gui_admin_setup"] = true;
            Stage = "login";
            Dictionary<string, object> login = Http("/api/v1/auth/login", new Dictionary<string, object> { { "username", username }, { "password", password } }, null, 200, 3000);
            token = Text(login, "session_token"); Require(Regex.IsMatch(token, "^[A-Za-z0-9_-]{32,128}$"));
            Receipt["http_admin_authenticated"] = true;
            Stage = "stream_probe";
            Dictionary<string, object> probe = Http("/api/v1/operator/camera-stream-probe", new Dictionary<string, object> { { "source_uri", source }, { "username", "" }, { "password", "" } }, token, 200, 20000);
            IList profiles = probe["profiles"] as IList; Require(profiles != null && profiles.Count == 1);
            Dictionary<string, object> selected = profiles[0] as Dictionary<string, object>;
            Require(selected != null && Text(selected, "codec") == "H264" && selected["supported"] is bool && (bool)selected["supported"]);
            Stage = "stream_enrollment";
            Dictionary<string, object> enrolled = Http("/api/v1/operator/cameras/stream-connect", new Dictionary<string, object> { { "name", "Generated boot witness" }, { "probe_id", Text(probe, "probe_id") }, { "profile_id", Text(selected, "id") } }, token, 201, 30000);
            string device = Text(enrolled, "device_id"); Guid parsed; Require(Guid.TryParse(device, out parsed) && parsed != Guid.Empty);
            Require(Text(enrolled, "connection_type") == "rtsp"); Receipt["device_id"] = device; Receipt["camera_source_enrolled"] = true;
            Stage = "recording";
            long before = RecordingBytes(recordingRoot); long began = Elapsed.ElapsedMilliseconds;
            Wait(delegate { return Elapsed.ElapsedMilliseconds - began >= 2000 && RecordingBytes(recordingRoot) > before && ServiceRunning(); }, 30000);
            Receipt["recording_bytes_before"] = before; Receipt["recording_bytes_after"] = RecordingBytes(recordingRoot);
            // This is the final authenticated API mutation. Confirmation
            // revokes the session; later checks use only SCM and file reads.
            EnableMfa(token, password); password = null; token = null; login.Clear();
            uint servicePid = ServicePid(); Receipt["service_pid_before_gui_quit"] = servicePid;
            Stage = "gui_quit"; Click(form, 1005); Require(client.WaitForExit(10000) && client.ExitCode == 0);
            Receipt["gui_client_closed"] = true;
            Stage = "postquit_recording";
            long closedBefore = RecordingBytes(recordingRoot); began = Elapsed.ElapsedMilliseconds;
            Wait(delegate { return Elapsed.ElapsedMilliseconds - began >= 2000 && RecordingBytes(recordingRoot) > closedBefore && ServiceRunning(); }, 30000);
            Receipt["post_quit_recording_bytes_before"] = closedBefore; Receipt["post_quit_recording_bytes_after"] = RecordingBytes(recordingRoot);
            uint servicePidAfter = ServicePid(); Require(servicePidAfter == servicePid);
            Receipt["service_pid_after_gui_quit"] = servicePidAfter;
            Receipt["service_running"] = ServiceRunning(); Receipt["status"] = "bootstrap_complete";
            Receipt["duration_seconds"] = Math.Round(Elapsed.Elapsed.TotalSeconds, 3);
            SaveReceipt(); return 0;
        }
        catch (Exception error)
        {
            WritingFailure = true;
            Receipt["status"] = "bootstrap_failed"; Receipt["stage"] = Stage;
            string type = error.GetType().Name;
            HashSet<string> allowed = new HashSet<string>(new[] { "InvalidOperationException", "TimeoutException",
                "WebException", "IOException", "UnauthorizedAccessException", "ArgumentException", "FormatException",
                "CryptographicException", "SecurityException", "OverflowException", "Win32Exception", "FileNotFoundException" });
            Receipt["failure_type"] = allowed.Contains(type) ? type : "Other";
            Receipt["failure_hresult"] = unchecked((uint)error.HResult);
            Receipt["http_status"] = HttpStatus;
            WebException network = error as WebException;
            if (network != null) Receipt["transport_status"] = network.Status.ToString();
            Receipt["duration_seconds"] = Math.Round(Elapsed.Elapsed.TotalSeconds, 3);
            if (admitted) { try { SaveReceipt(); } catch { } }
            return 1;
        }
        finally
        {
            password = null; token = null;
            // Never kill the recorder or an arbitrary GUI PID. A failed guest
            // bootstrap remains a failure for the outer owner to retire.
            if (client != null) client.Dispose();
        }
    }
}
