// Read-only CI diagnostic of the real compiled guard's ProgramData admission.
// No files/services/permissions are created or changed. No private path,
// account name, supplied argument or exception message is printed.
using System;
using System.Collections.Generic;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Security.AccessControl;
using System.Security.Principal;
using System.Web.Script.Serialization;

internal static class MachineAdmissionProbe
{
    [DllImport("shell32.dll", CharSet = CharSet.Unicode)]
    private static extern int SHGetKnownFolderPath(ref Guid folder, uint flags, IntPtr token, out IntPtr path);

    private static string FailureType(Exception error)
    {
        while ((error is TargetInvocationException || error is TypeInitializationException) &&
            error.InnerException != null) error = error.InnerException;
        return error.GetType().Name;
    }

    private static string Principal(string sid)
    {
        switch (sid)
        {
            case "S-1-1-0": return "everyone";
            case "S-1-5-11": return "authenticated_users";
            case "S-1-5-32-545": return "users";
            case "S-1-5-32-544": return "administrators";
            case "S-1-5-18": return "system";
            case "S-1-3-0": return "creator_owner";
            case "S-1-3-4": return "owner_rights";
            case "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464":
                return "trusted_installer";
            default: return "other";
        }
    }

    private static int Main(string[] arguments)
    {
        Dictionary<string, object> result = new Dictionary<string, object> {
            { "schema_version", 1 }, { "probe_completed", false } };
        int exit = 1;
        try
        {
            if (arguments.Length != 1) throw new ArgumentException();
            Guid folder = new Guid("62AB5D82-FDC1-4DC3-A9DD-070D1D495D97");
            IntPtr memory;
            if (SHGetKnownFolderPath(ref folder, 0, IntPtr.Zero, out memory) != 0 || memory == IntPtr.Zero)
                throw new InvalidOperationException();
            string parent;
            try { parent = Marshal.PtrToStringUni(memory); }
            finally { Marshal.FreeCoTaskMem(memory); }
            DirectorySecurity security = Directory.GetAccessControl(parent,
                AccessControlSections.Access | AccessControlSections.Owner);
            result["owner_sid"] = security.GetOwner(typeof(SecurityIdentifier)).Value;
            List<Dictionary<string, object>> entries = new List<Dictionary<string, object>>();
            foreach (FileSystemAccessRule rule in security.GetAccessRules(true, true, typeof(SecurityIdentifier)))
            {
                if (rule.AccessControlType != AccessControlType.Allow) continue;
                uint mask = (uint)rule.FileSystemRights;
                bool inheritOnly = (rule.PropagationFlags & PropagationFlags.InheritOnly) != 0;
                entries.Add(new Dictionary<string, object> {
                    { "principal", Principal(rule.IdentityReference.Value) }, { "mask", mask },
                    { "inheritance_flags", (int)rule.InheritanceFlags },
                    { "propagation_flags", (int)rule.PropagationFlags }, { "inherited", rule.IsInherited },
                    { "effective_ancestor_bits", inheritOnly ? 0u : mask & 0x510D0150u } });
            }
            result["allow_aces"] = entries;
            Assembly guard = Assembly.LoadFrom(Path.GetFullPath(arguments[0]));
            Type custody = guard.GetType("K5VisionDesktop.Custody", true);
            MethodInfo admit = custody.GetMethod("Admit", BindingFlags.Static | BindingFlags.NonPublic);
            if (admit == null) throw new MissingMethodException();
            bool admitted = false;
            string exceptionType = null;
            try { admit.Invoke(null, new object[] { parent, Path.Combine(parent, "K5Vision") }); admitted = true; }
            catch (TargetInvocationException error) { exceptionType = FailureType(error); }
            result["admitted"] = admitted;
            result["admission_exception_type"] = exceptionType;
            result["probe_completed"] = true;
            exit = 0;
        }
        catch (Exception error) { result["probe_exception_type"] = FailureType(error); }
        Console.WriteLine(new JavaScriptSerializer().Serialize(result));
        return exit;
    }
}
