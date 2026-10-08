// Task-local, read-only full runner DACL/inheritance inventory. No ACL setter.
// C:\ is an explicitly trusted OS/device-map anchor. Every descendant is opened
// as one component relative to a held parent. No namespace or runtime framework.
// Windows PowerShell 5.1 / .NET Framework / C# 5; source tests are not native proof.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.Runtime.InteropServices;
using System.Security.AccessControl;
using System.Security.Cryptography;
using System.Text;

public static class K5FixedGitObservation
{
    private const int MaxHandles = 64;
    private const int MaxNameUnits = 1024;
    private const long MaxElapsedMs = 75000;
    private const int MaxObjects = 50000;
    private const long MaxDaclBytes = 64L * 1024 * 1024;
    private const long MaxPathUnits = 8L * 1024 * 1024;
    private const uint ReadControl = 0x00020000;
    private const uint Synchronize = 0x00100000;
    private const uint ReadAttributes = 0x00000080;
    private const uint ReadData = 0x00000001;
    private const uint Traverse = 0x00000020;
    private const uint ShareRead = 0x00000003; // read/write sharing; deny delete/rename
    private const uint ObjCaseInsensitive = 0x00000040;
    private const uint ObjDontReparse = 0x00001000;
    private const uint FileDirectoryFile = 0x00000001;
    private const uint FileNonDirectoryFile = 0x00000040;
    private const uint FileSynchronousIoNonalert = 0x00000020;
    private const uint FileOpenReparsePoint = 0x00200000;
    private const uint AttributeDirectory = 0x00000010;
    private const uint AttributeReparse = 0x00000400;
    private static readonly IntPtr InvalidHandle = new IntPtr(-1);
    private static readonly Encoding StrictUtf16 = new UnicodeEncoding(false, false, true);
    private static readonly HashSet<string> Codes = new HashSet<string>(StringComparer.Ordinal)
    {
        "none", "platform", "abi", "anchor_open", "relative_open", "metadata",
        "filesystem", "reparse", "path_type", "acl_unavailable", "acl_null",
        "alias_outside_root", "alias_duplicate", "alias_missing_primary", "alias_count",
        "identity_mismatch", "path_shape", "name_bound", "file_size", "bytes_bound",
        "time_bound", "enumeration", "changed", "read_failed", "handle_bound",
        "cleanup_failed", "internal", "inventory_bound", "inventory_changed", "backup_create", "backup_verify", "backup_security"
    };

    // One owned-process deadline covers identity checks, both inventories,
    // validation, backup/readback and the wrapper's normal post checks together.
    public static System.Threading.Timer StartObservationDeadline()
    {
        return new System.Threading.Timer(delegate(object state) { Environment.Exit(124); },
            null, 150000, System.Threading.Timeout.Infinite);
    }

    // Invoke only after the parent has admitted this own PowerShell observation step.
    // Compilation is complete before this timer starts. No process lookup or kills.
    public static Dictionary<string, object> ObserveGuarded()
    {
        System.Threading.Timer watchdog = new System.Threading.Timer(
            delegate(object state) { Environment.Exit(124); }, null, 90000,
            System.Threading.Timeout.Infinite);
        try { return Observe(); }
        finally { watchdog.Dispose(); }
    }

    public static Dictionary<string, object> Observe()
    {
        Observation observation = new Observation();
        Dictionary<string, object> result = BaseRecord();
        Dictionary<string, object> complete = null;
        try
        {
            Require(Environment.OSVersion.Platform == PlatformID.Win32NT, "platform");
            AssertAbi();
            complete = observation.RunInventory();
        }
        catch (Refusal error)
        {
            result["code"] = Codes.Contains(error.Code) ? error.Code : "internal";
        }
        catch (Exception)
        {
            // No raw exception, pathname, security descriptor, or external text escapes.
            result["code"] = "internal";
        }
        finally
        {
            if (!observation.CloseAll())
            {
                complete = null;
                result["code"] = "cleanup_failed";
            }
        }
        long elapsed = observation.ElapsedMs;
        if (complete != null && elapsed > MaxElapsedMs)
        {
            complete = null;
            result["code"] = "time_bound";
        }
        if (complete != null) result = complete;
        result["bytes_read"] = observation.BytesRead;
        result["elapsed_ms"] = elapsed;
        return result;
    }

    // Only a new metadata-backup file outside the affected runner tree is written.
    // The caller must supply the exact reviewed inventory bytes; no ACL setter exists.
    public static Dictionary<string, object> SaveBackup(string localAppData, string name, byte[] bytes)
    {
        Observation observation = new Observation();
        System.Threading.Timer watchdog = new System.Threading.Timer(
            delegate(object state) { Environment.Exit(124); }, null, 30000,
            System.Threading.Timeout.Infinite);
        Dictionary<string, object> result = new Dictionary<string, object> {
            { "status", "refused" }, { "code", "internal" },
            { "readback_verified", false }, { "repair_ready", false }
        };
        try { result = observation.SaveBackup(localAppData, name, bytes); }
        catch (Refusal error) { result["code"] = Codes.Contains(error.Code) ? error.Code : "internal"; }
        catch (Exception) { result["code"] = "internal"; }
        finally
        {
            watchdog.Dispose();
            if (!observation.CloseAll())
            {
                result["status"] = "refused";
                result["code"] = "cleanup_failed";
                result["readback_verified"] = false;
            }
        }
        return result;
    }

