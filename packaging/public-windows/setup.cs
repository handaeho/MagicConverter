using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.IO.Compression;
using System.Net;
using System.Net.Http;
using System.Reflection;
using System.Security.Cryptography;
using System.Text;
using System.Threading.Tasks;
using System.Windows.Forms;

internal static class Setup
{
    private const string PythonUrl = "https://www.python.org/ftp/python/3.14.10/python-3.14.10-embed-amd64.zip";
    private const string PythonHash = "90905e64a840557d8c1bc4f5b74db3d7ee85e3ffb217611cac1c7813acea4159";
    private const string PipUrl = "https://files.pythonhosted.org/packages/07/25/ee8c38ae72d66b844ce8eb9711ba905dba21ca1a8a0c960057c41cd23337/pip-26.3-py3-none-any.whl";
    private const string PipHash = "f3b60b20ffaadd4415ed7957212a38ac63fa4c5fa375bd0f4a797777940fad13";
    private const string PipEntry = "import sys; sys.path.insert(0, sys.argv.pop(1)); from pip._internal.cli.main import main; raise SystemExit(main())";
    private const string PythonZip = "python-3.14.10-embed-amd64.zip";
    private const string PipWheel = "pip-26.3-py3-none-any.whl";
    private const string RuntimeLock = "magic_converter-runtime.txt";
    private const string OfflineFolder = "MagicConverter-offline-runtime";

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
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);
        var besideSetup = AppDomain.CurrentDomain.BaseDirectory;
        if (args.Length == 1 && args[0] == "--prepare-offline")
            return PrepareOfflineDialog(besideSetup);
        var root = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                                "Programs", "MagicConverter");
        var destination = Path.Combine(root, Version);
        try
        {
            if (!Ready(destination))
            {
                var bundle = Path.Combine(besideSetup, OfflineFolder);
                var offline = Directory.Exists(bundle) ? bundle : null;
                var source = offline == null
                    ? "Python.org와 PyPI에서 앱 전용 Python 3.14.10과 필수 라이브러리를 받습니다. 첫 설치에는 인터넷 연결이 필요합니다."
                    : "설치 EXE 옆의 오프라인 자료를 사용합니다. 인터넷 연결 없이 파일 해시를 확인하고 설치합니다.";
                var consent = MessageBox.Show(
                    "현재 Windows 계정에 MagicConverter를 설치할까요?\n\n" +
                    source + "\n\n설치 폴더\n" + destination,
                    Title, MessageBoxButtons.YesNo, MessageBoxIcon.Question, MessageBoxDefaultButton.Button2);
                if (consent != DialogResult.Yes)
                    return 2;
                ShowProgress(stage => Install(root, offline, stage));
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

    private static int PrepareOfflineDialog(string folder)
    {
        var log = Path.Combine(folder, "MagicConverter-offline-setup.log");
        var consent = MessageBox.Show(
            "이 설치 EXE 옆에 오프라인 설치 자료 폴더를 만들까요?\n\n" +
            "Python.org와 PyPI에서 필요한 파일을 받습니다. 만든 폴더는 인터넷이 없는 PC로 옮길 수 있습니다.",
            Title, MessageBoxButtons.YesNo, MessageBoxIcon.Question, MessageBoxDefaultButton.Button2);
        if (consent != DialogResult.Yes)
            return 2;
        try
        {
            ShowProgress(stage => PrepareOffline(folder, stage));
            MessageBox.Show("오프라인 설치 자료 준비 완료\n\n" + Path.Combine(folder, OfflineFolder), Title);
            return 0;
        }
        catch (Exception error)
        {
            MessageBox.Show(error.Message + "\n\n" + log, Title, MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
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
               File.Exists(Path.Combine(folder, "runtime", "python.exe")) &&
               File.Exists(Path.Combine(folder, "runtime", "python314._pth"));
    }

    internal static string Install(string root, string offline, Action<string> report)
    {
        var destination = Path.Combine(root, Version);
        Directory.CreateDirectory(root);
        using (var installLock = new FileStream(Path.Combine(root, ".setup.lock"), FileMode.OpenOrCreate,
                                                FileAccess.ReadWrite, FileShare.None))
        {
            if (Ready(destination))
                return destination;
            if (Directory.Exists(destination))
                throw new IOException("이 버전의 설치 폴더가 불완전합니다. 앱을 종료한 뒤 아래 폴더를 옮기고 설치를 다시 실행하세요.\n" + destination);

            var staging = Directory.CreateDirectory(Path.Combine(root, ".setup-" + Guid.NewGuid().ToString("N"))).FullName;
            var log = LogPath(root);
            var runtime = Path.Combine(staging, "runtime");
            string scratch = null;
            try
            {
                File.WriteAllText(log, Title + "\r\n", Encoding.UTF8);
                using (var payload = Assembly.GetExecutingAssembly().GetManifestResourceStream("magic-converter-setup.zip"))
                using (var archive = new ZipArchive(payload, ZipArchiveMode.Read))
                    archive.ExtractToDirectory(staging);

                FetchBootstrap(staging, offline, report);
                // Keep pip's target and its temporary extraction tree short. Some wheels
                // use relative paths with "..", which cannot be passed through \\?\ paths.
                var temp = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Temp");
                scratch = Directory.CreateDirectory(Path.Combine(temp, "mc-" + Guid.NewGuid().ToString("N").Substring(0, 12))).FullName;
                var shortRuntime = Path.Combine(scratch, "runtime");
                var python = UnpackPython(staging, shortRuntime);
                File.Delete(Path.Combine(staging, PythonZip));
                var pipWheel = Path.Combine(staging, PipWheel);
                var site = Path.Combine(shortRuntime, "Lib", "site-packages");
                report(offline == null ? "PyPI에서 필수 라이브러리 설치 중" : "오프라인 자료에서 필수 라이브러리 설치 중");
                InstallWheels(python, pipWheel, Path.Combine(staging, RuntimeLock), site, offline, log, scratch);
                report("설치된 런타임 검사 중");
                RunPython(python, log, scratch, "-c", PipEntry, pipWheel, "--isolated", "--disable-pip-version-check", "check");
                Directory.Move(shortRuntime, runtime);
                RunPython(Path.Combine(runtime, "python.exe"), log, scratch, "-c",
                    "import sys, struct; assert sys.version_info[:3] == (3, 14, 10) and struct.calcsize('P') == 8; " +
                    "import PySide6.QtWidgets, pymupdf, PIL.Image, pythoncom, magic_converter; print(magic_converter.__version__)");
                File.Delete(pipWheel);
                File.WriteAllText(Path.Combine(staging, ".installed"), Version, Encoding.ASCII);
                Directory.Move(staging, destination);
                return destination;
            }
            catch (Exception error)
            {
                File.AppendAllText(log, error.Message + Environment.NewLine, Encoding.UTF8);
                throw;
            }
            finally
            {
                if (scratch != null && Directory.Exists(runtime))
                    Directory.Move(runtime, Path.Combine(scratch, "runtime"));
                if (Directory.Exists(staging))
                    Directory.Delete(staging, true);
                if (scratch != null)
                    Directory.Delete(scratch, true);
            }
        }
    }

    internal static string PrepareOffline(string folder, Action<string> report)
    {
        var destination = Path.Combine(folder, OfflineFolder);
        if (Directory.Exists(destination))
            throw new IOException("기존 오프라인 자료 폴더를 다른 위치로 옮긴 뒤 다시 실행하세요.");
        var staging = Directory.CreateDirectory(Path.Combine(folder, ".offline-" + Guid.NewGuid().ToString("N"))).FullName;
        var log = Path.Combine(folder, "MagicConverter-offline-setup.log");
        try
        {
            File.WriteAllText(log, Title + "\r\n", Encoding.UTF8);
            var bundle = Directory.CreateDirectory(Path.Combine(staging, OfflineFolder)).FullName;
            FetchBootstrap(bundle, null, report);
            var python = UnpackPython(bundle, Path.Combine(staging, "runtime"));
            var lockFile = Path.Combine(staging, RuntimeLock);
            using (var payload = Assembly.GetExecutingAssembly().GetManifestResourceStream("magic-converter-setup.zip"))
            using (var archive = new ZipArchive(payload, ZipArchiveMode.Read))
                archive.GetEntry(RuntimeLock).ExtractToFile(lockFile);
            report("오프라인 설치에 필요한 휠 다운로드 중");
            RunPython(python, log, Path.GetTempPath(), "-c", PipEntry, Path.Combine(bundle, PipWheel), "--isolated",
                      "--disable-pip-version-check", "--no-input", "download", "--no-cache-dir",
                      "--index-url", "https://pypi.org/simple", "--require-hashes", "--only-binary=:all:",
                      "--dest", Path.Combine(bundle, "wheels"), "-r", lockFile);
            Directory.Move(bundle, destination);
            return destination;
        }
        finally
        {
            Directory.Delete(staging, true);
        }
    }

    private static void FetchBootstrap(string folder, string offline, Action<string> report)
    {
        var python = Path.Combine(folder, PythonZip);
        var pip = Path.Combine(folder, PipWheel);
        if (offline != null)
        {
            report("오프라인 Python과 설치 도구의 SHA-256 확인 중");
            File.Copy(Path.Combine(offline, PythonZip), python);
            File.Copy(Path.Combine(offline, PipWheel), pip);
            VerifyHash(python, PythonHash);
            VerifyHash(pip, PipHash);
            return;
        }
        ServicePointManager.SecurityProtocol = SecurityProtocolType.Tls12;
        using (var http = new HttpClient { Timeout = TimeSpan.FromMinutes(2) })
        {
            report("공식 Python 다운로드 및 SHA-256 확인 중");
            Download(http, PythonUrl, PythonHash, python);
            report("라이브러리 설치 도구 다운로드 및 SHA-256 확인 중");
            Download(http, PipUrl, PipHash, pip);
        }
    }

    private static string UnpackPython(string bundle, string runtime)
    {
        ZipFile.ExtractToDirectory(Path.Combine(bundle, PythonZip), runtime);
        File.WriteAllLines(Path.Combine(runtime, "python314._pth"),
                           new[] { "python314.zip", ".", @"..\app", @"Lib\site-packages", "import site" },
                           Encoding.ASCII);
        return Path.Combine(runtime, "python.exe");
    }

    private static void InstallWheels(string python, string pip, string lockFile, string site, string offline, string log, string temp)
    {
        var source = offline == null
            ? new[] { "--index-url", "https://pypi.org/simple" }
            : new[] { "--no-index", "--find-links", Path.Combine(offline, "wheels") };
        var args = new System.Collections.Generic.List<string> {
            "-c", PipEntry, pip, "--isolated", "--disable-pip-version-check", "--no-input", "install",
            "--no-cache-dir", "--require-hashes", "--only-binary=:all:", "--target", site, "-r", lockFile
        };
        args.AddRange(source);
        RunPython(python, log, temp, args.ToArray());
    }

    private static void Download(HttpClient client, string url, string digest, string destination)
    {
        File.WriteAllBytes(destination, client.GetByteArrayAsync(url).GetAwaiter().GetResult());
        VerifyHash(destination, digest);
    }

    private static void VerifyHash(string path, string digest)
    {
        using (var input = File.OpenRead(path))
        using (var sha = SHA256.Create())
        {
            var actual = BitConverter.ToString(sha.ComputeHash(input)).Replace("-", "").ToLowerInvariant();
            if (actual != digest)
                throw new InvalidDataException("설치 파일의 SHA-256이 일치하지 않습니다.\n" + Path.GetFileName(path));
        }
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
