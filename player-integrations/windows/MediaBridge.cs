// Read-only handle discovery is restricted to the installed Windows Media Player.
// No process memory, recent-file database, injection, or other app handles.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;

namespace Synchro
{
    public static class MediaBridge
    {
        private const string PlayerAppId = "Microsoft.ZuneMusic_8wekyb3d8bbwe!Microsoft.ZuneMusic";

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern IntPtr OpenProcess(uint access, bool inherit, int id);
        [DllImport("kernel32.dll")]
        private static extern IntPtr GetCurrentProcess();
        [DllImport("kernel32.dll")]
        private static extern bool CloseHandle(IntPtr handle);
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool DuplicateHandle(IntPtr process, IntPtr source, IntPtr targetProcess,
            out IntPtr target, uint access, bool inherit, uint options);
        [DllImport("kernel32.dll")]
        private static extern uint GetFileType(IntPtr handle);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern uint GetFinalPathNameByHandle(IntPtr handle, StringBuilder path,
            uint count, uint flags);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern bool QueryFullProcessImageName(IntPtr handle, uint flags,
            StringBuilder path, ref uint count);
        [DllImport("ntdll.dll")]
        private static extern int NtQueryInformationProcess(IntPtr process, int informationClass,
            IntPtr information, int size, out int returned);
        [DllImport("shell32.dll", CharSet = CharSet.Unicode, PreserveSig = false)]
        private static extern void SHParseDisplayName(string name, IntPtr context,
            out IntPtr itemId, uint attributesIn, out uint attributesOut);
        [DllImport("shell32.dll", PreserveSig = false)]
        private static extern void SHCreateShellItemArrayFromIDLists(uint count,
            [In] IntPtr[] items, out IntPtr array);

        [ComImport, Guid("2e941141-7f97-4756-ba1d-9decde894a3d"),
         InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
        private interface IApplicationActivationManager
        {
            void ActivateApplication([MarshalAs(UnmanagedType.LPWStr)] string appId,
                [MarshalAs(UnmanagedType.LPWStr)] string arguments, uint options, out uint processId);
            void ActivateForFile([MarshalAs(UnmanagedType.LPWStr)] string appId, IntPtr items,
                [MarshalAs(UnmanagedType.LPWStr)] string verb, out uint processId);
            void ActivateForProtocol([MarshalAs(UnmanagedType.LPWStr)] string appId,
                IntPtr items, out uint processId);
        }

        private static bool IsInstalledPlayer(IntPtr process)
        {
            var path = new StringBuilder(32768);
            uint size = (uint)path.Capacity;
            if (!QueryFullProcessImageName(process, 0, path, ref size)) return false;
            string image = path.ToString();
            string name = Path.GetFileName(image);
            return (string.Equals(name, "MediaPlayer.exe", StringComparison.OrdinalIgnoreCase)
                    || string.Equals(name, "Microsoft.Media.Player.exe", StringComparison.OrdinalIgnoreCase))
                && image.IndexOf(@"\Microsoft.ZuneMusic_", StringComparison.OrdinalIgnoreCase) >= 0;
        }

        public static string[] OpenMediaFiles()
        {
            var files = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            var candidates = new List<Process>(Process.GetProcessesByName("MediaPlayer"));
            candidates.AddRange(Process.GetProcessesByName("Microsoft.Media.Player"));
            foreach (Process candidate in candidates)
            {
                using (candidate)
                {
                    IntPtr process = OpenProcess(0x40 | 0x400 | 0x1000, false, candidate.Id);
                    if (process == IntPtr.Zero) continue;
                    try
                    {
                        if (!IsInstalledPlayer(process)) continue;
                        ReadHandles(process, files);
                    }
                    finally { CloseHandle(process); }
                }
            }
            var result = new string[files.Count];
            files.CopyTo(result);
            Array.Sort(result, StringComparer.OrdinalIgnoreCase);
            return result;
        }

        private static void ReadHandles(IntPtr process, HashSet<string> files)
        {
            int size = 65536;
            while (size <= 64 * 1024 * 1024)
            {
                IntPtr buffer = Marshal.AllocHGlobal(size);
                try
                {
                    int returned;
                    int status = NtQueryInformationProcess(process, 51, buffer, size, out returned);
                    if (status == unchecked((int)0xC0000004))
                    { size = Math.Max(size * 2, returned); continue; }
                    if (status != 0) return;
                    long count = Marshal.ReadIntPtr(buffer).ToInt64();
                    int stride = IntPtr.Size * 3 + 16;
                    if (count < 0 || count > (size - 2 * IntPtr.Size) / stride) return;
                    for (long index = 0; index < count; index++)
                    {
                        IntPtr source = Marshal.ReadIntPtr(buffer, checked(2 * IntPtr.Size + (int)index * stride));
                        IntPtr duplicate;
                        if (!DuplicateHandle(process, source, GetCurrentProcess(), out duplicate, 0, false, 2)) continue;
                        try
                        {
                            // Disk handles only: querying pipes/devices can block indefinitely.
                            if (GetFileType(duplicate) != 1) continue;
                            var name = new StringBuilder(32768);
                            uint length = GetFinalPathNameByHandle(duplicate, name, (uint)name.Capacity, 0);
                            if (length == 0 || length >= name.Capacity) continue;
                            string path = name.ToString();
                            if (path.StartsWith(@"\\?\UNC\", StringComparison.OrdinalIgnoreCase)) path = @"\\" + path.Substring(8);
                            else if (path.StartsWith(@"\\?\", StringComparison.Ordinal)) path = path.Substring(4);
                            if (File.Exists(path)) files.Add(path);
                        }
                        finally { CloseHandle(duplicate); }
                    }
                    return;
                }
                finally { Marshal.FreeHGlobal(buffer); }
            }
        }

        public static uint OpenMediaFile(string path)
        {
            path = Path.GetFullPath(path);
            if (!File.Exists(path)) throw new FileNotFoundException("The media file is unavailable", path);
            IntPtr item = IntPtr.Zero;
            IntPtr array = IntPtr.Zero;
            object instance = null;
            try
            {
                uint attributes;
                SHParseDisplayName(path, IntPtr.Zero, out item, 0, out attributes);
                SHCreateShellItemArrayFromIDLists(1, new[] { item }, out array);
                instance = Activator.CreateInstance(Type.GetTypeFromCLSID(
                    new Guid("45BA127D-10A8-46EA-8AB7-56EA9078943C")));
                uint processId;
                ((IApplicationActivationManager)instance).ActivateForFile(PlayerAppId, array, "open", out processId);
                return processId;
            }
            finally
            {
                if (instance != null) Marshal.FinalReleaseComObject(instance);
                if (array != IntPtr.Zero) Marshal.Release(array);
                if (item != IntPtr.Zero) Marshal.FreeCoTaskMem(item);
            }
        }
    }
}
