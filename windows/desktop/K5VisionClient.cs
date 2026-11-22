using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Runtime.InteropServices;
using System.Security.AccessControl;
using System.Security.Principal;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;

namespace K5VisionDesktop
{
    internal static class ClientProgram
    {
        [STAThread]
        private static int Main(string[] arguments)
        {
            string root = AppDomain.CurrentDomain.BaseDirectory.TrimEnd('\\');
            // Service and installer paths never construct a graphical window.
            if (arguments.Length == 1 && arguments[0] == "--recorder-host") return RecorderHost.Run();
            if (arguments.Length == 1 && arguments[0] == "--recorder-stop") return RecorderHost.RequestStop();
            if (arguments.Length == 1 && arguments[0] == "--approve-time")
            {
                try
                {
                    if (!IsAdministrator()) return 1;
                    Custody.VerifyPayload(root);
                    Dictionary<string, object> pending = RecorderClient.PendingTime();
                    if ((string)pending["status"] != "pending") return 1;
                    return TimeAdministration.ExecuteService((string)pending["request"], (string)pending["response"]);
                }
                catch { return 1; }
            }
            if (arguments.Length == 1 && arguments[0].StartsWith("--installation-service-", StringComparison.Ordinal))
                return ServiceInstallation.Execute(arguments[0].Substring("--installation-service-".Length), ServiceInstallation.InstallRoot);
            if (arguments.Length == 1 && arguments[0] == "--installation-payload-check")
            { try { Custody.VerifyPayload(root); return 0; } catch { return 1; } }
            if (arguments.Length == 1 && arguments[0] == "--installation-extraction-check")
            { try { Custody.VerifySealedFiles(root); return 0; } catch { return 1; } }
            if (arguments.Length == 1 && arguments[0] == "--installation-machine-extraction-check")
            { try { Custody.VerifySealedFiles(ServiceInstallation.InstallRoot); return 0; } catch { return 1; } }
            if (arguments.Length == 2 && arguments[0] == "--installation-identity")
                return WriteInstallationIdentity(arguments[1]);
            if (arguments.Length == 3 && arguments[0] == "--time-admin")
                return TimeAdministration.Execute(arguments[1], arguments[2]);
            if (arguments.Length == 2 && arguments[0] == "--migrate-legacy-admin")
            {
                Application.EnableVisualStyles();
                Application.SetCompatibleTextRenderingDefault(false);
                try
                {
                    string expected = new SecurityIdentifier(arguments[1]).Value;
                    using (WindowsIdentity identity = WindowsIdentity.GetCurrent())
                        if (!IsAdministrator() || expected != arguments[1] || identity.User.Value != expected)
                            throw new InvalidOperationException();
                    Custody.VerifyPayload(root);
                    if (!MigrationWindow.Available()) throw new InvalidOperationException();
                    using (ClientInstance instance = new ClientInstance(true))
                    {
                        if (!instance.IsPrimary) return 0;
                        using (MigrationWindow window = new MigrationWindow(root, instance.Activation))
                            return window.ShowDialog() == DialogResult.OK ? 0 : 1;
                    }
                }
                catch
                {
                    MessageBox.Show("Open K5 Vision using the Windows account that owns the existing site. " +
                        "Migration needs administrator permission for that same Windows account. " +
                        "The original site has been preserved.", "K5 Vision", MessageBoxButtons.OK, MessageBoxIcon.Warning);
                    return 1;
                }
            }
            bool noBrowser = false, setup = false;
            foreach (string argument in arguments)
            {
                if (argument == "--no-browser" && !noBrowser) noBrowser = true;
                else if (argument == "--setup-admin" && !setup) { setup = true; noBrowser = true; }
                else return 1; // A GUI may not redirect the machine profile or listener.
            }
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            try
            {
                if (setup && !IsAdministrator()) return 1;
                using (ClientInstance instance = new ClientInstance(setup))
                {
                    if (!instance.IsPrimary) return 0;
                    using (RecorderWindow window = new RecorderWindow(noBrowser, setup, instance.Activation))
                        Application.Run(window);
                }
                return 0;
            }
            catch
            {
                MessageBox.Show("K5 Vision could not connect safely to its recording service. " +
                    "Run the K5 Vision installer to repair the application. Existing recordings are preserved.",
                    "K5 Vision", MessageBoxButtons.OK, MessageBoxIcon.Warning);
                return 1;
            }
        }
        internal static bool IsAdministrator()
        {
            return new WindowsPrincipal(WindowsIdentity.GetCurrent()).IsInRole(WindowsBuiltInRole.Administrator);
        }
        private static int WriteInstallationIdentity(string path)
        {
            try
            {
                path = Custody.OrdinaryAbsolute(path);
                if (Path.GetFileName(path) != "k5-desktop-mutex-name.txt") return 1;
                string parent = Path.GetDirectoryName(path); Custody.Admit(parent, parent);
                using (FileStream file = new FileStream(path, FileMode.CreateNew, FileAccess.Write, FileShare.None))
                {
                    byte[] bytes = Encoding.ASCII.GetBytes(ServiceInstallation.InstallationMutex);
                    file.Write(bytes, 0, bytes.Length); file.Flush(true);
                }
                return 0;
            }
            catch { return 1; }
        }
    }

