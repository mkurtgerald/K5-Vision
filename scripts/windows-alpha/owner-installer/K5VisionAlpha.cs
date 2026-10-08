using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Reflection;
using System.Windows.Forms;

namespace K5VisionAlpha
{
    internal sealed class Launcher : Form
    {
        private readonly TextBox sourceBox = new TextBox();
        private readonly TextBox statusBox = new TextBox();
        private readonly Button runButton = new Button();
        private Process active;
        private volatile string failure = "The K5 test could not complete.";

        internal Launcher()
        {
            Text = "K5 Vision Alpha";
            Font = new Font("Segoe UI", 10);
            ClientSize = new Size(650, 375);
            MinimumSize = new Size(650, 375);
            StartPosition = FormStartPosition.CenterScreen;
            MaximizeBox = false;

            var title = new Label
            {
                Text = "K5 Vision  |  Windows Alpha",
                Font = new Font("Segoe UI", 16, FontStyle.Bold),
                Location = new Point(24, 22), Size = new Size(590, 36)
            };
            var explanation = new Label
            {
                Text = "Test a generated video source or paste a credential-free public RTSP URL.",
                Location = new Point(24, 72), Size = new Size(590, 25)
            };
            var label = new Label
            {
                Text = "Public RTSP URL (blank = local synthetic video)",
                Location = new Point(24, 108), Size = new Size(590, 24)
            };
            sourceBox.Location = new Point(24, 136);
            sourceBox.Size = new Size(594, 25);
            sourceBox.MaxLength = 2048;
            runButton.Text = "Run test";
            runButton.Location = new Point(24, 180);
            runButton.Size = new Size(155, 36);
            runButton.Click += RunTest;
            statusBox.Location = new Point(24, 236);
            statusBox.Size = new Size(594, 112);
            statusBox.Multiline = true;
            statusBox.ReadOnly = true;
            statusBox.ScrollBars = ScrollBars.Vertical;
            statusBox.Text = "Ready. Recording is disabled for these tests.";
            Controls.AddRange(new Control[] { title, explanation, label, sourceBox, runButton, statusBox });
            FormClosing += OnClosing;
        }

        private static string Quote(string value)
        {
            // Windows argv escaping: double backslashes only when adjacent
            // to a quote or at the end of the quoted argument.
            var builder = new System.Text.StringBuilder("\"");
            int slashes = 0;
            foreach (char c in value)
            {
                if (c == '\\') { slashes++; continue; }
                if (c == '"')
                {
                    builder.Append('\\', 2 * slashes + 1);
                    builder.Append('"');
                }
                else
                {
                    builder.Append('\\', slashes);
                    builder.Append(c);
                }
                slashes = 0;
            }
            builder.Append('\\', 2 * slashes);
            builder.Append('"');
            return builder.ToString();
        }

        private void Status(string message)
        {
            if (IsDisposed || Disposing) return;
            if (InvokeRequired)
            {
                try { BeginInvoke(new Action<string>(Status), message); }
                catch (InvalidOperationException) { }
                return;
            }
            if (statusBox.TextLength > 2400)
                statusBox.Text = "Earlier status omitted." + Environment.NewLine;
            statusBox.AppendText(Environment.NewLine + message);
        }

        private void RunTest(object sender, EventArgs args)
        {
            string source = sourceBox.Text.Trim();
            if (source.Length != 0)
            {
                Uri uri;
                if (!Uri.TryCreate(source, UriKind.Absolute, out uri) ||
                    !String.Equals(uri.Scheme, "rtsp", StringComparison.OrdinalIgnoreCase) ||
                    uri.UserInfo.Length != 0 || String.IsNullOrWhiteSpace(uri.Host) ||
                    uri.Fragment.Length != 0)
                {
                    MessageBox.Show(this,
                        "Provide a credential-free rtsp:// URL. K5 also refuses private or local camera addresses.",
                        "Invalid test source", MessageBoxButtons.OK, MessageBoxIcon.Warning);
                    return;
                }
            }

            string root = Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location);
            string launcher = Path.Combine(root, "Start-K5VisionAlpha.ps1");
            if (!File.Exists(launcher))
            {
                MessageBox.Show(this, "Installed K5 runtime is missing. Please reinstall K5 Vision Alpha.",
                    "Install repair required", MessageBoxButtons.OK, MessageBoxIcon.Error);
                return;
            }

            runButton.Enabled = false;
            sourceBox.Enabled = false;
            failure = "Check the installer or try a different public RTSP stream.";
            Status("Starting a bounded non-recording operator test...");