    private static Dictionary<string, object> BaseRecord()
    {
        return new Dictionary<string, object>(StringComparer.Ordinal)
        {
            { "schema_version", "fixed-runner-dacl-inventory-v1" },
            { "scope", "fixed-runner-readonly-dacl-inventory" },
            { "target", "runner-root-subtree" },
            { "status", "refused" },
            { "code", "internal" },
            { "closure_complete", false },
            { "exception_authority", false },
            { "storage_admission", false },
            { "retry_authority", false }
        };
    }

    private sealed class Refusal : Exception
    {
        internal readonly string Code;
        internal Refusal(string code) { Code = code; }
    }

    private static void Require(bool condition, string code)
    {
        if (!condition) throw new Refusal(code);
    }

    private sealed class Metadata
    {
        internal uint Volume, Links, Attributes;
        internal ulong Id;
    }

    private sealed class Held
    {
        internal IntPtr Handle;
        internal bool Directory;
        internal Metadata Initial;
        internal string Acl;
        internal Dictionary<string, object> Dacl;
    }

    private sealed class Observation
    {
        private readonly Stopwatch watch = Stopwatch.StartNew();
        private readonly List<IntPtr> owned = new List<IntPtr>();
        private readonly List<Held> held = new List<Held>();
        internal long BytesRead;
        internal long ElapsedMs { get { return watch.ElapsedMilliseconds; } }

        private void Tick() { Require(ElapsedMs <= MaxElapsedMs, "time_bound"); }

        private void BeforeOpen()
        {
            Tick();
            Require(owned.Count < MaxHandles, "handle_bound");
        }

        private void Own(IntPtr handle)
        {
            if (handle != IntPtr.Zero && handle != InvalidHandle) owned.Add(handle);
        }

        private Held Capture(IntPtr handle, bool directory, bool inventory = false)
        {
            Tick();
            Held item = new Held();
            item.Handle = handle;
            if (inventory)
            {
                ByHandleInformation shape;
                Require(Native.GetFileInformationByHandle(handle, out shape), "metadata");
                directory = (shape.Attributes & AttributeDirectory) != 0;
            }
            item.Directory = directory;
            item.Initial = ReadMetadata(handle, directory);
            item.Dacl = ReadAclRecord(handle);
            item.Acl = (string)item.Dacl["dacl_sha256"];
            held.Add(item);
            return item;
        }

        private Held OpenAnchor()
        {
            BeforeOpen();
            // The ONLY absolute filesystem open: trusted C:\ OS/device-map anchor.
            // Metadata reads allow existing content writers, but deny rename/deletion.
            // Content timestamps are not a DACL snapshot invariant.
            IntPtr handle = Native.CreateFileW(@"\\?\C:\", ReadAttributes | ReadControl |
                Synchronize | Traverse, ShareRead, IntPtr.Zero, 3,
                0x02000000 | FileOpenReparsePoint, IntPtr.Zero);
            Own(handle);
            Require(handle != IntPtr.Zero && handle != InvalidHandle, "anchor_open");
            return Capture(handle, true);
        }

        private Held OpenRelative(Held parent, string component, bool directory, bool inventory = false)
        {
            ValidateComponent(component); // Before allocating/opening any alias.
            Require(parent != null && parent.Directory, "path_type");
            BeforeOpen();
            IntPtr nameBuffer = IntPtr.Zero;
            IntPtr unicodeBuffer = IntPtr.Zero;
            try
            {
                nameBuffer = Marshal.StringToHGlobalUni(component);
                UnicodeString name = new UnicodeString();
                name.Length = checked((ushort)(component.Length * 2));
                name.MaximumLength = checked((ushort)(name.Length + 2));
                name.Buffer = nameBuffer;
                unicodeBuffer = Marshal.AllocHGlobal(Marshal.SizeOf(typeof(UnicodeString)));
                Marshal.StructureToPtr(name, unicodeBuffer, false);
                ObjectAttributes attributes = new ObjectAttributes();
                attributes.Length = (uint)Marshal.SizeOf(typeof(ObjectAttributes));
                attributes.RootDirectory = parent.Handle;
                attributes.ObjectName = unicodeBuffer;
                attributes.Attributes = ObjCaseInsensitive | ObjDontReparse;
                // Neither OBJ_INHERIT nor any security descriptor/QOS is supplied.
                IoStatusBlock io = new IoStatusBlock();
                IntPtr handle = IntPtr.Zero;
                int status = Native.NtOpenFile(out handle,
                    ReadAttributes | ReadControl | Synchronize | (directory || inventory ? ReadData : 0),
                    ref attributes, ref io, ShareRead,
                    FileOpenReparsePoint | FileSynchronousIoNonalert |
                    (inventory ? 0 : (directory ? FileDirectoryFile : FileNonDirectoryFile)));
                Own(handle); // Own even an anomalous non-null handle on failure.
                Require(status == 0 && io.Status == 0 && handle != IntPtr.Zero &&
                    handle != InvalidHandle, "relative_open");
                Held result = Capture(handle, directory, inventory);
                Require(result.Initial.Volume == parent.Initial.Volume, "identity_mismatch");
                return result;
            }
            finally
            {
                if (unicodeBuffer != IntPtr.Zero) Marshal.FreeHGlobal(unicodeBuffer);
                if (nameBuffer != IntPtr.Zero) Marshal.FreeHGlobal(nameBuffer);
            }
        }

