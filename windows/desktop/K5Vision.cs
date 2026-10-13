using System;
using System.Collections;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Linq;
using System.Net;
using System.Runtime.InteropServices;
using System.Security.AccessControl;
using System.Security.Cryptography;
using System.Security.Principal;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows.Forms;

namespace K5VisionDesktop
{
    internal static class Program
    {
        internal static readonly string Sid = WindowsIdentity.GetCurrent().User.Value;
        internal static readonly string MutexName = "Global\\K5VisionDesktop-" + Sid;
        internal static readonly string EventName = MutexName + "-Open";

        [STAThread]
        private static int Main(string[] arguments)
        {
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            if (arguments.Length == 1 && arguments[0] == "--installation-check")
                return InstallationCheck();
            if (arguments.Length == 2 && arguments[0] == "--installation-identity")
            {
                try
                {
                    string output = Custody.OrdinaryAbsolute(arguments[1]);
                    if (Path.GetFileName(output) != "k5-desktop-mutex-name.txt") return 1;
                    string parent = Path.GetDirectoryName(output);
                    Custody.Admit(parent, parent);
                    using (FileStream file = new FileStream(output, FileMode.CreateNew, FileAccess.Write, FileShare.None))
                    {
                        byte[] name = Encoding.ASCII.GetBytes(MutexName);
                        file.Write(name, 0, name.Length); file.Flush(true);
                    }
                    return 0;
                }
                catch { return 1; }
            }
            Mutex mutex = null;
            EventWaitHandle open = null;
            bool acquired = false;
            try
            {
                Options options = Options.Parse(arguments);
                bool created;
                MutexSecurity security = new MutexSecurity();
                security.SetOwner(new SecurityIdentifier(Sid));
                foreach (string identity in new[] { Sid, "S-1-5-18", "S-1-5-32-544" })
                    security.AddAccessRule(new MutexAccessRule(new SecurityIdentifier(identity),
                        MutexRights.FullControl, AccessControlType.Allow));
                mutex = new Mutex(false, MutexName, out created, security);
                AssertObjectOwner(mutex.GetAccessControl().GetOwner(typeof(SecurityIdentifier)));
                try { acquired = mutex.WaitOne(0); }
                catch (AbandonedMutexException) { acquired = true; }
                if (!acquired)
                {
                    using (EventWaitHandle existing = EventWaitHandle.OpenExisting(EventName,
                        EventWaitHandleRights.Modify | EventWaitHandleRights.ReadPermissions))
                    {
                        AssertObjectOwner(existing.GetAccessControl().GetOwner(typeof(SecurityIdentifier)));
                        existing.Set();
                    }
                    return 0;
                }
                EventWaitHandleSecurity eventSecurity = new EventWaitHandleSecurity();
                eventSecurity.SetOwner(new SecurityIdentifier(Sid));
                foreach (string identity in new[] { Sid, "S-1-5-18", "S-1-5-32-544" })
                    eventSecurity.AddAccessRule(new EventWaitHandleAccessRule(new SecurityIdentifier(identity),
                        EventWaitHandleRights.FullControl, AccessControlType.Allow));
                open = new EventWaitHandle(false, EventResetMode.AutoReset, EventName, out created, eventSecurity);
                AssertObjectOwner(open.GetAccessControl().GetOwner(typeof(SecurityIdentifier)));
                using (OperatorWindow window = new OperatorWindow(options, open))
                    Application.Run(window);
                return 0;
            }
            catch
            {
                MessageBox.Show("K5 Vision could not start safely. Close any running K5 Vision app, then try again. " +
                    "Your account and recordings have been kept.", "K5 Vision", MessageBoxButtons.OK, MessageBoxIcon.Warning);
                return 1;
            }
            finally
            {
                if (open != null) open.Dispose();
                if (acquired && mutex != null) mutex.ReleaseMutex();
                if (mutex != null) mutex.Dispose();
            }
        }

        private static void AssertObjectOwner(IdentityReference identity)
        {
            if (identity.Value != Sid && identity.Value != "S-1-5-18" && identity.Value != "S-1-5-32-544")
                throw new InvalidOperationException();
        }

