using System;
using System.IO;
using System.Linq;
using System.Text;
using System.Text.RegularExpressions;

internal sealed class InstallLocation
{
    internal const string FolderName = "magic-converter";
    internal const string TokenFile = ".magicconverter-install-id";
    internal readonly string Folder;
    internal readonly string Token;

    internal InstallLocation(string folder, string token)
    {
        Folder = folder;
        Token = token;
    }

    internal static string Root(string local)
    {
        return Path.Combine(local, "Programs", "MagicConverter");
    }

    internal static string DefaultParent(string root, string version)
    {
        return Path.Combine(root, version);
    }

    internal static string DefaultFolder(string root, string version)
    {
        return Path.Combine(DefaultParent(root, version), FolderName);
    }

    internal static string LookupFolder(string root, string version, InstallLocation saved)
    {
        if (saved != null)
            return saved.Folder;
        var legacy = DefaultParent(root, version);
        return HasMarker(legacy, version) ? legacy : DefaultFolder(root, version);
    }

    internal static string FolderUnder(string parent)
    {
        return Path.Combine(parent.Trim(), FolderName);
    }

    private static string Record(string root, string version)
    {
        return Path.Combine(root, "install-location-" + version + ".txt");
    }

    internal static InstallLocation Read(string root, string version)
    {
        var path = Record(root, version);
        RequirePlainPath(path);
        if (!File.Exists(path))
            return null;
        if (new FileInfo(path).Length > 2048)
            throw new InvalidDataException("설치 위치 기록이 올바르지 않습니다.");
        var lines = File.ReadAllLines(path, Encoding.UTF8);
        Guid id;
        if (lines.Length != 2 || !Guid.TryParseExact(lines[0], "N", out id))
            throw new InvalidDataException("설치 위치 기록이 올바르지 않습니다.");
        return new InstallLocation(ValidatePath(lines[1], root, version), lines[0]);
    }

    internal static void Save(string root, string version, InstallLocation location)
    {
        var path = Record(root, version);
        RequirePlainPath(path);
        if (location == null)
        {
            if (File.Exists(path))
                File.Delete(path);
            return;
        }
        var temporary = Path.Combine(root, ".location-" + Guid.NewGuid().ToString("N"));
        try
        {
            File.WriteAllLines(temporary, new[] { location.Token, location.Folder }, Encoding.UTF8);
            if (File.Exists(path))
                File.Replace(temporary, path, null);
            else
                File.Move(temporary, path);
        }
        finally
        {
            if (File.Exists(temporary))
                File.Delete(temporary);
        }
    }

    internal static string ValidatePath(string value, string root, string version)
    {
        value = value.Trim();
        if (!Regex.IsMatch(value, @"\A[A-Za-z]:\\") || value.Substring(2).IndexOf(':') >= 0 ||
            value.IndexOfAny(new[] { '*', '?', '"', '<', '>', '|', '/' }) >= 0)
            throw new IOException("로컬 디스크의 전체 폴더 경로를 입력하세요. 예: D:\\Apps\\MagicConverter");
        var parts = value.Substring(3).TrimEnd('\\').Split('\\');
        if (parts.Any(part => part.Length == 0 || part.EndsWith(" ") || part.EndsWith(".") ||
                Regex.IsMatch(part, @"\A(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|\z)", RegexOptions.IgnoreCase)))
            throw new IOException("폴더 이름에 사용할 수 없는 부분이 있습니다.");
        var folder = Path.GetFullPath(value).TrimEnd('\\');
        if (folder.Length > 175 || new DriveInfo(Path.GetPathRoot(folder)).DriveType != DriveType.Fixed)
            throw new IOException("175자 이내의 로컬 고정 디스크 폴더를 지정하세요.");
        var inRoot = Below(folder, root);
        var defaultPath = Same(folder, DefaultParent(root, version)) || Same(folder, DefaultFolder(root, version));
        var profile = Path.Combine(Path.GetDirectoryName(Path.GetDirectoryName(root)), "magic_converter");
        if (Same(folder, root) || Below(root, folder) || inRoot && !defaultPath ||
            Same(folder, profile) || Below(folder, profile) || Below(profile, folder))
            throw new IOException("앱 데이터 폴더와 겹치지 않는 전용 설치 폴더를 지정하세요.");
        RequirePlainPath(folder);
        return folder;
    }

    internal static string ValidateNewFolder(string value, string root, string version)
    {
        var folder = ValidatePath(value, root, version);
        if (!Same(Path.GetFileName(folder), FolderName))
            throw new IOException("설치 폴더 이름은 " + FolderName + "이어야 합니다.");
        if (!Same(folder, DefaultFolder(root, version)) && !Directory.Exists(Path.GetDirectoryName(folder)))
            throw new IOException("이미 있는 상위 폴더를 선택하세요.");
        if (File.Exists(folder) || Directory.Exists(folder))
            throw new IOException("선택한 폴더에 이미 " + FolderName + " 이름이 있습니다. 다른 상위 폴더를 선택하세요.\n" + folder);
        return folder;
    }

    internal static bool HasMarker(string folder, string version)
    {
        return SmallFileEquals(Path.Combine(folder, ".installed"), version);
    }

    internal bool Matches(string version)
    {
        return HasMarker(Folder, version) && SmallFileEquals(Path.Combine(Folder, TokenFile), Token);
    }

    private static bool SmallFileEquals(string file, string text)
    {
        RequirePlainPath(file);
        return File.Exists(file) && new FileInfo(file).Length <= 80 &&
               String.Equals(File.ReadAllText(file).Trim(), text, StringComparison.Ordinal);
    }

    internal static void RequirePlainPath(string path)
    {
        for (var part = path; part != null; part = Path.GetDirectoryName(part))
        {
            try
            {
                if ((File.GetAttributes(part) & FileAttributes.ReparsePoint) != 0)
                    throw new IOException("다른 경로로 연결되는 파일이나 폴더는 사용할 수 없습니다.\n" + part);
            }
            catch (FileNotFoundException) { }
            catch (DirectoryNotFoundException) { }
        }
    }

    internal static bool Same(string first, string second)
    {
        return String.Equals(first, second, StringComparison.OrdinalIgnoreCase);
    }

    private static bool Below(string path, string parent)
    {
        return path.StartsWith(parent.TrimEnd('\\') + "\\", StringComparison.OrdinalIgnoreCase);
    }
}
