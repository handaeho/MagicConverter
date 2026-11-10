using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
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
            var python = Path.Combine(folder, "runtime", "python.exe");
            var source = Path.Combine(folder, "app", "run_magic_converter.py");
            if (!File.Exists(python) || !File.Exists(source))
                throw new InvalidOperationException("MagicConverter-Setup.exe로 앱과 런타임을 먼저 설치하세요.");

            var arguments = new StringBuilder("-X utf8 -B ");
            if (Gui || selfTest)
                arguments.Append(WindowsArguments.Quote(source));
            else
            {
                arguments.Append("-c ").Append(WindowsArguments.Quote("import sys; from magic_converter.cli import run; sys.exit(run(sys.argv[1:] or ['--help']))"));
            }
            foreach (var arg in args)
                arguments.Append(' ').Append(WindowsArguments.Quote(arg));

            var start = new ProcessStartInfo(python, arguments.ToString());
            start.UseShellExecute = false;
            start.CreateNoWindow = Gui;
            start.RedirectStandardError = Gui;
            start.EnvironmentVariables.Remove("__PYVENV_LAUNCHER__");
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

}
