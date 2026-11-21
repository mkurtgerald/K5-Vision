using System;
using System.Collections;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Security.AccessControl;
using System.Security.Principal;
using System.Threading;
using Microsoft.Win32;
using Microsoft.Win32.SafeHandles;

namespace K5VisionDesktop
{
    // Invoked only by the hash-pinned native installer guard. No supplied
    // command, account, service name, executable or data path is accepted.
    internal static class ServiceInstallation
    {
        internal const string Name = "K5VisionRecorder";
        internal const string Account = "NT SERVICE\\K5VisionRecorder";
        internal const string InstallationMutex = "Global\\K5VisionMachineInstallation";
        private const uint AllAccess = 0xF01FF;
        private const uint QueryConfig = 1, QueryStatus = 4, Start = 0x10, Stop = 0x20;
        private const uint Delete = 0x10000, ReadControl = 0x20000;
        private const int MissingService = 1060;
        private enum RefusalPhase
        {
            Preflight = 10, Manifest = 11, OpenHandle = 12, FileIdentity = 13,
            Inventory = 14, Hash = 15, Acl = 16, ScmOpen = 20,
            ScmConfiguration = 21, ScmCreate = 22, ScmStop = 23, Data = 24,
            Recovery = 25, ScmStart = 26, Readiness = 27, Logging = 28
        }
        private static RefusalPhase refusalPhase = RefusalPhase.Preflight;
        private static int FailureCode(int nativeError)
        {
            return ((int)refusalPhase << 16) | (nativeError >= 0 && nativeError <= 65535 ? nativeError : 0);
        }
        private sealed class Refused : Exception
        {
            internal readonly int Code;
            internal Refused(int nativeError) { Code = FailureCode(nativeError); }
        }
        private static int NativeHResult(int value)
        {
            return ((uint)value & 0xFFFF0000u) == 0x80070000u ? value & 65535 : 0;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct ServiceStatus
        {
            internal uint Type, State, Accepted, Win32Exit, SpecificExit, Checkpoint, WaitHint;
        }
        [StructLayout(LayoutKind.Sequential)]
        private struct ProcessStatus
        {
            internal uint Type, State, Accepted, Win32Exit, SpecificExit, Checkpoint, WaitHint, Pid, Flags;
        }
        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
        private struct Configuration
        {
            internal uint Type, StartType, ErrorControl;
            internal IntPtr Binary, Group;
            internal uint Tag;
            internal IntPtr Dependencies, Account, Display;
        }
        [StructLayout(LayoutKind.Sequential)]
        private struct Flag { internal uint Value; }
        [StructLayout(LayoutKind.Sequential)]
        private struct RecoveryAction { internal uint Type, Delay; }
        [StructLayout(LayoutKind.Sequential)]
        private struct FailureActions
        {
            internal uint ResetPeriod;
            internal IntPtr Reboot, Command;
            internal uint Count;
            internal IntPtr Actions;
        }
        [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern IntPtr OpenSCManager(string machine, string database, uint access);
        [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern IntPtr OpenService(IntPtr manager, string name, uint access);
        [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern IntPtr CreateService(IntPtr manager, string name, string display,
            uint access, uint type, uint start, uint error, string binary, string group,
            IntPtr tag, string dependencies, string account, string password);
        [DllImport("advapi32.dll", SetLastError = true)]
        private static extern bool CloseServiceHandle(IntPtr handle);
        [DllImport("advapi32.dll", SetLastError = true)]
        private static extern bool QueryServiceStatusEx(IntPtr service, int level,
            out ProcessStatus status, int size, out int required);
        [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern bool QueryServiceConfig(IntPtr service, IntPtr config, int size, out int needed);
        [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern bool ChangeServiceConfig(IntPtr service, uint type, uint start,
            uint error, string binary, string group, IntPtr tag, string dependencies,
            string account, string password, string display);
        [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern bool ChangeServiceConfig2(IntPtr service, int kind, ref Flag value);
        [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern bool ChangeServiceConfig2(IntPtr service, int kind, ref FailureActions value);
        [DllImport("advapi32.dll", SetLastError = true)]
        private static extern bool SetServiceObjectSecurity(IntPtr service, uint information, byte[] descriptor);
        [DllImport("advapi32.dll", SetLastError = true)]
        private static extern bool StartService(IntPtr service, int count, IntPtr arguments);
        [DllImport("advapi32.dll", SetLastError = true)]
        private static extern bool ControlService(IntPtr service, uint control, out ServiceStatus status);
        [DllImport("advapi32.dll", SetLastError = true)]
        private static extern bool DeleteService(IntPtr service);
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern IntPtr OpenProcess(uint access, bool inherit, uint pid);
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern uint WaitForSingleObject(IntPtr handle, uint milliseconds);
        [DllImport("kernel32.dll")]
        private static extern bool CloseHandle(IntPtr handle);
        [StructLayout(LayoutKind.Sequential)]
        private struct FileIdentity
        {
            internal uint Attributes;
            internal System.Runtime.InteropServices.ComTypes.FILETIME Created, Accessed, Written;
            internal uint Volume, SizeHigh, SizeLow, Links, IndexHigh, IndexLow;
        }
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern SafeFileHandle CreateFile(string path, uint access, uint share,
            IntPtr security, uint creation, uint attributes, IntPtr template);
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool GetFileInformationByHandle(SafeFileHandle handle, out FileIdentity identity);
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern uint GetFileType(SafeFileHandle handle);
        [DllImport("advapi32.dll", SetLastError = true)]
        private static extern bool GetSecurityDescriptorOwner(IntPtr descriptor, out IntPtr owner, out bool defaulted);
        [DllImport("advapi32.dll", SetLastError = true)]
        private static extern bool GetSecurityDescriptorDacl(IntPtr descriptor, out bool present,
            out IntPtr acl, out bool defaulted);
        [DllImport("advapi32.dll", SetLastError = true)]
        private static extern uint SetSecurityInfo(SafeFileHandle handle, int kind, uint information,
            IntPtr owner, IntPtr group, IntPtr dacl, IntPtr sacl);

        internal static string InstallRoot
        {
            get { return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles), "K5 Vision"); }
        }
        internal static string DataRoot
        {
            get { return Custody.MachineDataRoot; }
        }
        private static void Require(bool condition)
        {
            if (!condition) throw new Refused(0);
        }
        private static void RequireWin32(bool condition)
        {
            if (!condition) throw new Refused(Marshal.GetLastWin32Error());
        }
        private static ProcessStatus Status(IntPtr service)
        {
            ProcessStatus result; int required;
            RequireWin32(QueryServiceStatusEx(service, 0, out result, Marshal.SizeOf(typeof(ProcessStatus)), out required));
            return result;
        }
        private static void AdmitConfiguration(IntPtr service, string root)
        {
            refusalPhase = RefusalPhase.ScmConfiguration;
            int required;
            QueryServiceConfig(service, IntPtr.Zero, 0, out required);
            Require(required >= Marshal.SizeOf(typeof(Configuration)) && required <= 32768);
            IntPtr buffer = Marshal.AllocHGlobal(required);
            try
            {
                RequireWin32(QueryServiceConfig(service, buffer, required, out required));
                Configuration value = (Configuration)Marshal.PtrToStructure(buffer, typeof(Configuration));
                Require(value.Type == 0x10 && String.Equals(Marshal.PtrToStringUni(value.Binary),
                    "\"" + Path.Combine(root, "K5VisionRecorder.exe") + "\"", StringComparison.OrdinalIgnoreCase));
                Require(String.Equals(Marshal.PtrToStringUni(value.Account), Account, StringComparison.OrdinalIgnoreCase));
            }
            finally { Marshal.FreeHGlobal(buffer); }
        }
        private static void StopAndWait(IntPtr service)
        {
            refusalPhase = RefusalPhase.ScmStop;
            Stopwatch elapsed = Stopwatch.StartNew();
            ProcessStatus current = Status(service);
            IntPtr process = IntPtr.Zero;
            try
            {
                if (current.State != 1)
                {
                    Require(current.Pid != 0);
                    process = OpenProcess(0x100000, false, current.Pid);
                    RequireWin32(process != IntPtr.Zero);
                    if (current.State != 3)
                    {
                        ServiceStatus ignored;
                        if (!ControlService(service, 1, out ignored))
                        {
                            int error = Marshal.GetLastWin32Error();
                            if (error != 1062) throw new Refused(error);
                            Require(Status(service).State == 1);
                        }
                    }
                }
                while (Status(service).State != 1)
                {
                    Require(elapsed.ElapsedMilliseconds < 90000);
                    Thread.Sleep(100);
                }
                // SCM STOPPED alone is not proof that the admitted wrapper has
                // exited. Hold and wait its original handle, immune to PID reuse.
                if (process != IntPtr.Zero)
                    Require(WaitForSingleObject(process, (uint)Math.Max(0, 90000 - elapsed.ElapsedMilliseconds)) == 0);
            }
            finally { if (process != IntPtr.Zero) CloseHandle(process); }
        }
        private static void PrepareData()
        {
            refusalPhase = RefusalPhase.Data;
            string directory = Custody.OrdinaryAbsolute(DataRoot);
            string parent = Path.GetDirectoryName(directory);
            // ProgramData itself permits creation of unrelated children. The
            // private boundary begins at our own atomically secured child.
            Custody.AdmitMachineData(parent, directory);
            SecurityIdentifier service = (SecurityIdentifier)new NTAccount(Account).Translate(typeof(SecurityIdentifier));
            if (!Directory.Exists(directory))
                Directory.CreateDirectory(directory, DataDirectorySecurity(service));
            AdmitData(directory, service);
            // Storage's default leaf already exists before Session 0 starts.
            // Its preparation therefore validates this private child directly,
            // without trying to create its already-existing private parent.
            string recordings = Path.Combine(directory, "recordings");
            if (!Directory.Exists(recordings))
                Directory.CreateDirectory(recordings, DataDirectorySecurity(service));
            AdmitData(recordings, service);
        }
        private static DirectorySecurity DataDirectorySecurity(SecurityIdentifier service)
        {
            DirectorySecurity security = new DirectorySecurity();
            security.SetAccessRuleProtection(true, false);
            security.SetOwner(new SecurityIdentifier("S-1-5-32-544"));
            foreach (SecurityIdentifier principal in new[] { service,
                new SecurityIdentifier("S-1-5-18"), new SecurityIdentifier("S-1-5-32-544") })
                security.AddAccessRule(new FileSystemAccessRule(principal, FileSystemRights.FullControl,
                    InheritanceFlags.ContainerInherit | InheritanceFlags.ObjectInherit,
                    PropagationFlags.None, AccessControlType.Allow));
            return security;
        }
        private static DirectorySecurity CodeDirectorySecurity(bool inherit)
        {
            DirectorySecurity security = new DirectorySecurity();
            security.SetAccessRuleProtection(true, false);
            security.SetOwner(new SecurityIdentifier("S-1-5-32-544"));
            foreach (string sid in new[] { "S-1-5-18", "S-1-5-32-544", "S-1-5-32-545" })
                security.AddAccessRule(new FileSystemAccessRule(new SecurityIdentifier(sid),
                    sid == "S-1-5-32-545" ? FileSystemRights.ReadAndExecute : FileSystemRights.FullControl,
                    inherit ? InheritanceFlags.ContainerInherit | InheritanceFlags.ObjectInherit : InheritanceFlags.None,
                    PropagationFlags.None, AccessControlType.Allow));
            return security;
        }
        private static void PrepareCode(string root)
        {
            refusalPhase = RefusalPhase.Preflight;
            Custody.AdmitCode(Path.GetDirectoryName(root), root);
            if (!Directory.Exists(root)) Directory.CreateDirectory(root, CodeDirectorySecurity(true));
            else Custody.AdmitCode(root, root);
        }
        private sealed class PinnedEntry : IDisposable
        {
            internal readonly string Relative;
            internal readonly bool Directory;
            internal readonly SafeFileHandle Handle;
            internal readonly FileStream Content;

            internal PinnedEntry(string path, string relative)
            {
                refusalPhase = RefusalPhase.OpenHandle;
                Relative = relative;
                // OPEN_REPARSE_POINT prevents following a newly planted link;
                // BACKUP_SEMANTICS admits ordinary directories. Share READ only
                // pins names/ancestors against rename and denies live writers.
                // MAXIMUM_ALLOWED is deliberate: SetSecurityInfo documents
                // that handles opened this way do not propagate ACL changes
                // to children. Every admitted object is secured explicitly.
                Handle = CreateFile(path, 0x02000000u, 1, IntPtr.Zero, 3, 0x02200000u, IntPtr.Zero);
                try
                {
                    FileIdentity identity = new FileIdentity();
                    RequireWin32(Handle != null && !Handle.IsInvalid);
                    refusalPhase = RefusalPhase.FileIdentity;
                    Require(GetFileType(Handle) == 1);
                    RequireWin32(GetFileInformationByHandle(Handle, out identity));
                    Require((identity.Attributes & 0x400u) == 0);
                    Directory = (identity.Attributes & 0x10u) != 0;
                    if (!Directory)
                    {
                        Require(identity.Links == 1);
                        Content = new FileStream(Handle, FileAccess.Read, 65536, false);
                    }
                }
                catch { Handle.Dispose(); throw; }
            }
            internal void AssertOrdinary()
            {
                refusalPhase = RefusalPhase.FileIdentity;
                FileIdentity identity;
                Require(GetFileType(Handle) == 1);
                RequireWin32(GetFileInformationByHandle(Handle, out identity));
                Require((identity.Attributes & 0x400u) == 0 && ((identity.Attributes & 0x10u) != 0) == Directory &&
                    (Directory || identity.Links == 1));
            }
            internal void ApplySecurity()
            {
                AssertOrdinary();
                refusalPhase = RefusalPhase.Acl;
                FileSystemSecurity security;
                // No inheritable ACEs: SetSecurityInfo may otherwise propagate
                // a directory DACL to an unlisted child added during staging.
                // Each admitted child receives its own protected DACL below.
                if (Directory) security = CodeDirectorySecurity(false);
                else
                {
                    FileSecurity file = new FileSecurity();
                    file.SetAccessRuleProtection(true, false);
                    file.SetOwner(new SecurityIdentifier("S-1-5-32-544"));
                    foreach (string sid in new[] { "S-1-5-18", "S-1-5-32-544", "S-1-5-32-545" })
                        file.AddAccessRule(new FileSystemAccessRule(new SecurityIdentifier(sid),
                            sid == "S-1-5-32-545" ? FileSystemRights.ReadAndExecute : FileSystemRights.FullControl,
                            AccessControlType.Allow));
                    security = file;
                }
                byte[] descriptor = security.GetSecurityDescriptorBinaryForm();
                GCHandle pinned = GCHandle.Alloc(descriptor, GCHandleType.Pinned);
                try
                {
                    IntPtr owner, dacl; bool defaulted, present;
                    RequireWin32(GetSecurityDescriptorOwner(pinned.AddrOfPinnedObject(), out owner, out defaulted));
                    RequireWin32(GetSecurityDescriptorDacl(pinned.AddrOfPinnedObject(), out present, out dacl, out defaulted));
                    Require(present);
                    // OWNER | DACL | PROTECTED_DACL, applied to the already
                    // verified object, never a path resolved a second time.
                    uint error = SetSecurityInfo(Handle, 1, 0x80000005u, owner, IntPtr.Zero, dacl, IntPtr.Zero);
                    if (error != 0) throw new Refused(error <= 65535 ? (int)error : 0);
                }
                finally { pinned.Free(); Array.Clear(descriptor, 0, descriptor.Length); }
            }
            public void Dispose()
            {
                if (Content != null) Content.Dispose();
                if (Handle != null) Handle.Dispose();
            }
        }
        private static void PinCodeTree(string directory, string root, HashSet<string> allowedFiles,
            HashSet<string> allowedDirectories, Dictionary<string, PinnedEntry> pinned)
        {
            string relative = directory.Length == root.Length ? "" : directory.Substring(root.Length + 1).Replace('\\', '/');
            PinnedEntry current = new PinnedEntry(directory, relative);
            try
            {
                refusalPhase = RefusalPhase.Inventory;
                Require(current.Directory && allowedDirectories.Contains(relative));
                pinned.Add(relative, current); current = null;
                foreach (string path in Directory.GetFileSystemEntries(directory))
                {
                    refusalPhase = RefusalPhase.Inventory;
                    string name = path.Substring(root.Length + 1).Replace('\\', '/');
                    Require(allowedFiles.Contains(name) || allowedDirectories.Contains(name));
                    PinnedEntry entry = new PinnedEntry(path, name);
                    try
                    {
                        refusalPhase = RefusalPhase.Inventory;
                        if (entry.Directory)
                        {
                            Require(allowedDirectories.Contains(name));
                            entry.Dispose(); entry = null;
                            PinCodeTree(path, root, allowedFiles, allowedDirectories, pinned);
                        }
                        else
                        {
                            Require(allowedFiles.Contains(name));
                            pinned.Add(name, entry); entry = null;
                        }
                    }
                    finally { if (entry != null) entry.Dispose(); }
                }
            }
            finally { if (current != null) current.Dispose(); }
        }
        private static void RecheckCodeInventory(string directory, string root, Dictionary<string, PinnedEntry> pinned,
            HashSet<string> observed)
        {
            refusalPhase = RefusalPhase.Inventory;
            string relative = directory.Length == root.Length ? "" : directory.Substring(root.Length + 1).Replace('\\', '/');
            Require(pinned.ContainsKey(relative) && pinned[relative].Directory && observed.Add(relative));
            foreach (string path in Directory.GetFileSystemEntries(directory))
            {
                string name = path.Substring(root.Length + 1).Replace('\\', '/');
                Require(pinned.ContainsKey(name));
                if (pinned[name].Directory) RecheckCodeInventory(path, root, pinned, observed);
                else Require(observed.Add(name));
            }
        }
        private static void SealCode(string root)
        {
            refusalPhase = RefusalPhase.Manifest;
            byte[] raw;
            using (FileStream source = new FileStream(Path.Combine(root, "payload-manifest.json"),
                FileMode.Open, FileAccess.Read, FileShare.Read))
            {
                Require(source.Length > 0 && source.Length <= 4 * 1024 * 1024);
                raw = new byte[(int)source.Length];
                int offset = 0;
                while (offset < raw.Length)
                {
                    int count = source.Read(raw, offset, raw.Length - offset);
                    Require(count > 0); offset += count;
                }
            }
            Require(Custody.Sha256(raw) == PayloadIdentity.ManifestSha256);
            Dictionary<string, object> manifest = Json.Parse(System.Text.Encoding.UTF8.GetString(raw));
            Require(Convert.ToInt32(manifest["schema_version"]) == 1 && (string)manifest["head_sha"] == PayloadIdentity.HeadSha);
            Dictionary<string, Dictionary<string, object>> expected = new Dictionary<string, Dictionary<string, object>>(StringComparer.OrdinalIgnoreCase);
            HashSet<string> allowedFiles = new HashSet<string>(StringComparer.OrdinalIgnoreCase) {
                "K5Vision.exe", "payload-manifest.json", "unins000.exe", "unins000.dat", "unins000.msg" };
            HashSet<string> allowedDirectories = new HashSet<string>(StringComparer.OrdinalIgnoreCase) { "" };
            foreach (object value in (IEnumerable)manifest["files"])
            {
                Dictionary<string, object> item = (Dictionary<string, object>)value;
                string path = (string)item["path"];
                Require(path.Length != 0 && !path.Contains("\\") && !path.StartsWith("/") &&
                    Array.TrueForAll(path.Split('/'), part => part.Length != 0 && part != "." && part != ".."));
                Require(!allowedFiles.Contains(path)); allowedFiles.Add(path); expected.Add(path, item);
                int slash = path.LastIndexOf('/');
                while (slash >= 0) { allowedDirectories.Add(path.Substring(0, slash)); slash = path.LastIndexOf('/', slash - 1); }
            }
            Dictionary<string, PinnedEntry> pinned = new Dictionary<string, PinnedEntry>(StringComparer.OrdinalIgnoreCase);
            try
            {
                PinCodeTree(root, root, allowedFiles, allowedDirectories, pinned);
                foreach (KeyValuePair<string, Dictionary<string, object>> item in expected)
                {
                    refusalPhase = RefusalPhase.Hash;
                    Require(pinned.ContainsKey(item.Key) && !pinned[item.Key].Directory);
                    FileStream content = pinned[item.Key].Content;
                    Require(content.Length == Convert.ToInt64(item.Value["bytes"]));
                    using (System.Security.Cryptography.SHA256 hash = System.Security.Cryptography.SHA256.Create())
                        Require(BitConverter.ToString(hash.ComputeHash(content)).Replace("-", "").ToLowerInvariant() == (string)item.Value["sha256"]);
                }
                Require(pinned.ContainsKey("K5Vision.exe") && pinned.ContainsKey("payload-manifest.json"));
                using (System.Security.Cryptography.SHA256 hash = System.Security.Cryptography.SHA256.Create())
                    Require(BitConverter.ToString(hash.ComputeHash(pinned["payload-manifest.json"].Content)).Replace("-", "").ToLowerInvariant() == PayloadIdentity.ManifestSha256);
                HashSet<string> observed = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
                RecheckCodeInventory(root, root, pinned, observed);
                Require(observed.Count == pinned.Count);
                foreach (PinnedEntry entry in pinned.Values) entry.AssertOrdinary();
                // No ACL change occurs until the entire allowed tree and all
                // ordinary single-link objects have been admitted and pinned.
                foreach (PinnedEntry entry in pinned.Values) entry.ApplySecurity();
            }
            finally { foreach (PinnedEntry entry in pinned.Values) entry.Dispose(); }
        }
        private static void RequireNoLegacyProfiles()
        {
            // Until migration is qualified, never replace an existing user's
            // installation or silently start a blank machine site. Enumerate
            // registered profiles, including the original user across UAC.
            using (RegistryKey profiles = Registry.LocalMachine.OpenSubKey(
                "SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\ProfileList", false))
            {
                Require(profiles != null);
                foreach (string name in profiles.GetSubKeyNames())
                using (RegistryKey profile = profiles.OpenSubKey(name, false))
                {
                    Require(profile != null);
                    string image = profile.GetValue("ProfileImagePath", null, RegistryValueOptions.DoNotExpandEnvironmentNames) as string;
                    Require(!String.IsNullOrEmpty(image));
                    string legacy = Path.Combine(Custody.OrdinaryAbsolute(Environment.ExpandEnvironmentVariables(image)),
                        "AppData", "Local", "K5VisionOperatorState");
                    try { File.GetAttributes(legacy); throw new InvalidOperationException(); }
                    catch (FileNotFoundException) { }
                    catch (DirectoryNotFoundException) { }
                    // Access denied, unavailable profile, and malformed paths
                    // remain ambiguous and fail closed instead of resetting.
                }
            }
        }
        private static void AdmitData(string directory, SecurityIdentifier service)
        {
            Require((File.GetAttributes(directory) & FileAttributes.ReparsePoint) == 0);
            DirectorySecurity security = Directory.GetAccessControl(directory,
                AccessControlSections.Access | AccessControlSections.Owner);
            string owner = security.GetOwner(typeof(SecurityIdentifier)).Value;
            string[] trusted = { service.Value, "S-1-5-18", "S-1-5-32-544" };
            Require(Array.IndexOf(trusted, owner) >= 0 && security.AreAccessRulesProtected);
            bool inheritedService = false;
            foreach (FileSystemAccessRule rule in security.GetAccessRules(true, true, typeof(SecurityIdentifier)))
            {
                if (rule.AccessControlType == AccessControlType.Allow)
                    Require(Array.IndexOf(trusted, rule.IdentityReference.Value) >= 0);
                if (rule.IdentityReference.Value == service.Value && rule.AccessControlType == AccessControlType.Allow &&
                    (rule.FileSystemRights & FileSystemRights.FullControl) == FileSystemRights.FullControl &&
                    (rule.InheritanceFlags & (InheritanceFlags.ContainerInherit | InheritanceFlags.ObjectInherit)) ==
                    (InheritanceFlags.ContainerInherit | InheritanceFlags.ObjectInherit)) inheritedService = true;
            }
            Require(inheritedService);
        }
        private static void ConfigureRecovery(IntPtr service)
        {
            refusalPhase = RefusalPhase.Recovery;
            Flag sid = new Flag { Value = 1 }; // SERVICE_SID_TYPE_UNRESTRICTED
            Flag nondelayed = new Flag { Value = 0 };
            Flag failedExit = new Flag { Value = 1 };
            RequireWin32(ChangeServiceConfig2(service, 5, ref sid));
            RequireWin32(ChangeServiceConfig2(service, 3, ref nondelayed));
            RequireWin32(ChangeServiceConfig2(service, 4, ref failedExit));
            int size = Marshal.SizeOf(typeof(RecoveryAction));
            IntPtr actions = Marshal.AllocHGlobal(size * 3);
            try
            {
                uint[] delays = { 5000, 15000, 60000 };
                for (int index = 0; index < delays.Length; index++)
                    Marshal.StructureToPtr(new RecoveryAction { Type = 1, Delay = delays[index] },
                        IntPtr.Add(actions, index * size), false);
                FailureActions failure = new FailureActions { ResetPeriod = 3600, Count = 3, Actions = actions };
                RequireWin32(ChangeServiceConfig2(service, 2, ref failure));
            }
            finally { Marshal.FreeHGlobal(actions); }
            RawSecurityDescriptor descriptor = new RawSecurityDescriptor(
                "D:P(A;;GA;;;SY)(A;;GA;;;BA)(A;;CCLCSWLORC;;;AU)");
            byte[] bytes = new byte[descriptor.BinaryLength]; descriptor.GetBinaryForm(bytes, 0);
            RequireWin32(SetServiceObjectSecurity(service, 4, bytes));
        }

        private static void RegisterEventLog()
        {
            refusalPhase = RefusalPhase.Logging;
            // Match WinSW's privileged install-time source registration. The
            // runtime virtual account must never create or rebind providers.
            if (!EventLog.SourceExists(Name, "."))
                EventLog.CreateEventSource(new EventSourceCreationData(Name, "Application"));
            Require(String.Equals(EventLog.LogNameFromSourceName(Name, "."),
                "Application", StringComparison.OrdinalIgnoreCase));
        }

        internal static int Execute(string operation, string root)
        {
            IntPtr manager = IntPtr.Zero, service = IntPtr.Zero;
            try
            {
                refusalPhase = RefusalPhase.Preflight;
                Require(new WindowsPrincipal(WindowsIdentity.GetCurrent()).IsInRole(WindowsBuiltInRole.Administrator));
                Require(operation == "stop" || operation == "install" || operation == "start" || operation == "remove" ||
                    operation == "prepare" || operation == "seal" || operation == "legacy-check");
                root = Custody.OrdinaryAbsolute(root);
                Require(String.Equals(root, Custody.OrdinaryAbsolute(InstallRoot), StringComparison.OrdinalIgnoreCase));
                if (operation == "legacy-check") { RequireNoLegacyProfiles(); return 0; }
                if (operation == "prepare") { PrepareCode(root); return 0; }
                if (operation == "seal")
                {
                    refusalPhase = RefusalPhase.Manifest;
                    Custody.VerifySealedFiles(root);
                    SealCode(root);
                    return 0;
                }
                refusalPhase = RefusalPhase.ScmOpen;
                manager = OpenSCManager(null, null, operation == "install" ? 3u : 1u);
                RequireWin32(manager != IntPtr.Zero);
                uint access = operation == "install" ? AllAccess : QueryConfig | QueryStatus | ReadControl |
                    (operation == "start" ? Start : Stop) | (operation == "remove" ? Delete : 0u);
                service = OpenService(manager, Name, access);
                if (service == IntPtr.Zero)
                {
                    int error = Marshal.GetLastWin32Error();
                    if (error != MissingService) throw new Refused(error);
                    if (operation == "stop" || operation == "remove") return 0;
                    Require(operation == "install");
                    refusalPhase = RefusalPhase.Manifest;
                    Custody.VerifyPayload(root);
                    refusalPhase = RefusalPhase.ScmCreate;
                    service = CreateService(manager, Name, "K5 Vision Recorder", AllAccess,
                        0x10, 2, 1, "\"" + Path.Combine(root, "K5VisionRecorder.exe") + "\"",
                        null, IntPtr.Zero, null, Account, null);
                    RequireWin32(service != IntPtr.Zero);
                }
                AdmitConfiguration(service, root);
                if (operation == "stop" || operation == "remove" || operation == "install") StopAndWait(service);
                if (operation == "remove") { RequireWin32(DeleteService(service)); return 0; }
                if (operation == "stop") return 0;
                refusalPhase = RefusalPhase.Manifest;
                Custody.VerifyPayload(root);
                if (operation == "install")
                {
                    refusalPhase = RefusalPhase.ScmConfiguration;
                    RequireWin32(ChangeServiceConfig(service, 0xFFFFFFFF, 2, 0xFFFFFFFF, null,
                        null, IntPtr.Zero, null, Account, null, "K5 Vision Recorder"));
                    ConfigureRecovery(service);
                    RegisterEventLog();
                    PrepareData();
                    return 0;
                }
                refusalPhase = RefusalPhase.ScmStart;
                if (!StartService(service, 0, IntPtr.Zero))
                {
                    int error = Marshal.GetLastWin32Error();
                    if (error != 1056) throw new Refused(error);
                }
                refusalPhase = RefusalPhase.Readiness;
                Stopwatch elapsed = Stopwatch.StartNew();
                int readinessError = 0;
                while (true)
                {
                    ProcessStatus current = Status(service);
                    if (current.State == 1 || elapsed.ElapsedMilliseconds >= 45000)
                        throw new Refused(readinessError);
                    if (current.State == 4)
                    {
                        // WinSW reports SCM_RUNNING before the engine is ready.
                        // Finish only after the independently attested service
                        // answers a finite, truthful startup state.
                        try
                        {
                            System.Collections.Generic.Dictionary<string, object> health = RecorderClient.Exchange(
                                RecorderPaths.Request("status"), false);
                            string state = health["status"] as string;
                            if (state == "account_required" || state == "running" || state == "preparing_recordings") return 0;
                        }
                        catch (IOException error) { readinessError = NativeHResult(error.HResult); }
                        catch (UnauthorizedAccessException) { readinessError = 5; }
                        catch { readinessError = 0; }
                    }
                    Thread.Sleep(100);
                }
            }
            catch (Refused error) { return error.Code; }
            catch (System.ComponentModel.Win32Exception error) { return FailureCode(error.NativeErrorCode); }
            catch (IOException error) { return FailureCode(NativeHResult(error.HResult)); }
            catch (UnauthorizedAccessException) { return FailureCode(5); }
            catch { return FailureCode(0); }
            finally
            {
                if (service != IntPtr.Zero) CloseServiceHandle(service);
                if (manager != IntPtr.Zero) CloseServiceHandle(manager);
            }
        }
    }
}