        // Every query uses a held handle, a zeroed fixed buffer, exact success, and
        // the native completed byte count. There is no growth/retry/partial parsing.
        private byte[] Query(IntPtr handle, int informationClass, int capacity, bool exact)
        {
            Tick();
            byte[] buffer = new byte[capacity];
            GCHandle pin = GCHandle.Alloc(buffer, GCHandleType.Pinned);
            try
            {
                IoStatusBlock io = new IoStatusBlock();
                int status = Native.NtQueryInformationFile(handle, ref io,
                    pin.AddrOfPinnedObject(), (uint)capacity, informationClass);
                Tick();
                ulong used = io.Information.ToUInt64();
                Require(status == 0 && io.Status == 0 && used > 0 &&
                    used <= (ulong)capacity && (!exact || used == (ulong)capacity), "enumeration");
                byte[] completed = new byte[(int)used];
                Buffer.BlockCopy(buffer, 0, completed, 0, completed.Length);
                return completed;
            }
            finally { pin.Free(); }
        }

        private Metadata ReadMetadata(IntPtr handle, bool directory)
        {
            Tick();
            Require(Native.GetFileType(handle) == 1, "path_type"); // FILE_TYPE_DISK
            ByHandleInformation info;
            Require(Native.GetFileInformationByHandle(handle, out info), "metadata");
            Tick();
            uint serial, maximumComponentLength, flags;
            StringBuilder filesystem = new StringBuilder(32);
            Require(Native.GetVolumeInformationByHandleW(handle, IntPtr.Zero, 0,
                out serial, out maximumComponentLength, out flags, filesystem, 32), "filesystem");
            Tick();
            Require(String.Equals(filesystem.ToString(), "NTFS", StringComparison.Ordinal) &&
                (flags & 0x00000008) != 0 && (flags & 0x00400000) != 0 &&
                maximumComponentLength > 0, "filesystem");
            Require(serial == info.VolumeSerial, "identity_mismatch");
            ulong identifier = U64(Query(handle, 6, 8, true), 0); // NTFS FileInternalInformation
            byte[] basic = Query(handle, 4, 40, true); // FileBasicInformation; includes ChangeTime.
            ulong byHandleId = ((ulong)info.FileIndexHigh << 32) | info.FileIndexLow;
            Require(identifier != 0 && identifier == byHandleId, "identity_mismatch");
            Require((info.Attributes & AttributeReparse) == 0, "reparse");
            Require(((info.Attributes & AttributeDirectory) != 0) == directory, "path_type");
            Require(U32(basic, 32) == info.Attributes, "changed");
            if (!directory)
            {
                Require(info.Links == 1, "alias_count"); // Unknown/outside hardlink closure refuses.
            }
            Metadata result = new Metadata();
            result.Volume = serial;
            result.Id = identifier;
            result.Links = info.Links;
            result.Attributes = info.Attributes;
            return result;
        }

        private string ReadAcl(IntPtr handle)
        {
            return (string)ReadAclRecord(handle)["dacl_sha256"];
        }