    // Only restore this Windows user's client in this interactive session.
    // Elevated credential entry has its own window; helpers and the recording
    // service never acquire these objects or receive activation commands.
    internal sealed class ClientInstance : IDisposable
    {
        private Mutex mutex;
        private EventWaitHandle activation;
        private bool owned;
        internal bool IsPrimary { get { return owned; } }
        internal EventWaitHandle Activation { get { return activation; } }

        internal ClientInstance(bool setupAdministrator)
        {
            try
            {
                string sid;
                using (WindowsIdentity identity = WindowsIdentity.GetCurrent()) sid = identity.User.Value;
                int session;
                using (Process current = Process.GetCurrentProcess()) session = current.SessionId;
                if (session <= 0) throw new InvalidOperationException();
                string name = "Local\\K5VisionClient.v1." + sid + "." +
                    session.ToString(System.Globalization.CultureInfo.InvariantCulture) +
                    (setupAdministrator ? ".Setup" : ".Main");
                SecurityIdentifier owner = new SecurityIdentifier(sid);
                string[] principals = new[] { sid, "S-1-5-18", "S-1-5-32-544" };
                EventWaitHandleSecurity eventSecurity = new EventWaitHandleSecurity();
                eventSecurity.SetOwner(owner); eventSecurity.SetAccessRuleProtection(true, false);
                foreach (string principal in principals)
                    eventSecurity.AddAccessRule(new EventWaitHandleAccessRule(new SecurityIdentifier(principal),
                        EventWaitHandleRights.FullControl, AccessControlType.Allow));
                // Publish the event before taking the mutex. A simultaneous
                // second launch can always signal the winning client, even
                // before its first window has been constructed.
                bool created;
                activation = new EventWaitHandle(false, EventResetMode.AutoReset, name + ".Activate",
                    out created, eventSecurity);
                Admit(activation.GetAccessControl(), principals);
                MutexSecurity mutexSecurity = new MutexSecurity();
                mutexSecurity.SetOwner(owner); mutexSecurity.SetAccessRuleProtection(true, false);
                foreach (string principal in principals)
                    mutexSecurity.AddAccessRule(new MutexAccessRule(new SecurityIdentifier(principal),
                        MutexRights.FullControl, AccessControlType.Allow));
                mutex = new Mutex(false, name, out created, mutexSecurity);
                Admit(mutex.GetAccessControl(), principals);
                try { owned = mutex.WaitOne(0); }
                catch (AbandonedMutexException) { owned = true; }
                if (!owned) activation.Set();
            }
            catch { Dispose(); throw; }
        }

        private static void Admit(NativeObjectSecurity security, string[] principals)
        {
            string owner = security.GetOwner(typeof(SecurityIdentifier)).Value;
            if (Array.IndexOf(principals, owner) < 0 || !security.AreAccessRulesProtected)
                throw new InvalidOperationException();
            HashSet<string> admitted = new HashSet<string>(StringComparer.Ordinal);
            foreach (AccessRule rule in security.GetAccessRules(true, true, typeof(SecurityIdentifier)))
            {
                string principal = rule.IdentityReference.Value;
                if (rule.IsInherited || rule.AccessControlType != AccessControlType.Allow ||
                    Array.IndexOf(principals, principal) < 0) throw new InvalidOperationException();
                MutexAccessRule mutexRule = rule as MutexAccessRule;
                EventWaitHandleAccessRule eventRule = rule as EventWaitHandleAccessRule;
                if ((mutexRule != null && mutexRule.MutexRights == MutexRights.FullControl) ||
                    (eventRule != null && eventRule.EventWaitHandleRights == EventWaitHandleRights.FullControl))
                    admitted.Add(principal);
                else throw new InvalidOperationException();
            }
            if (admitted.Count != principals.Length) throw new InvalidOperationException();
        }

        public void Dispose()
        {
            if (owned && mutex != null) { owned = false; mutex.ReleaseMutex(); }
            if (mutex != null) { mutex.Dispose(); mutex = null; }
            if (activation != null) { activation.Dispose(); activation = null; }
        }
    }