        private static int InstallationCheck()
        {
            try
            {
                using (Mutex mutex = Mutex.OpenExisting(MutexName, MutexRights.Synchronize | MutexRights.Modify))
                {
                    bool owned;
                    try { owned = mutex.WaitOne(0); }
                    catch (AbandonedMutexException) { owned = true; }
                    if (owned) mutex.ReleaseMutex();
                    return owned ? 0 : 2;
                }
            }
            catch (WaitHandleCannotBeOpenedException) { return 0; }
            catch { return 2; }
        }
    }

    internal sealed class Options
    {
        internal string Profile = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
            "K5VisionOperatorState");
        internal int Port = 8000;
        internal bool NoBrowser;

        internal static Options Parse(string[] arguments)
        {
            Options result = new Options();
            HashSet<string> seen = new HashSet<string>();
            for (int index = 0; index < arguments.Length; index++)
            {
                string argument = arguments[index];
                if (!seen.Add(argument)) throw new ArgumentException();
                if (argument == "--no-browser") result.NoBrowser = true;
                else if (argument == "--profile" && ++index < arguments.Length)
                    result.Profile = arguments[index];
                else if (argument == "--port" && ++index < arguments.Length)
                {
                    if (!Int32.TryParse(arguments[index], out result.Port) || result.Port < 1024 || result.Port > 65535)
                        throw new ArgumentException();
                }
                else throw new ArgumentException();
            }
            Custody.OrdinaryAbsolute(result.Profile);
            return result;
        }
    }

    internal static class Custody
    {
        private const string TrustedInstaller = "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464";
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern uint QueryDosDevice(string name, StringBuilder target, int maximum);

        internal static string OrdinaryAbsolute(string path)
        {
            if (String.IsNullOrEmpty(path) || path.Length > 4096 || path.IndexOfAny(new[] { '\0', '\r', '\n', '"' }) >= 0 ||
                path.Length < 3 || path[1] != ':' || path[2] != '\\' ||
                path.Split('\\', '/').Any(part => part == ".." || part.EndsWith(".") || part.EndsWith(" ")))
                throw new InvalidOperationException();
            string full = Path.GetFullPath(path).TrimEnd('\\');
            if (full.Length < 3) full += "\\";
            StringBuilder device = new StringBuilder(512);
            if (QueryDosDevice(full.Substring(0, 2), device, device.Capacity) == 0 ||
                !device.ToString().StartsWith("\\Device\\HarddiskVolume", StringComparison.Ordinal) ||
                new DriveInfo(Path.GetPathRoot(full)).DriveType != DriveType.Fixed)
                throw new InvalidOperationException();
            return full;
        }

        internal static void Admit(string path, string privateRoot)
        {
            path = OrdinaryAbsolute(path);
            privateRoot = OrdinaryAbsolute(privateRoot);
            string current = path;
            while (!String.IsNullOrEmpty(current))
            {
                FileAttributes attributes = File.GetAttributes(current);
                if ((attributes & FileAttributes.ReparsePoint) != 0) throw new InvalidOperationException();
                bool directory = (attributes & FileAttributes.Directory) != 0;
                FileSystemSecurity security = directory
                    ? (FileSystemSecurity)Directory.GetAccessControl(current, AccessControlSections.Access | AccessControlSections.Owner)
                    : File.GetAccessControl(current, AccessControlSections.Access | AccessControlSections.Owner);
                string owner = security.GetOwner(typeof(SecurityIdentifier)).Value;
                HashSet<string> trusted = new HashSet<string>(new[] { Program.Sid, "S-1-5-18", "S-1-5-32-544", TrustedInstaller });
                if (!trusted.Contains(owner)) throw new InvalidOperationException();
                bool privateBoundary = String.Equals(current, privateRoot, StringComparison.OrdinalIgnoreCase) ||
                    current.StartsWith(privateRoot + "\\", StringComparison.OrdinalIgnoreCase);
                foreach (FileSystemAccessRule rule in security.GetAccessRules(true, true, typeof(SecurityIdentifier)))
                {
                    if (rule.AccessControlType != AccessControlType.Allow ||
                        (rule.PropagationFlags & PropagationFlags.InheritOnly) != 0) continue;
                    string sid = rule.IdentityReference.Value;
                    if (sid == "S-1-3-4") sid = owner;
                    if (!trusted.Contains(sid) && ((uint)rule.FileSystemRights &
                        (privateBoundary ? UInt32.MaxValue : 0x010D0150u)) != 0)
                        throw new InvalidOperationException();
                }
                current = Path.GetDirectoryName(current);
            }
        }

        internal static string Sha256(byte[] bytes)
        {
            using (SHA256 algorithm = SHA256.Create())
                return BitConverter.ToString(algorithm.ComputeHash(bytes)).Replace("-", "").ToLowerInvariant();
        }

        internal static void VerifyPayload(string root)
        {
            Admit(root, root);
            string manifestPath = Path.Combine(root, "payload-manifest.json");
            Admit(manifestPath, root);
            if (new FileInfo(manifestPath).Length > 4 * 1024 * 1024) throw new InvalidOperationException();
            byte[] bytes = File.ReadAllBytes(manifestPath);
            if (Sha256(bytes) != PayloadIdentity.ManifestSha256) throw new InvalidOperationException();
            Dictionary<string, object> manifest = Json.Parse(Encoding.UTF8.GetString(bytes));
            if (Convert.ToInt32(manifest["schema_version"]) != 1 ||
                (string)manifest["head_sha"] != PayloadIdentity.HeadSha) throw new InvalidOperationException();
            HashSet<string> admitted = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            foreach (object entry in (IEnumerable)manifest["files"])
            {
                Dictionary<string, object> file = (Dictionary<string, object>)entry;
                string relative = (string)file["path"];
                if (relative.Length == 0 || relative.Contains("\\") || relative.StartsWith("/") ||
                    relative.Split('/').Any(part => part == ".." || part == "." || part.Length == 0) ||
                    !admitted.Add(relative)) throw new InvalidOperationException();
                string target = Path.Combine(root, relative.Replace('/', '\\'));
                Admit(target, root);
                using (FileStream stream = new FileStream(target, FileMode.Open, FileAccess.Read, FileShare.Read))
                using (SHA256 hash = SHA256.Create())
                {
                    if (stream.Length != Convert.ToInt64(file["bytes"]) ||
                        BitConverter.ToString(hash.ComputeHash(stream)).Replace("-", "").ToLowerInvariant() != (string)file["sha256"])
                        throw new InvalidOperationException();
                }
            }
            if (!admitted.Contains("runtime/python.exe") || !admitted.Contains("runtime/python312._pth") ||
                !admitted.Contains("workers/mediamtx.exe") || !admitted.Contains("workers/caddy.exe"))
                throw new InvalidOperationException();
            foreach (string tree in new[] { "runtime", "workers" })
                VerifyTree(Path.Combine(root, tree), root, admitted);
        }

        private static void VerifyTree(string directory, string root, HashSet<string> admitted)
        {
            Admit(directory, root);
            foreach (string entry in Directory.GetFileSystemEntries(directory))
            {
                Admit(entry, root);
                if (Directory.Exists(entry)) VerifyTree(entry, root, admitted);
                else if (!admitted.Contains(entry.Substring(root.Length + 1).Replace('\\', '/')))
                    throw new InvalidOperationException();
            }
        }

        internal static void PrepareProfile(string profile, string application)
        {
            profile = OrdinaryAbsolute(profile);
            application = OrdinaryAbsolute(application);
            if (profile.Equals(application, StringComparison.OrdinalIgnoreCase) ||
                profile.StartsWith(application + "\\", StringComparison.OrdinalIgnoreCase) ||
                application.StartsWith(profile + "\\", StringComparison.OrdinalIgnoreCase))
                throw new InvalidOperationException();
            if (!Directory.Exists(profile))
            {
                string parent = Path.GetDirectoryName(profile);
                Admit(parent, parent);
                Directory.CreateDirectory(profile);
            }
            Admit(profile, profile);
            string recordings = Path.Combine(profile, "recordings");
            if (!Directory.Exists(recordings)) Directory.CreateDirectory(recordings);
            Admit(recordings, profile);
        }
    }

    internal static class Json
    {
        internal static readonly JavaScriptSerializer Serializer = new JavaScriptSerializer { MaxJsonLength = 4 * 1024 * 1024 };
        internal static Dictionary<string, object> Parse(string text)
        {
            if (text == null || text.Length > 4 * 1024 * 1024) throw new InvalidOperationException();
            return Serializer.Deserialize<Dictionary<string, object>>(text);
        }
    }

    internal sealed class ProcessJob : IDisposable
    {
        [StructLayout(LayoutKind.Sequential)]
        private struct BasicLimits
        {
            internal long PerProcessUserTime, PerJobUserTime;
            internal uint LimitFlags;
            internal UIntPtr MinimumWorkingSet, MaximumWorkingSet;
            internal uint ActiveProcessLimit;
            internal UIntPtr Affinity;
            internal uint PriorityClass, SchedulingClass;
        }
        [StructLayout(LayoutKind.Sequential)]
        private struct ExtendedLimits
        {
            internal BasicLimits Basic;
            internal ulong ReadOperations, WriteOperations, OtherOperations, ReadTransfers, WriteTransfers, OtherTransfers;
            internal UIntPtr ProcessMemory, JobMemory, PeakProcessMemory, PeakJobMemory;
        }
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern IntPtr CreateJobObject(IntPtr attributes, string name);
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool SetInformationJobObject(IntPtr job, int kind, ref ExtendedLimits data, int length);
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);
        [DllImport("kernel32.dll")]
        private static extern bool CloseHandle(IntPtr handle);
        private IntPtr handle;

        internal ProcessJob()
        {
            handle = CreateJobObject(IntPtr.Zero, null);
            ExtendedLimits limits = new ExtendedLimits();
            limits.Basic.LimitFlags = 0x2000;
            if (handle == IntPtr.Zero || !SetInformationJobObject(handle, 9, ref limits, Marshal.SizeOf(limits)))
            { Dispose(); throw new InvalidOperationException(); }
        }
        internal void Assign(Process process)
        {
            if (!AssignProcessToJobObject(handle, process.Handle)) throw new InvalidOperationException();
        }
        public void Dispose()
        {
            if (handle != IntPtr.Zero) { CloseHandle(handle); handle = IntPtr.Zero; }
        }
    }

    internal sealed class Bridge : IDisposable
    {
        internal readonly Process Process;
        internal readonly ManualResetEvent Running = new ManualResetEvent(false);
        private readonly object gate = new object();
        private Dictionary<string, object> outcome;
        internal volatile bool InvalidOutput;
        private int lines;

        internal Bridge(string root, Dictionary<string, object> request, ProcessJob job, bool persistent)
        {
            ProcessStartInfo start = new ProcessStartInfo(Path.Combine(root, "runtime", "python.exe"),
                "-I -B -m k5vision.desktop_setup");
            start.WorkingDirectory = root;
            start.UseShellExecute = false;
            start.CreateNoWindow = true;
            start.RedirectStandardInput = start.RedirectStandardOutput = start.RedirectStandardError = true;
            start.StandardOutputEncoding = start.StandardErrorEncoding = Encoding.UTF8;
            start.EnvironmentVariables.Remove("PYTHONHOME");
            start.EnvironmentVariables.Remove("PYTHONPATH");
            Process = new Process { StartInfo = start };
            Process.OutputDataReceived += Receive;
            Process.ErrorDataReceived += delegate { };
            try
            {
                if (!Process.Start()) throw new InvalidOperationException();
                job.Assign(Process);
                Process.BeginOutputReadLine();
                Process.BeginErrorReadLine();
                byte[] payload = Encoding.UTF8.GetBytes(Json.Serializer.Serialize(request) + "\n");
                try
                {
                    if (payload.Length > 32768) throw new InvalidOperationException();
                    Process.StandardInput.BaseStream.Write(payload, 0, payload.Length);
                    Process.StandardInput.BaseStream.Flush();
                }
                finally { Array.Clear(payload, 0, payload.Length); }
                if (!persistent) Process.StandardInput.Close();
            }
            catch
            {
                try { if (!Process.HasExited) Process.Kill(); } catch { }
                Process.Dispose();
                throw;
            }
        }

        private void Receive(object sender, DataReceivedEventArgs args)
        {
            if (args.Data == null) return;
            lock (gate)
            {
                try
                {
                    if (++lines > 2 || args.Data.Length > 1024) throw new InvalidOperationException();
                    Dictionary<string, object> response = Json.Parse(args.Data);
                    if (Convert.ToInt32(response["schema_version"]) != 1 ||
                        !new[] { "ready", "new", "refused", "running", "stopped" }.Contains((string)response["status"]))
                        throw new InvalidOperationException();
                    outcome = response;
                    if ((string)response["status"] == "running") Running.Set();
                }
                catch { InvalidOutput = true; }
            }
        }

        internal Dictionary<string, object> Complete(int timeout)
        {
            if (!Process.WaitForExit(timeout)) throw new InvalidOperationException();
            Process.WaitForExit();
            lock (gate)
            {
                if (InvalidOutput || outcome == null) throw new InvalidOperationException();
                return outcome;
            }
        }

        internal bool Stop()
        {
            try
            {
                if (!Process.HasExited)
                {
                    Process.StandardInput.WriteLine("stop");
                    Process.StandardInput.Flush();
                    Process.StandardInput.Close();
                }
                Dictionary<string, object> result = Complete(20000);
                return (string)result["status"] == "stopped" && Process.ExitCode == 0;
            }
            catch { return false; }
        }

        public void Dispose()
        {
            try { Process.StandardInput.Close(); } catch { }
            Process.Dispose();
            Running.Dispose();
        }
    }

    internal sealed class OperatorWindow : Form
    {
        [DllImport("user32.dll", EntryPoint = "SetWindowLongPtrW")]
        private static extern IntPtr SetWindowLongPtr(IntPtr window, int index, IntPtr value);
        private readonly Options options;
        private readonly string root = AppDomain.CurrentDomain.BaseDirectory.TrimEnd('\\');
        private readonly Label status = new Label();
        private readonly Panel setup = new Panel();
        private readonly TextBox username = new TextBox();
        private readonly TextBox password = new TextBox();
        private readonly TextBox confirmation = new TextBox();
        private readonly Button create = new Button();
        private readonly Button open = new Button();
        private readonly Button quit = new Button();
        private readonly NotifyIcon tray = new NotifyIcon();
        private readonly System.Windows.Forms.Timer timer = new System.Windows.Forms.Timer();
        private readonly EventWaitHandle openEvent;
        private readonly ProcessJob job = new ProcessJob();
        private Bridge backend;
        private Task operation = Task.FromResult(true);
        private volatile bool exiting;
        private bool busy = true, ready, checkedPayload;

        internal OperatorWindow(Options options, EventWaitHandle openEvent)
        {
            this.options = options;
            this.openEvent = openEvent;
            Text = "K5 Vision";
            Font = new Font("Segoe UI", 10);
            ClientSize = new Size(560, 450);
            MinimumSize = MaximumSize = Size;
            MaximizeBox = false;
            StartPosition = FormStartPosition.CenterScreen;
            BackColor = Color.FromArgb(248, 250, 253);
            Label title = new Label { Text = "Your cameras. Your recordings.", Location = new Point(28, 24),
                Size = new Size(510, 38), Font = new Font("Segoe UI", 18, FontStyle.Bold) };
            Controls.Add(title);
            status.Location = new Point(30, 78); status.Size = new Size(500, 62);
            status.Text = "Preparing K5 Vision…"; status.Name = "Status"; SetId(status, 1006);
            Controls.Add(status);
            setup.Location = new Point(30, 146); setup.Size = new Size(500, 226); setup.Visible = false;
            AddInput("Administrator username", username, 0, false, 1008);
            username.Text = "admin"; username.MaxLength = 64; username.Name = "Username";
            AddInput("Password (at least 12 characters)", password, 61, true, 1001);
            password.Name = "Password";
            AddInput("Confirm password", confirmation, 122, true, 1002);
            confirmation.Name = "ConfirmPassword";
            create.Text = "Create account"; create.Name = "CreateAccount";
            create.Location = new Point(0, 185); create.Size = new Size(166, 36); SetId(create, 1003);
            create.Click += delegate { CreateAccount(); };
            setup.Controls.Add(create); Controls.Add(setup);
            open.Text = "Open cameras"; open.Name = "OpenOperator";
            open.Location = new Point(30, 389); open.Size = new Size(166, 36); open.Enabled = false; SetId(open, 1004);
            open.Click += delegate { OpenDashboard(); }; Controls.Add(open);
            quit.Text = "Stop and quit"; quit.Name = "StopRecording";
            quit.Location = new Point(350, 389); quit.Size = new Size(166, 36); SetId(quit, 1005);
            quit.Click += delegate { StopAndQuit(); }; Controls.Add(quit);
            ContextMenuStrip menu = new ContextMenuStrip();
            menu.Items.Add("Open cameras", null, delegate { ShowOwner(); OpenDashboard(); });
            menu.Items.Add("Show K5 Vision", null, delegate { ShowOwner(); });
            menu.Items.Add("Stop and quit", null, delegate { StopAndQuit(); });
            tray.Icon = SystemIcons.Application; Icon = SystemIcons.Application;
            tray.Text = "K5 Vision"; tray.ContextMenuStrip = menu; tray.Visible = true;
            tray.DoubleClick += delegate { ShowOwner(); OpenDashboard(); };
            timer.Interval = 250;
            timer.Tick += delegate
            {
                if (openEvent.WaitOne(0)) { ShowOwner(); if (!options.NoBrowser) OpenDashboard(); }
                if (ready && backend != null && backend.Process.HasExited)
                { ready = false; open.Enabled = false; status.Text = "Recording stopped unexpectedly. Close and reopen K5 Vision to try again. Your recordings have been kept."; ShowOwner(); }
            };
            timer.Start();
            FormClosing += delegate(object sender, FormClosingEventArgs args)
            {
                if (!exiting && args.CloseReason == CloseReason.UserClosing) { args.Cancel = true; Hide(); }
            };
            Shown += delegate { Prepare(); };
        }

        private static void SetId(Control control, int id)
        {
            control.HandleCreated += delegate { SetWindowLongPtr(control.Handle, -12, new IntPtr(id)); };
        }

        private void AddInput(string label, TextBox input, int y, bool secret, int id)
        {
            setup.Controls.Add(new Label { Text = label, Location = new Point(0, y), Size = new Size(460, 23) });
            input.Location = new Point(0, y + 24); input.Size = new Size(460, 27);
            input.UseSystemPasswordChar = secret; input.MaxLength = 256; SetId(input, id); setup.Controls.Add(input);
        }

        private Dictionary<string, object> Request(string operation)
        {
            return new Dictionary<string, object> { { "schema_version", 1 }, { "operation", operation },
                { "identity_directory", Path.Combine(options.Profile, "identity") } };
        }

        private async void Prepare()
        {
            try
            {
                Task<Dictionary<string, object>> preparation = Task.Run(delegate
                {
                    Custody.VerifyPayload(root); checkedPayload = true;
                    Custody.PrepareProfile(options.Profile, root);
                    if (exiting) throw new InvalidOperationException();
                    using (Bridge bridge = new Bridge(root, Request("status"), job, false)) return bridge.Complete(30000);
                });
                operation = preparation;
                Dictionary<string, object> result = await preparation;
                if (exiting) return;
                if ((string)result["status"] == "new")
                { busy = false; setup.Visible = true; status.Text = "Create your administrator account to get started."; }
                else if ((string)result["status"] == "ready") StartRecording();
                else Fail("K5 Vision could not use this account profile safely. Your existing data has been kept.");
            }
            catch { Fail("K5 Vision could not verify its installation. Reinstall the downloaded setup to try again. Your recordings have been kept."); }
        }

        private async void CreateAccount()
        {
            if (busy || exiting || !checkedPayload) return;
            if (password.Text.Length < 12 || password.Text.Length > 256)
            { status.Text = "Choose a password with 12 to 256 characters."; return; }
            if (password.Text != confirmation.Text)
            { status.Text = "The passwords do not match. Please enter them again."; return; }
            busy = true; create.Enabled = false;
            Dictionary<string, object> request = Request("initialize");
            request["username"] = username.Text; request["password"] = password.Text; request["confirmation"] = confirmation.Text;
            password.Clear(); confirmation.Clear(); status.Text = "Creating your account…";
            try
            {
                Task<Dictionary<string, object>> initialization = Task.Run(delegate
                {
                    try
                    {
                        if (exiting) throw new InvalidOperationException();
                        using (Bridge bridge = new Bridge(root, request, job, false)) return bridge.Complete(30000);
                    }
                    finally { request["password"] = request["confirmation"] = null; }
                });
                operation = initialization;
                Dictionary<string, object> result = await initialization;
                if (exiting) return;
                if ((string)result["status"] == "ready") { setup.Visible = false; StartRecording(); }
                else
                {
                    string reason = result.ContainsKey("reason") ? (string)result["reason"] : "";
                    busy = false; create.Enabled = true;
                    if (reason == "invalid_username") status.Text = "Choose a username using letters, numbers, dots, dashes or underscores.";
                    else if (reason == "password_mismatch" || reason == "invalid_password") status.Text = "Enter matching passwords with 12 to 256 characters.";
                    else Fail("Account setup could not complete safely. Your existing data has been kept.");
                }
            }
            catch { Fail("Account setup could not complete. Close and reopen K5 Vision to try again."); }
        }

        private async void StartRecording()
        {
            if (exiting) return;
            busy = true; setup.Visible = false; status.Text = "Starting your cameras…";
            try
            {
                Task startup = Task.Run(delegate
                {
                    if (exiting) throw new InvalidOperationException();
                    Dictionary<string, object> request = Request("serve");
                    request["recording_root"] = Path.Combine(options.Profile, "recordings");
                    request["media_executable"] = Path.Combine(root, "workers", "mediamtx.exe");
                    request["gateway_executable"] = Path.Combine(root, "workers", "caddy.exe");
                    request["port"] = options.Port; request["recording_limit_gb"] = 20;
                    backend = new Bridge(root, request, job, true);
                    Stopwatch deadline = Stopwatch.StartNew();
                    while (!backend.Running.WaitOne(50))
                        if (exiting || backend.InvalidOutput || backend.Process.HasExited || deadline.ElapsedMilliseconds > 45000)
                            throw new InvalidOperationException();
                    bool healthy = false;
                    while (!healthy && deadline.ElapsedMilliseconds < 45000 && !exiting && !backend.Process.HasExited)
                    {
                        try
                        {
                            HttpWebRequest health = (HttpWebRequest)WebRequest.Create("http://127.0.0.1:" + options.Port + "/api/v1/health");
                            health.Proxy = null; health.AllowAutoRedirect = false; health.Timeout = health.ReadWriteTimeout = 1000;
                            using (HttpWebResponse response = (HttpWebResponse)health.GetResponse())
                                healthy = response.StatusCode == HttpStatusCode.OK;
                        }
                        catch (WebException) { }
                        if (!healthy) Thread.Sleep(100);
                    }
                    if (!healthy) throw new InvalidOperationException();
                });
                operation = startup;
                await startup;
                if (exiting) return;
                ready = true; busy = false; open.Enabled = true;
                status.Text = "Recording service is running. You can close this window; K5 Vision stays in the tray.";
                tray.Text = "K5 Vision — recording service running";
                if (!options.NoBrowser) OpenDashboard();
            }
            catch
            {
                await Task.Run(delegate { if (backend != null && !backend.Stop()) job.Dispose(); });
                Fail("K5 Vision could not start recording. Close any other K5 Vision app and try again. Your recordings have been kept.");
            }
        }

        private void OpenDashboard()
        {
            if (!ready || exiting) return;
            try { Process.Start(new ProcessStartInfo("http://127.0.0.1:" + options.Port + "/operator/") { UseShellExecute = true }); }
            catch { status.Text = "Your browser could not open. Reopen K5 Vision after choosing a default browser in Windows Settings."; }
        }

        private void ShowOwner()
        {
            Show(); WindowState = FormWindowState.Normal; Activate(); BringToFront();
        }

        private void Fail(string message)
        {
            if (exiting || IsDisposed) return;
            busy = false; ready = false; open.Enabled = false; create.Enabled = false;
            status.Text = message; ShowOwner();
        }

        private async void StopAndQuit()
        {
            if (exiting) return;
            exiting = true; ready = false; timer.Stop(); open.Enabled = create.Enabled = quit.Enabled = false;
            status.Text = "Stopping recording safely…";
            Task pending = operation;
            await Task.Run(delegate
            {
                try { pending.Wait(); } catch { }
                if (backend != null) backend.Stop();
            });
            tray.Visible = false; job.Dispose(); Close();
        }

        protected override void Dispose(bool disposing)
        {
            if (disposing)
            {
                exiting = true; timer.Dispose(); tray.Visible = false; tray.Dispose();
                job.Dispose(); if (backend != null) backend.Dispose();
            }
            base.Dispose(disposing);
        }
    }
}