        private Dictionary<string, object> ReadAclRecord(IntPtr handle, int checkBackup = 0)
        {
            Tick();
            IntPtr descriptor = IntPtr.Zero;
            try
            {
                IntPtr owner, dacl;
                uint status = Native.GetSecurityInfo(handle, 1, checkBackup != 0 ? 0x00000005U : 0x00000004U,
                    out owner, IntPtr.Zero, out dacl, IntPtr.Zero, out descriptor);
                Require(status == 0 && descriptor != IntPtr.Zero && dacl != IntPtr.Zero,
                    "acl_unavailable"); // Inventory: DACL only. New backup: owner + DACL custody check only.
                ushort control;
                uint revision;
                Require(Native.IsValidSecurityDescriptor(descriptor) &&
                    Native.GetSecurityDescriptorControl(descriptor, out control, out revision) &&
                    revision == 1 && (control & 0x8004) == 0x8004, "acl_unavailable");
                uint length = Native.GetSecurityDescriptorLength(descriptor);
                Require(length >= 20 && length <= 65536, "acl_unavailable");
                byte[] bytes = new byte[(int)length];
                Marshal.Copy(descriptor, bytes, 0, bytes.Length);
                RawSecurityDescriptor raw = new RawSecurityDescriptor(bytes, 0);
                Require(raw.DiscretionaryAcl != null && raw.DiscretionaryAcl.Count <= 128,
                    "acl_unavailable");
                if (checkBackup != 0)
                {
                    HashSet<string> trusted = new HashSet<string>(StringComparer.Ordinal) {
                        "S-1-5-18", "S-1-5-32-544", "S-1-5-21-283315059-370827648-873861665-1000"
                    };
                    Require(raw.Owner != null && trusted.Contains(raw.Owner.Value), "backup_security");
                    foreach (GenericAce ace in raw.DiscretionaryAcl)
                    {
                        CommonAce common = ace as CommonAce;
                        Require(common != null && !common.IsCallback &&
                            (common.AceQualifier == AceQualifier.AccessAllowed ||
                             common.AceQualifier == AceQualifier.AccessDenied), "backup_security");
                        if (common.AceQualifier == AceQualifier.AccessAllowed &&
                            (common.AceFlags & AceFlags.InheritOnly) == 0 && common.AccessMask != 0)
                        {
                            // On ancestors, ordinary add-file/subdirectory rights
                            // cannot replace our FILE_CREATE backup. Refuse effective
                            // delete-child/delete, WRITE_DAC/OWNER or GENERIC_ALL.
                            uint mask = unchecked((uint)common.AccessMask);
                            if (checkBackup == 1 || (mask & 0x100d0040U) != 0)
                                Require(trusted.Contains(common.SecurityIdentifier.Value), "backup_security");
                        }
                    }
                }
                ControlFlags flags = raw.ControlFlags & (ControlFlags.DiscretionaryAclPresent |
                    ControlFlags.DiscretionaryAclDefaulted | ControlFlags.DiscretionaryAclAutoInheritRequired |
                    ControlFlags.DiscretionaryAclAutoInherited | ControlFlags.DiscretionaryAclProtected |
                    ControlFlags.SelfRelative);
                RawSecurityDescriptor stable = new RawSecurityDescriptor(flags, null, null, null,
                    raw.DiscretionaryAcl);
                byte[] normalized = new byte[stable.BinaryLength];
                stable.GetBinaryForm(normalized, 0);
                using (SHA256 hash = SHA256.Create())
                    return new Dictionary<string, object> {
                        { "control", (int)flags }, { "dacl_sha256", Hex(hash.ComputeHash(normalized)) },
                        { "dacl_base64", Convert.ToBase64String(normalized) },
                        { "dacl_bytes", normalized.Length }, { "ace_count", raw.DiscretionaryAcl.Count }
                    };
            }
            finally
            {
                if (descriptor != IntPtr.Zero)
                    Require(Native.LocalFree(descriptor) == IntPtr.Zero, "cleanup_failed");
            }
        }

        private static bool SameObject(Metadata left, Metadata right)
        {
            // A running runner appends logs. Data size/time changes are not ACL changes.
            return left.Volume == right.Volume && left.Id == right.Id && left.Links == right.Links &&
                left.Attributes == right.Attributes;
        }

        private void Release(Held item)
        {
            Require(Native.CloseHandle(item.Handle), "cleanup_failed");
            owned.Remove(item.Handle);
            held.Remove(item);
        }

        private List<Dictionary<string, object>> inventory;
        private HashSet<string> identities;
        private long daclBytes, pathUnits, enumeratedUnits;
        private int discoveredCount;

        private List<string> ReadChildNames(Held directory)
        {
            Require(directory.Directory, "path_type");
            List<string> names = new List<string>();
            byte[] buffer = new byte[4096];
            GCHandle pin = GCHandle.Alloc(buffer, GCHandleType.Pinned);
            try
            {
                bool restart = true;
                int queries = 0;
                while (true)
                {
                    Tick();
                    Require(++queries <= MaxObjects + 3, "inventory_bound");
                    Array.Clear(buffer, 0, buffer.Length);
                    IoStatusBlock io = new IoStatusBlock();
                    int status = Native.NtQueryDirectoryFile(directory.Handle, IntPtr.Zero,
                        IntPtr.Zero, IntPtr.Zero, ref io, pin.AddrOfPinnedObject(),
                        (uint)buffer.Length, 12, true, IntPtr.Zero, restart);
                    restart = false;
                    Tick();
                    if (status == unchecked((int)0x80000006)) // STATUS_NO_MORE_FILES
                    {
                        Require(io.Status == status && io.Information.ToUInt64() == 0, "enumeration");
                        break;
                    }
                    ulong used = io.Information.ToUInt64();
                    Require(status == 0 && io.Status == 0 && used >= 14 && used <= 4096,
                        "enumeration");
                    uint length = U32(buffer, 8);
                    Require(U32(buffer, 0) == 0 && length >= 2 && length <= 2048 &&
                        (length & 1) == 0 && used >= 12 + length && used <= ((12 + length + 7) & ~7U),
                        "enumeration");
                    string name;
                    try { name = StrictUtf16.GetString(buffer, 12, (int)length); }
                    catch (DecoderFallbackException) { throw new Refusal("path_shape"); }
                    if (name == "." || name == "..") continue;
                    ValidateComponent(name);
                    enumeratedUnits += name.Length;
                    Require(++discoveredCount < MaxObjects &&
                        enumeratedUnits <= MaxPathUnits, "inventory_bound");
                    names.Add(name);
                }
                return names;
            }
            finally { pin.Free(); }
        }