    // This window is a service client. Its close, exception, disposal, or user
    // session ending has no control channel to stop the recording engine.
    internal sealed class RecorderWindow : Form
    {
        [DllImport("user32.dll", EntryPoint = "SetWindowLongPtrW")]
        private static extern IntPtr SetWindowLongPtr(IntPtr window, int index, IntPtr value);
        private readonly bool noBrowser, setupAdministrator;
        private readonly string root = AppDomain.CurrentDomain.BaseDirectory.TrimEnd('\\');
        private readonly Label status = new Label(), help = new Label();
        private readonly Panel setup = new Panel();
        private readonly TextBox username = new TextBox(), password = new TextBox(), confirmation = new TextBox();
        private readonly TextBox diagnostic = new TextBox();
        private readonly Button create = new Button(), open = new Button(), quit = new Button(), approveTime = new Button(), migrate = new Button();
        private readonly NotifyIcon tray = new NotifyIcon();
        private readonly System.Windows.Forms.Timer timer = new System.Windows.Forms.Timer();
        private readonly System.Windows.Forms.Timer activationTimer = new System.Windows.Forms.Timer();
        private bool checkedPayload, busy, disposed, ready, openedBrowser, legacyProfile, exitRequested;
        private string phase = "preparing";
        private string browserDispatch = "not_requested";
        private int browserAttempts, browserNativeError;

