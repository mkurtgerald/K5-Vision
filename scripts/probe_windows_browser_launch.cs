// Native diagnostic only. Compile with production sources, select this Main.
// The unshown product form never runs Prepare, product Main, SCM or profile I/O.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Drawing.Imaging;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Windows.Automation;
using System.Windows.Forms;

namespace K5VisionDesktop
{
    internal static class BrowserLaunchProbe
    {
        private static readonly HashSet<string> Browsers = new HashSet<string>(StringComparer.OrdinalIgnoreCase) {
            "msedge", "chrome", "firefox", "brave", "opera", "vivaldi", "iexplore" };
        private static volatile bool firstRun, errorPage, chooser, uiaDone, uiaUnavailable;
        private static volatile bool stopping, originRequest, originResponse;
        private static volatile bool documentTitle, pageHeading, addressMatches;
        private static readonly object CaptureGate = new object();
        private static readonly List<Dictionary<string, object>> Labels = new List<Dictionary<string, object>>();
        [StructLayout(LayoutKind.Sequential)]
        private struct WindowRectangle { internal int Left, Top, Right, Bottom; }
        [DllImport("user32.dll")]
        private static extern bool GetWindowRect(IntPtr window, out WindowRectangle rectangle);
        [DllImport("user32.dll")]
        private static extern bool SetForegroundWindow(IntPtr window);
        [UnmanagedFunctionPointer(CallingConvention.Winapi)]
        private delegate bool EnumWindowsCallback(IntPtr window, IntPtr data);
        [DllImport("user32.dll")]
        private static extern bool EnumWindows(EnumWindowsCallback callback, IntPtr data);
        [DllImport("user32.dll")]
        private static extern bool IsWindowVisible(IntPtr window);
        [DllImport("user32.dll", CharSet = CharSet.Unicode)]
        private static extern int GetWindowText(IntPtr window, StringBuilder text, int maximum);
        [DllImport("user32.dll")]
        private static extern uint GetWindowThreadProcessId(IntPtr window, out uint pid);

        private static void Classify(string value)
        {
            if (value == null || value.Length > 2048) return;
            string text = value.ToLowerInvariant();
            if (text.Contains("welcome to microsoft edge") || text.Contains("welcome to chrome") ||
                text.Contains("start without your data") || text.Contains("sign in to sync") ||
                text.Contains("confirm and continue") || text.Contains("accept and get started") ||
                text.Contains("personalize your web experience") || text.Contains("complete setup") ||
                text.Contains("browse without your data") || text.Contains("how would you like to set up")) firstRun = true;
            if (text.Contains("can't reach this page") || text.Contains("can’t reach this page") ||
                text.Contains("this site can’t be reached") || text.Contains("this site can't be reached") ||
                text.Contains("err_connection_refused") || text.Contains("err_empty_response") ||
                text.Contains("this page isn't working") || text.Contains("your connection is not private")) errorPage = true;
            if (text.Contains("how do you want to open") || text.Contains("choose a default app") ||
                text.Contains("select an app to open")) chooser = true;
        }

        private static void InspectAutomation(IntPtr window)
        {
            try
            {
                lock (CaptureGate) Labels.Clear();
                AutomationElement root = AutomationElement.FromHandle(window);
                Queue<AutomationElement> pending = new Queue<AutomationElement>();
                pending.Enqueue(root);
                int observed = 0;
                while (pending.Count != 0 && ++observed <= 160 && !stopping)
                {
                    AutomationElement element = pending.Dequeue();
                    string name = element.Current.Name ?? "";
                    string kind = element.Current.ControlType.ProgrammaticName;
                    string value = "";
                    Classify(name);
                    if (kind == "ControlType.Document" && name.Contains("K5 Vision")) documentTitle = true;
                    if (name == "K5 Vision browser probe") pageHeading = true;
                    if (kind == "ControlType.Edit" || kind == "ControlType.Document")
                    {
                        object pattern;
                        if (element.TryGetCurrentPattern(ValuePattern.Pattern, out pattern))
                            value = ((ValuePattern)pattern).Current.Value ?? "";
                        if (value == "http://127.0.0.1:8000/operator/" || value == "127.0.0.1:8000/operator/")
                            addressMatches = true;
                    }
                    if (name.Length != 0 || value.Length != 0)
                        lock (CaptureGate)
                        {
                            if (Labels.Count < 96)
                                Labels.Add(new Dictionary<string, object> {
                                    { "control_type", Clip(kind, 64) }, { "name", Clip(name, 240) },
                                    { "value", Clip(value, 240) } });
                        }
                    AutomationElement child = TreeWalker.ControlViewWalker.GetFirstChild(element);
                    for (int count = 0; child != null && count < 24; count++)
                    {
                        pending.Enqueue(child);
                        child = TreeWalker.ControlViewWalker.GetNextSibling(child);
                    }
                }
            }
            catch { uiaUnavailable = true; }
            finally { uiaDone = true; }
        }

