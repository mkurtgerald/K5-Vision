// WinSW owns SCM registration, boot start and recovery. This process owns only
// the sealed Python engine and its private pipes; interactive clients own none
// of the engine's handles. Credentials never enter arguments or environment.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.IO.Pipes;
using System.Linq;
using System.Runtime.InteropServices;
using System.Security.AccessControl;
using System.Security.Principal;
using System.Text;
using System.Threading;
using Microsoft.Win32.SafeHandles;

namespace K5VisionDesktop
{
    internal static class RecorderProcessIdentity
    {
        internal static bool Same(uint expectedPid, long expectedBirth, uint actualPid, long actualBirth)
        {
            return expectedPid != 0 && expectedBirth > 0 && expectedPid == actualPid && expectedBirth == actualBirth;
        }

        internal static bool OwnedStartupHost(uint parentPid, long parentBirth, uint parentPidAgain,
            long parentBirthAgain, uint helperPid, uint hostPid, uint nativeCreatorPid, long hostBirth, long hostBirthAgain)
        {
            // Native creation times identify one kernel process lifetime. Their
            // order cannot establish ancestry across an NTP/system clock step.
            return Same(parentPid, parentBirth, parentPidAgain, parentBirthAgain) &&
                helperPid != 0 && hostPid != helperPid && nativeCreatorPid == parentPid &&
                Same(hostPid, hostBirth, hostPid, hostBirthAgain);
        }
    }

    internal static class RecorderBootstrap
    {
        private static readonly object Gate = new object();
        private static string phase = "token";
        internal static void Advance(string value)
        {
            lock (Gate)
            {
                if (phase == value && value != "token") return;
                phase = value;
                Emit(new Dictionary<string, object> { { "schema_version", 1 }, { "k5_recorder_bootstrap", phase } });
            }
        }
        internal static void Failure(Exception error)
        {
            string kind = error.GetType().Name;
            if (error.GetType().Namespace == null || !error.GetType().Namespace.StartsWith("System", StringComparison.Ordinal) ||
                !new[] { "InvalidOperationException", "UnauthorizedAccessException", "IOException", "Win32Exception",
                    "ArgumentException", "NotSupportedException", "TypeInitializationException", "SecurityException", "TimeoutException" }
                    .Contains(kind)) kind = "Other";
            lock (Gate) Emit(new Dictionary<string, object> { { "schema_version", 1 }, { "k5_recorder_bootstrap", phase },
                { "failure_type", kind }, { "failure_code", error.HResult & 65535 } });
        }
        private static void Emit(Dictionary<string, object> value)
        {
            try
            {
                value["host_pid"] = Process.GetCurrentProcess().Id;
                value["host_created_100ns"] = RecorderNative.CurrentCreated();
                Console.Out.WriteLine(Json.Serializer.Serialize(value)); Console.Out.Flush();
            }
            catch { }
        }
    }

    internal static class RecorderPaths
    {
        internal const string ServiceName = "K5VisionRecorder";
        internal const int Port = 8000;
        internal const string StatusPipe = "K5VisionRecorder.Status.v1";
        internal const string AdminPipe = "K5VisionRecorder.Admin.v1";
        internal const string ControlPipe = "K5VisionRecorder.Control.v1";
        internal static readonly string ServiceSid = ((SecurityIdentifier)new NTAccount(
            "NT SERVICE", ServiceName).Translate(typeof(SecurityIdentifier))).Value;

        [DllImport("shell32.dll", CharSet = CharSet.Unicode)]
        private static extern int SHGetKnownFolderPath(ref Guid folder, uint flags, IntPtr token, out IntPtr path);

        internal static string DataRoot
        {
            get
            {
                Guid folder = new Guid("62AB5D82-FDC1-4DC3-A9DD-070D1D495D97");
                IntPtr memory;
                if (SHGetKnownFolderPath(ref folder, 0, IntPtr.Zero, out memory) != 0 || memory == IntPtr.Zero)
                    throw new InvalidOperationException();
                try { return Custody.OrdinaryAbsolute(Path.Combine(Marshal.PtrToStringUni(memory), "K5Vision")); }
                finally { Marshal.FreeCoTaskMem(memory); }
            }
        }

        internal static Dictionary<string, object> Request(string operation)
        {
            return new Dictionary<string, object> { { "schema_version", 1 }, { "operation", operation } };
        }
    }

