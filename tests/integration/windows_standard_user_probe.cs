// Acceptance-only probe: never bundled into K5 or used as a product entry point.
// The elevated controller creates one fresh local user, launches this probe as
// that user, and removes the account/profile. No credential enters arguments.
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.IO.Pipes;
using System.Linq;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Security;
using System.Security.AccessControl;
using System.Security.Cryptography;
using System.Security.Principal;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;

internal static class StandardUserProbe
{
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct UserInfo
    {
        internal string Name, Password;
        internal uint PasswordAge, Privilege;
        internal string Home, Comment;
        internal uint Flags;
        internal string Script;
    }
    [DllImport("netapi32.dll", CharSet = CharSet.Unicode)]
    private static extern uint NetUserAdd(string server, uint level, ref UserInfo user, out uint error);
    [DllImport("netapi32.dll", CharSet = CharSet.Unicode)]
    private static extern uint NetUserDel(string server, string user);
    [DllImport("netapi32.dll", CharSet = CharSet.Unicode)]
    private static extern uint NetLocalGroupAddMembers(string server, string group, uint level,
        ref IntPtr member, uint count);
    [DllImport("userenv.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool DeleteProfile(string sid, string path, string computer);
    [DllImport("advapi32.dll", SetLastError = true)]
    private static extern bool GetTokenInformation(IntPtr token, int information,
        out int value, int size, out int needed);

    private static readonly JavaScriptSerializer Json = new JavaScriptSerializer();
    private static string stage = "start";
    private static void Require(bool condition) { if (!condition) throw new InvalidOperationException(); }
    private static string Quote(string value) { Require(value.IndexOf('"') < 0); return "\"" + value + "\""; }

    private static NamedPipeClientStream Open(string name)
    {
        NamedPipeClientStream pipe = new NamedPipeClientStream(".", name,
            PipeAccessRights.ReadWrite | PipeAccessRights.Synchronize, PipeOptions.Asynchronous,
            TokenImpersonationLevel.Impersonation, HandleInheritability.None);
        try { pipe.Connect(2000); return pipe; }
        catch { pipe.Dispose(); throw; }
    }

    private static bool Denied(string name)
    {
        try { using (Open(name)) { return false; } }
        catch (UnauthorizedAccessException) { return true; }
        catch (IOException error) { return (error.HResult & 65535) == 5; }
    }

    private static void Rejected(NamedPipeClientStream pipe, int timeout)
    {
        byte[] output = new byte[1];
        IAsyncResult pending = pipe.BeginRead(output, 0, 1, null, null);
        Require(pending.AsyncWaitHandle.WaitOne(timeout));
        try { Require(pipe.EndRead(pending) == 0); }
        catch (IOException) { }
        finally { pending.AsyncWaitHandle.Close(); }
    }

    private static Dictionary<string, object> Child(string installed, string expectedSid)
    {
        Dictionary<string, object> result = new Dictionary<string, object>();
        stage = "ordinary_token";
        using (WindowsIdentity identity = WindowsIdentity.GetCurrent())
        {
            int elevated, needed;
            Require(identity.User != null && identity.User.Value == expectedSid);
            Require(!identity.Groups.Cast<IdentityReference>().Any(group => group.Value == "S-1-5-32-544"));
            Require(identity.Groups.Cast<IdentityReference>().Any(group => group.Value == "S-1-5-32-545"));
            Require(!new WindowsPrincipal(identity).IsInRole(WindowsBuiltInRole.Administrator));
            Require(GetTokenInformation(identity.Token, 20, out elevated, sizeof(int), out needed) && elevated == 0);
        }
        result["ordinary_token_user_verified"] = true;
        result["builtin_users_group_present"] = true;
        result["administrator_group_present"] = false;
        result["token_elevated"] = false;
        Assembly product = Assembly.LoadFrom(Path.Combine(installed, "K5Vision.exe"));
        MethodInfo exchange = product.GetType("K5VisionDesktop.RecorderClient", true).GetMethod(
            "Exchange", BindingFlags.Static | BindingFlags.NonPublic);
        Require(exchange != null);
        Action valid = delegate {
            Dictionary<string, object> request = new Dictionary<string, object> {
                { "schema_version", 1 }, { "operation", "status" } };
            Dictionary<string, object> response = (Dictionary<string, object>)exchange.Invoke(null,
                new object[] { request, false });
            Require(Object.Equals(response["schema_version"], 1) && Object.Equals(response["status"], "running") &&
                Object.Equals(response["port"], 8000));
        };
        stage = "product_status"; valid();
        stage = "admin_pipe_denial"; Require(Denied("K5VisionRecorder.Admin.v1"));
        result["admin_pipe_denied"] = true;
        stage = "control_pipe_denial"; Require(Denied("K5VisionRecorder.Control.v1"));
        result["control_pipe_denied"] = true;
        stage = "malformed_status";
        using (NamedPipeClientStream pipe = Open("K5VisionRecorder.Status.v1"))
        { byte[] oversized = { 1, 2, 0, 0 }; pipe.Write(oversized, 0, 4); Rejected(pipe, 3000); }
        valid(); result["malformed_status_reconnect"] = true;
        stage = "eof_status";
        using (NamedPipeClientStream pipe = Open("K5VisionRecorder.Status.v1"))
        { pipe.Write(new byte[] { 1, 0 }, 0, 2); }
        valid(); result["eof_status_reconnect"] = true;
        stage = "slow_status";
        Stopwatch slow = Stopwatch.StartNew();
        using (NamedPipeClientStream pipe = Open("K5VisionRecorder.Status.v1"))
        { pipe.Write(new byte[] { 1 }, 0, 1); Rejected(pipe, 8500); }
        Require(slow.ElapsedMilliseconds >= 4000 && slow.ElapsedMilliseconds < 8500);
        valid(); result["slow_status_reconnect"] = true;
        result["slow_status_seconds"] = Math.Round(slow.Elapsed.TotalSeconds, 3);
        result["product_status_success_count"] = 4;
        result["actual_product_client"] = true;
        return result;
    }

    private static DirectorySecurity Security(string sid, FileSystemRights rights)
    {
        DirectorySecurity result = new DirectorySecurity();
        result.SetAccessRuleProtection(true, false);
        result.SetOwner(new SecurityIdentifier("S-1-5-32-544"));
        foreach (string admin in new[] { "S-1-5-18", "S-1-5-32-544" })
            result.AddAccessRule(new FileSystemAccessRule(new SecurityIdentifier(admin), FileSystemRights.FullControl,
                InheritanceFlags.ContainerInherit | InheritanceFlags.ObjectInherit, PropagationFlags.None, AccessControlType.Allow));
        result.AddAccessRule(new FileSystemAccessRule(new SecurityIdentifier(sid), rights,
            InheritanceFlags.ContainerInherit | InheritanceFlags.ObjectInherit, PropagationFlags.None, AccessControlType.Allow));
        return result;
    }

    private static int Main(string[] arguments)
    {
        if (arguments.Length == 4 && arguments[0] == "child")
        {
            try { File.WriteAllText(arguments[3], Json.Serialize(Child(arguments[1], arguments[2])), Encoding.UTF8); return 0; }
            catch (Exception error) {
                File.WriteAllText(arguments[3], Json.Serialize(new {
                    failure_stage = stage, failure_type = error.GetType().Name }), Encoding.UTF8); return 1; }
        }
        if (arguments.Length != 2) return 2;
        string username = "k5qa" + Guid.NewGuid().ToString("N").Substring(0, 12);
        string home = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData),
            "K5-QA-" + Guid.NewGuid().ToString("N"));
        string sid = null;
        bool added = false, removed = false, profileRemoved = false, treeRemoved = false;
        Dictionary<string, object> receipt = new Dictionary<string, object>();
        Process child = null;
        try
        {
            stage = "create_ordinary_account";
            byte[] random = new byte[24];
            using (RandomNumberGenerator generator = RandomNumberGenerator.Create()) generator.GetBytes(random);
            string password = "Aa1!" + Convert.ToBase64String(random);
            Array.Clear(random, 0, random.Length);
            UserInfo user = new UserInfo { Name = username, Password = password, PasswordAge = 0,
                Privilege = 1, Home = null, Comment = "Generated K5 acceptance account",
                Flags = 0x10201, Script = null };
            uint error;
            Require(NetUserAdd(null, 1, ref user, out error) == 0); added = true;
            user.Password = null;
            sid = ((SecurityIdentifier)new NTAccount(Environment.MachineName, username).Translate(typeof(SecurityIdentifier))).Value;
            SecurityIdentifier identifier = new SecurityIdentifier(sid);
            byte[] member = new byte[identifier.BinaryLength];
            identifier.GetBinaryForm(member, 0);
            GCHandle pinned = GCHandle.Alloc(member, GCHandleType.Pinned);
            try {
                IntPtr memberSid = pinned.AddrOfPinnedObject();
                string group = ((NTAccount)new SecurityIdentifier("S-1-5-32-545").Translate(typeof(NTAccount))).Value;
                group = group.Substring(group.LastIndexOf('\\') + 1);
                uint membership = NetLocalGroupAddMembers(null, group, 0, ref memberSid, 1);
                Require(membership == 0 || membership == 1378);
            }
            finally { pinned.Free(); Array.Clear(member, 0, member.Length); }
            Directory.CreateDirectory(home, Security(sid, FileSystemRights.ReadAndExecute));
            string executable = Path.Combine(home, "OrdinaryUserProbe.exe");
            File.Copy(Assembly.GetExecutingAssembly().Location, executable, false);
            string results = Path.Combine(home, "results");
            Directory.CreateDirectory(results, Security(sid, FileSystemRights.Modify));
            string output = Path.Combine(results, "result.json");
            stage = "launch_ordinary_account";
            using (SecureString secret = new SecureString())
            {
                foreach (char character in password) secret.AppendChar(character);
                secret.MakeReadOnly(); password = null;
                ProcessStartInfo start = new ProcessStartInfo(executable,
                    "child " + Quote(arguments[0]) + " " + Quote(sid) + " " + Quote(output)) {
                    UserName = username, Domain = Environment.MachineName, Password = secret,
                    LoadUserProfile = true, UseShellExecute = false, CreateNoWindow = true, WorkingDirectory = home };
                start.EnvironmentVariables.Clear();
                foreach (string key in new[] { "SystemRoot", "WINDIR" })
                    start.EnvironmentVariables[key] = Environment.GetEnvironmentVariable(key);
                start.EnvironmentVariables["PATH"] = Path.Combine(Environment.GetEnvironmentVariable("SystemRoot"), "System32");
                start.EnvironmentVariables["TEMP"] = results;
                start.EnvironmentVariables["TMP"] = results;
                child = Process.Start(start);
                Require(child != null && child.WaitForExit(30000));
                Require(File.Exists(output) && new FileInfo(output).Length <= 4096);
                receipt = Json.Deserialize<Dictionary<string, object>>(File.ReadAllText(output, Encoding.UTF8));
                Require(child.ExitCode == 0);
            }
        }
        catch (Exception error) {
            if (!receipt.ContainsKey("failure_stage")) receipt["failure_stage"] = stage;
            if (!receipt.ContainsKey("failure_type")) receipt["failure_type"] = error.GetType().Name;
            Win32Exception native = error as Win32Exception;
            if (native != null && native.NativeErrorCode >= 0 && native.NativeErrorCode <= 65535)
                receipt["failure_win32_error"] = native.NativeErrorCode;
        }
        finally
        {
            if (child != null)
            {
                try { if (!child.HasExited) { child.Kill(); Require(child.WaitForExit(3000)); } }
                catch { receipt["failure_stage"] = "child_cleanup"; }
                child.Dispose();
            }
            if (sid != null)
                for (int attempt = 0; attempt < 20 && !profileRemoved; attempt++)
                { profileRemoved = DeleteProfile(sid, null, null) || Marshal.GetLastWin32Error() == 2; if (!profileRemoved) Thread.Sleep(100); }
            if (added) removed = NetUserDel(null, username) == 0;
            try { if (Directory.Exists(home)) Directory.Delete(home, true); treeRemoved = !Directory.Exists(home); }
            catch { }
            receipt["generated_account_removed"] = removed;
            receipt["generated_profile_removed"] = profileRemoved;
            receipt["generated_probe_tree_removed"] = treeRemoved;
            if (!receipt.ContainsKey("failure_stage") && (!removed || !profileRemoved || !treeRemoved))
                receipt["failure_stage"] = !removed ? "account_cleanup" : !profileRemoved ? "profile_cleanup" : "tree_cleanup";
            File.WriteAllText(arguments[1], Json.Serialize(receipt), Encoding.UTF8);
        }
        return receipt.ContainsKey("failure_stage") || !removed || !profileRemoved || !treeRemoved ? 1 : 0;
    }
}
