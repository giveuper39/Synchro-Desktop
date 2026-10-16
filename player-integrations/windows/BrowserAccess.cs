// A bounded accessibility adapter, not browser history or a remote debugger.
// Only a top-level window whose title matches the selected media session is
// inspected. Document subtrees and other tabs are deliberately never traversed.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows.Automation;

namespace Synchro {
    public static class BrowserAccess {
        delegate bool EnumWindowsCallback(IntPtr handle, IntPtr argument);
        [DllImport("user32.dll")] static extern bool EnumWindows(EnumWindowsCallback callback, IntPtr argument);
        [DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr handle);
        [DllImport("user32.dll")] static extern bool IsWindow(IntPtr handle);
        [DllImport("user32.dll", CharSet = CharSet.Unicode)] static extern int GetWindowText(IntPtr handle, StringBuilder text, int count);
        [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr handle, out uint process);
        [DllImport("user32.dll")] static extern bool SetForegroundWindow(IntPtr handle);
        [DllImport("user32.dll")] static extern IntPtr GetForegroundWindow();
        [DllImport("user32.dll")] static extern uint SendInput(uint count, INPUT[] input, int size);
        [StructLayout(LayoutKind.Sequential)] struct INPUT { public uint type; public INPUTUNION data; }
        [StructLayout(LayoutKind.Explicit)] struct INPUTUNION {
            [FieldOffset(0)] public KEYBDINPUT keyboard;
            // Native INPUT's union must retain MOUSEINPUT's size on x64.
            [FieldOffset(0)] public MOUSEINPUT mouse;
        }
        [StructLayout(LayoutKind.Sequential)] struct KEYBDINPUT {
            public ushort virtualKey, scan; public uint flags, time; public UIntPtr extra;
        }
        [StructLayout(LayoutKind.Sequential)] struct MOUSEINPUT {
            public int x, y; public uint mouseData, flags, time; public UIntPtr extra;
        }
        static readonly JavaScriptSerializer Json = new JavaScriptSerializer();