        private void Walk(Held node, string path, string parentId, int depth)
        {
            Tick();
            Require(depth <= 32 && inventory.Count < MaxObjects && path.Length <= 4096,
                "inventory_bound");
            pathUnits += path.Length;
            daclBytes += (int)node.Dacl["dacl_bytes"];
            Require(pathUnits <= MaxPathUnits && daclBytes <= MaxDaclBytes, "inventory_bound");
            string objectId = node.Initial.Volume.ToString("x8", CultureInfo.InvariantCulture) + ":" +
                node.Initial.Id.ToString("x16", CultureInfo.InvariantCulture);
            Require(identities.Add(objectId), "identity_mismatch");
            Dictionary<string, object> record = new Dictionary<string, object>(node.Dacl);
            record["path"] = path;
            record["object_id"] = objectId;
            record["parent_id"] = parentId;
            record["object_kind"] = node.Directory ? "directory" : "file";
            record["link_count"] = node.Initial.Links;
            record["attributes"] = node.Initial.Attributes;
            inventory.Add(record);
            if (node.Directory)
            {
                // Names come from the already-held directory handle, never a
                // pathname reopen. The kernel cannot follow a substituted junction.
                List<string> names = ReadChildNames(node);
                names.Sort(StringComparer.Ordinal);
                HashSet<string> unique = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
                foreach (string name in names)
                {
                    Require(unique.Add(name), "path_shape");
                    Held child = OpenRelative(node, name, false, true);
                    Walk(child, path + @"\" + name, objectId, depth + 1);
                    Release(child);
                }
            }
            Require(SameObject(node.Initial, ReadMetadata(node.Handle, node.Directory)) &&
                node.Acl == ReadAcl(node.Handle), "changed");
        }

        private List<Dictionary<string, object>> Snapshot(Held root)
        {
            inventory = new List<Dictionary<string, object>>();
            identities = new HashSet<string>(StringComparer.Ordinal);
            daclBytes = 0;
            pathUnits = 0;
            enumeratedUnits = 0;
            discoveredCount = 0;
            Walk(root, @"C:\K5PhysicalRunner", "outside-approved-root", 0);
            return inventory;
        }

        internal Dictionary<string, object> RunInventory()
        {
            Held anchor = OpenAnchor();
            Held root = OpenRelative(anchor, "K5PhysicalRunner", true);
            List<Dictionary<string, object>> first = Snapshot(root);
            List<Dictionary<string, object>> second = Snapshot(root);
            Require(first.Count == second.Count, "inventory_changed");
            for (int i = 0; i < first.Count; i++)
                foreach (string key in new string[] { "path", "object_id", "parent_id", "object_kind",
                    "link_count", "attributes", "control", "dacl_base64", "dacl_sha256", "dacl_bytes", "ace_count" })
                    Require(Object.Equals(first[i][key], second[i][key]), "inventory_changed");
            Require(SameObject(anchor.Initial, ReadMetadata(anchor.Handle, true)) &&
                anchor.Acl == ReadAcl(anchor.Handle), "changed");
            Dictionary<string, object> result = BaseRecord();
            result["status"] = "observed";
            result["code"] = "none";
            result["closure_complete"] = true;
            result["records"] = first;
            result["object_count"] = first.Count;
            result["dacl_bytes"] = daclBytes;
            result["path_units"] = pathUnits;
            result["repair_ready"] = false;
            return result;
        }

        internal Dictionary<string, object> SaveBackup(string localAppData, string name, byte[] bytes)
        {
            Require(bytes != null && bytes.Length > 0 && bytes.Length <= 256 * 1024 * 1024,
                "bytes_bound");
            Require(localAppData != null && localAppData ==
                Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "path_shape");
            string[] parts = localAppData.Split('\\');
            Require(parts.Length == 5 && parts[0] == "C:" && parts[1] == "Users" &&
                parts[3] == "AppData" && parts[4] == "Local", "path_shape");
            ValidateComponent(parts[2]);
            ValidateComponent(name);
            const string prefix = "K5RunnerDaclBackup-";
            Require(name.StartsWith(prefix, StringComparison.Ordinal) &&
                name.EndsWith(".json", StringComparison.Ordinal), "path_shape");
            string[] run = name.Substring(prefix.Length, name.Length - prefix.Length - 5).Split('-');
            Require(run.Length == 2, "path_shape");
            foreach (string value in run)
            {
                Require(value.Length >= 1 && value.Length <= 20 && value[0] != '0', "path_shape");
                foreach (char digit in value) Require(digit >= '0' && digit <= '9', "path_shape");
            }
            Held anchor = OpenAnchor();
            Held users = OpenRelative(anchor, "Users", true);
            Held profile = OpenRelative(users, parts[2], true);
            Held appData = OpenRelative(profile, "AppData", true);
            Held local = OpenRelative(appData, "Local", true);
            Held[] custodyParents = new Held[] { users, profile, appData, local };
            List<string> parentIds = new List<string>();
            foreach (Held parent in custodyParents)
            {
                Require(parent.Acl == (string)ReadAclRecord(parent.Handle, 2)["dacl_sha256"], "changed");
                parentIds.Add(parent.Initial.Volume.ToString("x8", CultureInfo.InvariantCulture) + ":" +
                    parent.Initial.Id.ToString("x16", CultureInfo.InvariantCulture));
            }
            IntPtr buffer = IntPtr.Zero;
            IntPtr unicodeBuffer = IntPtr.Zero;
            IntPtr handle = IntPtr.Zero;
            try
            {
                BeforeOpen();
                buffer = Marshal.StringToHGlobalUni(name);
                UnicodeString nativeName = new UnicodeString();
                nativeName.Length = checked((ushort)(name.Length * 2));
                nativeName.MaximumLength = checked((ushort)(nativeName.Length + 2));
                nativeName.Buffer = buffer;
                unicodeBuffer = Marshal.AllocHGlobal(Marshal.SizeOf(typeof(UnicodeString)));
                Marshal.StructureToPtr(nativeName, unicodeBuffer, false);
                ObjectAttributes attributes = new ObjectAttributes();
                attributes.Length = (uint)Marshal.SizeOf(typeof(ObjectAttributes));
                attributes.RootDirectory = local.Handle;
                attributes.ObjectName = unicodeBuffer;
                attributes.Attributes = ObjCaseInsensitive | ObjDontReparse;
                IoStatusBlock io = new IoStatusBlock();
                // FILE_CREATE: refuse any existing name. Inherit normal profile
                // security; never change directory, owner, group, SACL or DACL.
                int status = Native.NtCreateFile(out handle, ReadData | 2 | ReadAttributes |
                    ReadControl | Synchronize, ref attributes, ref io, IntPtr.Zero,
                    0x00000080, 1, 2, FileNonDirectoryFile | FileOpenReparsePoint |
                    FileSynchronousIoNonalert, IntPtr.Zero, 0);
                Own(handle);
                Require(status == 0 && io.Status == 0 && io.Information.ToUInt64() == 2 &&
                    handle != IntPtr.Zero && handle != InvalidHandle, "backup_create");
                Metadata initial = ReadMetadata(handle, false);
                string initialDacl = (string)ReadAclRecord(handle, 1)["dacl_sha256"];
                string digest;
                using (SHA256 hash = SHA256.Create()) digest = Hex(hash.ComputeHash(bytes));
                using (Microsoft.Win32.SafeHandles.SafeFileHandle safe =
                    new Microsoft.Win32.SafeHandles.SafeFileHandle(handle, false))
                using (System.IO.FileStream stream = new System.IO.FileStream(safe,
                    System.IO.FileAccess.ReadWrite, 65536, false))
                {
                    stream.Write(bytes, 0, bytes.Length);
                    stream.Flush(true); // Flush file data through the OS before verification.
                    Require(stream.Length == bytes.Length, "backup_verify");
                    stream.Position = 0;
                    using (SHA256 hash = SHA256.Create())
                        Require(Hex(hash.ComputeHash(stream)) == digest, "backup_verify");
                    Require(stream.Position == bytes.Length && stream.Length == bytes.Length,
                        "backup_verify");
                }
                Metadata afterWrite = ReadMetadata(handle, false);
                // Our own data write may set FILE_ATTRIBUTE_ARCHIVE. Identity,
                // single-link/non-reparse file type and security remain invariant.
                Require(initial.Volume == afterWrite.Volume && initial.Id == afterWrite.Id &&
                    initial.Links == afterWrite.Links &&
                    initialDacl == (string)ReadAclRecord(handle, 1)["dacl_sha256"], "backup_verify");
                foreach (Held parent in custodyParents)
                    Require(SameObject(parent.Initial, ReadMetadata(parent.Handle, true)) &&
                        parent.Acl == (string)ReadAclRecord(parent.Handle, 2)["dacl_sha256"], "changed");
                Require(SameObject(anchor.Initial, ReadMetadata(anchor.Handle, true)) &&
                    anchor.Acl == ReadAcl(anchor.Handle), "changed");
                return new Dictionary<string, object> {
                    { "status", "observed" }, { "code", "none" },
                    { "backup_role", "current-profile-local-app-data" }, { "backup_name", name },
                    { "backup_sha256", digest }, { "backup_bytes", bytes.Length },
                    { "backup_object_id", initial.Volume.ToString("x8", CultureInfo.InvariantCulture) +
                        ":" + initial.Id.ToString("x16", CultureInfo.InvariantCulture) },
                    { "readback_verified", true }, { "repair_ready", false },
                    { "backup_parent_ids", parentIds }, { "requires_fresh_custody_revalidation", true }
                };
            }
            finally
            {
                if (unicodeBuffer != IntPtr.Zero) Marshal.FreeHGlobal(unicodeBuffer);
                if (buffer != IntPtr.Zero) Marshal.FreeHGlobal(buffer);
            }
        }

        internal bool CloseAll()
        {
            bool success = true;
            for (int i = owned.Count - 1; i >= 0; i--)
            {
                try { if (!Native.CloseHandle(owned[i])) success = false; }
                catch (Exception) { success = false; }
            }
            owned.Clear();
            return success;
        }
    }