        internal RecorderWindow(bool noBrowser, bool setupAdministrator)
            : this(noBrowser, setupAdministrator, null) { }
        internal RecorderWindow(bool noBrowser, bool setupAdministrator, EventWaitHandle activation)
        {
            this.noBrowser = noBrowser; this.setupAdministrator = setupAdministrator;
            Text = "K5 Vision"; Font = new Font("Segoe UI", 10);
            ClientSize = new Size(560, 450); MinimumSize = MaximumSize = Size;
            MaximizeBox = false; StartPosition = FormStartPosition.CenterScreen;
            BackColor = Color.FromArgb(248, 250, 253);
            Controls.Add(new Label { Text = "Your cameras. Your recordings.", Location = new Point(28, 24),
                Size = new Size(510, 38), Font = new Font("Segoe UI", 18, FontStyle.Bold) });
            status.Location = new Point(30, 78); status.Size = new Size(500, 62); status.Name = "Status";
            status.Text = "Connecting to the recording service…"; SetId(status, 1006); Controls.Add(status);
            help.Location = new Point(30, 157); help.Size = new Size(500, 110);
            help.ForeColor = Color.FromArgb(75, 89, 108);
            help.Text = "The Windows recording service starts automatically when this PC starts. " +
                "Recording continues when you close this app, close the browser, or sign out of Windows.";
            Controls.Add(help);
            diagnostic.Visible = false; diagnostic.ReadOnly = true; diagnostic.Name = "Diagnostic";
            SetId(diagnostic, 1010); Controls.Add(diagnostic); GC.KeepAlive(diagnostic.Handle);
            setup.Location = new Point(30, 146); setup.Size = new Size(500, 226); setup.Visible = false;
            AddInput("Administrator username", username, 0, false, 1008);
            username.Text = "admin"; username.MaxLength = 64; username.Name = "Username";
            AddInput("Password (at least 12 characters)", password, 61, true, 1001); password.Name = "Password";
            AddInput("Confirm password", confirmation, 122, true, 1002); confirmation.Name = "ConfirmPassword";
            create.Text = "Set up administrator"; create.Name = "CreateAccount";
            create.Location = new Point(30, 285); create.Size = new Size(220, 38); create.Visible = false;
            SetId(create, 1003); create.Click += delegate { CreateAccount(); }; Controls.Add(create);
            setup.Controls.Add(create); Controls.Add(setup);
            approveTime.Text = "Approve time change"; approveTime.Name = "ApproveTimeChange";
            approveTime.Location = new Point(30, 285); approveTime.Size = new Size(240, 38);
            approveTime.Visible = false; SetId(approveTime, 1011);
            approveTime.Click += delegate { ApproveTime(); }; Controls.Add(approveTime);
            migrate.Text = "Move existing K5 site"; migrate.Name = "MigrateExistingSite";
            migrate.Location = new Point(30, 285); migrate.Size = new Size(250, 38);
            migrate.Visible = false; SetId(migrate, 1012);
            migrate.Click += delegate { MigrateLegacy(); }; Controls.Add(migrate);
            open.Text = "Open camera wall"; open.Name = "OpenOperator";
            open.Location = new Point(30, 383); open.Size = new Size(220, 42); open.Enabled = false; SetId(open, 1004);
            open.FlatStyle = FlatStyle.Flat; open.FlatAppearance.BorderSize = 0;
            open.BackColor = Color.FromArgb(27, 109, 159); open.ForeColor = Color.White;
            open.UseVisualStyleBackColor = false; open.Font = new Font("Segoe UI", 10, FontStyle.Bold);
            open.Click += delegate { OpenDashboard(); }; Controls.Add(open);
            quit.Text = "Close K5 Vision"; quit.Name = "CloseK5Vision";
            quit.Location = new Point(340, 389); quit.Size = new Size(180, 36); SetId(quit, 1005);
            quit.Click += delegate { ExitClient(); }; Controls.Add(quit);
            ContextMenuStrip menu = new ContextMenuStrip();
            menu.Items.Add("Open camera wall", null, delegate { ShowOwner(); OpenDashboard(); });
            menu.Items.Add("Show K5 Vision", null, delegate { ShowOwner(); });
            menu.Items.Add("Close K5 Vision (recording continues)", null, delegate { ExitClient(); });
            tray.Icon = SystemIcons.Application; Icon = SystemIcons.Application; tray.Text = "K5 Vision";
            tray.ContextMenuStrip = menu; tray.Visible = true;
            tray.DoubleClick += delegate { ShowOwner(); OpenDashboard(); };
            timer.Interval = 1500; timer.Tick += delegate { RefreshStatus(); };
            if (activation != null)
            {
                activationTimer.Interval = 200;
                activationTimer.Tick += delegate
                {
                    if (!disposed && activation.WaitOne(0))
                    {
                        ShowOwner();
                        if (!noBrowser) OpenDashboard();
                    }
                };
                activationTimer.Start();
            }
            Shown += delegate { Prepare(); };
            UpdateDiagnostic();
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
        private static Dictionary<string, object> Request(string operation)
        {
            return new Dictionary<string, object> { { "schema_version", 1 }, { "operation", operation } };
        }
        private async void Prepare()
        {
            busy = true;
            try
            {
                await Task.Run(delegate { Custody.VerifyPayload(root); });
                if (disposed) return;
                checkedPayload = true;
                string legacy = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                    "K5VisionOperatorState", "identity");
                legacyProfile = Directory.Exists(legacy) || File.Exists(legacy);
                busy = false; timer.Start(); RefreshStatus();
            }
            catch { if (!disposed) Fail("K5 Vision could not verify its installation. Run the downloaded installer to repair it. Existing recordings are preserved."); }
        }
        private async void RefreshStatus()
        {
            if (busy || disposed || !checkedPayload) return;
            busy = true;
            try
            {
                Dictionary<string, object> result = await Task.Run(delegate { return RecorderClient.Exchange(Request("status"), false); });
                if (disposed) return;
                if (Convert.ToInt32(result["schema_version"]) != 1 || Convert.ToInt32(result["port"]) != 8000)
                    throw new InvalidOperationException();
                phase = (string)result["status"]; ready = phase == "running"; open.Enabled = ready;
                approveTime.Visible = result.ContainsKey("time_approval_pending") &&
                    result["time_approval_pending"] is bool && (bool)result["time_approval_pending"];
                setup.Visible = false; create.Visible = false; migrate.Visible = false; help.Visible = true;
                if (phase == "migration_required" || (phase == "account_required" && legacyProfile))
                {
                    phase = "migration_required";
                    status.Text = "Move your existing K5 site before using the recording service. Your original account, cameras and recordings are preserved.";
                    migrate.Visible = legacyProfile; migrate.Enabled = legacyProfile;
                    help.Text = legacyProfile ?
                        "Close the previous K5 Vision app, then select Move existing K5 site. Sign in with its K5 administrator account to copy the site and start recording with the Windows service." :
                        "Sign in to Windows using the account that owns the existing K5 site, then open K5 Vision. No replacement site will be created.";
                }
                else if (phase == "account_required")
                {
                        bool admin = ClientProgram.IsAdministrator();
                        setup.Visible = admin; help.Visible = !admin;
                        Control parent = admin ? (Control)setup : this;
                        if (create.Parent != parent) create.Parent = parent;
                        create.Location = admin ? new Point(0, 185) : new Point(30, 285);
                        create.Text = admin ? "Create account" : "Set up administrator";
                        create.Visible = true; create.Enabled = true;
                        status.Text = admin ? "Create your administrator account to get started." : "This PC needs its first K5 administrator account.";
                        if (!admin) help.Text = "Select Set up administrator and approve the Windows permission prompt. Recording then runs independently of signed-in users.";
                }
                else if (ready)
                {
                    status.Text = browserDispatch == "failed" ?
                        "Recording service is running. Windows could not open your browser. Choose a default browser in Windows Settings, then select Open camera wall." :
                        "Recording service is running. Open the camera wall to view cameras, recordings and administration.";
                    help.Text = "The Windows recording service starts automatically when this PC starts. Recording continues when you close this app, close the browser, or sign out of Windows.";
                    tray.Text = "K5 Vision — recording service running";
                    if (!openedBrowser && !noBrowser) { openedBrowser = true; OpenDashboard(); }
                }
                else if (phase == "preparing_recordings")
                {
                    int progress = Convert.ToInt32(result["progress"]);
                    if (progress < 0 || progress > 100) throw new InvalidOperationException();
                    status.Text = "Preparing recordings (" + progress + "%). Original footage is preserved.";
                }
                else if (phase == "migrating")
                {
                    status.Text = "Moving your existing K5 site. Keep the migration window open.";
                    help.Text = "The original site is preserved while K5 checks accounts, copies recordings and prepares the recording service.";
                }
                else if (phase == "starting" || phase == "initializing") status.Text = "The recording service is preparing the camera engine…";
                else if (phase == "stopping") status.Text = "The recording service is stopping for maintenance.";
                else if (phase == "unavailable") status.Text = "The recording service is recovering. This window will reconnect automatically.";
                else throw new InvalidOperationException();
            }
            catch
            {
                if (!disposed)
                {
                    phase = "unavailable"; ready = false; open.Enabled = false;
                    status.Text = "The recording service is unavailable. Run the K5 Vision installer to repair it. Existing recordings are preserved.";
                }
            }
            finally { busy = false; if (!disposed) UpdateDiagnostic(); }
        }
        private async void CreateAccount()
        {
            if (busy || disposed || !checkedPayload || legacyProfile) return;
            if (!ClientProgram.IsAdministrator())
            {
                try
                {
                    Process.Start(new ProcessStartInfo(Path.Combine(root, "K5Vision.exe"), "--setup-admin")
                        { UseShellExecute = true, Verb = "runas", WorkingDirectory = root });
                    status.Text = "Complete administrator setup in the Windows permission window. This app will reconnect automatically.";
                }
                catch { status.Text = "Administrator setup was not opened. Select Set up administrator to try again."; }
                return;
            }
            if (password.Text.Length < 12 || password.Text.Length > 256 || password.Text != confirmation.Text)
            { status.Text = "Enter matching passwords with 12 to 256 characters."; return; }
            busy = true; create.Enabled = false; phase = "initializing";
            Dictionary<string, object> request = Request("initialize");
            request["username"] = username.Text; request["password"] = password.Text; request["confirmation"] = confirmation.Text;
            password.Clear(); confirmation.Clear(); status.Text = "Creating your administrator account…";
            try
            {
                Dictionary<string, object> result = await Task.Run(delegate
                {
                    try { return RecorderClient.Exchange(request, true); }
                    finally { request["password"] = request["confirmation"] = null; }
                });
                if (disposed) return;
                if ((string)result["status"] != "ready")
                {
                    string reason = result.ContainsKey("reason") ? (string)result["reason"] : "";
                    status.Text = reason == "invalid_username" ? "Choose a username using letters, numbers, dots, dashes or underscores." :
                        "Administrator setup could not complete safely. Existing data has been preserved.";
                }
                else { setup.Visible = false; create.Visible = false; status.Text = "Administrator created. The recording service is starting…"; }
            }
            catch { if (!disposed) status.Text = "Administrator setup could not complete. Close and reopen K5 Vision to try again."; }
            finally { busy = false; if (!disposed) { create.Enabled = true; UpdateDiagnostic(); } }
        }
        private void OpenDashboard()
        {
            if (!ready || disposed) return;
            browserAttempts = Math.Min(1000, browserAttempts + 1); browserNativeError = 0;
            try
            {
                using (Process launched = Process.Start(new ProcessStartInfo("http://127.0.0.1:8000/operator/")
                    { UseShellExecute = true })) { }
                browserDispatch = "dispatched";
            }
            catch (System.ComponentModel.Win32Exception error)
            {
                browserDispatch = "failed";
                browserNativeError = error.NativeErrorCode >= 0 && error.NativeErrorCode <= 65535 ? error.NativeErrorCode : 0;
            }
            catch { browserDispatch = "failed"; }
            if (browserDispatch == "failed")
                status.Text = "Choose a default browser in Windows Settings, then select Open camera wall.";
            UpdateDiagnostic();
        }
        private void ApproveTime()
        {
            if (!checkedPayload || disposed || !approveTime.Visible) return;
            try
            {
                Process.Start(new ProcessStartInfo(Path.Combine(root, "K5Vision.exe"), "--approve-time")
                    { UseShellExecute = true, Verb = "runas", WorkingDirectory = root });
                status.Text = "Approve the Windows permission prompt to complete the pending time change.";
            }
            catch { status.Text = "Time approval was not opened. Select Approve time change to try again before the request expires."; }
        }
        private void MigrateLegacy()
        {
            if (!checkedPayload || disposed || !legacyProfile || phase != "migration_required") return;
            try
            {
                string sid;
                using (WindowsIdentity identity = WindowsIdentity.GetCurrent()) sid = identity.User.Value;
                using (Process launched = Process.Start(new ProcessStartInfo(Path.Combine(root, "K5Vision.exe"),
                    "--migrate-legacy-admin " + sid) { UseShellExecute = true, Verb = "runas", WorkingDirectory = root })) { }
                status.Text = "Complete the migration in the Windows permission window. This app will reconnect to the recording service.";
            }
            catch { status.Text = "Migration was not opened. Select Move existing K5 site to try again."; }
        }
        private void ExitClient() { exitRequested = true; Close(); }
        protected override void OnFormClosing(FormClosingEventArgs e)
        {
            if (!exitRequested && e.CloseReason == CloseReason.UserClosing)
            {
                e.Cancel = true;
                Hide();
            }
            base.OnFormClosing(e);
        }
        private void ShowOwner() { Show(); WindowState = FormWindowState.Normal; Activate(); BringToFront(); }
        private void Fail(string message) { phase = "failed"; busy = false; status.Text = message; open.Enabled = false; UpdateDiagnostic(); }
        private void UpdateDiagnostic()
        {
            diagnostic.Text = Json.Serializer.Serialize(new Dictionary<string, object> {
                { "schema_version", 1 }, { "phase", phase }, { "ready", ready }, { "exiting", disposed },
                { "service_client", true }, { "backend_present", false }, { "backend_exited", false },
                { "backend_exit_code", null }, { "backend_invalid_output", false }, { "backend_status", phase },
                { "browser_dispatch", browserDispatch }, { "browser_attempts", browserAttempts },
                { "browser_native_error", browserNativeError },
                { "setup_administrator", setupAdministrator } });
        }
        protected override void Dispose(bool disposing)
        {
            if (disposing && !disposed)
            {
                disposed = true; activationTimer.Stop(); activationTimer.Dispose();
                timer.Stop(); timer.Dispose(); tray.Visible = false; tray.Dispose();
                password.Clear(); confirmation.Clear();
            }
            base.Dispose(disposing);
        }
    }