        static string StringValue(Dictionary<string, object> data, string name) {
            object value;
            return data.TryGetValue(name, out value) && value != null ? Convert.ToString(value) : "";
        }
        static string WindowTitle(IntPtr window) {
            var title = new StringBuilder(4096);
            GetWindowText(window, title, title.Capacity);
            return title.ToString();
        }
        static string Normalized(string title) {
            return Regex.Replace(title ?? "", @"\s+", " ").Trim().ToLowerInvariant();
        }
        static bool MatchesTitle(string window, string media) {
            string normalized = Normalized(media);
            return normalized.Length >= 3 && Normalized(window).Contains(normalized)
                && window.IndexOf("youtube", StringComparison.OrdinalIgnoreCase) >= 0;
        }
        static bool IsYouTube(string address) {
            Uri url;
            if (!Uri.TryCreate(address, UriKind.Absolute, out url)) {
                if (!Uri.TryCreate("https://" + address, UriKind.Absolute, out url)) return false;
            }
            return url.Scheme == "https" && url.UserInfo == "" && url.IsDefaultPort
                && (url.Host == "youtube.com" || url.Host == "www.youtube.com" || url.Host == "m.youtube.com");
        }
        static bool AddressControl(AutomationElement element) {
            var current = element.Current;
            if (current.IsPassword || current.ControlType != ControlType.Edit) return false;
            string id = (current.AutomationId ?? "").ToLowerInvariant();
            string name = (current.Name ?? "").ToLowerInvariant();
            return id == "urlbar-input" || id == "address and search bar" || id == "omnibox"
                || id.Contains("address") || id.Contains("urlbar")
                || name.Contains("address and search") || name.Contains("search or enter address")
                || name.Contains("адресная строка") || name.Contains("адрес и поиск")
                || name.Contains("введите адрес") || name.Contains("поиск в google или введите url");
        }
        static AutomationElement AddressElement(AutomationElement element, int depth, ref int remaining) {
            if (depth > 7 || --remaining <= 0) return null;
            var current = element.Current;
            if (current.ControlType == ControlType.Document || current.ControlType == ControlType.List
                || current.ControlType == ControlType.TabItem || current.IsPassword) return null;
            if (AddressControl(element)) return element;
            var walker = TreeWalker.ControlViewWalker;
            for (var child = walker.GetFirstChild(element); child != null; child = walker.GetNextSibling(child)) {
                var found = AddressElement(child, depth + 1, ref remaining);
                if (found != null) return found;
            }
            return null;
        }
        static string ReadAddress(IntPtr window) {
            int remaining = 200;
            var element = AddressElement(AutomationElement.FromHandle(window), 0, ref remaining);
            if (element == null) return null;
            object pattern;
            if (!element.TryGetCurrentPattern(ValuePattern.Pattern, out pattern)) return null;
            string value = ((ValuePattern)pattern).Current.Value;
            if (!IsYouTube(value)) return null;
            if (!value.StartsWith("https://", StringComparison.OrdinalIgnoreCase)) value = "https://" + value;
            return value;
        }
        static bool ProcessMatches(string executable, string appId) {
            // Chromium AppUserModelIds can be profile GUIDs, so an unknown appId
            // is not treated as an executable identity. Title + YouTube URL must
            // still match, and ambiguous windows are rejected below.
            var match = Regex.Match(appId ?? "", @"(?:^|[/\\])([\w.-]+)\.exe(?:$|[!#])", RegexOptions.IgnoreCase);
            return !match.Success || String.Equals(executable, match.Groups[1].Value, StringComparison.OrdinalIgnoreCase);
        }
        static Dictionary<string, object> Inspect(Dictionary<string, object> request) {
            var found = new List<Dictionary<string, object>>();
            EnumWindows(delegate(IntPtr window, IntPtr unused) {
                if (!IsWindowVisible(window) || !MatchesTitle(WindowTitle(window), StringValue(request, "title"))) return true;
                try {
                    uint pid;
                    GetWindowThreadProcessId(window, out pid);
                    using (var process = Process.GetProcessById((int)pid)) {
                        if (!ProcessMatches(process.ProcessName, StringValue(request, "appId"))) return true;
                        string address = ReadAddress(window);
                        if (address == null) return true;
                        found.Add(new Dictionary<string, object> {
                            { "windowId", window.ToInt64().ToString() }, { "pid", pid },
                            { "browser", process.ProcessName }, { "url", address }
                        });
                    }
                } catch (ElementNotAvailableException) { }
                catch (InvalidOperationException) { }
                catch (UnauthorizedAccessException) { }
                return true;
            }, IntPtr.Zero);
            return found.Count == 1 ? found[0] : new Dictionary<string, object>();
        }
        static INPUT Key(ushort value, bool release, bool unicode) {
            var input = new INPUT();
            input.type = 1;
            input.data.keyboard.virtualKey = unicode ? (ushort)0 : value;
            input.data.keyboard.scan = unicode ? value : (ushort)0;
            input.data.keyboard.flags = (release ? 2u : 0u) | (unicode ? 4u : 0u);
            return input;
        }
        static void Send(INPUT[] input) {
            if (SendInput((uint)input.Length, input, Marshal.SizeOf(typeof(INPUT))) != input.Length)
                throw new InvalidOperationException("The selected browser is not accepting input");
        }
        static Dictionary<string, object> Navigate(Dictionary<string, object> request) {
            string destination = StringValue(request, "url");
            if (!IsYouTube(destination)) throw new ArgumentException("Only YouTube destinations are supported");
            var window = new IntPtr(Int64.Parse(StringValue(request, "windowId")));
            uint pid;
            GetWindowThreadProcessId(window, out pid);
            if (!IsWindow(window) || pid != UInt32.Parse(StringValue(request, "pid")))
                throw new InvalidOperationException("The selected browser was closed");
            string expected = StringValue(request, "expectedUrl");
            string current = ReadAddress(window);
            // Never navigate a different tab if the user changed the selection.
            if (current == null || !String.Equals(current, expected, StringComparison.Ordinal))
                throw new InvalidOperationException("Select the original YouTube tab before returning playback");
            SetForegroundWindow(window);
            Thread.Sleep(60);
            if (GetForegroundWindow() != window)
                throw new InvalidOperationException("The selected browser could not receive focus");
            Send(new INPUT[] { Key(0x11, false, false), Key(0x4C, false, false), Key(0x4C, true, false), Key(0x11, true, false) });
            var keys = new List<INPUT>();
            foreach (char character in destination) {
                keys.Add(Key(character, false, true)); keys.Add(Key(character, true, true));
            }
            Send(keys.ToArray());
            Send(new INPUT[] { Key(0x0D, false, false), Key(0x0D, true, false) });
            return new Dictionary<string, object> { { "ok", true } };
        }
        public static void Run(string input) {
            try {
                var request = Json.Deserialize<Dictionary<string, object>>(input);
                object response = StringValue(request, "action") == "navigate" ? Navigate(request) : Inspect(request);
                Console.Write(Json.Serialize(response));
            } catch {
                Console.Write("{\"error\":\"The selected browser could not expose its YouTube address\"}");
            }
        }
    }
}
