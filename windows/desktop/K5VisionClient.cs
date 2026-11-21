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
                using (RecorderWindow window = new RecorderWindow(noBrowser, setup)) Application.Run(window);
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
        private readonly Button create = new Button(), open = new Button(), quit = new Button(), approveTime = new Button();
        private readonly NotifyIcon tray = new NotifyIcon();
        private readonly System.Windows.Forms.Timer timer = new System.Windows.Forms.Timer();
        private bool checkedPayload, busy, disposed, ready, openedBrowser, legacyProfile;
        private string phase = "preparing";

        internal RecorderWindow(bool noBrowser, bool setupAdministrator)
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
            open.Text = "Open camera wall"; open.Name = "OpenOperator";
            open.Location = new Point(30, 383); open.Size = new Size(220, 42); open.Enabled = false; SetId(open, 1004);
            open.FlatStyle = FlatStyle.Flat; open.FlatAppearance.BorderSize = 0;
            open.BackColor = Color.FromArgb(27, 109, 159); open.ForeColor = Color.White;
            open.UseVisualStyleBackColor = false; open.Font = new Font("Segoe UI", 10, FontStyle.Bold);
            open.Click += delegate { OpenDashboard(); }; Controls.Add(open);
            quit.Text = "Close K5 Vision"; quit.Name = "CloseK5Vision";
            quit.Location = new Point(340, 389); quit.Size = new Size(180, 36); SetId(quit, 1005);
            quit.Click += delegate { Close(); }; Controls.Add(quit);
            ContextMenuStrip menu = new ContextMenuStrip();
            menu.Items.Add("Open camera wall", null, delegate { ShowOwner(); OpenDashboard(); });
            menu.Items.Add("Show K5 Vision", null, delegate { ShowOwner(); });
            menu.Items.Add("Close K5 Vision (recording continues)", null, delegate { Close(); });
            tray.Icon = SystemIcons.Application; Icon = SystemIcons.Application; tray.Text = "K5 Vision";
            tray.ContextMenuStrip = menu; tray.Visible = true;
            tray.DoubleClick += delegate { ShowOwner(); OpenDashboard(); };
            timer.Interval = 1500; timer.Tick += delegate { RefreshStatus(); };
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
                setup.Visible = false; create.Visible = false; help.Visible = true;
                if (phase == "account_required")
                {
                    if (legacyProfile)
                    {
                        phase = "migration_required";
                        status.Text = "Your existing K5 site needs a verified migration before this service can be used. Your account, cameras and recordings have been preserved.";
                        help.Text = "This candidate cannot import an existing user profile yet. Do not create a replacement site. Use the previous installation until an upgrade with verified migration is available.";
                    }
                    else
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
                }
                else if (ready)
                {
                    status.Text = "Recording service is running. Open the camera wall to view cameras, recordings and administration.";
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
            try { Process.Start(new ProcessStartInfo("http://127.0.0.1:8000/operator/") { UseShellExecute = true }); }
            catch { status.Text = "Choose a default browser in Windows Settings, then select Open camera wall."; }
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
        private void ShowOwner() { Show(); WindowState = FormWindowState.Normal; Activate(); BringToFront(); }
        private void Fail(string message) { phase = "failed"; busy = false; status.Text = message; open.Enabled = false; UpdateDiagnostic(); }
        private void UpdateDiagnostic()
        {
            diagnostic.Text = Json.Serializer.Serialize(new Dictionary<string, object> {
                { "schema_version", 1 }, { "phase", phase }, { "ready", ready }, { "exiting", disposed },
                { "service_client", true }, { "backend_present", false }, { "backend_exited", false },
                { "backend_exit_code", null }, { "backend_invalid_output", false }, { "backend_status", phase },
                { "setup_administrator", setupAdministrator } });
        }
        protected override void Dispose(bool disposing)
        {
            if (disposing && !disposed)
            {
                disposed = true; timer.Stop(); timer.Dispose(); tray.Visible = false; tray.Dispose();
                password.Clear(); confirmation.Clear();
            }
            base.Dispose(disposing);
        }
    }
}
