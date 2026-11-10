using System;
using System.Diagnostics;
using System.IO;
using System.IO.Compression;
using System.Reflection;
using System.Security.Cryptography;
using System.Text;
using System.Windows.Forms;

internal static class Launcher
{
#if GUI
    private static readonly bool Gui = true;
#else
    private static readonly bool Gui = false;
#endif

    public static int Main(string[] args)
    {
        if (args.Length == 1 && args[0] == "--version")
        {
            Console.WriteLine(Assembly.GetExecutingAssembly().GetName().Version.ToString(3));
            return 0;
        }
        bool selfTest = args.Length > 0 && args[0] == "--self-test";
        try
        {
            var folder = AppDomain.CurrentDomain.BaseDirectory;
            var runtime = Environment.GetEnvironmentVariable("MAGIC_CONVERTER_PUBLIC_RUNTIME");
            if (String.IsNullOrEmpty(runtime))
                runtime = Path.Combine(folder, ".runtime");
            else if (!Path.IsPathRooted(runtime))
                throw new InvalidOperationException("런타임 경로는 절대 경로여야 합니다.");
            runtime = Path.GetFullPath(runtime);
            var python = Path.Combine(runtime, "Scripts", "python.exe");
            if (!File.Exists(python) || !File.Exists(Path.Combine(runtime, "pyvenv.cfg")))
                throw new InvalidOperationException("MagicConverter 런타임이 필요합니다.\nGitHub Releases의 Windows 설치 안내에 따라 .runtime을 준비하세요.");

            var source = ExtractSource(runtime);
            var arguments = new StringBuilder();
            if (Gui || selfTest)
                arguments.Append(Quote(Path.Combine(source, "run_magic_converter.py")));
            else
            {
                arguments.Append("-c ").Append(Quote("import sys; from magic_converter.cli import run; sys.exit(run(sys.argv[1:] or ['--help']))"));
            }
            foreach (var arg in args)
                arguments.Append(' ').Append(Quote(arg));

            var start = new ProcessStartInfo(python, arguments.ToString());
            start.UseShellExecute = false;
            start.CreateNoWindow = Gui;
            start.RedirectStandardError = Gui;
            start.EnvironmentVariables.Remove("PYTHONHOME");
            start.EnvironmentVariables.Remove("PYTHONEXECUTABLE");
            start.EnvironmentVariables.Remove("__PYVENV_LAUNCHER__");
            start.EnvironmentVariables["PYTHONPATH"] = source;
            start.EnvironmentVariables["PYTHONSAFEPATH"] = "1";
            start.EnvironmentVariables["PYTHONNOUSERSITE"] = "1";
            start.EnvironmentVariables["PYTHONUTF8"] = "1";
            start.EnvironmentVariables["PYTHONDONTWRITEBYTECODE"] = "1";
            start.EnvironmentVariables["MAGIC_CONVERTER_PUBLIC_GUI"] = Path.Combine(folder, "magic_converter.exe");
            if (!Gui)
                Console.CancelKeyPress += (sender, signal) => signal.Cancel = true;

            using (var child = Process.Start(start))
            {
                var error = Gui ? child.StandardError.ReadToEndAsync() : null;
                child.WaitForExit();
                if (Gui && child.ExitCode != 0)
                {
                    var message = error.GetAwaiter().GetResult();
                    if (message.Length > 1800)
                        message = message.Substring(message.Length - 1800);
                    if (selfTest)
                        Console.Error.WriteLine(message);
                    else
                        MessageBox.Show("실행에 실패했습니다. 런타임 설치 상태를 확인하세요.\n\n" + message, "MagicConverter");
                }
                return child.ExitCode;
            }
        }
        catch (Exception error)
        {
            if (Gui && !selfTest)
                MessageBox.Show(error.Message, "MagicConverter");
            else
                Console.Error.WriteLine(error.Message);
            return 1;
        }
    }

    private static string ExtractSource(string runtime)
    {
        using (var stream = Assembly.GetExecutingAssembly().GetManifestResourceStream("magic-converter.zip"))
        using (var sha = SHA256.Create())
        {
            var name = BitConverter.ToString(sha.ComputeHash(stream)).Replace("-", "").ToLowerInvariant();
            var parent = Directory.CreateDirectory(Path.Combine(runtime, "magic-converter-app")).FullName;
            var final = Path.Combine(parent, name);
            if (Directory.Exists(final))
                return final;
            var staging = Directory.CreateDirectory(Path.Combine(parent, Guid.NewGuid().ToString("N"))).FullName;
            try
            {
                stream.Position = 0;
                using (var archive = new ZipArchive(stream, ZipArchiveMode.Read))
                    archive.ExtractToDirectory(staging);
                try { Directory.Move(staging, final); }
                catch (IOException) { if (!Directory.Exists(final)) throw; }
                return final;
            }
            finally
            {
                if (Directory.Exists(staging))
                    Directory.Delete(staging, true);
            }
        }
    }

    // ProcessStartInfo uses a command line string on .NET Framework.
    private static string Quote(string value)
    {
        var output = new StringBuilder("\"");
        int slashes = 0;
        foreach (char character in value)
        {
            if (character == '\\')
            {
                slashes++;
                continue;
            }
            output.Append('\\', character == '"' ? slashes * 2 + 1 : slashes);
            output.Append(character);
            slashes = 0;
        }
        return output.Append('\\', slashes * 2).Append('"').ToString();
    }
}