        private static string Clip(string value, int maximum)
        { return value.Length <= maximum ? value : value.Substring(0, maximum); }

        private static bool CaptureWindow(IntPtr window, uint expectedPid)
        {
            uint pid;
            GetWindowThreadProcessId(window, out pid);
            if (window == IntPtr.Zero || pid != expectedPid || !IsWindowVisible(window)) return false;
            using (Process process = Process.GetProcessById((int)pid))
                if (!Browsers.Contains(process.ProcessName)) return false;
            SetForegroundWindow(window);
            Thread.Sleep(200);
            WindowRectangle bounds;
            if (!GetWindowRect(window, out bounds)) return false;
            Rectangle screen = SystemInformation.VirtualScreen;
            Rectangle area = Rectangle.Intersect(screen, Rectangle.FromLTRB(bounds.Left, bounds.Top, bounds.Right, bounds.Bottom));
            if (area.Width < 50 || area.Height < 50 || area.Width > 4096 || area.Height > 3072) return false;
            using (Bitmap image = new Bitmap(area.Width, area.Height))
            using (Graphics graphics = Graphics.FromImage(image))
            {
                graphics.CopyFromScreen(area.Left, area.Top, 0, 0, area.Size, CopyPixelOperation.SourceCopy);
                image.Save("browser-dispatch-native.png", ImageFormat.Png);
            }
            return true;
        }

        private static void Serve(TcpListener listener)
        {
            while (!stopping)
            {
                try
                {
                    using (TcpClient client = listener.AcceptTcpClient())
                    {
                        client.ReceiveTimeout = client.SendTimeout = 2000;
                        using (NetworkStream stream = client.GetStream())
                        {
                            byte[] request = new byte[8192];
                            int length = 0;
                            while (length < request.Length)
                            {
                                int count = stream.Read(request, length, request.Length - length);
                                if (count <= 0) break;
                                length += count;
                                if (Encoding.ASCII.GetString(request, 0, length).Contains("\r\n\r\n")) break;
                            }
                            string header = Encoding.ASCII.GetString(request, 0, length);
                            bool target = header.StartsWith("GET /operator/ HTTP/1.", StringComparison.Ordinal) &&
                                header.IndexOf("\r\nHost: 127.0.0.1:8000\r\n", StringComparison.OrdinalIgnoreCase) >= 0;
                            if (target) originRequest = true;
                            byte[] body = Encoding.UTF8.GetBytes(target ?
                                "<!doctype html><title>K5 Vision</title><h1>K5 Vision browser probe</h1>" : "Not found");
                            byte[] response = Encoding.ASCII.GetBytes("HTTP/1.1 " + (target ? "200 OK" : "404 Not Found") +
                                "\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: " + body.Length +
                                "\r\nConnection: close\r\nCache-Control: no-store\r\n\r\n");
                            stream.Write(response, 0, response.Length);
                            stream.Write(body, 0, body.Length); stream.Flush();
                            if (target) originResponse = true;
                        }
                    }
                }
                catch { if (stopping) return; }
            }
        }

