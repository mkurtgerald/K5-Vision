using System;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Security.AccessControl;
using System.Security.Principal;
using System.Threading;
using Microsoft.Win32;

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

        internal static string InstallRoot
        {
            get { return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles), "K5 Vision"); }
        }
        internal static string DataRoot
        {
            get { return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData), "K5Vision"); }
        }
        private static void Require(bool condition)
        {
            if (!condition) throw new InvalidOperationException();
        }
        private static ProcessStatus Status(IntPtr service)
        {
            ProcessStatus result; int required;
            Require(QueryServiceStatusEx(service, 0, out result, Marshal.SizeOf(typeof(ProcessStatus)), out required));
            return result;
        }
        private static void AdmitConfiguration(IntPtr service, string root)
        {
            int required;
            QueryServiceConfig(service, IntPtr.Zero, 0, out required);
            Require(required >= Marshal.SizeOf(typeof(Configuration)) && required <= 32768);
            IntPtr buffer = Marshal.AllocHGlobal(required);
            try
            {
                Require(QueryServiceConfig(service, buffer, required, out required));
                Configuration value = (Configuration)Marshal.PtrToStructure(buffer, typeof(Configuration));
                Require(value.Type == 0x10 && String.Equals(Marshal.PtrToStringUni(value.Binary),
                    "\"" + Path.Combine(root, "K5VisionRecorder.exe") + "\"", StringComparison.OrdinalIgnoreCase));
                Require(String.Equals(Marshal.PtrToStringUni(value.Account), Account, StringComparison.OrdinalIgnoreCase));
            }
            finally { Marshal.FreeHGlobal(buffer); }
        }
        private static void StopAndWait(IntPtr service)
        {
            Stopwatch elapsed = Stopwatch.StartNew();
            ProcessStatus current = Status(service);
            IntPtr process = IntPtr.Zero;
            try
            {
                if (current.State != 1)
                {
                    Require(current.Pid != 0);
                    process = OpenProcess(0x100000, false, current.Pid);
                    Require(process != IntPtr.Zero);
                    if (current.State != 3)
                    {
                        ServiceStatus ignored;
                        if (!ControlService(service, 1, out ignored))
                            Require(Marshal.GetLastWin32Error() == 1062 && Status(service).State == 1);
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
            string directory = Custody.OrdinaryAbsolute(DataRoot);
            string parent = Path.GetDirectoryName(directory);
            // ProgramData itself permits creation of unrelated children. The
            // private boundary begins at our own atomically secured child.
            Custody.Admit(parent, directory);
            SecurityIdentifier service = (SecurityIdentifier)new NTAccount(Account).Translate(typeof(SecurityIdentifier));
            if (!Directory.Exists(directory))
            {
                DirectorySecurity security = new DirectorySecurity();
                security.SetAccessRuleProtection(true, false);
                security.SetOwner(new SecurityIdentifier("S-1-5-32-544"));
                foreach (SecurityIdentifier principal in new[] { service,
                    new SecurityIdentifier("S-1-5-18"), new SecurityIdentifier("S-1-5-32-544") })
                    security.AddAccessRule(new FileSystemAccessRule(principal, FileSystemRights.FullControl,
                        InheritanceFlags.ContainerInherit | InheritanceFlags.ObjectInherit,
                        PropagationFlags.None, AccessControlType.Allow));
                Directory.CreateDirectory(directory, security);
            }
            AdmitData(directory, service);
        }
        private static DirectorySecurity CodeDirectorySecurity()
        {
            DirectorySecurity security = new DirectorySecurity();
            security.SetAccessRuleProtection(true, false);
            security.SetOwner(new SecurityIdentifier("S-1-5-32-544"));
            foreach (string sid in new[] { "S-1-5-18", "S-1-5-32-544", "S-1-5-32-545" })
                security.AddAccessRule(new FileSystemAccessRule(new SecurityIdentifier(sid),
                    sid == "S-1-5-32-545" ? FileSystemRights.ReadAndExecute : FileSystemRights.FullControl,
                    InheritanceFlags.ContainerInherit | InheritanceFlags.ObjectInherit,
                    PropagationFlags.None, AccessControlType.Allow));
            return security;
        }
        private static void PrepareCode(string root)
        {
            Custody.AdmitCode(Path.GetDirectoryName(root), root);
            if (!Directory.Exists(root)) Directory.CreateDirectory(root, CodeDirectorySecurity());
            else
            {
                Custody.AdmitCode(root, root);
                Directory.SetAccessControl(root, CodeDirectorySecurity());
            }
        }
        private static void SealCode(string directory, string root)
        {
            Require((File.GetAttributes(directory) & FileAttributes.ReparsePoint) == 0);
            Directory.SetAccessControl(directory, CodeDirectorySecurity());
            foreach (string entry in Directory.GetFileSystemEntries(directory))
            {
                Require((File.GetAttributes(entry) & FileAttributes.ReparsePoint) == 0);
                if (Directory.Exists(entry)) SealCode(entry, root);
                else
                {
                    FileSecurity security = new FileSecurity();
                    security.SetAccessRuleProtection(true, false);
                    security.SetOwner(new SecurityIdentifier("S-1-5-32-544"));
                    foreach (string sid in new[] { "S-1-5-18", "S-1-5-32-544", "S-1-5-32-545" })
                        security.AddAccessRule(new FileSystemAccessRule(new SecurityIdentifier(sid),
                            sid == "S-1-5-32-545" ? FileSystemRights.ReadAndExecute : FileSystemRights.FullControl,
                            AccessControlType.Allow));
                    File.SetAccessControl(entry, security);
                }
            }
            Directory.SetAccessControl(directory, CodeDirectorySecurity());
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
            Flag sid = new Flag { Value = 1 }; // SERVICE_SID_TYPE_UNRESTRICTED
            Flag nondelayed = new Flag { Value = 0 };
            Flag failedExit = new Flag { Value = 1 };
            Require(ChangeServiceConfig2(service, 5, ref sid));
            Require(ChangeServiceConfig2(service, 3, ref nondelayed));
            Require(ChangeServiceConfig2(service, 4, ref failedExit));
            int size = Marshal.SizeOf(typeof(RecoveryAction));
            IntPtr actions = Marshal.AllocHGlobal(size * 3);
            try
            {
                uint[] delays = { 5000, 15000, 60000 };
                for (int index = 0; index < delays.Length; index++)
                    Marshal.StructureToPtr(new RecoveryAction { Type = 1, Delay = delays[index] },
                        IntPtr.Add(actions, index * size), false);
                FailureActions failure = new FailureActions { ResetPeriod = 3600, Count = 3, Actions = actions };
                Require(ChangeServiceConfig2(service, 2, ref failure));
            }
            finally { Marshal.FreeHGlobal(actions); }
            RawSecurityDescriptor descriptor = new RawSecurityDescriptor(
                "D:P(A;;GA;;;SY)(A;;GA;;;BA)(A;;CCLCSWLORC;;;AU)");
            byte[] bytes = new byte[descriptor.BinaryLength]; descriptor.GetBinaryForm(bytes, 0);
            Require(SetServiceObjectSecurity(service, 4, bytes));
        }

        internal static int Execute(string operation, string root)
        {
            IntPtr manager = IntPtr.Zero, service = IntPtr.Zero;
            try
            {
                Require(new WindowsPrincipal(WindowsIdentity.GetCurrent()).IsInRole(WindowsBuiltInRole.Administrator));
                Require(operation == "stop" || operation == "install" || operation == "start" || operation == "remove" ||
                    operation == "prepare" || operation == "seal" || operation == "legacy-check");
                root = Custody.OrdinaryAbsolute(root);
                Require(String.Equals(root, Custody.OrdinaryAbsolute(InstallRoot), StringComparison.OrdinalIgnoreCase));
                if (operation == "legacy-check") { RequireNoLegacyProfiles(); return 0; }
                if (operation == "prepare") { PrepareCode(root); return 0; }
                if (operation == "seal")
                {
                    Custody.VerifySealedFiles(root);
                    SealCode(root, root);
                    return 0;
                }
                manager = OpenSCManager(null, null, operation == "install" ? 3u : 1u);
                Require(manager != IntPtr.Zero);
                uint access = operation == "install" ? AllAccess : QueryConfig | QueryStatus | ReadControl |
                    (operation == "start" ? Start : Stop) | (operation == "remove" ? Delete : 0u);
                service = OpenService(manager, Name, access);
                if (service == IntPtr.Zero)
                {
                    Require(Marshal.GetLastWin32Error() == MissingService);
                    if (operation == "stop" || operation == "remove") return 0;
                    Require(operation == "install");
                    Custody.VerifyPayload(root);
                    service = CreateService(manager, Name, "K5 Vision Recorder", AllAccess,
                        0x10, 2, 1, "\"" + Path.Combine(root, "K5VisionRecorder.exe") + "\"",
                        null, IntPtr.Zero, null, Account, null);
                    Require(service != IntPtr.Zero);
                }
                AdmitConfiguration(service, root);
                if (operation == "stop" || operation == "remove" || operation == "install") StopAndWait(service);
                if (operation == "remove") { Require(DeleteService(service)); return 0; }
                if (operation == "stop") return 0;
                Custody.VerifyPayload(root);
                if (operation == "install")
                {
                    Require(ChangeServiceConfig(service, 0xFFFFFFFF, 2, 0xFFFFFFFF, null,
                        null, IntPtr.Zero, null, Account, null, "K5 Vision Recorder"));
                    ConfigureRecovery(service);
                    PrepareData();
                    return 0;
                }
                Require(StartService(service, 0, IntPtr.Zero) || Marshal.GetLastWin32Error() == 1056);
                Stopwatch elapsed = Stopwatch.StartNew();
                while (true)
                {
                    ProcessStatus current = Status(service);
                    Require(current.State != 1 && elapsed.ElapsedMilliseconds < 45000);
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
                        catch { }
                    }
                    Thread.Sleep(100);
                }
            }
            catch { return 1; }
            finally
            {
                if (service != IntPtr.Zero) CloseServiceHandle(service);
                if (manager != IntPtr.Zero) CloseServiceHandle(manager);
            }
        }
    }
}
