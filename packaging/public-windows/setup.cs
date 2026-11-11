using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.IO.Compression;
using System.Reflection;
using System.Security.Cryptography;
using System.Text;
using System.Threading.Tasks;
using System.Windows.Forms;

internal static class Setup
{
    private const string PipEntry = "import sys; sys.path.insert(0, sys.argv.pop(1)); from pip._internal.cli.main import main; raise SystemExit(main())";
    private const string RuntimeLock = "magic_converter-runtime.txt";

    private static readonly string Version = Assembly.GetExecutingAssembly().GetName().Version.ToString(3);
    private static string Title { get { return "MagicConverter " + Version + " 설치"; } }

    [STAThread]
    public static int Main(string[] args)
    {
        if (args.Length == 1 && args[0] == "--version")
        {
            Console.WriteLine(Version);
            return 0;
        }
        if (args.Length != 0)
        {
            Console.Error.WriteLine("지원하지 않는 설치 옵션입니다.");
            return 2;
        }
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);
        var local = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        if (String.IsNullOrEmpty(local))
            return 1;
        var root = InstallLocation.Root(local);
        try
        {
            var saved = InstallLocation.Read(root, Version);
            var destination = FindExisting(root, saved);
            if (destination == null)
            {
                var source = "설치 EXE에 포함된 Python, 필수 라이브러리, FFmpeg·FFprobe와 rhwp를 설치합니다.";
                var defaultParent = InstallLocation.DefaultParent(root, Version);
                var parent = saved == null || InstallLocation.Same(saved.Folder, defaultParent)
                    ? defaultParent : Path.GetDirectoryName(saved.Folder);
                using (var wizard = new SetupWizard(root, Version, parent, source, ReadLicense()))
                {
                    if (wizard.ShowDialog() != DialogResult.OK)
                        return 2;
                    destination = wizard.Destination;
                }
                ShowProgress(stage => InstallAt(root, destination, stage));
            }
            try
            {
                CreateShortcut(destination, Environment.GetFolderPath(Environment.SpecialFolder.Programs));
            }
            catch (Exception error)
            {
                MessageBox.Show("시작 메뉴 바로가기를 만들지 못했습니다.\n" + destination + "\n\n" + error.Message, Title);
            }
            using (Launch(destination)) { }
            return 0;
        }
        catch (Exception error)
        {
            MessageBox.Show(error.Message + "\n\n설치 로그\n" + LogPath(root), Title,
                            MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
    }

    private static string ReadLicense()
    {
        using (var payload = Assembly.GetExecutingAssembly().GetManifestResourceStream("magic-converter-setup.zip"))
        using (var archive = new ZipArchive(payload, ZipArchiveMode.Read))
        {
            var text = new StringBuilder();
            foreach (var name in new[] { "app/packaging/public-windows/THIRD_PARTY_NOTICES.txt",
                                         "app/LICENSE", "app/licenses/Microsoft-VC-Runtime-2026.txt" })
                using (var reader = new StreamReader(archive.GetEntry(name).Open(), Encoding.UTF8))
                    text.AppendLine(reader.ReadToEnd()).AppendLine();
            return text.ToString();
        }
    }

    private static void ShowProgress(Action<Action<string>> operation)
    {
        Exception failure = null;
        using (var window = new Form
        {
            Text = Title, StartPosition = FormStartPosition.CenterScreen, ClientSize = new Size(470, 96),
            FormBorderStyle = FormBorderStyle.FixedDialog, ControlBox = false
        })
        using (var label = new Label { Text = "설치 준비 중", Location = new Point(18, 14), Size = new Size(434, 27) })
        using (var progress = new ProgressBar { Style = ProgressBarStyle.Marquee, Location = new Point(18, 51), Size = new Size(434, 20) })
        {
            window.Controls.Add(label);
            window.Controls.Add(progress);
            window.Shown += async (sender, args) =>
            {
                try
                {
                    await Task.Run(() => operation(stage => window.BeginInvoke((Action)(() => label.Text = stage))));
                }
                catch (Exception error) { failure = error; }
                finally { window.Close(); }
            };
            window.ShowDialog();
        }
        if (failure != null)
            throw failure;
    }

    internal static bool Ready(string folder)
    {
        return File.Exists(Path.Combine(folder, ".installed")) &&
               File.Exists(Path.Combine(folder, "magic_converter.exe")) &&
               File.Exists(Path.Combine(folder, "magic_converter-cli.exe")) &&
               File.Exists(Path.Combine(folder, "app", "run_magic_converter.py")) &&
               File.Exists(Path.Combine(folder, "app", "vendor", "ffmpeg", "ffmpeg.exe")) &&
               File.Exists(Path.Combine(folder, "app", "vendor", "ffmpeg", "ffprobe.exe")) &&
               File.Exists(Path.Combine(folder, "app", "vendor", "rhwp", "rhwp.exe")) &&
               File.Exists(Path.Combine(folder, "runtime", "python.exe")) &&
               File.Exists(Path.Combine(folder, "runtime", "python314._pth"));
    }

    private static string FindExisting(string root, InstallLocation saved)
    {
        var folder = InstallLocation.LookupFolder(root, Version, saved);
        InstallLocation.RequirePlainPath(folder);
        if (Ready(folder) && (saved == null ? InstallLocation.HasMarker(folder, Version) : saved.Matches(Version)))
            return folder;
        if (saved != null && (File.Exists(folder) || Directory.Exists(folder) && Directory.GetFileSystemEntries(folder).Length != 0))
            throw new IOException("기록된 설치 폴더를 확인할 수 없습니다.\n" + folder);
        return null;
    }

    internal static string InstallAt(string root, string destination, Action<string> report)
    {
        AppContext.SetSwitch("Switch.System.IO.UseLegacyPathHandling", false);
        AppContext.SetSwitch("Switch.System.IO.BlockLongPaths", false);
        destination = InstallLocation.ValidatePath(destination, root, Version);
        var lockPath = Path.Combine(root, ".setup.lock");
        InstallLocation.RequirePlainPath(lockPath);
        Directory.CreateDirectory(root);
        using (var installLock = new FileStream(lockPath, FileMode.OpenOrCreate,
                                                FileAccess.ReadWrite, FileShare.None))
        {
            var previous = InstallLocation.Read(root, Version);
            var existing = FindExisting(root, previous);
            if (existing != null)
            {
                if (!InstallLocation.Same(existing, destination))
                    throw new IOException("이 버전이 이미 다른 폴더에 설치되어 있습니다.\n" + existing);
                return existing;
            }
            destination = InstallLocation.ValidateNewFolder(destination, root, Version);
            var log = LogPath(root);
            InstallLocation.RequirePlainPath(log);
            var installParent = Path.GetDirectoryName(destination);
            var stagingParent = Directory.Exists(installParent) ? installParent : root;
            var staging = Directory.CreateDirectory(Path.Combine(stagingParent,
                ".mc-" + Guid.NewGuid().ToString("N").Substring(0, 12))).FullName;
            var runtime = Path.Combine(staging, "runtime");
            string scratch = null;
            string python = null;
            try
            {
                File.WriteAllText(log, Title + "\r\n", Encoding.UTF8);
                using (var payload = Assembly.GetExecutingAssembly().GetManifestResourceStream("magic-converter-setup.zip"))
                using (var archive = new ZipArchive(payload, ZipArchiveMode.Read))
                    archive.ExtractToDirectory(staging);
                report("내장 변환 엔진 확인 중");
                ExtractVerified("magic-converter-engines.zip", RuntimeInfo.EnginesSha256, Path.Combine(staging, "app"));

                // Keep pip's target and its temporary extraction tree short. Some wheels
                // use relative paths with "..", which cannot be passed through \\?\ paths.
                var temp = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Temp");
                if (stagingParent.Length < temp.Length)
                    temp = stagingParent;
                scratch = Directory.CreateDirectory(Path.Combine(temp, "mc-" + Guid.NewGuid().ToString("N").Substring(0, 12))).FullName;
                var bundle = Path.Combine(scratch, "bundle");
                report("내장 런타임 파일 확인 중");
                ExtractVerified("magic-converter-runtime.zip", RuntimeInfo.ArchiveSha256, bundle);
                var shortRuntime = Path.Combine(scratch, "runtime");
                python = UnpackPython(bundle, shortRuntime);
                foreach (var file in Directory.GetFiles(Path.Combine(staging, "app", "vendor", "runtime"), "*.dll"))
                    File.Copy(file, Path.Combine(shortRuntime, Path.GetFileName(file)), true);
                var pipWheel = Path.Combine(bundle, RuntimeInfo.PipWheel);
                var site = Path.Combine(shortRuntime, "Lib", "site-packages");
                report("내장 필수 라이브러리 설치 중");
                InstallWheels(python, pipWheel, Path.Combine(staging, RuntimeLock), site, Path.Combine(bundle, "wheels"), log, scratch);
                report("설치된 런타임 검사 중");
                RunPython(python, log, scratch, "-c", PipEntry, pipWheel, "--isolated", "--disable-pip-version-check", "check");
                report("선택한 설치 폴더에 런타임 복사 중");
                RunPython(python, log, scratch, "-c", "import shutil, sys; shutil.copytree(*sys.argv[1:])",
                          @"\\?\" + shortRuntime, @"\\?\" + runtime);
                RunPython(Path.Combine(runtime, "python.exe"), log, scratch, "-c",
                    "import sys, struct; assert '.'.join(map(str, sys.version_info[:3])) == '" + RuntimeInfo.PythonVersion +
                    "' and struct.calcsize('P') == 8; " +
                    "import PySide6.QtWidgets, pymupdf, PIL.Image, pythoncom, magic_converter; print(magic_converter.__version__)");
                var receipt = new InstallLocation(destination, Guid.NewGuid().ToString("N"));
                File.WriteAllText(Path.Combine(staging, ".installed"), Version, Encoding.ASCII);
                File.WriteAllText(Path.Combine(staging, InstallLocation.TokenFile), receipt.Token, Encoding.ASCII);
                InstallLocation.ValidateNewFolder(destination, root, Version);
                InstallLocation.Save(root, Version, receipt);
                try
                {
                    if (InstallLocation.Same(destination, InstallLocation.DefaultFolder(root, Version)))
                        Directory.CreateDirectory(installParent);
                    Directory.Move(staging, destination);
                }
                catch
                {
                    InstallLocation.Save(root, Version, previous);
                    throw;
                }
                return destination;
            }
            catch (Exception error)
            {
                File.AppendAllText(log, error.Message + Environment.NewLine, Encoding.UTF8);
                throw;
            }
            finally
            {
                if (python != null && Directory.Exists(runtime))
                    RunPython(python, log, scratch, "-c", "import shutil, sys; shutil.rmtree(sys.argv[1])",
                              @"\\?\" + runtime);
                if (Directory.Exists(staging))
                    Directory.Delete(staging, true);
                if (scratch != null)
                    Directory.Delete(scratch, true);
            }
        }
    }

    private static void ExtractVerified(string resource, string expectedHash, string folder)
    {
        using (var payload = Assembly.GetExecutingAssembly().GetManifestResourceStream(resource))
        using (var sha = SHA256.Create())
        {
            if (payload == null)
                throw new InvalidDataException("설치 EXE에 필요한 내장 파일이 없습니다.\n" + resource);
            var actual = BitConverter.ToString(sha.ComputeHash(payload)).Replace("-", "").ToLowerInvariant();
            if (actual != expectedHash)
                throw new InvalidDataException("내장 파일의 SHA-256이 일치하지 않습니다.\n" + resource);
            payload.Position = 0;
            using (var archive = new ZipArchive(payload, ZipArchiveMode.Read))
                archive.ExtractToDirectory(folder);
        }
    }

    private static string UnpackPython(string bundle, string runtime)
    {
        ZipFile.ExtractToDirectory(Path.Combine(bundle, RuntimeInfo.PythonZip), runtime);
        File.WriteAllLines(Path.Combine(runtime, "python314._pth"),
                           new[] { "python314.zip", ".", @"..\app", @"Lib\site-packages", "import site" },
                           Encoding.ASCII);
        return Path.Combine(runtime, "python.exe");
    }

    private static void InstallWheels(string python, string pip, string lockFile, string site, string wheels, string log, string temp)
    {
        RunPython(python, log, temp, "-c", PipEntry, pip, "--isolated", "--disable-pip-version-check",
                  "--no-input", "install", "--no-index", "--find-links", wheels, "--no-cache-dir",
                  "--require-hashes", "--only-binary=:all:", "--target", site, "-r", lockFile);
    }

    private static void RunPython(string python, string log, string temp, params string[] args)
    {
        var arguments = "-X utf8 -B " + String.Join(" ", Array.ConvertAll(args, WindowsArguments.Quote));
        var start = new ProcessStartInfo(python, arguments)
        {
            UseShellExecute = false, CreateNoWindow = true,
            RedirectStandardOutput = true, RedirectStandardError = true,
            StandardOutputEncoding = Encoding.UTF8, StandardErrorEncoding = Encoding.UTF8
        };
        start.EnvironmentVariables["PIP_CONFIG_FILE"] = "NUL";
        start.EnvironmentVariables["TMP"] = temp;
        start.EnvironmentVariables["TEMP"] = temp;
        start.EnvironmentVariables.Remove("__PYVENV_LAUNCHER__");
        using (var child = Process.Start(start))
        {
            var output = child.StandardOutput.ReadToEndAsync();
            var error = child.StandardError.ReadToEndAsync();
            if (!child.WaitForExit(15 * 60 * 1000))
            {
                child.Kill();
                child.WaitForExit();
                throw new TimeoutException("런타임 설치 시간이 초과되었습니다.");
            }
            File.AppendAllText(log, output.GetAwaiter().GetResult() + error.GetAwaiter().GetResult(), Encoding.UTF8);
            if (child.ExitCode != 0)
                throw new InvalidOperationException("필수 라이브러리를 설치하거나 검사하는 중 오류가 발생했습니다.");
        }
    }

    internal static void CreateShortcut(string destination, string programs)
    {
        Directory.CreateDirectory(programs);
        dynamic shell = Activator.CreateInstance(Type.GetTypeFromProgID("WScript.Shell", true));
        dynamic shortcut = shell.CreateShortcut(Path.Combine(programs, "MagicConverter.lnk"));
        shortcut.TargetPath = Path.Combine(destination, "magic_converter.exe");
        shortcut.WorkingDirectory = destination;
        shortcut.Description = "MagicConverter " + Version;
        shortcut.Save();
    }

    internal static Process Launch(string destination)
    {
        return Process.Start(new ProcessStartInfo(Path.Combine(destination, "magic_converter.exe"))
        {
            UseShellExecute = false, WorkingDirectory = destination
        });
    }

    private static string LogPath(string root) { return Path.Combine(root, "setup-" + Version + ".log"); }
}