    private static void ValidateComponent(string name)
    {
        Require(name != null && name.Length >= 1 && name.Length <= MaxNameUnits, "name_bound");
        Require(name != "." && name != ".." && !name.EndsWith(" ", StringComparison.Ordinal) &&
            !name.EndsWith(".", StringComparison.Ordinal), "path_shape");
        foreach (char character in name)
            Require(!Char.IsControl(character) && "<>:\"/\\|?*".IndexOf(character) < 0,
                "path_shape");
        // Reject DOS device stems even with extensions, spaces, and superscript digits.
        string stem = name.Split('.')[0].TrimEnd(' ').ToUpperInvariant();
        Require(stem != "CON" && stem != "PRN" && stem != "AUX" && stem != "NUL" &&
            stem != "CLOCK$" && stem != "CONIN$" && stem != "CONOUT$", "path_shape");
        if (stem.Length == 4 && (stem.StartsWith("COM", StringComparison.Ordinal) ||
            stem.StartsWith("LPT", StringComparison.Ordinal)))
            Require("0123456789\u00b9\u00b2\u00b3".IndexOf(stem[3]) < 0, "path_shape");
        // Strict decoder covers native input; explicit check also protects fixed/internal names.
        try { StrictUtf16.GetBytes(name); }
        catch (EncoderFallbackException) { throw new Refusal("path_shape"); }
    }