    internal sealed class MigrationWindow : Form
    {
        [DllImport("user32.dll", EntryPoint = "SetWindowLongPtrW")]
        private static extern IntPtr SetWindowLongPtr(IntPtr window, int index, IntPtr value);
        private readonly string root;
        private readonly TextBox username = new TextBox(), password = new TextBox(), factor = new TextBox();
        private readonly Label status = new Label();
        private readonly Button start = new Button(), cancel = new Button();
        private readonly object workerGate = new object();
        private readonly System.Windows.Forms.Timer activationTimer = new System.Windows.Forms.Timer();
        private MigrationWorker worker;
        private Task cancellationTask;
        private volatile bool cancelRequested, disposed;
        private bool running, closeRequested, allowClose;

        internal static bool Available()
        {
            try
            {
                if (!ClientProgram.IsAdministrator()) return false;
                string data = Custody.MachineDataRoot;
                Custody.AdmitMigrationData(data);
                string identity = Path.Combine(data, "identity");
                if (File.Exists(identity)) return false;
                if (Directory.Exists(identity)) Custody.AdmitMigrationData(identity);
                // A pending transaction may already have published a manifest.
                // Only the admitted service's guarded state can offer recovery;
                // the target journal decides whether anything can be activated.
                Dictionary<string, object> state = RecorderClient.Exchange(Request("status"), false);
                if (!(state["schema_version"] is int) || (int)state["schema_version"] != 1 ||
                    !Object.Equals(state["status"], "migration_required") ||
                    !(state["port"] is int) || (int)state["port"] != 8000) return false;
                string legacy = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                    "K5VisionOperatorState", "identity");
                Custody.Admit(legacy, legacy);
                string source = Path.Combine(legacy, "identity.json");
                Custody.Admit(source, legacy);
                return File.Exists(source);
            }
            catch { return false; }
        }

