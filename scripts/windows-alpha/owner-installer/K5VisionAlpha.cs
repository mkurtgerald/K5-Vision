using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Reflection;
using System.Text.RegularExpressions;
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
        private volatile bool healthEvidence, analyticsEvidence, boxesEvidence, presentationEvidence, privacyEvidence;

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
            int missing = MissingRuntimeComponent(Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location));
            if (missing != 0)
            {
                statusBox.Text = "Installation needs repair: " + MissingRuntimeDescription(missing) +
                    ". Reinstall K5 Vision Alpha (check " + missing + ").";
                runButton.Enabled = false;
            }
            FormClosing += OnClosing;
        }

        private static void ConfigureAnalyticsForTest(ProcessStartInfo info, string root, bool publicSource)
        {
            // Alter only the child environment. An inherited owner setting must not
            // enable analytics for generated test patterns or replace the public-test
            // package selection. Missing/invalid public config still fails preflight.
            info.EnvironmentVariables.Remove("K5_ANALYTICS_CONFIG");
            if (publicSource)
            {
                string installedAnalyticsConfig = Path.Combine(root, "analytics-config.json");
                info.EnvironmentVariables["K5_ANALYTICS_CONFIG"] = installedAnalyticsConfig;
            }
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
            int missing = MissingRuntimeComponent(root);
            if (missing != 0)
            {
                MessageBox.Show(this, "Installation needs repair: " + MissingRuntimeDescription(missing) +
                    ". Reinstall K5 Vision Alpha (check " + missing + ").",
                    "Install repair required", MessageBoxButtons.OK, MessageBoxIcon.Error);
                return;
            }
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
            healthEvidence = analyticsEvidence = boxesEvidence = presentationEvidence = privacyEvidence = false;
            Status("Starting a bounded non-recording operator test...");

            var info = new ProcessStartInfo();
            info.FileName = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System),
                @"WindowsPowerShell\v1.0\powershell.exe");
            info.Arguments = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File " + Quote(launcher) +
                " -ExitAfterPublicTest" + (source.Length == 0 ? "" : " -PublicRtspSource " + Quote(source));
            info.WorkingDirectory = root;
            // Synthetic video is an explicitly video-only smoke, never person-detection
            // evidence. Public tests always select the packaged verified analytics config.
            ConfigureAnalyticsForTest(info, root, source.Length != 0);

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
                if (line == "K5 analytics disabled; video-only alpha acceptance selected.")
                {
                    if (source.Length == 0)
                        Status("Synthetic video-only mode selected; public RTSP analytics remains unqualified.");
                    else
                    {
                        failure = "Analytics is not provisioned or configured. This installer cannot pass Stage-One acceptance.";
                        Status(failure);
                    }
                }
                if (line == "K5 Vision Alpha health check PASS.") { healthEvidence = true; Status("K5 local service health passed."); }
                Match a = Regex.Match(line, @"^K5 analytics PASS: submissions=([1-9][0-9]*), completions=([1-9][0-9]*), failures=0$");
                long submitted, completed;
                if (a.Success && Int64.TryParse(a.Groups[1].Value, out submitted) &&
                    Int64.TryParse(a.Groups[2].Value, out completed) && completed <= submitted)
                { analyticsEvidence = true; Status("Positive analytics submissions and completions; zero failures."); }
                Match b = Regex.Match(line, @"^K5 analytics rendered boxes: ([1-9][0-9]*)$");
                long boxCount;
                if (b.Success && Int64.TryParse(b.Groups[1].Value, out boxCount))
                { boxesEvidence = true; Status("Positive rendered detection-box count confirmed."); }
                Match o = Regex.Match(line, @"^K5 operator PASS: frames=([1-9][0-9]*), presentations=([1-9][0-9]*)$");
                long frames, presentations;
                if (o.Success && Int64.TryParse(o.Groups[1].Value, out frames) &&
                    Int64.TryParse(o.Groups[2].Value, out presentations))
                { presentationEvidence = true; Status("Positive operator frames and presentations confirmed."); }
                if (line == "No test-stream recording or retained media was created.")
                { privacyEvidence = true; Status("No test media was retained."); }
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
                Process finished = (Process)s;
                try { finished.WaitForExit(); code = finished.ExitCode; } catch (InvalidOperationException) { }
                int result = code;
                if (IsDisposed || Disposing) return;
                try
                {
                    BeginInvoke(new Action(delegate
                    {
                        bool video = result == 0 && healthEvidence && presentationEvidence && privacyEvidence;
                        bool full = source.Length > 0 && video && analyticsEvidence && boxesEvidence;
                        Status(full ? "Stage-One receipt passed: analytics, boxes and operator presentation confirmed." :
                            source.Length == 0 && video ? "Synthetic video-only smoke passed; public RTSP analytics NOT qualified." :
                            result == 0 ? "Stage-One NOT qualified: missing health, analytics, boxes, presentation or privacy evidence." :
                            "Test did not pass: " + failure + " (exit " + result + ")");
                        runButton.Enabled = true;
                        sourceBox.Enabled = true;
                        finished.Dispose();
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

        // Fixed component codes are safe for CI and the owner GUI. They never
        // expose source URIs, private files, installed paths or credentials.
        private static int MissingRuntimeComponent(string root)
        {
            if (!File.Exists(Path.Combine(root, @"python312\python.exe"))) return 3;
            if (!File.Exists(Path.Combine(root, @".venv\Scripts\python.exe"))) return 4;
            if (!File.Exists(Path.Combine(root, "Start-K5VisionAlpha.ps1"))) return 5;
            if (!File.Exists(Path.Combine(root, "k5-revision.txt"))) return 6;
            if (!File.Exists(Path.Combine(root, "analytics-config.json"))) return 7;
            if (!File.Exists(Path.Combine(root, @"analytics-models\person-detection-retail-0013\FP16\person-detection-retail-0013.xml"))) return 8;
            if (!File.Exists(Path.Combine(root, @"analytics-models\human-pose-estimation-0001\FP16\human-pose-estimation-0001.bin"))) return 9;
            string gst = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                @"K5RunnerTools\k5-gstreamer\1.28.7\msvc_x86_64\bin\gst-launch-1.0.exe");
            if (!File.Exists(gst)) return 10;
            return 0;
        }

        private static string MissingRuntimeDescription(int code)
        {
            switch (code)
            {
                case 3: return "private Python runtime";
                case 4: return "private K5 application environment";
                case 5: return "application launcher";
                case 6: return "exact revision identity";
                case 7: return "analytics configuration";
                case 8: return "person-detection model asset";
                case 9: return "pose-estimation model asset";
                case 10: return "GStreamer video runtime";
                default: return "unknown runtime prerequisite";
            }
        }

        [STAThread]
        private static int Main(string[] args)
        {
            if (args.Length == 1 && args[0] == "--self-check")
            {
                return MissingRuntimeComponent(Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location));
            }
            if (args.Length != 0) return 2;
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            Application.Run(new Launcher());
            return 0;
        }
    }
}