    private static uint U32(byte[] bytes, int offset) { return BitConverter.ToUInt32(bytes, offset); }
    private static ulong U64(byte[] bytes, int offset) { return BitConverter.ToUInt64(bytes, offset); }
    private static string Hex(byte[] bytes)
    {
        StringBuilder result = new StringBuilder(bytes.Length * 2);
        foreach (byte value in bytes) result.Append(value.ToString("x2", CultureInfo.InvariantCulture));
        return result.ToString();
    }

    private static void AssertAbi()
    {
        Require(IntPtr.Size == 8 && BitConverter.IsLittleEndian, "abi");
        Require(Marshal.SizeOf(typeof(UnicodeString)) == 16 &&
            Offset(typeof(UnicodeString), "Buffer") == 8 &&
            Marshal.SizeOf(typeof(ObjectAttributes)) == 48 &&
            Offset(typeof(ObjectAttributes), "RootDirectory") == 8 &&
            Offset(typeof(ObjectAttributes), "ObjectName") == 16 &&
            Offset(typeof(ObjectAttributes), "Attributes") == 24 &&
            Offset(typeof(ObjectAttributes), "SecurityDescriptor") == 32 &&
            Offset(typeof(ObjectAttributes), "SecurityQualityOfService") == 40 &&
            Marshal.SizeOf(typeof(IoStatusBlock)) == 16 &&
            Offset(typeof(IoStatusBlock), "Information") == 8 &&
            Marshal.SizeOf(typeof(ByHandleInformation)) == 52 &&
            Offset(typeof(ByHandleInformation), "VolumeSerial") == 28 &&
            Offset(typeof(ByHandleInformation), "FileIndexHigh") == 44 &&
            Offset(typeof(ByHandleInformation), "FileIndexLow") == 48, "abi");
    }

