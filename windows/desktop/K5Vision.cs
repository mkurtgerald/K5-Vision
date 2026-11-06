using System;
using System.Collections;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.IO.Compression;
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
using Microsoft.Win32.SafeHandles;

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
            if (arguments.Length == 3 && arguments[0] == "--time-admin")
                return TimeAdministration.Execute(arguments[1], arguments[2]);
            if (arguments.Length == 1 && arguments[0] == "--installation-check")
                return InstallationCheck();
            if (arguments.Length == 1 && arguments[0] == "--installation-payload-check")
            {
                // Used by the actual Windows installer after file extraction.
                // Verify sealed release bytes without opening a window or touching user state.
                try { Custody.VerifyPayload(Path.GetDirectoryName(Application.ExecutablePath)); return 0; }
                catch { return 1; }
            }
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
                !admitted.Contains("workers/donor-downloads.json") ||
                !admitted.Contains("workers/mediamtx.exe") || !admitted.Contains("workers/caddy.exe") ||
                !admitted.Contains("workers/mediamtx-LICENSE") || !admitted.Contains("workers/caddy-LICENSE"))
                throw new InvalidOperationException();
            foreach (string bootstrap in new[] { "K5Vision.exe", "payload-manifest.json", "unins000.exe", "unins000.dat", "unins000.msg" })
                admitted.Add(bootstrap);
            VerifyTree(root, root, admitted);
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

    internal static class TimeAdministration
    {
        [StructLayout(LayoutKind.Sequential)]
        private struct FileIdentity
        {
            internal uint Attributes;
            internal System.Runtime.InteropServices.ComTypes.FILETIME Created, Accessed, Written;
            internal uint Volume, SizeHigh, SizeLow, Links, IndexHigh, IndexLow;
        }
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern SafeFileHandle CreateFile(string path, uint access, uint share,
            IntPtr security, uint creation, uint flags, IntPtr template);
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool GetFileInformationByHandle(SafeFileHandle file, out FileIdentity info);
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern IntPtr OpenProcess(uint access, bool inherit, uint pid);
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool GetProcessTimes(IntPtr process,
            out System.Runtime.InteropServices.ComTypes.FILETIME created,
            out System.Runtime.InteropServices.ComTypes.FILETIME exited,
            out System.Runtime.InteropServices.ComTypes.FILETIME system,
            out System.Runtime.InteropServices.ComTypes.FILETIME user);
        [DllImport("advapi32.dll", SetLastError = true)]
        private static extern bool OpenProcessToken(IntPtr process, uint access, out IntPtr token);
        [DllImport("advapi32.dll", SetLastError = true)]
        private static extern bool GetTokenInformation(IntPtr token, int kind, IntPtr buffer,
            uint length, out uint needed);
        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
        private struct StartupInfo
        {
            internal uint Size;
            internal string Reserved, Desktop, Title;
            internal uint X, Y, XSize, YSize, XChars, YChars, Fill, Flags;
            internal ushort Show, ReservedBytes;
            internal IntPtr ReservedPointer, StandardInput, StandardOutput, StandardError;
        }
        [StructLayout(LayoutKind.Sequential)]
        private struct ProcessInformation
        {
            internal IntPtr Process, Thread;
            internal uint ProcessId, ThreadId;
        }
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern bool CreateProcess(string executable, StringBuilder command,
            IntPtr processSecurity, IntPtr threadSecurity, bool inheritHandles, uint flags,
            IntPtr environment, string directory, ref StartupInfo startup, out ProcessInformation info);
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern uint ResumeThread(IntPtr thread);
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern uint WaitForSingleObject(IntPtr handle, uint milliseconds);
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern uint WaitForMultipleObjects(uint count, IntPtr[] handles,
            bool waitAll, uint milliseconds);
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool GetExitCodeProcess(IntPtr process, out uint code);
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool TerminateProcess(IntPtr process, uint code);
        [DllImport("kernel32.dll")]
        private static extern bool CloseHandle(IntPtr handle);

        private static FileIdentity Identify(FileStream stream)
        {
            FileIdentity identity;
            if (!GetFileInformationByHandle(stream.SafeFileHandle, out identity) ||
                identity.Links != 1 || (identity.Attributes & (0x10u | 0x400u)) != 0)
                throw new InvalidOperationException();
            return identity;
        }

        private static long Integer(object value)
        {
            if (value is int) return (int)value;
            if (value is long) return (long)value;
            throw new InvalidOperationException();
        }

        private sealed class LiveIssuer : IDisposable
        {
            internal IntPtr Handle;
            private ulong created100ns;
            internal void Bind(uint pid, ulong created)
            {
                if (Handle != IntPtr.Zero || pid == 0 || created == 0) throw new InvalidOperationException();
                // Keep the exact kernel process object, not a PID lookup that
                // could later select a replacement process after PID reuse.
                Handle = OpenProcess(0x00100000u | 0x00001000u, false, pid);
                if (Handle == IntPtr.Zero) throw new InvalidOperationException();
                created100ns = created;
                Check();
            }
            internal void Check()
            {
                if (Handle == IntPtr.Zero || WaitForSingleObject(Handle, 0) != 258)
                    throw new InvalidOperationException();
                System.Runtime.InteropServices.ComTypes.FILETIME created, exited, system, user;
                if (!GetProcessTimes(Handle, out created, out exited, out system, out user) ||
                    (((ulong)(uint)created.dwHighDateTime << 32) | (uint)created.dwLowDateTime) != created100ns)
                    throw new InvalidOperationException();
                IntPtr token = IntPtr.Zero, buffer = IntPtr.Zero;
                try
                {
                    if (!OpenProcessToken(Handle, 8, out token)) throw new UnauthorizedAccessException();
                    uint needed;
                    GetTokenInformation(token, 1, IntPtr.Zero, 0, out needed);
                    if (needed < IntPtr.Size || needed > 4096) throw new InvalidOperationException();
                    buffer = Marshal.AllocHGlobal((int)needed);
                    if (!GetTokenInformation(token, 1, buffer, needed, out needed))
                        throw new InvalidOperationException();
                    IntPtr sid = Marshal.ReadIntPtr(buffer);
                    if (sid == IntPtr.Zero || new SecurityIdentifier(sid).Value != Program.Sid)
                        throw new UnauthorizedAccessException();
                    if (WaitForSingleObject(Handle, 0) != 258) throw new InvalidOperationException();
                }
                finally
                {
                    if (buffer != IntPtr.Zero) Marshal.FreeHGlobal(buffer);
                    if (token != IntPtr.Zero) CloseHandle(token);
                }
            }
            public void Dispose()
            {
                if (Handle != IntPtr.Zero) { CloseHandle(Handle); Handle = IntPtr.Zero; }
            }
        }

        private sealed class DirectoryPins : IDisposable
        {
            private readonly List<SafeFileHandle> handles = new List<SafeFileHandle>();
            internal void Acquire(string directory, string root)
            {
                List<string> ancestors = new List<string>();
                for (string current = directory; !String.IsNullOrEmpty(current);
                    current = Path.GetDirectoryName(current)) ancestors.Add(current);
                ancestors.Reverse();
                foreach (string ancestor in ancestors)
                {
                    Custody.Admit(ancestor, root);
                    // Pin every directory from the fixed volume downwards.
                    // No FILE_SHARE_DELETE: a later reparse/rename cannot redirect
                    // elevated response creation to a different directory.
                    SafeFileHandle handle = CreateFile(ancestor, 0, 3, IntPtr.Zero,
                        3, 0x02000000u | 0x00200000u, IntPtr.Zero);
                    FileIdentity identity;
                    if (handle.IsInvalid || !GetFileInformationByHandle(handle, out identity) ||
                        (identity.Attributes & 0x400u) != 0 || (identity.Attributes & 0x10u) == 0)
                    {
                        handle.Dispose();
                        throw new InvalidOperationException();
                    }
                    handles.Add(handle);
                }
                Custody.Admit(directory, root);
            }
            public void Dispose()
            {
                for (int index = handles.Count - 1; index >= 0; index--) handles[index].Dispose();
                handles.Clear();
            }
        }

        [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern IntPtr OpenSCManager(string machine, string database, uint access);
        [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern IntPtr OpenService(IntPtr manager, string name, uint access);
        [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern bool ChangeServiceConfig(IntPtr service, uint type, uint start,
            uint error, string binary, string group, IntPtr tag, string dependencies,
            string account, string password, string display);
        [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern bool StartService(IntPtr service, uint count, IntPtr arguments);
        [StructLayout(LayoutKind.Sequential)]
        private struct ServiceStatus
        {
            internal uint Kind, State, Accepted, Win32Exit, ServiceExit, Checkpoint, WaitHint;
        }
        [DllImport("advapi32.dll", SetLastError = true)]
        private static extern bool QueryServiceStatus(IntPtr service, out ServiceStatus status);
        [DllImport("advapi32.dll")]
        private static extern bool CloseServiceHandle(IntPtr handle);

        private static void StartTimeService(Action admit, Stopwatch elapsed)
        {
            IntPtr manager = OpenSCManager(null, null, 1);
            if (manager == IntPtr.Zero) throw new UnauthorizedAccessException();
            IntPtr service = IntPtr.Zero;
            try
            {
                service = OpenService(manager, "W32Time", 0x0002 | 0x0004 | 0x0010);
                if (service == IntPtr.Zero) throw new UnauthorizedAccessException();
                admit();
                if (!ChangeServiceConfig(service, UInt32.MaxValue, 2, UInt32.MaxValue,
                    null, null, IntPtr.Zero, null, null, null, null))
                    throw new InvalidOperationException();
                admit();
                if (!StartService(service, 0, IntPtr.Zero) && Marshal.GetLastWin32Error() != 1056)
                    throw new InvalidOperationException();
                long deadline = Math.Min(45000, elapsed.ElapsedMilliseconds + 5000);
                ServiceStatus status;
                while (elapsed.ElapsedMilliseconds < deadline)
                {
                    admit();
                    if (!QueryServiceStatus(service, out status)) throw new InvalidOperationException();
                    if (status.State == 4) return;
                    if (status.State != 2) throw new InvalidOperationException();
                    Thread.Sleep(50);
                }
                throw new TimeoutException();
            }
            finally
            {
                if (service != IntPtr.Zero) CloseServiceHandle(service);
                CloseServiceHandle(manager);
            }
        }

        private static bool HexNonce(string nonce)
        {
            return nonce != null && nonce.Length == 32 &&
                nonce.All(c => (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'));
        }

        private static void CheckCapability(string path, string root, byte[] admitted,
            FileIdentity expected, LiveIssuer owner)
        {
            owner.Check();
            Custody.Admit(path, root);
            using (FileStream stream = new FileStream(path, FileMode.Open, FileAccess.Read,
                FileShare.Read | FileShare.Delete))
            {
                FileIdentity identity = Identify(stream);
                if (identity.Volume != expected.Volume || identity.IndexHigh != expected.IndexHigh ||
                    identity.IndexLow != expected.IndexLow) throw new InvalidOperationException();
                if (stream.Length != admitted.Length) throw new InvalidOperationException();
                byte[] current = new byte[admitted.Length];
                int read = 0;
                while (read < current.Length)
                {
                    int count = stream.Read(current, read, current.Length - read);
                    if (count <= 0) throw new InvalidOperationException();
                    read += count;
                }
                if (!current.SequenceEqual(admitted)) throw new InvalidOperationException();
            }
            owner.Check();
        }

        private static void RunTimeCommand(string arguments, Stopwatch elapsed,
            LiveIssuer owner, Action admit)
        {
            int remaining = Math.Min(25000, 45000 - (int)elapsed.ElapsedMilliseconds);
            if (remaining <= 0) throw new TimeoutException();
            string system = Environment.GetFolderPath(Environment.SpecialFolder.System);
            string executable = Path.Combine(system, "w32tm.exe");
            Custody.Admit(executable, Path.GetDirectoryName(Application.ExecutablePath));
            string windows = Environment.GetFolderPath(Environment.SpecialFolder.Windows);
            string environment = "PATH=" + system + ";" + windows + "\0SystemDrive=" +
                Path.GetPathRoot(windows).TrimEnd('\\') + "\0SystemRoot=" + windows +
                "\0WINDIR=" + windows + "\0\0";
            ProcessInformation information = new ProcessInformation();
            using (ProcessJob job = new ProcessJob())
            {
                IntPtr environmentBlock = Marshal.StringToHGlobalUni(environment);
                try
                {
                    StartupInfo startup = new StartupInfo();
                    startup.Size = (uint)Marshal.SizeOf(startup);
                    // Fixed system command starts suspended. No privileged
                    // action runs before its exact process belongs to our Job.
                    admit();
                    if (!CreateProcess(executable, new StringBuilder("\"" + executable + "\" " + arguments),
                        IntPtr.Zero, IntPtr.Zero, false, 0x08000000u | 0x00000004u | 0x00000400u,
                        environmentBlock, system, ref startup, out information))
                        throw new InvalidOperationException();
                    using (Process process = Process.GetProcessById((int)information.ProcessId)) job.Assign(process);
                    admit();
                    if (ResumeThread(information.Thread) == UInt32.MaxValue) throw new InvalidOperationException();
                    uint waited = WaitForMultipleObjects(2,
                        new[] { information.Process, owner.Handle }, false, (uint)remaining);
                    if (waited == 258) throw new TimeoutException();
                    if (waited != 0) throw new InvalidOperationException();
                    uint exitCode;
                    if (!GetExitCodeProcess(information.Process, out exitCode)) throw new InvalidOperationException();
                    if (exitCode == 0x800705B4u) throw new TimeSourceUnavailable();
                    if (exitCode != 0) throw new InvalidOperationException();
                }
                finally
                {
                    if (information.Process != IntPtr.Zero)
                    {
                        if (WaitForSingleObject(information.Process, 0) != 0)
                        {
                            TerminateProcess(information.Process, 1);
                            if (WaitForSingleObject(information.Process, 2000) != 0)
                            {
                                job.Dispose();
                                WaitForSingleObject(information.Process, 2000);
                            }
                        }
                        CloseHandle(information.Process);
                    }
                    if (information.Thread != IntPtr.Zero) CloseHandle(information.Thread);
                    Marshal.FreeHGlobal(environmentBlock);
                }
            }
        }

        private sealed class TimeSourceUnavailable : Exception { }

        internal static int Execute(string requestArgument, string responseArgument)
        {
            using (System.Threading.Timer watchdog = new System.Threading.Timer(
                delegate { Environment.Exit(1); }, null, 45000, Timeout.Infinite))
            using (DirectoryPins directoryPins = new DirectoryPins())
            using (LiveIssuer owner = new LiveIssuer())
            {
            string response = null, root = null, nonce = null;
            string code = "invalid_request";
            bool succeeded = false;
            Stopwatch elapsed = Stopwatch.StartNew();
            try
            {
                string application = Path.GetDirectoryName(Application.ExecutablePath);
                Custody.VerifyPayload(application);
                string requestPath = Custody.OrdinaryAbsolute(requestArgument);
                response = Custody.OrdinaryAbsolute(responseArgument);
                string directory = Path.GetDirectoryName(requestPath);
                if (!String.Equals(Path.GetDirectoryName(response), directory,
                    StringComparison.OrdinalIgnoreCase) || Path.GetFileName(directory) != "time-admin")
                    throw new InvalidOperationException();
                root = Path.GetDirectoryName(directory);
                directoryPins.Acquire(directory, root);
                Custody.Admit(directory, root);
                if (File.Exists(response) || Directory.Exists(response)) throw new InvalidOperationException();
                Custody.Admit(requestPath, root);
                byte[] bytes;
                FileIdentity requestIdentity;
                using (FileStream input = new FileStream(requestPath, FileMode.Open, FileAccess.Read,
                    FileShare.Read | FileShare.Delete))
                {
                    requestIdentity = Identify(input);
                    if (input.Length <= 0 || input.Length > 4096) throw new InvalidOperationException();
                    bytes = new byte[(int)input.Length];
                    int position = 0;
                    while (position < bytes.Length)
                    {
                        int count = input.Read(bytes, position, bytes.Length - position);
                        if (count <= 0) throw new InvalidOperationException();
                        position += count;
                    }
                }
                Dictionary<string, object> request = Json.Parse(new UTF8Encoding(false, true).GetString(bytes));
                HashSet<string> keys = new HashSet<string>(new[] {
                    "schema_version", "operation", "ntp_servers", "mode", "nonce",
                    "owner_pid", "owner_created_100ns" });
                if (!keys.SetEquals(request.Keys) || !(request["schema_version"] is int) ||
                    (int)request["schema_version"] != 1) throw new InvalidOperationException();
                nonce = request["nonce"] as string;
                if (!HexNonce(nonce) || Path.GetFileName(requestPath) != ".time-admin-" + nonce + ".json" ||
                    Path.GetFileName(response) != ".time-admin-" + nonce + ".result.json")
                    throw new InvalidOperationException();
                string operation = request["operation"] as string;
                string mode = request["mode"] as string;
                if ((operation != "configure" && operation != "resync") ||
                    (mode != "manual" && mode != "domain")) throw new InvalidOperationException();
                IEnumerable values = request["ntp_servers"] as IEnumerable;
                if (values == null || values is string) throw new InvalidOperationException();
                List<string> servers = new List<string>();
                foreach (object value in values)
                {
                    string host = value as string;
                    if (host == null || host.Length < 1 || host.Length > 253 ||
                        !host.All(c => (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
                            (c >= '0' && c <= '9') || c == '.' || c == '-' || c == ':') ||
                        Uri.CheckHostName(host) == UriHostNameType.Unknown ||
                        servers.Contains(host) || servers.Count >= 4) throw new InvalidOperationException();
                    servers.Add(host);
                }
                if (mode == "manual" && operation == "configure" && servers.Count == 0)
                    throw new InvalidOperationException();
                long ownerPid = Integer(request["owner_pid"]);
                long ownerCreated = Integer(request["owner_created_100ns"]);
                if (ownerPid < 1 || ownerPid > UInt32.MaxValue || ownerCreated < 1)
                    throw new InvalidOperationException();
                owner.Bind((uint)ownerPid, (ulong)ownerCreated);
                WindowsPrincipal principal = new WindowsPrincipal(WindowsIdentity.GetCurrent());
                if (!principal.IsInRole(WindowsBuiltInRole.Administrator))
                    throw new UnauthorizedAccessException();
                Action admit = delegate { CheckCapability(requestPath, root, bytes, requestIdentity, owner); };
                admit();
                StartTimeService(admit, elapsed);
                admit();
                if (operation == "configure")
                {
                    using (Microsoft.Win32.RegistryKey parameters = Microsoft.Win32.Registry.LocalMachine.OpenSubKey(
                        @"SYSTEM\CurrentControlSet\Services\W32Time\Parameters", true))
                    {
                        if (parameters == null) throw new InvalidOperationException();
                        admit();
                        parameters.SetValue("Type", mode == "domain" ? "NT5DS" : "NTP", Microsoft.Win32.RegistryValueKind.String);
                        admit();
                        if (mode == "manual") parameters.SetValue("NtpServer",
                            String.Join(" ", servers.Select(s => s + ",0x9")), Microsoft.Win32.RegistryValueKind.String);
                    }
                    admit();
                    RunTimeCommand("/config /update", elapsed, owner, admit);
                }
                else RunTimeCommand("/resync /rediscover", elapsed, owner, admit);
                admit();
                succeeded = true; code = "completed";
            }
            catch (UnauthorizedAccessException) { code = "access_denied"; }
            catch (TimeoutException) { code = "timeout"; }
            catch (TimeSourceUnavailable) { code = "ntp_unreachable"; }
            catch { code = "operation_failed"; }
            try
            {
                if (response == null || root == null || !HexNonce(nonce)) return 1;
                // No delayed response/profile write after its issuing backend
                // has exited, including a hard-killed owner with a live file.
                owner.Check();
                Custody.Admit(Path.GetDirectoryName(response), root);
                using (FileStream output = new FileStream(response, FileMode.CreateNew,
                    FileAccess.Write, FileShare.None))
                {
                    byte[] result = Encoding.UTF8.GetBytes(Json.Serializer.Serialize(new Dictionary<string, object> {
                        { "schema_version", 1 }, { "nonce", nonce }, { "succeeded", succeeded }, { "code", code } }));
                    output.Write(result, 0, result.Length); output.Flush(true);
                }
                Custody.Admit(response, root);
            }
            catch { return 1; }
            return succeeded ? 0 : 1;
            }
        }
    }

    internal static class Workers
    {
        private static void VerifyFile(string path, string root, long bytes, string expected)
        {
            Custody.Admit(path, root);
            using (FileStream stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read))
            using (SHA256 hash = SHA256.Create())
                if ((bytes >= 0 && stream.Length != bytes) ||
                    BitConverter.ToString(hash.ComputeHash(stream)).Replace("-", "").ToLowerInvariant() != expected)
                    throw new InvalidOperationException();
        }

        internal static Dictionary<string, string> Provision(string application, string profile, CancellationToken cancellation)
        {
            // The owner workstation must not download executable dependencies.
            // Verify the installation and exact source identities on every start.
            Custody.Admit(profile, profile);
            string path = Path.Combine(application, "workers", "donor-downloads.json");
            Custody.Admit(path, application);
            Dictionary<string, object> manifest = Json.Parse(File.ReadAllText(path, Encoding.UTF8));
            if (Convert.ToInt32(manifest["schema_version"]) != 1) throw new InvalidOperationException();
            Dictionary<string, string> result = new Dictionary<string, string>();
            foreach (object item in (IEnumerable)manifest["artifacts"])
            {
                cancellation.ThrowIfCancellationRequested();
                Dictionary<string, object> artifact = (Dictionary<string, object>)item;
                string component = (string)artifact["component"];
                if (component != "MediaMTX" && component != "Caddy") throw new InvalidOperationException();
                if (result.ContainsKey(component)) throw new InvalidOperationException();
                string executable = component == "MediaMTX" ? "mediamtx.exe" : "caddy.exe";
                string name = component == "MediaMTX" ? "mediamtx" : "caddy";
                string version = (string)artifact["version"];
                if (version.Length > 32 || !System.Text.RegularExpressions.Regex.IsMatch(version, "^v[0-9]+[.][0-9]+[.][0-9]+$") ||
                    (string)artifact["executable"] != executable) throw new InvalidOperationException();
                string binary = Path.Combine(application, "workers", executable);
                string license = Path.Combine(application, "workers", name + "-LICENSE");
                VerifyFile(binary, application, Convert.ToInt64(artifact["executable_bytes"]),
                    (string)artifact["executable_sha256"]);
                VerifyFile(license, application, -1, (string)artifact["license_sha256"]);
                result[component] = binary;
            }
            if (result.Count != 2) throw new InvalidOperationException();
            return result;
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
        private int progressLines;
        private readonly bool persistent;
        internal volatile bool PreparingRecordings;
        internal volatile int RecordingsProgress;

        internal Bridge(string root, Dictionary<string, object> request, ProcessJob job, bool persistent)
        {
            this.persistent = persistent;
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
                    if (args.Data.Length > 1024) throw new InvalidOperationException();
                    Dictionary<string, object> response = Json.Parse(args.Data);
                    if (response.ContainsKey("status") && (string)response["status"] == "preparing_recordings")
                    {
                        if (!persistent || Running.WaitOne(0) || ++progressLines > 101 ||
                            response.Count != 3 || !(response["schema_version"] is int) ||
                            (int)response["schema_version"] != 1 || !(response["progress"] is int))
                            throw new InvalidOperationException();
                        int percent = (int)response["progress"];
                        if (percent < 0 || percent > 100 || percent < RecordingsProgress)
                            throw new InvalidOperationException();
                        RecordingsProgress = percent; PreparingRecordings = true;
                        return;
                    }
                    if (++lines > 2) throw new InvalidOperationException();
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
                // Allow a bounded camera connection to finish before recording
                // flush and the two native-worker cleanup budgets run.
                Dictionary<string, object> result = Complete(35000);
                return (string)result["status"] == "stopped" && Process.ExitCode == 0;
            }
            catch { return false; }
        }

        internal void AddDiagnostic(Dictionary<string, object> result)
        {
            lock (gate)
            {
                result["backend_invalid_output"] = InvalidOutput;
                result["backend_status"] = outcome == null ? "none" : (string)outcome["status"];
            }
            try
            {
                bool exited = Process.HasExited;
                result["backend_exited"] = exited;
                result["backend_exit_code"] = exited ? (object)Process.ExitCode : null;
            }
            catch { }
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
        private readonly Label workspaceHelp = new Label();
        private readonly TextBox diagnostic = new TextBox();
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
        private readonly CancellationTokenSource cancellation = new CancellationTokenSource();
        private Bridge backend;
        private Dictionary<string, string> workers;
        private Task operation = Task.FromResult(true);
        private volatile bool exiting;
        private bool busy = true, ready, checkedPayload;
        private bool disposed;
        private string phase = "preparing";

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
            workspaceHelp.Location = new Point(30, 158); workspaceHelp.Size = new Size(500, 100);
            workspaceHelp.Text = "This window starts K5 Vision and keeps it running in the notification area. " +
                "Open the camera wall in your browser for live viewing, recordings and camera controls. " +
                "Closing either window keeps the camera engine running.";
            workspaceHelp.ForeColor = Color.FromArgb(75, 89, 108); workspaceHelp.Visible = false;
            Controls.Add(workspaceHelp);
            diagnostic.Visible = false; diagnostic.ReadOnly = true; diagnostic.Name = "Diagnostic";
            SetId(diagnostic, 1010); Controls.Add(diagnostic);
            GC.KeepAlive(diagnostic.Handle);
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
            open.Text = "Open camera wall"; open.Name = "OpenOperator";
            open.Location = new Point(30, 383); open.Size = new Size(220, 42); open.Enabled = false; SetId(open, 1004);
            open.FlatStyle = FlatStyle.Flat; open.FlatAppearance.BorderSize = 0;
            open.BackColor = Color.FromArgb(27, 109, 159); open.ForeColor = Color.White;
            open.FlatAppearance.MouseOverBackColor = Color.FromArgb(22, 91, 134);
            open.FlatAppearance.MouseDownBackColor = Color.FromArgb(17, 74, 110);
            open.UseVisualStyleBackColor = false; open.Font = new Font("Segoe UI", 10, FontStyle.Bold);
            open.Click += delegate { OpenDashboard(); }; Controls.Add(open);
            quit.Text = "Stop and quit"; quit.Name = "StopRecording";
            quit.Location = new Point(350, 389); quit.Size = new Size(166, 36); SetId(quit, 1005);
            quit.Click += delegate { StopAndQuit(); }; Controls.Add(quit);
            ContextMenuStrip menu = new ContextMenuStrip();
            menu.Items.Add("Open camera wall", null, delegate { ShowOwner(); OpenDashboard(); });
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
                { phase = "failed"; ready = false; open.Enabled = false; workspaceHelp.Visible = false; status.Text = "Recording stopped unexpectedly. Close and reopen K5 Vision to try again. Your recordings have been kept."; ShowOwner(); }
                UpdateDiagnostic();
            };
            timer.Start();
            FormClosing += delegate(object sender, FormClosingEventArgs args)
            {
                if (!exiting && args.CloseReason == CloseReason.UserClosing) { args.Cancel = true; Hide(); }
            };
            Shown += delegate { Prepare(); };
            UpdateDiagnostic();
        }

        private void UpdateDiagnostic()
        {
            Dictionary<string, object> snapshot = new Dictionary<string, object> {
                { "schema_version", 1 }, { "phase", phase }, { "exiting", exiting }, { "ready", ready },
                { "backend_present", backend != null }, { "backend_exited", false },
                { "backend_exit_code", null }, { "backend_invalid_output", false }, { "backend_status", "none" } };
            if (backend != null) backend.AddDiagnostic(snapshot);
            diagnostic.Text = Json.Serializer.Serialize(snapshot);
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
                { phase = "account"; busy = false; setup.Visible = true; status.Text = "Create your administrator account to get started."; }
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
            phase = "initializing"; busy = true; create.Enabled = false;
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
            phase = "starting"; busy = true; setup.Visible = false; status.Text = "Starting your locally bundled camera services…";
            bool failed = false;
            try
            {
                Task startup = Task.Run(delegate
                {
                    if (exiting) throw new InvalidOperationException();
                    workers = Workers.Provision(root, options.Profile, cancellation.Token);
                    if (exiting) throw new InvalidOperationException();
                    Dictionary<string, object> request = Request("serve");
                    request["recording_root"] = Path.Combine(options.Profile, "recordings");
                    request["media_executable"] = workers["MediaMTX"];
                    request["gateway_executable"] = workers["Caddy"];
                    request["port"] = options.Port; request["recording_limit_gb"] = 20;
                    backend = new Bridge(root, request, job, true);
                    Stopwatch deadline = Stopwatch.StartNew();
                    int lastMigrationProgress = -1;
                    while (!backend.Running.WaitOne(50))
                    {
                        int startupLimit = backend.PreparingRecordings ? 330000 : 45000;
                        if (backend.PreparingRecordings && !exiting &&
                            backend.RecordingsProgress != lastMigrationProgress)
                        {
                            int prepared = backend.RecordingsProgress;
                            lastMigrationProgress = prepared;
                            BeginInvoke((Action)delegate {
                                if (!exiting && !IsDisposed)
                                    status.Text = "Preparing recordings (" + prepared +
                                        "%). Original footage is preserved.";
                            });
                        }
                        if (exiting || backend.InvalidOutput || backend.Process.HasExited || deadline.ElapsedMilliseconds > startupLimit)
                            throw new InvalidOperationException();
                    }
                    bool healthy = false;
                    while (!healthy && deadline.ElapsedMilliseconds < (backend.PreparingRecordings ? 330000 : 45000) && !exiting && !backend.Process.HasExited)
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
                phase = "running"; ready = true; busy = false; open.Enabled = true;
                workspaceHelp.Visible = true;
                status.Text = "Recording service is running. Camera engine ready. Select Open camera wall to add cameras and check live video and recording.";
                tray.Text = "K5 Vision — camera engine ready";
                if (!options.NoBrowser) OpenDashboard();
            }
            catch { failed = true; }
            if (failed)
            {
                await Task.Run(delegate { if (backend != null && !backend.Stop()) job.Dispose(); });
                Fail("K5 Vision could not prepare the recording service. Check your Internet connection, close any other K5 Vision app, and reopen this app. Your recordings have been kept.");
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
            phase = "failed"; busy = false; ready = false; open.Enabled = false; create.Enabled = false; workspaceHelp.Visible = false;
            status.Text = message; ShowOwner();
        }

        private async void StopAndQuit()
        {
            if (exiting) return;
            phase = "stopping"; exiting = true; ready = false; timer.Stop(); open.Enabled = create.Enabled = quit.Enabled = false;
            UpdateDiagnostic();
            cancellation.Cancel();
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
            if (disposing && !disposed)
            {
                disposed = true;
                exiting = true; timer.Dispose(); tray.Visible = false; tray.Dispose();
                cancellation.Cancel(); cancellation.Dispose();
                job.Dispose(); if (backend != null) backend.Dispose();
            }
            base.Dispose(disposing);
        }
    }
}