            var info = new ProcessStartInfo();
            info.FileName = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System),
                @"WindowsPowerShell\v1.0\powershell.exe");
            info.Arguments = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File " + Quote(launcher) +
                " -ExitAfterPublicTest" + (source.Length == 0 ? "" : " -PublicRtspSource " + Quote(source));
            info.WorkingDirectory = root;
            info.UseShellExecute = false;
            info.CreateNoWindow = true;
            info.WindowStyle = ProcessWindowStyle.Hidden;
            info.RedirectStandardInput = true;
            info.RedirectStandardOutput = true;
            info.RedirectStandardError = true;
            active = new Process();
            active.StartInfo = info;
            active.EnableRaisingEvents = true;
            active.OutputDataReceived += delegate(object s, DataReceivedEventArgs e)
            {
                if (String.IsNullOrWhiteSpace(e.Data)) return;
                string line = e.Data;
                // Fixed-status projection only; never display the raw URI, credentials or log.
                if (line.Contains("health check PASS")) Status("K5 local service health passed.");
                if (line.StartsWith("K5 analytics PASS:", StringComparison.Ordinal))
                    Status("Analytics completed with zero reported failures.");
                if (line.StartsWith("K5 analytics rendered boxes:", StringComparison.Ordinal))
                    Status("Rendered detection-box receipt produced.");
                if (line.StartsWith("K5 operator PASS:", StringComparison.Ordinal))
                    Status("Operator video presentation completed.");
                if (line.Contains("No test-stream recording or retained media"))
                    Status("No test media was retained.");
            };
            active.ErrorDataReceived += delegate(object s, DataReceivedEventArgs e)
            {
                if (String.IsNullOrEmpty(e.Data)) return;
                if (e.Data.IndexOf("GStreamer", StringComparison.OrdinalIgnoreCase) >= 0)
                    failure = "GStreamer runtime validation failed; repair the installation.";
                else if (e.Data.IndexOf("RTSP", StringComparison.OrdinalIgnoreCase) >= 0)
                    failure = "The RTSP test source could not be decoded or validated.";
                else if (e.Data.IndexOf("analytics", StringComparison.OrdinalIgnoreCase) >= 0)
                    failure = "Analytics did not meet the Stage-One acceptance gate.";
                else if (e.Data.IndexOf("health", StringComparison.OrdinalIgnoreCase) >= 0)
                    failure = "The local K5 service did not pass its health check.";
            };
            active.Exited += delegate(object s, EventArgs e)
            {
                int code = -1;
                try { code = active.ExitCode; } catch (InvalidOperationException) { }
                int result = code;
                if (IsDisposed || Disposing) return;
                try
                {
                    BeginInvoke(new Action(delegate
                    {
                        Status(result == 0 ? "Test completed." :
                            "Test did not pass: " + failure + " (exit " + result + ")");
                        runButton.Enabled = true;
                        sourceBox.Enabled = true;
                        active.Dispose();
                        active = null;
                    }));
                }
                catch (InvalidOperationException) { }
            };
            try
            {
                active.Start();
                active.StandardInput.Close();
                active.BeginOutputReadLine();
                active.BeginErrorReadLine();
            }
            catch (Exception)
            {
                active.Dispose();
                active = null;
                runButton.Enabled = true;
                sourceBox.Enabled = true;
                MessageBox.Show(this, "Unable to launch the installed K5 runtime. Repair the installation.",
                    "Launch failed", MessageBoxButtons.OK, MessageBoxIcon.Error);
            }
        }

        private void OnClosing(object sender, FormClosingEventArgs args)
        {
            if (active != null)
            {
                args.Cancel = true;
                Status("Test is finishing and securely cleaning up. Close this window after completion.");
            }
        }

        [STAThread]
        private static int Main(string[] args)
        {
            if (args.Length == 1 && args[0] == "--self-check")
            {
                string root = Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location);
                string gst = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                    @"K5RunnerTools\k5-gstreamer\1.28.7\msvc_x86_64\bin\gst-launch-1.0.exe");
                return File.Exists(Path.Combine(root, @"python312\python.exe")) &&
                    File.Exists(Path.Combine(root, @".venv\Scripts\python.exe")) &&
                    File.Exists(Path.Combine(root, "Start-K5VisionAlpha.ps1")) &&
                    File.Exists(Path.Combine(root, "k5-revision.txt")) &&
                    File.Exists(gst) ? 0 : 3;
            }
            if (args.Length != 0) return 2;
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            Application.Run(new Launcher());
            return 0;
        }
    }
}