    private static int Offset(Type type, string field) { return Marshal.OffsetOf(type, field).ToInt32(); }
    [StructLayout(LayoutKind.Explicit, Size = 16)]
    private struct UnicodeString
    {
        [FieldOffset(0)] internal ushort Length;
        [FieldOffset(2)] internal ushort MaximumLength;
        [FieldOffset(8)] internal IntPtr Buffer;
    }
    [StructLayout(LayoutKind.Explicit, Size = 48)]
    private struct ObjectAttributes
    {
        [FieldOffset(0)] internal uint Length;
        [FieldOffset(8)] internal IntPtr RootDirectory;
        [FieldOffset(16)] internal IntPtr ObjectName;
        [FieldOffset(24)] internal uint Attributes;
        [FieldOffset(32)] internal IntPtr SecurityDescriptor;
        [FieldOffset(40)] internal IntPtr SecurityQualityOfService;
    }
    [StructLayout(LayoutKind.Explicit, Size = 16)]
    private struct IoStatusBlock
    {
        [FieldOffset(0)] internal int Status;
        [FieldOffset(8)] internal UIntPtr Information;
    }
    [StructLayout(LayoutKind.Sequential)]
    private struct NativeFileTime
    {
        internal uint Low, High;
        internal ulong Value { get { return ((ulong)High << 32) | Low; } }
    }
    [StructLayout(LayoutKind.Sequential)]
    private struct ByHandleInformation
    {
        internal uint Attributes;
        internal NativeFileTime Created, Accessed, Modified;
        internal uint VolumeSerial, SizeHigh, SizeLow, Links, FileIndexHigh, FileIndexLow;
    }
    private static class Native
    {
        // Resolve only the existing trusted Windows system DLLs, never the work directory.
        [DllImport("ntdll.dll", ExactSpelling = true)]
        [DefaultDllImportSearchPaths(DllImportSearchPath.System32)]
        internal static extern int NtOpenFile(out IntPtr handle, uint access,
            ref ObjectAttributes attributes, ref IoStatusBlock io, uint share, uint options);
        [DllImport("ntdll.dll", ExactSpelling = true)]
        [DefaultDllImportSearchPaths(DllImportSearchPath.System32)]
        internal static extern int NtQueryInformationFile(IntPtr handle, ref IoStatusBlock io,
            IntPtr information, uint length, int informationClass);
        [DllImport("ntdll.dll", ExactSpelling = true)]
        [DefaultDllImportSearchPaths(DllImportSearchPath.System32)]
        internal static extern int NtQueryDirectoryFile(IntPtr handle, IntPtr eventHandle,
            IntPtr apcRoutine, IntPtr apcContext, ref IoStatusBlock io, IntPtr information,
            uint length, int informationClass, [MarshalAs(UnmanagedType.U1)] bool singleEntry,
            IntPtr fileName, [MarshalAs(UnmanagedType.U1)] bool restartScan);
        [DllImport("ntdll.dll", ExactSpelling = true)]
        [DefaultDllImportSearchPaths(DllImportSearchPath.System32)]
        internal static extern int NtCreateFile(out IntPtr handle, uint access,
            ref ObjectAttributes attributes, ref IoStatusBlock io, IntPtr allocationSize,
            uint fileAttributes, uint share, uint disposition, uint options, IntPtr eaBuffer, uint eaLength);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, ExactSpelling = true)]
        [DefaultDllImportSearchPaths(DllImportSearchPath.System32)]
        internal static extern IntPtr CreateFileW(string name, uint access, uint share,
            IntPtr security, uint disposition, uint flags, IntPtr template);
        [DllImport("kernel32.dll", ExactSpelling = true)]
        [DefaultDllImportSearchPaths(DllImportSearchPath.System32)]
        [return: MarshalAs(UnmanagedType.Bool)]
        internal static extern bool GetFileInformationByHandle(IntPtr handle, out ByHandleInformation info);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, ExactSpelling = true)]
        [DefaultDllImportSearchPaths(DllImportSearchPath.System32)]
        [return: MarshalAs(UnmanagedType.Bool)]
        internal static extern bool GetVolumeInformationByHandleW(IntPtr handle, IntPtr volumeName,
            uint volumeNameSize, out uint serial, out uint maxComponent, out uint flags,
            StringBuilder filesystem, uint filesystemSize);
        [DllImport("kernel32.dll", ExactSpelling = true)]
        [DefaultDllImportSearchPaths(DllImportSearchPath.System32)]
        internal static extern uint GetFileType(IntPtr handle);
        [DllImport("kernel32.dll", ExactSpelling = true)]
        [DefaultDllImportSearchPaths(DllImportSearchPath.System32)]
        [return: MarshalAs(UnmanagedType.Bool)]
        internal static extern bool CloseHandle(IntPtr handle);
        [DllImport("kernel32.dll", ExactSpelling = true)]
        [DefaultDllImportSearchPaths(DllImportSearchPath.System32)]
        internal static extern IntPtr LocalFree(IntPtr memory);
        [DllImport("advapi32.dll", ExactSpelling = true)]
        [DefaultDllImportSearchPaths(DllImportSearchPath.System32)]
        internal static extern uint GetSecurityInfo(IntPtr handle, int kind, uint flags,
            out IntPtr owner, IntPtr group, out IntPtr dacl, IntPtr sacl, out IntPtr descriptor);
        [DllImport("advapi32.dll", ExactSpelling = true)]
        [DefaultDllImportSearchPaths(DllImportSearchPath.System32)]
        [return: MarshalAs(UnmanagedType.Bool)]
        internal static extern bool IsValidSecurityDescriptor(IntPtr descriptor);
        [DllImport("advapi32.dll", ExactSpelling = true)]
        [DefaultDllImportSearchPaths(DllImportSearchPath.System32)]
        [return: MarshalAs(UnmanagedType.Bool)]
        internal static extern bool GetSecurityDescriptorControl(IntPtr descriptor, out ushort control, out uint revision);
        [DllImport("advapi32.dll", ExactSpelling = true)]
        [DefaultDllImportSearchPaths(DllImportSearchPath.System32)]
        internal static extern uint GetSecurityDescriptorLength(IntPtr descriptor);
    }
}