    internal static class RecorderNative
    {
        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
        private struct ProcessEntry
        {
            internal uint Size, Usage, Pid;
            internal UIntPtr Heap;
            internal uint Module, Threads, Parent;
            internal int Priority;
            internal uint Flags;
            [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 260)] internal string Executable;
        }
        [StructLayout(LayoutKind.Sequential)]
        private struct ScmStatus
        {
            internal uint Type, State, Accepted, Win32Exit, SpecificExit, Checkpoint, WaitHint, Pid, Flags;
        }
        [StructLayout(LayoutKind.Sequential)]
        private struct ScmConfiguration
        {
            internal uint Type, Start, Error;
            internal IntPtr Binary, Group;
            internal uint Tag;
            internal IntPtr Dependencies, Account, Display;
        }
        [StructLayout(LayoutKind.Sequential)]
        private struct ProcessBasicInformation
        {
            internal IntPtr Reserved1, Peb, Reserved2, Reserved3, ProcessId, ParentProcessId;
        }
        [StructLayout(LayoutKind.Sequential)]
        private struct SecurityAttributes
        {
            internal int Length;
            internal IntPtr Descriptor;
            [MarshalAs(UnmanagedType.Bool)] internal bool Inherit;
        }

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern SafePipeHandle CreateNamedPipe(string name, uint openMode, uint pipeMode,
            uint instances, uint outputSize, uint inputSize, uint defaultTimeout, ref SecurityAttributes security);
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool GetNamedPipeServerProcessId(SafePipeHandle pipe, out uint processId);
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern SafeProcessHandle OpenProcess(uint access, bool inherit, uint processId);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern bool QueryFullProcessImageName(SafeProcessHandle process, uint flags,
            StringBuilder name, ref uint size);
        [DllImport("ntdll.dll")]
        private static extern int NtQueryInformationProcess(IntPtr process, int information,
            ref ProcessBasicInformation data, int length, out int returned);
        [DllImport("advapi32.dll", SetLastError = true)]
        private static extern bool OpenProcessToken(SafeProcessHandle process, uint access, out SafeAccessTokenHandle token);
        [DllImport("advapi32.dll", SetLastError = true)]
        private static extern bool GetTokenInformation(SafeAccessTokenHandle token, int information,
            IntPtr data, uint length, out uint required);
        [DllImport("kernel32.dll", SetLastError = true)]
        internal static extern uint WaitForSingleObject(SafeProcessHandle process, uint milliseconds);
        [DllImport("kernel32.dll", SetLastError = true)]
        internal static extern bool TerminateProcess(SafeProcessHandle process, uint exitCode);
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool CancelIoEx(SafePipeHandle pipe, IntPtr overlapped);
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool GetProcessTimes(SafeProcessHandle process, out long created, out long exited,
            out long kernel, out long user);
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern uint GetProcessId(SafeProcessHandle process);
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool ProcessIdToSessionId(uint process, out uint session);
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern SafeFileHandle CreateToolhelp32Snapshot(uint flags, uint process);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern bool Process32First(SafeFileHandle snapshot, ref ProcessEntry entry);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern bool Process32Next(SafeFileHandle snapshot, ref ProcessEntry entry);
        [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern IntPtr OpenSCManager(string machine, string database, uint access);
        [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern IntPtr OpenService(IntPtr manager, string name, uint access);
        [DllImport("advapi32.dll", SetLastError = true)]
        private static extern bool CloseServiceHandle(IntPtr handle);
        [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern bool QueryServiceConfig(IntPtr service, IntPtr data, int length, out int needed);
        [DllImport("advapi32.dll", SetLastError = true)]
        private static extern bool QueryServiceStatusEx(IntPtr service, int level, out ScmStatus status, int size, out int needed);

        internal static void Disconnect(NamedPipeServerStream pipe)
        {
            // Keep the FIRST_PIPE_INSTANCE handle and its name while cancelling
            // a slow/malformed client. Releasing that name would permit a local
            // pipe squatter to deny recording through the public status pipe.
            CancelIoEx(pipe.SafePipeHandle, IntPtr.Zero);
            // A client EOF changes .NET's state to Broken (IsConnected=false).
            // Disconnect supports Broken and is required before the next wait.
            pipe.Disconnect();
        }

        internal static void Cancel(PipeStream pipe) { CancelIoEx(pipe.SafePipeHandle, IntPtr.Zero); }

        internal static void AdmitStatusOwner(NamedPipeClientStream pipe)
        {
            // Standard operators need no QUERY_PROCESS/TOKEN_QUERY permission
            // on a virtual-account process just to read finite public status.
            // An ordinary user cannot assign this foreign service SID owner.
            if (pipe.GetAccessControl().GetOwner(typeof(SecurityIdentifier)).Value != RecorderPaths.ServiceSid)
                throw new InvalidOperationException();
        }

        internal static NamedPipeServerStream CreatePipe(string name, bool publicStatus)
        {
            PipeSecurity security = new PipeSecurity();
            security.SetOwner(new SecurityIdentifier(RecorderPaths.ServiceSid));
            security.SetAccessRuleProtection(true, false);
            foreach (string sid in new[] { RecorderPaths.ServiceSid, "S-1-5-18", "S-1-5-32-544" })
                security.AddAccessRule(new PipeAccessRule(new SecurityIdentifier(sid), PipeAccessRights.FullControl,
                    AccessControlType.Allow));
            if (publicStatus)
                security.AddAccessRule(new PipeAccessRule(new SecurityIdentifier("S-1-5-11"),
                    PipeAccessRights.ReadWrite | PipeAccessRights.Synchronize, AccessControlType.Allow));
            byte[] descriptor = security.GetSecurityDescriptorBinaryForm();
            GCHandle pinned = GCHandle.Alloc(descriptor, GCHandleType.Pinned);
            SafePipeHandle handle = null;
            try
            {
                SecurityAttributes attributes = new SecurityAttributes {
                    Length = Marshal.SizeOf(typeof(SecurityAttributes)), Descriptor = pinned.AddrOfPinnedObject(), Inherit = false };
                // Duplex, overlapped, FIRST_PIPE_INSTANCE, byte stream, and
                // PIPE_REJECT_REMOTE_CLIENTS. No inherited service/pipe handles.
                handle = CreateNamedPipe("\\\\.\\pipe\\" + name, 0x40080003u, 0x8u, 1, 32772, 32772, 1000, ref attributes);
                if (handle == null || handle.IsInvalid)
                    throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
                NamedPipeServerStream stream = new NamedPipeServerStream(PipeDirection.InOut, true, false, handle);
                handle = null;
                return stream;
            }
            finally
            {
                if (handle != null) handle.Dispose();
                pinned.Free();
                Array.Clear(descriptor, 0, descriptor.Length);
            }
        }

        internal static SafeProcessHandle AdmitServer(NamedPipeClientStream pipe, bool allowTerminate)
        {
            uint pid;
            if (!GetNamedPipeServerProcessId(pipe.SafePipeHandle, out pid) || pid == 0) throw new InvalidOperationException();
            return AdmitProcess(pid, "K5Vision.exe", allowTerminate);
        }

        internal static SafeProcessHandle AdmitParent()
        {
            ProcessBasicInformation information = new ProcessBasicInformation();
            int returned;
            if (NtQueryInformationProcess(Process.GetCurrentProcess().Handle, 0, ref information,
                Marshal.SizeOf(typeof(ProcessBasicInformation)), out returned) != 0 ||
                information.ParentProcessId.ToInt64() <= 0 || information.ParentProcessId.ToInt64() > UInt32.MaxValue)
                throw new InvalidOperationException();
            SafeProcessHandle parent = AdmitProcess((uint)information.ParentProcessId.ToInt64(), "K5VisionRecorder.exe", false);
            try
            {
                uint parentPid = GetProcessId(parent);
                long parentBirth = Created(parent);
                using (SafeProcessHandle current = OpenProcess(0x00101000u, false, (uint)Process.GetCurrentProcess().Id))
                    if (current == null || current.IsInvalid || Creator(current) != parentPid ||
                        !RecorderProcessIdentity.Same(parentPid, parentBirth, GetProcessId(parent), Created(parent)) ||
                        WaitForSingleObject(parent, 0) != 0x102u)
                        throw new InvalidOperationException();
                SafeProcessHandle admitted = parent; parent = null; return admitted;
            }
            finally { if (parent != null) parent.Dispose(); }
        }

        private static SafeProcessHandle AdmitProcess(uint pid, string executable, bool allowTerminate)
        {
            SafeProcessHandle process = OpenProcess(0x00101000u | (allowTerminate ? 1u : 0u), false, pid);
            try
            {
                if (process == null || process.IsInvalid || WaitForSingleObject(process, 0) != 0x102u)
                    throw new InvalidOperationException();
                uint session;
                if (!ProcessIdToSessionId(pid, out session) || session != 0) throw new InvalidOperationException();
                StringBuilder image = new StringBuilder(4096);
                uint length = (uint)image.Capacity;
                if (!QueryFullProcessImageName(process, 0, image, ref length) ||
                    !String.Equals(Custody.OrdinaryAbsolute(image.ToString()),
                        Custody.OrdinaryAbsolute(Path.Combine(AppDomain.CurrentDomain.BaseDirectory, executable)),
                        StringComparison.OrdinalIgnoreCase)) throw new InvalidOperationException();
                SafeAccessTokenHandle token;
                if (!OpenProcessToken(process, 0x8u, out token)) throw new InvalidOperationException();
                using (token)
                {
                    uint required;
                    GetTokenInformation(token, 1, IntPtr.Zero, 0, out required);
                    if (required < IntPtr.Size || required > 4096) throw new InvalidOperationException();
                    IntPtr buffer = Marshal.AllocHGlobal((int)required);
                    try
                    {
                        if (!GetTokenInformation(token, 1, buffer, required, out required) ||
                            new SecurityIdentifier(Marshal.ReadIntPtr(buffer)).Value != RecorderPaths.ServiceSid)
                            throw new InvalidOperationException();
                    }
                    finally { Marshal.FreeHGlobal(buffer); }
                }
                SafeProcessHandle admitted = process;
                process = null;
                return admitted;
            }
            finally { if (process != null) process.Dispose(); }
        }

        private static long Created(SafeProcessHandle process)
        {
            long created, exited, kernel, user;
            if (!GetProcessTimes(process, out created, out exited, out kernel, out user) || created <= 0)
                throw new InvalidOperationException();
            return created;
        }

        internal static long CurrentCreated()
        {
            using (SafeProcessHandle current = OpenProcess(0x00101000u, false, (uint)Process.GetCurrentProcess().Id))
            {
                if (current == null || current.IsInvalid) throw new InvalidOperationException();
                return Created(current);
            }
        }

        private static uint Creator(SafeProcessHandle process)
        {
            ProcessBasicInformation information = new ProcessBasicInformation();
            int returned;
            if (NtQueryInformationProcess(process.DangerousGetHandle(), 0, ref information,
                Marshal.SizeOf(typeof(ProcessBasicInformation)), out returned) != 0 ||
                information.ParentProcessId.ToInt64() <= 0 || information.ParentProcessId.ToInt64() > UInt32.MaxValue)
                throw new InvalidOperationException();
            return (uint)information.ParentProcessId.ToInt64();
        }

        private static void BindScmParent(SafeProcessHandle parent)
        {
            IntPtr manager = OpenSCManager(null, null, 1), service = IntPtr.Zero, buffer = IntPtr.Zero;
            try
            {
                if (manager == IntPtr.Zero) throw new InvalidOperationException();
                service = OpenService(manager, RecorderPaths.ServiceName, 1 | 4);
                if (service == IntPtr.Zero) throw new InvalidOperationException();
                int needed;
                QueryServiceConfig(service, IntPtr.Zero, 0, out needed);
                if (needed < Marshal.SizeOf(typeof(ScmConfiguration)) || needed > 32768)
                    throw new InvalidOperationException();
                buffer = Marshal.AllocHGlobal(needed);
                if (!QueryServiceConfig(service, buffer, needed, out needed)) throw new InvalidOperationException();
                ScmConfiguration config = (ScmConfiguration)Marshal.PtrToStructure(buffer, typeof(ScmConfiguration));
                string root = Custody.OrdinaryAbsolute(ServiceInstallation.InstallRoot);
                if (!String.Equals(Custody.OrdinaryAbsolute(AppDomain.CurrentDomain.BaseDirectory), root,
                    StringComparison.OrdinalIgnoreCase) || config.Type != 0x10 ||
                    !String.Equals(Marshal.PtrToStringUni(config.Binary), "\"" + Path.Combine(root, "K5VisionRecorder.exe") + "\"",
                        StringComparison.OrdinalIgnoreCase) ||
                    !String.Equals(Marshal.PtrToStringUni(config.Account), "NT SERVICE\\K5VisionRecorder",
                        StringComparison.OrdinalIgnoreCase)) throw new InvalidOperationException();
                Custody.AdmitCode(Path.Combine(root, "K5VisionRecorder.exe"), root);
                Custody.AdmitCode(Path.Combine(root, "K5Vision.exe"), root);
                ScmStatus status;
                if (!QueryServiceStatusEx(service, 0, out status, Marshal.SizeOf(typeof(ScmStatus)), out needed) ||
                    (status.State != 2 && status.State != 3 && status.State != 4) || status.Pid == 0 ||
                    status.Pid != GetProcessId(parent) || WaitForSingleObject(parent, 0) != 0x102u)
                    throw new InvalidOperationException();
            }
            finally
            {
                if (buffer != IntPtr.Zero) Marshal.FreeHGlobal(buffer);
                if (service != IntPtr.Zero) CloseServiceHandle(service);
                if (manager != IntPtr.Zero) CloseServiceHandle(manager);
            }
        }

        internal static SafeProcessHandle AdmitStartupHost()
        {
            if (Program.Sid != RecorderPaths.ServiceSid || Process.GetCurrentProcess().SessionId != 0)
                throw new InvalidOperationException();
            using (SafeProcessHandle parent = AdmitParent())
            using (SafeProcessHandle helper = OpenProcess(0x00101000u, false, (uint)Process.GetCurrentProcess().Id))
            using (SafeFileHandle snapshot = CreateToolhelp32Snapshot(2, 0))
            {
                BindScmParent(parent);
                if (helper == null || helper.IsInvalid || snapshot == null || snapshot.IsInvalid)
                    throw new InvalidOperationException();
                uint parentPid = GetProcessId(parent), helperPid = GetProcessId(helper);
                long parentCreated = Created(parent);
                SafeProcessHandle host = null;
                try
                {
                    ProcessEntry entry = new ProcessEntry();
                    entry.Size = (uint)Marshal.SizeOf(typeof(ProcessEntry));
                    if (!Process32First(snapshot, ref entry)) throw new InvalidOperationException();
                    do
                    {
                        if (entry.Parent != parentPid || entry.Pid == helperPid ||
                            !String.Equals(entry.Executable, "K5Vision.exe", StringComparison.OrdinalIgnoreCase)) continue;
                        SafeProcessHandle candidate = AdmitProcess(entry.Pid, "K5Vision.exe", true);
                        try
                        {
                            long birth = Created(candidate);
                            if (!RecorderProcessIdentity.OwnedStartupHost(parentPid, parentCreated,
                                GetProcessId(parent), Created(parent), helperPid, GetProcessId(candidate),
                                Creator(candidate), birth, Created(candidate)) ||
                                WaitForSingleObject(candidate, 0) != 0x102u || host != null)
                                throw new InvalidOperationException();
                            host = candidate; candidate = null;
                        }
                        finally { if (candidate != null) candidate.Dispose(); }
                    } while (Process32Next(snapshot, ref entry));
                    if (Marshal.GetLastWin32Error() != 18 || host == null) throw new InvalidOperationException();
                    BindScmParent(parent);
                    SafeProcessHandle admitted = host; host = null;
                    return admitted;
                }
                finally { if (host != null) host.Dispose(); }
            }
        }

        internal static void AdmitAdministrator(NamedPipeServerStream pipe)
        {
            bool allowed = false;
            pipe.RunAsClient(delegate {
                using (WindowsIdentity identity = WindowsIdentity.GetCurrent())
                    allowed = identity.User != null && (identity.User.Value == RecorderPaths.ServiceSid ||
                        identity.User.Value == "S-1-5-18" ||
                        new WindowsPrincipal(identity).IsInRole(WindowsBuiltInRole.Administrator));
            });
            if (!allowed) throw new InvalidOperationException();
        }
    }

    internal sealed class RecorderIncompleteIo : IOException
    {
        internal readonly byte[] PendingBuffer;
        internal RecorderIncompleteIo(byte[] buffer) { PendingBuffer = buffer; }

        internal void Drain(Stream stream, IAsyncResult result, bool writing, WaitHandle completed)
        {
            object gate = new object();
            RegisteredWaitHandle registration = null;
            lock (gate)
                registration = ThreadPool.RegisterWaitForSingleObject(completed, delegate {
                    lock (gate) registration.Unregister(null);
                    try { if (writing) stream.EndWrite(result); else stream.EndRead(result); }
                    catch { }
                    finally { Array.Clear(PendingBuffer, 0, PendingBuffer.Length); }
                }, null, Timeout.Infinite, true);
        }
    }

    internal static class RecorderWire
    {
        private static void Transfer(Stream stream, byte[] buffer, bool writing, int milliseconds,
            WaitHandle cancellation, bool expectEndOfStream = false)
        {
            Stopwatch budget = Stopwatch.StartNew();
            int offset = 0;
            while (offset < buffer.Length)
            {
                int remaining = milliseconds - (int)budget.ElapsedMilliseconds;
                if (remaining <= 0) throw new InvalidOperationException();
                IAsyncResult result = writing ? stream.BeginWrite(buffer, offset, buffer.Length - offset, null, null)
                    : stream.BeginRead(buffer, offset, buffer.Length - offset, null, null);
                WaitHandle completed = result.AsyncWaitHandle;
                bool endInvoked = false;
                try
                {
                    int selected = WaitHandle.WaitAny(new[] { completed, cancellation }, remaining);
                    if (selected != 0) throw new InvalidOperationException();
                    // Framework PipeStream.End* owns and closes the APM wait
                    // handle, including when it throws. Never touch it again.
                    endInvoked = true;
                    if (writing) { stream.EndWrite(result); offset = buffer.Length; }
                    else
                    {
                        int count = stream.EndRead(result);
                        if (expectEndOfStream)
                        {
                            if (count == 0) return;
                            throw new InvalidOperationException();
                        }
                        if (count <= 0) throw new InvalidOperationException();
                        offset += count;
                    }
                }
                finally
                {
                    if (!endInvoked)
                    {
                        try { RecorderNative.Cancel((PipeStream)stream); }
                        catch (ObjectDisposedException) { }
                        if (!completed.WaitOne(1000))
                        {
                            // Preserve the buffer until native I/O completes;
                            // the endpoint must fail, never reuse this pipe.
                            RecorderIncompleteIo failure = new RecorderIncompleteIo(buffer);
                            failure.Drain(stream, result, writing, completed);
                            throw failure;
                        }
                        endInvoked = true;
                        try { if (writing) stream.EndWrite(result); else stream.EndRead(result); }
                        catch { }
                    }
                }
            }
        }

        internal static void AwaitPeerClose(Stream stream, int milliseconds, WaitHandle cancellation)
        {
            byte[] sentinel = new byte[1];
            RecorderIncompleteIo pending = null;
            try { Transfer(stream, sentinel, false, milliseconds, cancellation, true); }
            catch (RecorderIncompleteIo error) { pending = error; throw; }
            finally
            {
                if (pending == null || !Object.ReferenceEquals(pending.PendingBuffer, sentinel))
                    Array.Clear(sentinel, 0, sentinel.Length);
            }
        }

        internal static Dictionary<string, object> Read(Stream stream, int maximum, int milliseconds,
            WaitHandle cancellation)
        {
            byte[] header = new byte[4];
            byte[] payload = null;
            RecorderIncompleteIo pending = null;
            try
            {
                Transfer(stream, header, false, milliseconds, cancellation);
                uint count = (uint)(header[0] | (header[1] << 8) | (header[2] << 16) | (header[3] << 24));
                if (count == 0 || count > maximum) throw new InvalidOperationException();
                payload = new byte[count];
                Transfer(stream, payload, false, milliseconds, cancellation);
                return Json.Parse(new UTF8Encoding(false, true).GetString(payload));
            }
            catch (RecorderIncompleteIo error) { pending = error; throw; }
            finally
            {
                if (pending == null || !Object.ReferenceEquals(pending.PendingBuffer, header))
                    Array.Clear(header, 0, header.Length);
                if (payload != null && (pending == null || !Object.ReferenceEquals(pending.PendingBuffer, payload)))
                    Array.Clear(payload, 0, payload.Length);
            }
        }

        internal static void Write(Stream stream, Dictionary<string, object> value, int maximum,
            int milliseconds, WaitHandle cancellation)
        {
            byte[] payload = new UTF8Encoding(false, true).GetBytes(Json.Serializer.Serialize(value));
            byte[] header = new byte[4];
            RecorderIncompleteIo pending = null;
            try
            {
                if (payload.Length == 0 || payload.Length > maximum) throw new InvalidOperationException();
                for (int index = 0; index < 4; index++) header[index] = (byte)((uint)payload.Length >> (8 * index));
                Transfer(stream, header, true, milliseconds, cancellation);
                Transfer(stream, payload, true, milliseconds, cancellation);
            }
            catch (RecorderIncompleteIo error) { pending = error; throw; }
            finally
            {
                if (pending == null || !Object.ReferenceEquals(pending.PendingBuffer, header))
                    Array.Clear(header, 0, header.Length);
                if (pending == null || !Object.ReferenceEquals(pending.PendingBuffer, payload))
                    Array.Clear(payload, 0, payload.Length);
            }
        }

        internal static string Operation(Dictionary<string, object> request, params string[] fields)
        {
            if (request == null || request.Count != fields.Length + 2 ||
                !request.ContainsKey("schema_version") || !(request["schema_version"] is int) ||
                (int)request["schema_version"] != 1 || !request.ContainsKey("operation") ||
                !(request["operation"] is string) || fields.Any(field => !request.ContainsKey(field)))
                throw new InvalidOperationException();
            return (string)request["operation"];
        }
    }

    internal static class RecorderClient
    {
        internal static Dictionary<string, object> Exchange(Dictionary<string, object> request, bool administrative)
        {
            string operation = administrative ? RecorderWire.Operation(request, "username", "password", "confirmation")
                : RecorderWire.Operation(request);
            if (operation != (administrative ? "initialize" : "status")) throw new InvalidOperationException();
            return ExchangeOwned(request, administrative);
        }

        internal static Dictionary<string, object> PendingTime()
        {
            return ExchangeOwned(RecorderPaths.Request("pending-time"), true);
        }

        private static Dictionary<string, object> ExchangeOwned(Dictionary<string, object> request, bool administrative)
        {
            using (ManualResetEvent cancellation = new ManualResetEvent(false))
            using (NamedPipeClientStream pipe = new NamedPipeClientStream(".", administrative ? RecorderPaths.AdminPipe :
                RecorderPaths.StatusPipe, PipeAccessRights.ReadWrite | PipeAccessRights.Synchronize,
                PipeOptions.Asynchronous, TokenImpersonationLevel.Impersonation, HandleInheritability.None))
            {
                pipe.Connect(2000);
                if (!administrative) RecorderNative.AdmitStatusOwner(pipe);
                using (SafeProcessHandle server = administrative ? RecorderNative.AdmitServer(pipe, false) : null)
                {
                    RecorderWire.Write(pipe, request, administrative ? 32768 : 512, 5000, cancellation);
                    Dictionary<string, object> response = RecorderWire.Read(pipe, 2048, administrative ? 40000 : 5000, cancellation);
                    if (!response.ContainsKey("schema_version") || !(response["schema_version"] is int) ||
                        (int)response["schema_version"] != 1 || !response.ContainsKey("status") || !(response["status"] is string))
                        throw new InvalidOperationException();
                    return response;
                }
            }
        }

        internal static int StopAndWait()
        {
            SafeProcessHandle server = null;
            Stopwatch budget = Stopwatch.StartNew();
            try
            {
                try
                {
                    using (ManualResetEvent cancellation = new ManualResetEvent(false))
                    using (NamedPipeClientStream pipe = new NamedPipeClientStream(".", RecorderPaths.ControlPipe,
                        PipeAccessRights.ReadWrite | PipeAccessRights.Synchronize, PipeOptions.Asynchronous,
                        TokenImpersonationLevel.Impersonation, HandleInheritability.None))
                    {
                        pipe.Connect(2000);
                        server = RecorderNative.AdmitServer(pipe, true);
                        RecorderWire.Write(pipe, RecorderPaths.Request("stop"), 512, 5000, cancellation);
                        Dictionary<string, object> response = RecorderWire.Read(pipe, 2048, 5000, cancellation);
                        if (!response.ContainsKey("status") || !Object.Equals(response["status"], "stopping"))
                            throw new InvalidOperationException();
                    }
                }
                catch
                {
                    // Before bootstrap opens its pipe, admit only the unique
                    // host child of this stop helper's exact SCM-bound parent.
                    // A connected/admitted server handle remains authoritative
                    // even if its request/response stalls during shutdown.
                    if (server == null) server = RecorderNative.AdmitStartupHost();
                }
                int grace = Math.Max(0, Math.Min(35000, 40000 - (int)budget.ElapsedMilliseconds));
                uint outcome = RecorderNative.WaitForSingleObject(server, (uint)grace);
                if (outcome == 0) return 0;
                // WinSW 2.12 does not bound its stoparguments wait. Only the
                // pinned, admitted host can be terminated, never a name/PID or
                // the SCM wrapper. Host exit closes its owned engine Job.
                if (outcome == 0x102u && RecorderNative.TerminateProcess(server, 2) &&
                    RecorderNative.WaitForSingleObject(server, 5000) == 0) return 0;
                return 1;
            }
            catch { return 1; }
            finally { if (server != null) server.Dispose(); }
        }
    }

    internal sealed class RecorderEndpoint : IDisposable
    {
        private readonly bool publicStatus;
        private readonly int maximum;
        private readonly ManualResetEvent stopping;
        private readonly Func<Dictionary<string, object>, Dictionary<string, object>> handler;
        private readonly Action afterStopReply;
        private readonly Action infrastructureFailure;
        private readonly Thread thread;
        private readonly object gate = new object();
        private NamedPipeServerStream pipe;

        internal RecorderEndpoint(string name, bool publicStatus, int maximum, ManualResetEvent stopping,
            Func<Dictionary<string, object>, Dictionary<string, object>> handler, Action afterStopReply,
            Action infrastructureFailure)
        {
            this.publicStatus = publicStatus; this.maximum = maximum;
            this.stopping = stopping; this.handler = handler; this.afterStopReply = afterStopReply;
            this.infrastructureFailure = infrastructureFailure;
            pipe = RecorderNative.CreatePipe(name, publicStatus);
            thread = new Thread(Run) { IsBackground = true, Name = "k5-recorder-pipe" };
            thread.Start();
        }

        private void Run()
        {
            while (!stopping.WaitOne(0))
            {
                NamedPipeServerStream current;
                lock (gate) current = pipe;
                if (current == null) return;
                try
                {
                    current.WaitForConnection();
                    Dictionary<string, object> request = RecorderWire.Read(current, maximum, 5000, stopping);
                    // Windows makes the client's impersonation token available
                    // only after data has been read. Authenticate before any
                    // handler can initialize an account or stop the service.
                    if (!publicStatus) RecorderNative.AdmitAdministrator(current);
                    Dictionary<string, object> reply = handler(request);
                    RecorderWire.Write(current, reply, 2048, 5000, stopping);
                    // DisconnectNamedPipe discards unread buffered replies.
                    // Existing clients validate the frame and close their end;
                    // wait for that EOF with a finite, cancellable read instead
                    // of the unbounded FlushFileBuffers/WaitForPipeDrain APIs.
                    RecorderWire.AwaitPeerClose(current, 5000, stopping);
                    bool stop = afterStopReply != null && Object.Equals(reply["status"], "stopping");
                    current.Disconnect();
                    if (stop) afterStopReply();
                }
                catch (RecorderIncompleteIo) { infrastructureFailure(); return; }
                catch
                {
                    lock (gate)
                    {
                        if (pipe != current) return;
                        if (!stopping.WaitOne(0))
                        {
                            try { RecorderNative.Disconnect(current); }
                            catch { infrastructureFailure(); return; }
                        }
                    }
                }
            }
        }

        public void Dispose()
        {
            lock (gate) { if (pipe != null) { pipe.Dispose(); pipe = null; } }
            if (Thread.CurrentThread != thread) thread.Join(500);
        }
    }

    internal static class RecorderHost
    {
        internal static int RequestStop() { return RecorderClient.StopAndWait(); }

        internal static int Run()
        {
            try
            {
                RecorderBootstrap.Advance("token");
                if (Program.Sid != RecorderPaths.ServiceSid || Process.GetCurrentProcess().SessionId != 0)
                    throw new UnauthorizedAccessException();
                RecorderBootstrap.Advance("code_admission");
                string root = Custody.OrdinaryAbsolute(AppDomain.CurrentDomain.BaseDirectory);
                if (!String.Equals(root, Custody.OrdinaryAbsolute(ServiceInstallation.InstallRoot),
                    StringComparison.OrdinalIgnoreCase)) throw new InvalidOperationException();
                Custody.AdmitCode(Path.Combine(root, "K5Vision.exe"), root);
                RecorderBootstrap.Advance("pipe_create");
                using (ManualResetEvent stopping = new ManualResetEvent(false))
                using (RecorderEndpoint control = new RecorderEndpoint(RecorderPaths.ControlPipe, false, 512, stopping,
                    delegate(Dictionary<string, object> request) {
                        if (RecorderWire.Operation(request) != "stop") throw new InvalidOperationException();
                        RecorderBootstrap.Advance("stopping");
                        return new Dictionary<string, object> { { "schema_version", 1 }, { "status", "stopping" } };
                    }, delegate { stopping.Set(); }, delegate { Environment.Exit(1); }))
                {
                    if (stopping.WaitOne(0)) return 0;
                    RecorderBootstrap.Advance("payload_verify");
                    Custody.VerifyPayload(root);
                    if (stopping.WaitOne(0)) return 0;
                    RecorderBootstrap.Advance("data_admission");
                    string data = RecorderPaths.DataRoot;
                    // No identity operation is admitted before the full seal.
                    Custody.AdmitMachineData(data, data);
                    if (stopping.WaitOne(0)) return 0;
                    using (RecorderLifetime lifetime = new RecorderLifetime(root, data, stopping)) return lifetime.Run();
                }
            }
            catch (Exception error) { RecorderBootstrap.Failure(error); return 1; }
        }
    }

    internal sealed class RecorderLifetime : IDisposable
    {
        private readonly string root;
        private readonly string data;
        private readonly ProcessJob job = new ProcessJob();
        private readonly ManualResetEvent stopping;
        private readonly AutoResetEvent startRequested = new AutoResetEvent(false);
        private readonly object gate = new object();
        private RecorderEndpoint statusPipe, adminPipe;
        private string state = "starting";
        private int initializing;
        private Bridge backend;
        private volatile bool failed;

        internal RecorderLifetime(string root, string data, ManualResetEvent stopping)
        { this.root = root; this.data = data; this.stopping = stopping; }

        private Dictionary<string, object> Setup(Dictionary<string, object> request)
        {
            using (Bridge worker = new Bridge(root, request, job, false, true))
            {
                Dictionary<string, object> response = worker.Complete(30000);
                if (worker.Process.ExitCode != 0 && !Object.Equals(response["status"], "refused"))
                    throw new InvalidOperationException();
                return response;
            }
        }

        private void SetState(string value) { lock (gate) state = value; }

        private Dictionary<string, object> Status(Dictionary<string, object> request)
        {
            if (RecorderWire.Operation(request) != "status") throw new InvalidOperationException();
            bool timePending = PendingTime()["status"].Equals("pending");
            lock (gate)
                return new Dictionary<string, object> { { "schema_version", 1 }, { "status", state },
                    { "port", RecorderPaths.Port }, { "progress", backend == null ? 0 : backend.RecordingsProgress },
                    { "time_approval_pending", timePending } };
        }

        private Dictionary<string, object> PendingTime()
        {
            try
            {
                string directory = Path.Combine(data, "identity", "time-admin");
                if (!Directory.Exists(directory)) return NoPendingTime();
                Custody.AdmitMachineData(directory, data);
                string[] files = Directory.EnumerateFiles(directory, ".time-admin-*.json").Take(3).ToArray();
                foreach (string request in files)
                {
                    string name = Path.GetFileName(request);
                    if (!System.Text.RegularExpressions.Regex.IsMatch(name, "^[.]time-admin-[a-f0-9]{32}[.]json$")) continue;
                    Custody.AdmitMachineData(request, data);
                    FileInfo info = new FileInfo(request);
                    if (info.Length < 1 || info.Length > 4096) continue;
                    string response = request.Substring(0, request.Length - 5) + ".result.json";
                    if (File.Exists(response) || Directory.Exists(response)) continue;
                    return new Dictionary<string, object> { { "schema_version", 1 }, { "status", "pending" },
                        { "request", request }, { "response", response } };
                }
            }
            catch { }
            return NoPendingTime();
        }

        private static Dictionary<string, object> NoPendingTime()
        {
            return new Dictionary<string, object> { { "schema_version", 1 }, { "status", "none" } };
        }

        private Dictionary<string, object> Admin(Dictionary<string, object> request)
        {
            if (request != null && Object.Equals(request["operation"], "pending-time"))
            {
                if (RecorderWire.Operation(request) != "pending-time") throw new InvalidOperationException();
                return PendingTime();
            }
            return Initialize(request);
        }

        private Dictionary<string, object> Initialize(Dictionary<string, object> request)
        {
            if (RecorderWire.Operation(request, "username", "password", "confirmation") != "initialize")
                throw new InvalidOperationException();
            lock (gate)
            {
                if (state != "account_required" || Interlocked.CompareExchange(ref initializing, 1, 0) != 0)
                    return Refused("setup_unavailable");
                state = "initializing";
            }
            try
            {
                Dictionary<string, object> reply = Setup(request);
                if (Object.Equals(reply["status"], "ready"))
                {
                    SetState("starting"); startRequested.Set();
                }
                else if (Object.Equals(reply["status"], "refused"))
                {
                    if (reply.ContainsKey("reason") && new[] { "invalid_username", "invalid_password", "password_mismatch" }
                        .Contains(reply["reason"] as string)) SetState("account_required");
                    else { SetState("unavailable"); failed = true; }
                }
                else throw new InvalidOperationException();
                return reply;
            }
            catch { SetState("unavailable"); failed = true; return Refused("setup_failed"); }
            finally
            {
                request["password"] = null; request["confirmation"] = null;
                Interlocked.Exchange(ref initializing, 0);
            }
        }

        private static Dictionary<string, object> Refused(string reason)
        {
            return new Dictionary<string, object> { { "schema_version", 1 }, { "status", "refused" }, { "reason", reason } };
        }

        internal int Run()
        {
            RecorderBootstrap.Advance("parent_admission");
            using (SafeProcessHandle parent = RecorderNative.AdmitParent())
            using (ManualResetEvent parentGuardDone = new ManualResetEvent(false))
            {
                // WinSW itself can be killed. A pinned parent lifetime, rather
                // than an executable-name sweep, prevents an orphaned engine
                // retaining the profile lease while SCM starts a replacement.
                Thread parentGuard = new Thread(delegate() {
                    while (!parentGuardDone.WaitOne(200))
                        if (RecorderNative.WaitForSingleObject(parent, 0) != 0x102u)
                            Environment.Exit(3); // OS closes the engine Job.
                }) { IsBackground = true, Name = "k5-recorder-parent-lifetime" };
                parentGuard.Start();
                try { return RunEngine(); }
                finally { parentGuardDone.Set(); parentGuard.Join(1000); }
            }
        }

        private int RunEngine()
        {
            if (stopping.WaitOne(0)) return 0;
            RecorderBootstrap.Advance("identity_probe");
            Dictionary<string, object> identity = Setup(RecorderPaths.Request("status"));
            if (Object.Equals(identity["status"], "ready")) startRequested.Set();
            else if (Object.Equals(identity["status"], "new")) SetState("account_required");
            else throw new InvalidOperationException();
            if (stopping.WaitOne(0)) return 0;
            RecorderBootstrap.Advance("pipe_create");
            statusPipe = new RecorderEndpoint(RecorderPaths.StatusPipe, true, 512, stopping, Status, null, delegate { failed = true; });
            adminPipe = new RecorderEndpoint(RecorderPaths.AdminPipe, false, 32768, stopping, Admin, null, delegate { failed = true; });
            if (Object.Equals(identity["status"], "new")) RecorderBootstrap.Advance("account_required");
            while (!stopping.WaitOne(0))
            {
                if (failed) return 1;
                if (startRequested.WaitOne(0))
                {
                    if (backend != null) throw new InvalidOperationException();
                    RecorderBootstrap.Advance("serve_start");
                    Workers.Provision(root, data, CancellationToken.None);
                    backend = new Bridge(root, RecorderPaths.Request("serve"), job, true, true);
                }
                if (backend != null)
                {
                    if (backend.InvalidOutput || backend.Process.HasExited) return 1;
                    if (backend.Running.WaitOne(0)) { RecorderBootstrap.Advance("running"); SetState("running"); }
                    else if (backend.PreparingRecordings) SetState("preparing_recordings");
                }
                stopping.WaitOne(200);
            }
            SetState("stopping");
            RecorderBootstrap.Advance("stopping");
            return backend == null || backend.Stop() ? 0 : 1;
        }

        public void Dispose()
        {
            stopping.Set();
            if (statusPipe != null) statusPipe.Dispose();
            if (adminPipe != null) adminPipe.Dispose();
            job.Dispose();
            if (backend != null) backend.Dispose();
            // Background setup handlers might still be finishing their bounded
            // child read; OS handles remain valid until this process exits.
        }
    }
}