        internal MigrationWindow(string root) : this(root, null) { }
        internal MigrationWindow(string root, EventWaitHandle activation)
        {
            this.root = root;
            Text = "Move your K5 site"; Font = new Font("Segoe UI", 10);
            ClientSize = new Size(580, 470); MinimumSize = MaximumSize = Size;
            MaximizeBox = false; MinimizeBox = false; StartPosition = FormStartPosition.CenterScreen;
            BackColor = Color.FromArgb(248, 250, 253);
            Controls.Add(new Label { Text = "Keep your cameras and recordings", Location = new Point(28, 22),
                Size = new Size(525, 38), Font = new Font("Segoe UI", 17, FontStyle.Bold) });
            Controls.Add(new Label { Text = "Close the previous K5 Vision app before continuing. Sign in with its K5 " +
                "administrator account. K5 keeps the original site while it checks and copies your data.",
                Location = new Point(30, 72), Size = new Size(520, 72) });
            AddInput("Existing K5 administrator username", username, 148, false, 1101);
            username.MaxLength = 64; username.Name = "LegacyAdministratorUsername";
            AddInput("Existing K5 password", password, 210, true, 1102);
            password.Name = "LegacyAdministratorPassword";
            AddInput("Authenticator or recovery code (if enabled)", factor, 272, true, 1103);
            factor.MaxLength = 64; factor.Name = "LegacyAdministratorFactor";
            status.Location = new Point(30, 340); status.Size = new Size(520, 60);
            status.Text = "Your original site will be preserved."; status.Name = "MigrationStatus";
            SetId(status, 1106); Controls.Add(status);
            start.Text = "Move existing site"; start.Name = "BeginMigration";
            start.Location = new Point(30, 414); start.Size = new Size(250, 38);
            start.BackColor = Color.FromArgb(27, 109, 159); start.ForeColor = Color.White;
            start.FlatStyle = FlatStyle.Flat; start.FlatAppearance.BorderSize = 0;
            start.UseVisualStyleBackColor = false; SetId(start, 1104);
            start.Click += delegate { BeginMigration(); }; Controls.Add(start);
            cancel.Text = "Cancel"; cancel.Name = "CancelMigration";
            cancel.Location = new Point(340, 414); cancel.Size = new Size(210, 38);
            SetId(cancel, 1105); cancel.Click += delegate { RequestClose(); }; Controls.Add(cancel);
            AcceptButton = start; CancelButton = cancel;
            // Modal CancelButton normally assigns DialogResult.Cancel itself.
            // Wait for the owned actor to retire before allowing this close.
            cancel.DialogResult = DialogResult.None;
            if (activation != null)
            {
                activationTimer.Interval = 200;
                activationTimer.Tick += delegate
                {
                    if (!disposed && activation.WaitOne(0))
                    { Show(); WindowState = FormWindowState.Normal; Activate(); BringToFront(); }
                };
                activationTimer.Start();
            }
        }

