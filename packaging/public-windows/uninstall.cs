using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.IO.Compression;
using System.Linq;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text.RegularExpressions;
using System.Windows.Forms;
using Microsoft.Win32;

internal static class Uninstaller
{
    internal static readonly string Version = Assembly.GetExecutingAssembly().GetName().Version.ToString(3);
    private const string Title = "MagicConverter 제거";
    private const string ExplorerKey = @"Software\Classes\*\shell\MagicConverter";
    private static readonly Regex ProfileFile = new Regex(
        @"\A(?:(?:settings|history|automation)\.json(?:\.bak|\.lock|\.corrupt-[0-9T-]+)?|" +
        @"(?:ipc-receipts|watch-journal)\.sqlite3(?:-wal|-shm|-journal)?|" +
        @"(?:magic_converter|watch)\.lock|magic_converter\.log(?:\.[12])?|\.magic-json-[a-z0-9_]+)\z",
        RegexOptions.IgnoreCase | RegexOptions.CultureInvariant);

    [STAThread]
    public static int Main(string[] args)
    {
        if (args.Length == 1 && args[0] == "--version")
        {
            Console.WriteLine(Version);
            return 0;
        }
        if (args.Length != 0)
            return 2;
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);
        var local = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        var programs = Environment.GetFolderPath(Environment.SpecialFolder.Programs);
        if (String.IsNullOrEmpty(local) || String.IsNullOrEmpty(programs))
            return 1;
        try
        {
            var folder = InstalledFolder(local);
            using (var wizard = new UninstallWizard(Version, folder, () =>
            {
                using (var native = RegistryKey.OpenBaseKey(RegistryHive.CurrentUser, RegistryView.Registry64))
                using (var redirected = RegistryKey.OpenBaseKey(RegistryHive.CurrentUser, RegistryView.Registry32))
                    return Remove(local, programs, folder, native, redirected);
            }))
            {
                wizard.ShowDialog();
                return wizard.ExitCode;
            }
        }
        catch (Exception error)
        {
            MessageBox.Show("제거를 완료하지 못했습니다.\n\n" + error.Message, Title,
                            MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
    }

    internal static string InstalledFolder(string local)
    {
        var root = InstallLocation.Root(Path.GetFullPath(local));
        var saved = InstallLocation.Read(root, Version);
        return InstallLocation.LookupFolder(root, Version, saved);
    }

    internal static List<string> Remove(string local, string programs, string confirmedFolder, params RegistryKey[] userHives)
    {
        AppContext.SetSwitch("Switch.System.IO.UseLegacyPathHandling", false);
        AppContext.SetSwitch("Switch.System.IO.BlockLongPaths", false);
        local = Path.GetFullPath(local);
        programs = Path.GetFullPath(programs);
        var root = InstallLocation.Root(local);
        var saved = InstallLocation.Read(root, Version);
        var installed = InstallLocation.LookupFolder(root, Version, saved);
        if (!InstallLocation.Same(installed, confirmedFolder))
            throw new IOException("설치 위치가 바뀌었습니다. 제거 도구를 다시 실행하세요.");
        var marker = Path.Combine(installed, ".installed");
        var token = Path.Combine(installed, InstallLocation.TokenFile);
        var gui = Path.Combine(installed, "magic_converter.exe");
        var profile = Path.Combine(local, "magic_converter", "magic_converter");
        var shortcut = Path.Combine(programs, "MagicConverter.lnk");
        var kept = new List<string>();
        var lockPath = Path.Combine(root, ".setup.lock");
        foreach (var path in new[] { root, installed, marker, token, profile, copyFolder, copy, shortcut, lockPath })
            RequirePlainPath(path);
        var current = Assembly.GetExecutingAssembly().Location;
        if (Below(current, installed) || Below(current, profile))
            throw new IOException("제거 EXE를 설치·데이터 폴더 밖으로 옮긴 뒤 실행하세요.");

        var setupLock = Directory.Exists(root)
            ? new FileStream(lockPath, FileMode.OpenOrCreate, FileAccess.ReadWrite, FileShare.None)
            : null;
        try
        {
            var currentLocation = InstallLocation.Read(root, Version);
            if ((saved == null) != (currentLocation == null) ||
                saved != null && (saved.Token != currentLocation.Token || !InstallLocation.Same(saved.Folder, currentLocation.Folder)))
                throw new IOException("설치 위치가 바뀌었습니다. 제거 도구를 다시 실행하세요.");
            if (saved != null && Directory.Exists(installed) && !saved.Matches(Version))
                throw new IOException("지정한 위치의 설치 표식을 확인할 수 없습니다.\n" + installed);
            if (File.Exists(marker) && (new FileInfo(marker).Length > 32 || File.ReadAllText(marker).Trim() != Version))
                throw new IOException(Version + " 설치 표식을 확인할 수 없습니다.\n" + installed);
            if (OwnedProcessRunning(installed) || LockOwnerRunning(Path.Combine(profile, "magic_converter.lock"))
                                               || LockOwnerRunning(Path.Combine(profile, "watch.lock")))
                throw new IOException("MagicConverter 또는 폴더 감시가 실행 중입니다. 종료한 뒤 다시 실행하세요.");

            RemoveShortcut(shortcut, gui);
            foreach (var hive in userHives)
                RemoveRegistry(hive, gui, copy);

            RemoveProfile(profile, kept);
            if (File.Exists(copy))
            {
                    File.Delete(copy);
                else
                    kept.Add(copyFolder);
            }
            RemoveEmpty(copyFolder);
            RemoveEmpty(Path.GetDirectoryName(copyFolder));
            RemoveEmpty(Path.Combine(local, "magic_converter"));

            if (File.Exists(marker))
            {
                RemoveInstalledFiles(installed);
                File.Delete(marker);
                if (saved != null)
                    File.Delete(token);
                RemoveEmpty(installed);
            }
            if (InstallLocation.Same(installed, InstallLocation.DefaultFolder(root, Version)))
                RemoveEmpty(InstallLocation.DefaultParent(root, Version));
            if (Directory.Exists(installed))
                kept.Add(installed);
            var log = Path.Combine(root, "setup-" + Version + ".log");
            RequirePlainPath(log);
            if (File.Exists(log))
                File.Delete(log);
            if (saved != null)
                InstallLocation.Save(root, Version, null);
        }
        finally
        {
            if (setupLock != null)
                setupLock.Dispose();
        }
        if (File.Exists(lockPath))
            File.Delete(lockPath);
        RemoveEmpty(root);
        return kept;
    }

    private static void RemoveInstalledFiles(string installed)
    {
        var directories = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        using (var resource = Assembly.GetExecutingAssembly().GetManifestResourceStream("uninstall-files.gz"))
        using (var gzip = new GZipStream(resource, CompressionMode.Decompress))
        using (var input = new StreamReader(gzip))
        {
            string name;
            while ((name = input.ReadLine()) != null)
            {
                var path = Path.GetFullPath(Path.Combine(installed, name.Replace('/', '\\')));
                if (!Below(path, installed))
                    throw new InvalidDataException("설치 파일 목록이 올바르지 않습니다.");
                if (PlainPath(path))
                {
                    var attributes = Attributes(path);
                    if (attributes.HasValue && (attributes.Value & FileAttributes.Directory) == 0 &&
                        !DeleteFile(LongPath(path)))
                    {
                        int error = Marshal.GetLastWin32Error();
                        if (error != 2 && error != 3)
                            throw new Win32Exception(error, "설치 파일을 지울 수 없습니다.\n" + path);
                    }
                }
                for (var folder = Path.GetDirectoryName(path); Below(folder, installed); folder = Path.GetDirectoryName(folder))
                    directories.Add(folder);
            }
        }
        foreach (var folder in directories.OrderByDescending(path => path.Length))
            RemoveEmpty(folder);
    }

    private static void RemoveProfile(string profile, List<string> kept)
    {
        if (!Directory.Exists(profile))
            return;
        foreach (var file in Directory.EnumerateFiles(profile))
            if (ProfileFile.IsMatch(Path.GetFileName(file)) && PlainPath(file))
                File.Delete(file);
        // The watcher uses SHA-256 event IDs for its private staging directories.
        var scratch = Path.Combine(profile, "watch-work");
        if (Directory.Exists(scratch) && PlainPath(scratch))
        {
            foreach (var job in Directory.EnumerateDirectories(scratch))
                if (Regex.IsMatch(Path.GetFileName(job), @"\A[0-9a-f]{64}\z") && PlainPath(job))
                    Directory.Delete(job, true);
            RemoveEmpty(scratch);
        }
        RemoveEmpty(profile);
        if (Directory.Exists(profile))
            kept.Add(profile);
    }

    private static void RemoveShortcut(string path, string gui)
    {
        if (!File.Exists(path))
            return;
        dynamic shell = Activator.CreateInstance(Type.GetTypeFromProgID("WScript.Shell", true));
        dynamic shortcut = shell.CreateShortcut(path);
        if (String.Equals((string)shortcut.TargetPath, gui, StringComparison.OrdinalIgnoreCase))
            File.Delete(path);
    }

    private static void RemoveRegistry(RegistryKey hive, string gui, string copy)
    {
        hive.DeleteSubKeyTree(@"Software\magic_converter\magic_converter", false);
        const string settings = @"Software\magic_converter";
        bool emptySettings;
        using (var key = hive.OpenSubKey(settings))
            emptySettings = key != null && key.ValueCount == 0 && key.SubKeyCount == 0;
        if (emptySettings)
            hive.DeleteSubKey(settings, false);
        string command;
        using (var key = hive.OpenSubKey(ExplorerKey + @"\command"))
            command = key == null ? null : key.GetValue("") as string;
        const string suffix = " --open \"%1\"";
        if (String.Equals(command, "\"" + gui + "\"" + suffix, StringComparison.OrdinalIgnoreCase) ||
            String.Equals(command, gui + suffix, StringComparison.OrdinalIgnoreCase))
            hive.DeleteSubKeyTree(ExplorerKey, false);
    }

    private static bool OwnedProcessRunning(string installed)
    {
        foreach (var name in new[] { "magic_converter", "magic_converter-cli", "python", "pythonw" })
            foreach (var process in Process.GetProcessesByName(name))
                using (process)
                {
                    try
                    {
                        var module = process.MainModule;
                        if (module != null && Below(module.FileName, installed))
                            return true;
                    }
                    catch (Win32Exception) { }
                    catch (InvalidOperationException) { }
                }
        return false;
    }

    private static bool LockOwnerRunning(string path)
    {
        RequirePlainPath(path);
        if (!File.Exists(path))
            return false;
        var lines = File.ReadLines(path).Take(3).ToArray();
        int pid;
        if (lines.Length != 3 || !String.Equals(lines[2], Environment.MachineName, StringComparison.OrdinalIgnoreCase) ||
            !Int32.TryParse(lines[0], out pid) || pid <= 0)
            return false;
        try
        {
            using (var process = Process.GetProcessById(pid))
                return process.StartTime.ToUniversalTime() <= File.GetLastWriteTimeUtc(path).AddSeconds(2);
        }
        catch (ArgumentException) { return false; }
        catch (InvalidOperationException) { return false; }
    }

    private static string Hash(string path)
    {
        using (var stream = File.OpenRead(path))
        using (var sha = SHA256.Create())
            return BitConverter.ToString(sha.ComputeHash(stream)).Replace("-", "").ToLowerInvariant();
    }

    private static bool Below(string path, string parent)
    {
        return path != null && path.StartsWith(parent.TrimEnd('\\') + "\\", StringComparison.OrdinalIgnoreCase);
    }

    private static bool PlainPath(string path)
    {
        for (var part = path; part != null; part = Path.GetDirectoryName(part))
        {
            var attributes = Attributes(part);
            if (attributes.HasValue && (attributes.Value & FileAttributes.ReparsePoint) != 0)
                return false;
        }
        return true;
    }

    private static void RequirePlainPath(string path)
    {
        if (!PlainPath(path))
            throw new IOException("다른 경로로 연결되는 파일이나 폴더는 처리할 수 없습니다.\n" + path);
    }

    private static void RemoveEmpty(string path)
    {
        var attributes = Attributes(path);
        if (!attributes.HasValue || (attributes.Value & FileAttributes.Directory) == 0 || !PlainPath(path))
            return;
        if (!RemoveDirectory(LongPath(path)))
        {
            int error = Marshal.GetLastWin32Error();
            if (error != 145 && error != 2 && error != 3)
                throw new Win32Exception(error, "설치 폴더를 지울 수 없습니다.\n" + path);
        }
    }

    private static FileAttributes? Attributes(string path)
    {
        uint attributes = GetFileAttributes(LongPath(path));
        if (attributes != UInt32.MaxValue)
            return (FileAttributes)attributes;
        int error = Marshal.GetLastWin32Error();
        if (error == 2 || error == 3)
            return null;
        throw new Win32Exception(error, "파일 상태를 확인할 수 없습니다.\n" + path);
    }

    // Use canonical extended paths for the long filenames in the Qt wheels.
    private static string LongPath(string path)
    {
        return path.StartsWith(@"\\", StringComparison.Ordinal) ? @"\\?\UNC\" + path.Substring(2) : @"\\?\" + path;
    }

    [DllImport("kernel32.dll", EntryPoint = "GetFileAttributesW", ExactSpelling = true, CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern uint GetFileAttributes(string path);

    [DllImport("kernel32.dll", EntryPoint = "DeleteFileW", ExactSpelling = true, CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool DeleteFile(string path);

    [DllImport("kernel32.dll", EntryPoint = "RemoveDirectoryW", ExactSpelling = true, CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool RemoveDirectory(string path);
}
