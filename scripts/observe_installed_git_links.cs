// Task-local, read-only ACL and .service prerequisite observation. No repair code.
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
    private const int MaxLinks = 16;
    private const int MaxHandles = 64;
    private const int MaxNameUnits = 1024;
    private const int LinkBufferBytes = 65536;
    private const long MaxFileBytes = 32L * 1024 * 1024;
    private const long MaxTotalBytes = 128L * 1024 * 1024;
    private const long MaxElapsedMs = 8000;
    private const uint ReadControl = 0x00020000;
    private const uint Synchronize = 0x00100000;
    private const uint ReadAttributes = 0x00000080;
    private const uint ReadData = 0x00000001;
    private const uint Traverse = 0x00000020;
    private const uint ShareRead = 0x00000001;
    private const uint ObjCaseInsensitive = 0x00000040;
    private const uint ObjDontReparse = 0x00001000;
    private const uint FileDirectoryFile = 0x00000001;
    private const uint FileNonDirectoryFile = 0x00000040;
    private const uint FileSynchronousIoNonalert = 0x00000020;
    private const uint FileOpenReparsePoint = 0x00200000;
    private const uint AttributeDirectory = 0x00000010;
    private const uint AttributeReparse = 0x00000400;
    private const int FileHardLinkInformation = 46;
    private static readonly IntPtr InvalidHandle = new IntPtr(-1);
    private static readonly Encoding StrictUtf16 = new UnicodeEncoding(false, false, true);
    private static readonly HashSet<string> Codes = new HashSet<string>(StringComparer.Ordinal)
    {
        "none", "platform", "abi", "anchor_open", "relative_open", "metadata",
        "filesystem", "reparse", "path_type", "acl_unavailable", "acl_null",
        "alias_outside_root", "alias_duplicate", "alias_missing_primary", "alias_count",
        "identity_mismatch", "path_shape", "name_bound", "file_size", "bytes_bound",
        "time_bound", "enumeration", "changed", "read_failed", "handle_bound",
        "cleanup_failed", "internal", "service_metadata_absent", "service_metadata"
    };

    // Invoke only after the parent has admitted this own PowerShell observation step.
    // Compilation is complete before this timer starts. No process lookup or kills.
    public static Dictionary<string, object> ObserveGuarded()
    {
        System.Threading.Timer watchdog = new System.Threading.Timer(
            delegate(object state) { Environment.Exit(124); }, null, 20000,
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
            complete = observation.RunStorage();
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

    private static Dictionary<string, object> BaseRecord()
    {
        return new Dictionary<string, object>(StringComparer.Ordinal)
        {
            { "schema_version", "fixed-runner-acl-prerequisite-v1" },
            { "scope", "fixed-runner-readonly-acl" },
            { "target", "runner-root-chain" },
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
        internal long Size, Created, Modified, Changed;
        internal bool Same(Metadata other)
        {
            return Volume == other.Volume && Id == other.Id && Links == other.Links &&
                Attributes == other.Attributes && Size == other.Size &&
                Created == other.Created && Modified == other.Modified && Changed == other.Changed;
        }
    }

    private sealed class Held
    {
        internal IntPtr Handle;
        internal bool Directory;
        internal Metadata Initial;
        internal string Acl;
    }

    private sealed class Link
    {
        internal Held Parent;
        internal ulong ParentId;
        internal string Name, Relative;
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

        private Held Capture(IntPtr handle, bool directory)
        {
            Tick();
            Held item = new Held();
            item.Handle = handle;
            item.Directory = directory;
            item.Initial = ReadMetadata(handle, directory);
            item.Acl = ReadAcl(handle);
            held.Add(item);
            return item;
        }

        private Held OpenAnchor()
        {
            BeforeOpen();
            // The ONLY absolute filesystem open: trusted C:\ OS/device-map anchor.
            // Share-read refuses conflicting writers/deleters; no fallback or retry.
            IntPtr handle = Native.CreateFileW(@"\\?\C:\", ReadAttributes | ReadControl |
                Synchronize | Traverse, ShareRead, IntPtr.Zero, 3,
                0x02000000 | FileOpenReparsePoint, IntPtr.Zero);
            Own(handle);
            Require(handle != IntPtr.Zero && handle != InvalidHandle, "anchor_open");
            return Capture(handle, true);
        }

        private Held OpenRelative(Held parent, string component, bool directory, bool serviceMetadata = false)
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
                    ReadAttributes | ReadControl | Synchronize | (directory ? Traverse : ReadData),
                    ref attributes, ref io, ShareRead,
                    FileOpenReparsePoint | FileSynchronousIoNonalert |
                    (directory ? FileDirectoryFile : FileNonDirectoryFile));
                Own(handle); // Own even an anomalous non-null handle on failure.
                if (serviceMetadata && status == unchecked((int)0xc0000034) &&
                    (handle == IntPtr.Zero || handle == InvalidHandle))
                    throw new Refusal("service_metadata_absent");
                Require(status == 0 && io.Status == 0 && handle != IntPtr.Zero &&
                    handle != InvalidHandle, "relative_open");
                Held result = Capture(handle, directory);
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
            Require(U32(basic, 32) == info.Attributes &&
                U64(basic, 0) == info.Created.Value && U64(basic, 16) == info.Modified.Value,
                "changed");
            ulong size = ((ulong)info.SizeHigh << 32) | info.SizeLow;
            Require(size <= Int64.MaxValue, "file_size");
            if (!directory)
            {
                Require(info.Links >= 1 && info.Links <= MaxLinks, "alias_count");
                Require(size > 0 && size <= (ulong)MaxFileBytes, "file_size");
            }
            Metadata result = new Metadata();
            result.Volume = serial;
            result.Id = identifier;
            result.Links = info.Links;
            result.Attributes = info.Attributes;
            result.Size = (long)size;
            result.Created = unchecked((long)U64(basic, 0));
            result.Modified = unchecked((long)U64(basic, 16));
            result.Changed = unchecked((long)U64(basic, 24));
            return result;
        }

        private string ReadAcl(IntPtr handle)
        {
            return (string)ReadAclRecord(handle)["acl_sha256"];
        }

        private Dictionary<string, object> ReadAclRecord(IntPtr handle)
        {
            Tick();
            IntPtr descriptor = IntPtr.Zero;
            try
            {
                IntPtr owner, dacl;
                uint status = Native.GetSecurityInfo(handle, 1, 0x00000005,
                    out owner, IntPtr.Zero, out dacl, IntPtr.Zero, out descriptor);
                Tick();
                Require(status == 0 && descriptor != IntPtr.Zero && owner != IntPtr.Zero,
                    "acl_unavailable"); // OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION
                Require(dacl != IntPtr.Zero, "acl_null");
                ushort control = 0;
                uint revision = 0;
                Require(Native.IsValidSecurityDescriptor(descriptor) &&
                    Native.GetSecurityDescriptorControl(descriptor, out control, out revision),
                    "acl_unavailable");
                Require(revision == 1 && (control & 0x8004) == 0x8004, "acl_unavailable");
                uint length = Native.GetSecurityDescriptorLength(descriptor);
                Require(length >= 20 && length <= 65536, "acl_unavailable");
                byte[] bytes = new byte[(int)length];
                Marshal.Copy(descriptor, bytes, 0, bytes.Length);
                RawSecurityDescriptor raw = new RawSecurityDescriptor(bytes, 0);
                Require(raw.Owner != null && raw.DiscretionaryAcl != null, "acl_null");
                Require(raw.SystemAcl == null, "acl_unavailable");
                // Re-encode owner + DACL as a stable self-relative descriptor. This
                // excludes native pointers, padding, omitted group, and all SACL data.
                ControlFlags retained = raw.ControlFlags & (ControlFlags.OwnerDefaulted |
                    ControlFlags.DiscretionaryAclPresent | ControlFlags.DiscretionaryAclDefaulted |
                    ControlFlags.DiscretionaryAclAutoInheritRequired |
                    ControlFlags.DiscretionaryAclAutoInherited |
                    ControlFlags.DiscretionaryAclProtected | ControlFlags.SelfRelative);
                RawSecurityDescriptor stable = new RawSecurityDescriptor(retained,
                    raw.Owner, null, null, raw.DiscretionaryAcl);
                byte[] normalized = new byte[stable.BinaryLength];
                stable.GetBinaryForm(normalized, 0);
                using (SHA256 hash = SHA256.Create())
                {
                    string digest = Hex(hash.ComputeHash(normalized));
                    Tick();
                    Require(raw.DiscretionaryAcl.Count <= 128, "acl_unavailable");
                    List<Dictionary<string, object>> entries = new List<Dictionary<string, object>>();
                    foreach (GenericAce ace in raw.DiscretionaryAcl)
                    {
                        KnownAce known = ace as KnownAce;
                        Require(known != null && known.SecurityIdentifier != null, "acl_unavailable");
                        entries.Add(new Dictionary<string, object> {
                            { "type", (int)ace.AceType }, { "flags", (int)ace.AceFlags },
                            { "mask", unchecked((uint)known.AccessMask) },
                            { "sid", known.SecurityIdentifier.Value }
                        });
                    }
                    return new Dictionary<string, object> {
                        { "owner_sid", raw.Owner.Value }, { "control", (int)retained },
                        { "acl_sha256", digest }, { "owner_dacl_base64", Convert.ToBase64String(normalized) },
                        { "aces", entries }
                    };
                }
            }
            finally
            {
                if (descriptor != IntPtr.Zero)
                    Require(Native.LocalFree(descriptor) == IntPtr.Zero, "cleanup_failed");
            }
        }

        private List<Link> Closure(Held primary, Held git, Held cmd)
        {
            Require(git.Initial.Volume == primary.Initial.Volume &&
                cmd.Initial.Volume == primary.Initial.Volume && git.Initial.Id != cmd.Initial.Id,
                "identity_mismatch");
            byte[] bytes = Query(primary.Handle, FileHardLinkInformation, LinkBufferBytes, false);
            Require(bytes.Length >= 30, "enumeration");
            uint needed = U32(bytes, 0);
            uint count = U32(bytes, 4);
            Require(needed == (uint)bytes.Length && needed <= LinkBufferBytes, "enumeration");
            Require(count >= 1 && count <= MaxLinks && count == primary.Initial.Links, "alias_count");
            List<Link> result = new List<Link>();
            HashSet<string> names = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            bool hasPrimary = false;
            int position = 8; // Native FILE_LINKS_INFORMATION.Entry starts at offset 8.
            for (uint index = 0; index < count; index++)
            {
                Tick();
                Require((position & 7) == 0 && position <= bytes.Length - 20, "enumeration");
                uint next = U32(bytes, position);
                ulong parentId = U64(bytes, position + 8);
                uint nameUnits = U32(bytes, position + 16);
                Require(parentId != 0, "identity_mismatch");
                Require(nameUnits >= 1 && nameUnits <= MaxNameUnits, "name_bound");
                int nameBytes = checked((int)nameUnits * 2); // WCHAR count, not byte count.
                int end = checked(position + 20 + nameBytes);
                Require(end <= bytes.Length, "enumeration");
                string name;
                try { name = StrictUtf16.GetString(bytes, position + 20, nameBytes); }
                catch (DecoderFallbackException) { throw new Refusal("path_shape"); }
                ValidateComponent(name);
                // Only two genuine, already-held NTFS parents are resolvable. Never
                // search a directory, open by ID, or open metadata outside this closure.
                Held parent;
                string relative;
                if (parentId == git.Initial.Id) { parent = git; relative = name; }
                else if (parentId == cmd.Initial.Id) { parent = cmd; relative = "cmd/" + name; }
                else throw new Refusal("alias_outside_root");
                Require(names.Add(relative), "alias_duplicate");
                if (parent == cmd && String.Equals(name, "git.exe", StringComparison.OrdinalIgnoreCase))
                    hasPrimary = true;
                result.Add(new Link { Parent = parent, ParentId = parentId, Name = name, Relative = relative });
                if (index + 1 < count)
                {
                    int minimum = checked((20 + nameBytes + 7) & ~7);
                    Require(next >= (uint)minimum && (next & 7) == 0 &&
                        next <= (uint)(bytes.Length - position - 20), "enumeration");
                    position = checked(position + (int)next);
                }
                else
                {
                    // No counted NUL terminator is permitted. At most native tail padding.
                    Require(next == 0 && bytes.Length >= end &&
                        bytes.Length <= ((end + 7) & ~7), "enumeration");
                }
            }
            Require(hasPrimary, "alias_missing_primary");
            result.Sort(delegate(Link a, Link b) { return StringComparer.Ordinal.Compare(a.Relative, b.Relative); });
            return result;
        }

        private string Digest(Held file)
        {
            Tick();
            long newPosition;
            Require(Native.SetFilePointerEx(file.Handle, 0, out newPosition, 0) && newPosition == 0,
                "read_failed"); // Position of our held handle only, no filesystem mutation.
            Tick();
            long read = 0;
            byte[] buffer = new byte[65536];
            using (SHA256 hash = SHA256.Create())
            {
                while (read < file.Initial.Size)
                {
                    Tick();
                    uint request = (uint)Math.Min((long)buffer.Length, file.Initial.Size - read);
                    // Bound the requested transfer BEFORE ReadFile, including failed/short reads.
                    Require(request > 0 && BytesRead <= MaxTotalBytes - request, "bytes_bound");
                    uint received;
                    bool ok = Native.ReadFile(file.Handle, buffer, request, out received, IntPtr.Zero);
                    Require(received <= request, "read_failed");
                    BytesRead = checked(BytesRead + received);
                    Tick();
                    Require(ok && received > 0, "read_failed");
                    read = checked(read + received);
                    hash.TransformBlock(buffer, 0, (int)received, buffer, 0);
                }
                Require(read == file.Initial.Size, "read_failed");
                hash.TransformFinalBlock(new byte[0], 0, 0);
                Tick();
                return Hex(hash.Hash);
            }
        }

        internal Dictionary<string, object> Run()
        {
            Held anchor = OpenAnchor();
            Held programFiles = OpenRelative(anchor, "Program Files", true);
            Held git = OpenRelative(programFiles, "Git", true);
            Held cmd = OpenRelative(git, "cmd", true);
            Held primary = OpenRelative(cmd, "git.exe", false);
            Require(primary.Initial.Size <= MaxTotalBytes / (primary.Initial.Links + 2L), "bytes_bound");
            string primaryHash = Digest(primary);
            List<Link> first = Closure(primary, git, cmd);
            List<Dictionary<string, object>> aliases = new List<Dictionary<string, object>>();
            foreach (Link link in first)
            {
                Held alias = OpenRelative(link.Parent, link.Name, false);
                Require(alias.Initial.Same(primary.Initial), "identity_mismatch");
                Require(String.Equals(alias.Acl, primary.Acl, StringComparison.Ordinal), "identity_mismatch");
                string digest = Digest(alias);
                Require(String.Equals(digest, primaryHash, StringComparison.Ordinal), "identity_mismatch");
                aliases.Add(new Dictionary<string, object>(StringComparer.Ordinal)
                {
                    { "relative_name", link.Relative },
                    { "ntfs_volume_serial", alias.Initial.Volume.ToString("x8", CultureInfo.InvariantCulture) },
                    { "ntfs_file_id", alias.Initial.Id.ToString("x16", CultureInfo.InvariantCulture) },
                    { "link_count", alias.Initial.Links },
                    { "size_bytes", alias.Initial.Size },
                    { "sha256", digest },
                    { "acl_sha256", alias.Acl }
                });
            }
            Require(String.Equals(Digest(primary), primaryHash, StringComparison.Ordinal), "changed");
            List<Link> second = Closure(primary, git, cmd);
            Require(first.Count == second.Count, "changed");
            for (int i = 0; i < first.Count; i++)
                Require(first[i].ParentId == second[i].ParentId &&
                    String.Equals(first[i].Relative, second[i].Relative, StringComparison.Ordinal), "changed");
            // Recheck every held ancestor and alias, including ACL and ChangeTime.
            foreach (Held item in held)
            {
                Require(item.Initial.Same(ReadMetadata(item.Handle, item.Directory)), "changed");
                Require(String.Equals(item.Acl, ReadAcl(item.Handle), StringComparison.Ordinal), "changed");
            }
            Tick();
            Dictionary<string, object> result = BaseRecord();
            result["status"] = "observed";
            result["code"] = "none";
            result["closure_complete"] = true;
            result["link_count"] = primary.Initial.Links;
            result["primary_sha256"] = primaryHash;
            result["aliases"] = aliases;
            return result;
        }

        private string ReadServiceName(Held file)
        {
            // Only the fixed runner .service file is read. Never .credentials,
            // .runner, logs, executable content, or a caller-provided pathname.
            Require(file.Initial.Links == 1 && file.Initial.Size <= 512, "service_metadata");
            long position;
            Require(Native.SetFilePointerEx(file.Handle, 0, out position, 0) && position == 0,
                "read_failed");
            byte[] bytes = new byte[(int)file.Initial.Size];
            uint received;
            Tick();
            Require(BytesRead <= 1024 - bytes.Length, "bytes_bound");
            bool read = Native.ReadFile(file.Handle, bytes, (uint)bytes.Length, out received, IntPtr.Zero);
            Require(received <= bytes.Length, "read_failed");
            BytesRead += received;
            Tick();
            Require(read && received == bytes.Length, "read_failed");
            string text;
            try { text = new UTF8Encoding(false, true).GetString(bytes); }
            catch (DecoderFallbackException) { throw new Refusal("service_metadata"); }
            if (text.StartsWith("\ufeff", StringComparison.Ordinal)) text = text.Substring(1);
            if (text.EndsWith("\r\n", StringComparison.Ordinal)) text = text.Substring(0, text.Length - 2);
            else if (text.EndsWith("\n", StringComparison.Ordinal)) text = text.Substring(0, text.Length - 1);
            Require(text.Length > 15 && text.Length <= 256 &&
                text.StartsWith("actions.runner.", StringComparison.Ordinal), "service_metadata");
            foreach (char value in text)
                Require((value >= 'a' && value <= 'z') || (value >= 'A' && value <= 'Z') ||
                    (value >= '0' && value <= '9') || value == '.' || value == '_' || value == '-',
                    "service_metadata");
            return text;
        }

        internal Dictionary<string, object> RunStorage()
        {
            // Fixed directory chain and one non-secret .service metadata file only.
            // No recursion, executable reads, permission mutation, or creation.
            Held anchor = OpenAnchor();
            Held runner = OpenRelative(anchor, "K5PhysicalRunner", true);
            Held work = OpenRelative(runner, "_work", true);
            Held repo = OpenRelative(work, "K5-Vision", true);
            Held workspace = OpenRelative(repo, "K5-Vision", true);
            Held temp = OpenRelative(work, "_temp", true);
            Held serviceFile = OpenRelative(runner, ".service", false, true);
            string serviceName = ReadServiceName(serviceFile);
            Held[] nodes = new Held[] { anchor, runner, work, repo, workspace, temp };
            string[] roles = new string[] { "volume_root", "runner_root", "work_root", "repository_parent", "workspace", "temp_root" };
            string tokenSid;
            using (System.Security.Principal.WindowsIdentity identity = System.Security.Principal.WindowsIdentity.GetCurrent())
            {
                Require(identity.User != null, "acl_unavailable");
                tokenSid = identity.User.Value;
            }
            List<Dictionary<string, object>> records = new List<Dictionary<string, object>>();
            for (int i = 0; i < nodes.Length; i++)
            {
                Dictionary<string, object> record = ReadAclRecord(nodes[i].Handle);
                Require((string)record["acl_sha256"] == nodes[i].Acl, "changed");
                record["role"] = roles[i];
                record["ntfs_volume_serial"] = nodes[i].Initial.Volume.ToString("x8", CultureInfo.InvariantCulture);
                record["ntfs_file_id"] = nodes[i].Initial.Id.ToString("x16", CultureInfo.InvariantCulture);
                records.Add(record);
            }
            foreach (Held item in held)
            {
                Require(item.Initial.Same(ReadMetadata(item.Handle, item.Directory)), "changed");
                Require(item.Acl == ReadAcl(item.Handle), "changed");
            }
            Require(ReadServiceName(serviceFile) == serviceName, "changed");
            using (System.Security.Principal.WindowsIdentity identity = System.Security.Principal.WindowsIdentity.GetCurrent())
                Require(identity.User != null && identity.User.Value == tokenSid, "changed");
            Dictionary<string, object> result = BaseRecord();
            result["status"] = "observed";
            result["code"] = "none";
            result["closure_complete"] = true;
            result["current_token_sid"] = tokenSid;
            result["service_identity"] = "unresolved";
            result["runner_service_name"] = serviceName;
            result["group_metadata"] = "not_queried";
            result["retention_nodes"] = "not_queried";
            result["directories"] = records;
            return result;
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
            Offset(typeof(ByHandleInformation), "FileIndexLow") == 48 &&
            Marshal.SizeOf(typeof(LinkEntryLayout)) == 24 &&
            Offset(typeof(LinkEntryLayout), "ParentFileId") == 8 &&
            Offset(typeof(LinkEntryLayout), "NameUnits") == 16 &&
            Offset(typeof(LinkEntryLayout), "FirstNameUnit") == 20 &&
            Offset(typeof(LinksLayout), "Entry") == 8, "abi");
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
    [StructLayout(LayoutKind.Explicit, Size = 24)]
    private struct LinkEntryLayout
    {
        [FieldOffset(0)] internal uint NextEntryOffset;
        [FieldOffset(8)] internal ulong ParentFileId;
        [FieldOffset(16)] internal uint NameUnits;
        [FieldOffset(20)] internal ushort FirstNameUnit;
    }
    [StructLayout(LayoutKind.Explicit, Size = 32)]
    private struct LinksLayout
    {
        [FieldOffset(0)] internal uint BytesNeeded;
        [FieldOffset(4)] internal uint EntriesReturned;
        [FieldOffset(8)] internal LinkEntryLayout Entry;
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
        internal static extern bool ReadFile(IntPtr handle, [Out] byte[] buffer, uint count,
            out uint read, IntPtr overlapped);
        [DllImport("kernel32.dll", ExactSpelling = true)]
        [DefaultDllImportSearchPaths(DllImportSearchPath.System32)]
        [return: MarshalAs(UnmanagedType.Bool)]
        internal static extern bool SetFilePointerEx(IntPtr handle, long distance, out long position, uint method);
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