        private static void SetId(Control control, int id)
        { control.HandleCreated += delegate { SetWindowLongPtr(control.Handle, -12, new IntPtr(id)); }; }
        private void AddInput(string label, TextBox input, int y, bool secret, int id)
        {
            Controls.Add(new Label { Text = label, Location = new Point(30, y), Size = new Size(520, 23) });
            input.Location = new Point(30, y + 24); input.Size = new Size(520, 27);
            input.MaxLength = 256; input.UseSystemPasswordChar = secret; SetId(input, id); Controls.Add(input);
        }
        private static Dictionary<string, object> Request(string operation)
        { return new Dictionary<string, object> { { "schema_version", 1 }, { "operation", operation } }; }
        private void Progress(string phase)
        {
            string message;
            switch (phase)
            {
                case "checking_site": message = "Checking your existing site…"; break;
                case "copying_accounts": message = "Copying accounts and camera settings…"; break;
                case "checking_recordings": message = "Checking retained recordings and exports…"; break;
                case "copying_recordings": message = "Copying recordings and exports. Large sites can take time…"; break;
                case "verifying_copy": message = "Verifying the copy before starting the recording service…"; break;
                default: throw new InvalidOperationException();
            }
            if (disposed || !IsHandleCreated) return;
            try { BeginInvoke((Action)delegate { if (!disposed && !cancelRequested) status.Text = message; }); }
            catch (InvalidOperationException) { if (!disposed) throw; }
        }
        private Dictionary<string, object> RunActor(Dictionary<string, object> credentials)
        {
            MigrationWorker active = null;
            try
            {
                if (!Available() || cancelRequested) throw new OperationCanceledException();
                active = new MigrationWorker(root, false);
                lock (workerGate) worker = active;
                if (cancelRequested) { active.Cancel(); throw new OperationCanceledException(); }
                try { active.Send(credentials); }
                finally { credentials.Clear(); }
                while (true)
                {
                    if (cancelRequested) throw new OperationCanceledException();
                    Dictionary<string, object> message = active.Receive();
                    if (!message.ContainsKey("schema_version") || !(message["schema_version"] is int) ||
                        (int)message["schema_version"] != 1 || !message.ContainsKey("operation") ||
                        !(message["operation"] is string)) throw new InvalidOperationException();
                    string operation = (string)message["operation"];
                    if (operation == "progress" && message.Count == 3 && message.ContainsKey("phase") &&
                        message["phase"] is string)
                    {
                        Progress((string)message["phase"]);
                        active.Send(new Dictionary<string, object> { { "schema_version", 1 }, { "status", "ok" } });
                    }
                    else if (operation == "service" && message.Count == 3 && message.ContainsKey("request") &&
                        message["request"] is Dictionary<string, object>)
                    {
                        Dictionary<string, object> request = (Dictionary<string, object>)message["request"];
                        Dictionary<string, object> reply = null;
                        try { reply = RecorderClient.Migration(request); active.Send(reply); }
                        finally { request.Clear(); if (reply != null) reply.Clear(); message.Clear(); }
                    }
                    else if (operation == "result" && message.Count == 4 && message.ContainsKey("status") &&
                        message.ContainsKey("reason") && message["status"] is string && message["reason"] is string)
                    {
                        string state = (string)message["status"], reason = (string)message["reason"];
                        if ((state == "ready" && reason == "migration_complete") ||
                            (state == "refused" && (reason == "close_previous_app" ||
                            reason == "authentication_not_accepted" || reason == "migration_unavailable"))) return message;
                        throw new InvalidOperationException();
                    }
                    else throw new InvalidOperationException();
                }
            }
            finally
            {
                credentials.Clear();
                lock (workerGate) { if (worker == active) worker = null; }
                if (active != null) active.Dispose();
            }
        }
        private async void BeginMigration()
        {
            if (running || disposed) return;
            if (username.Text.Length < 3 || password.Text.Length == 0)
            { status.Text = "Enter your existing K5 administrator username and password."; return; }
            Dictionary<string, object> credentials = Request("authorize");
            credentials["username"] = username.Text; credentials["password"] = password.Text;
            credentials["code"] = factor.Text;
            password.Clear(); factor.Clear();
            running = true; cancelRequested = false; closeRequested = false; cancellationTask = null;
            username.Enabled = password.Enabled = factor.Enabled = start.Enabled = false;
            status.Text = "Checking your existing administrator account…";
            Dictionary<string, object> result = null;
            try { result = await Task.Run(delegate { return RunActor(credentials); }); }
            catch { /* Only finite operator messages are shown below. */ }
            finally { credentials.Clear(); }
            Task pendingCancellation = cancellationTask;
            if (pendingCancellation != null) { try { await pendingCancellation; } catch { } }
            if (disposed) return;
            running = false;
            bool complete = result != null && (string)result["status"] == "ready";
            if (complete)
            {
                status.Text = "Your site has moved. The recording service is starting…";
                allowClose = true; DialogResult = DialogResult.OK; Close(); return;
            }
            if (closeRequested) { allowClose = true; DialogResult = DialogResult.Cancel; Close(); return; }
            string reason = result != null ? (string)result["reason"] : "";
            status.Text = reason == "close_previous_app" ? "Close the previous K5 Vision app, then try again." :
                reason == "authentication_not_accepted" ? "The administrator sign-in was not accepted. Check the password and any authenticator or recovery code." :
                "The migration could not be confirmed. Your original site is preserved. Reopen K5 Vision to check the recorder.";
            username.Enabled = password.Enabled = factor.Enabled = true;
            bool available = await Task.Run(delegate { return Available(); });
            if (!disposed) start.Enabled = available;
        }
        private void RequestClose()
        {
            if (!running) { allowClose = true; DialogResult = DialogResult.Cancel; Close(); return; }
            closeRequested = true; cancelRequested = true;
            MigrationWorker active;
            lock (workerGate) active = worker;
            // Cancel is intentionally outside both actor and publication locks.
            if (active != null) active.Cancel();
            if (cancellationTask == null)
                cancellationTask = Task.Run(delegate { try { RecorderClient.Migration(Request("cancel")); } catch { } });
            status.Text = "Cancelling and checking whether the migration has finished. Your original site is preserved…";
            cancel.Enabled = false;
        }
        protected override void OnFormClosing(FormClosingEventArgs e)
        {
            if (running && !allowClose && e.CloseReason == CloseReason.UserClosing)
            { e.Cancel = true; RequestClose(); }
            else if (running && !allowClose) RequestClose();
            base.OnFormClosing(e);
        }
        protected override void Dispose(bool disposing)
        {
            if (disposing && !disposed)
            {
                disposed = true; cancelRequested = true;
                MigrationWorker active;
                lock (workerGate) active = worker;
                if (active != null) active.Cancel();
                activationTimer.Stop(); activationTimer.Dispose(); password.Clear(); factor.Clear();
            }
            base.Dispose(disposing);
        }
    }
}