        [STAThread]
        private static int Main(string[] arguments)
        {
            Dictionary<string, object> result = new Dictionary<string, object> { { "schema_version", 1 } };
            TcpListener listener = null;
            Thread server = null;
            RecorderWindow form = null;
            try
            {
                if (arguments.Length != 0) throw new InvalidOperationException();
                int before = 0;
                foreach (Process process in Process.GetProcesses())
                    using (process)
                    {
                        try { if (Browsers.Contains(process.ProcessName)) before++; }
                        catch { }
                    }
                result["preexisting_browser_count"] = Math.Min(1000, before);
                if (before != 0)
                {
                    result["status"] = "preexisting_browser_refused";
                    result["unowned_processes_preserved"] = true;
                    Console.WriteLine(Json.Serializer.Serialize(result)); return 0;
                }
                listener = new TcpListener(IPAddress.Loopback, 8000); listener.Start(4);
                server = new Thread(delegate() { Serve(listener); }) { IsBackground = true };
                server.Start();
                Application.EnableVisualStyles(); Application.SetCompatibleTextRenderingDefault(false);
                form = new RecorderWindow(true, false);
                typeof(RecorderWindow).GetField("ready", BindingFlags.Instance | BindingFlags.NonPublic).SetValue(form, true);
                typeof(RecorderWindow).GetMethod("OpenDashboard", BindingFlags.Instance | BindingFlags.NonPublic).Invoke(form, null);
                string dispatch = (string)typeof(RecorderWindow).GetField("browserDispatch", BindingFlags.Instance | BindingFlags.NonPublic).GetValue(form);
                int nativeError = (int)typeof(RecorderWindow).GetField("browserNativeError", BindingFlags.Instance | BindingFlags.NonPublic).GetValue(form);
                result["actual_product_dispatch"] = dispatch == "dispatched";
                result["dispatch_native_error"] = nativeError;
                bool visible = false, title = false, automationStarted = false;
                long lastAutomation = -1000;
                IntPtr captureWindow = IntPtr.Zero;
                uint capturePid = 0;
                long captureArea = 0;
                List<string> captions = new List<string>();
                HashSet<uint> observed = new HashSet<uint>();
                Stopwatch elapsed = Stopwatch.StartNew();
                while (elapsed.ElapsedMilliseconds < 23000)
                {
                    Application.DoEvents();
                    EnumWindows(delegate(IntPtr window, IntPtr unused) {
                        if (!IsWindowVisible(window)) return true;
                        uint pid; GetWindowThreadProcessId(window, out pid);
                        try
                        {
                            using (Process process = Process.GetProcessById((int)pid))
                            {
                                string name = process.ProcessName;
                                bool browser = Browsers.Contains(name);
                                if (!browser && name != "OpenWith" && name != "SystemSettings" && name != "ApplicationFrameHost") return true;
                                StringBuilder text = new StringBuilder(2048); GetWindowText(window, text, text.Capacity);
                                Classify(text.ToString());
                                if (browser)
                                {
                                    observed.Add(pid); visible = true; title |= text.ToString().Contains("K5 Vision");
                                    WindowRectangle bounds;
                                    if (GetWindowRect(window, out bounds))
                                    {
                                        long area = (long)(bounds.Right - bounds.Left) * (bounds.Bottom - bounds.Top);
                                        if (area > captureArea) { captureWindow = window; capturePid = pid; captureArea = area; }
                                    }
                                    string caption = Clip(text.ToString(), 240);
                                    if (captions.Count < 8 && !captions.Contains(caption)) captions.Add(caption);
                                }
                                if (!automationStarted || (uiaDone && !firstRun && !errorPage && !chooser &&
                                    elapsed.ElapsedMilliseconds - lastAutomation >= 1000))
                                {
                                    automationStarted = true;
                                    uiaDone = false;
                                    lastAutomation = elapsed.ElapsedMilliseconds;
                                    Thread inspect = new Thread(delegate() { InspectAutomation(window); }) { IsBackground = true };
                                    inspect.Start();
                                }
                            }
                        }
                        catch { }
                        return true;
                    }, IntPtr.Zero);
                    if (originResponse && title && uiaDone) break;
                    Thread.Sleep(100);
                }
                result["status"] = "diagnostic_complete";
                result["elapsed_seconds"] = Math.Round(elapsed.Elapsed.TotalSeconds, 3);
                result["origin_request_observed"] = originRequest;
                result["origin_response_sent"] = originResponse;
                result["browser_window_visible"] = visible;
                result["browser_title_matches"] = title;
                result["browser_window_process_count"] = observed.Count;
                result["first_run_prompt_observed"] = firstRun;
                result["browser_error_observed"] = errorPage;
                result["default_app_chooser_observed"] = chooser;
                result["uia_completed"] = uiaDone;
                result["uia_unavailable"] = uiaUnavailable;
                result["document_title_matches"] = documentTitle;
                result["page_heading_matches"] = pageHeading;
                result["address_matches"] = addressMatches;
                result["qa_browser_captions"] = captions;
                lock (CaptureGate) result["qa_uia_projection"] = Labels.ToArray();
                try { result["qa_window_capture_saved"] = CaptureWindow(captureWindow, capturePid); }
                catch (Exception error) { result["qa_window_capture_saved"] = false; result["qa_capture_failure_type"] = error.GetType().Name; }
                // A diagnostic must not sweep/kill processes by name. The
                // disposable VM owns its lifetime; preserve every browser here.
                result["unowned_processes_preserved"] = true;
                Console.WriteLine(Json.Serializer.Serialize(result)); return 0;
            }
            catch (Exception error)
            {
                result["status"] = "probe_failed";
                result["failure_type"] = error.GetType().Name;
                Console.WriteLine(Json.Serializer.Serialize(result)); return 1;
            }
            finally
            {
                stopping = true;
                if (form != null) form.Dispose();
                if (listener != null) listener.Stop();
                if (server != null) server.Join(2500);
            }
        }
    }
}
