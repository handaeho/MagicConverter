using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Windows.Forms;

internal sealed class SetupWizard : WizardWindow
{
    private readonly Control[] pages;
    private readonly TextBox location;
    private readonly TextBox destinationPreview;
    private readonly TextBox confirmedLocation = ReadOnlyText("");
    private readonly CheckBox agreement;
    private readonly CheckBox launchApp = new CheckBox { Text = "MagicConverter 실행", Checked = true, AutoSize = true };
    private readonly string root;
    private readonly string version;
    private readonly string logPath;
    private readonly Func<string, Action<string>, string> install;
    private int page;

    internal string Destination { get; private set; }
    internal int ExitCode { get; private set; }
    internal bool LaunchAfterInstall { get; private set; }

    internal SetupWizard(string root, string version, string defaultParent, string license, string logPath,
                         Func<string, Action<string>, string> install)
        : base("MagicConverter " + version + " 설치 마법사")
    {
        this.root = root;
        this.version = version;
        this.logPath = logPath;
        this.install = install;
        ExitCode = 2;

        {
            Dock = DockStyle.Fill
        }, 0, 0);
        var links = new FlowLayoutPanel { Dock = DockStyle.Fill, AutoSize = true };
        links.Controls.AddRange(new Control[] { download, guide });
        var welcome = Welcome("MagicConverter " + version + "\r\n설치 마법사에 오신 것을 환영합니다",

        var terms = new TableLayoutPanel { ColumnCount = 1, RowCount = 2 };
        terms.RowStyles.Add(new RowStyle(SizeType.Percent, 100F));
        terms.RowStyles.Add(new RowStyle(SizeType.Absolute, 36F));
        var licenseText = ReadOnlyText(license.Replace("\r\n", "\n").Replace("\n", "\r\n"), true);
        licenseText.Name = "LicenseText";
        terms.Controls.Add(licenseText, 0, 0);
        agreement = new CheckBox { Name = "LicenseAgreement", Text = "라이선스 조건에 동의합니다.", AutoSize = true };
        agreement.CheckedChanged += (sender, args) => { if (page == 1) Next.Enabled = agreement.Checked; };
        terms.Controls.Add(agreement, 0, 1);

        location = new TextBox { Name = "InstallParent", Text = defaultParent, Dock = DockStyle.Fill };
        destinationPreview = ReadOnlyText("");
        destinationPreview.Name = "InstallDestination";
        location.TextChanged += (sender, args) => UpdateDestinationPreview();
        UpdateDestinationPreview();

        var review = new TableLayoutPanel { ColumnCount = 1, RowCount = 3 };
        review.RowStyles.Add(new RowStyle(SizeType.Absolute, 27F));
        review.RowStyles.Add(new RowStyle(SizeType.Absolute, 41F));
        review.Controls.Add(new Label { Text = "설치 위치", AutoSize = true }, 0, 0);
        review.Controls.Add(confirmedLocation, 0, 1);
        review.Controls.Add(new Label { Text = "설치를 시작하려면 설치를 누르세요. 위치를 바꾸려면 이전을 누르세요.", Dock = DockStyle.Fill }, 0, 2);

        pages = new[]
        {
            welcome,
            Step("라이선스 계약", "계속하려면 조건을 읽고 동의해 주세요.", terms),
            Step("설치 위치 선택", "선택한 상위 폴더에 magic-converter 폴더를 새로 만듭니다.", FolderFields()),
            Step("설치 준비 완료", "설치할 위치를 확인하세요.", review)
        };
        Next.Click += (sender, args) => Advance();
        Back.Click += (sender, args) => ShowPage(page - 1);
        ShowPage(0);
    }

    private Control FolderFields()
    {
        var fields = new TableLayoutPanel { ColumnCount = 2, RowCount = 6 };
        fields.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100F));
        fields.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 104F));
        foreach (float height in new[] { 24F, 93F, 25F, 34F, 25F })
            fields.RowStyles.Add(new RowStyle(SizeType.Absolute, height));

        var summary = new Label { Text = "설치 구성", AutoSize = true };
        fields.Controls.Add(summary, 0, 0);
        var contents = new ListBox { Dock = DockStyle.Fill, SelectionMode = SelectionMode.None, IntegralHeight = false };
        contents.Items.AddRange(new object[] { "MagicConverter", "앱 전용 Python 및 필수 라이브러리", "FFmpeg·FFprobe 및 rhwp" });
        fields.Controls.Add(contents, 0, 1);
        fields.SetColumnSpan(contents, 2);
        fields.Controls.Add(new Label { Text = "상위 폴더", AutoSize = true }, 0, 2);
        fields.Controls.Add(location, 0, 3);
        var browse = new Button { Text = "찾아보기…", Dock = DockStyle.Top, Height = 27, FlatStyle = FlatStyle.System };
        browse.Click += (sender, args) => Browse();
        fields.Controls.Add(browse, 1, 3);
        fields.Controls.Add(new Label { Text = "실제 설치 위치", AutoSize = true }, 0, 4);
        fields.Controls.Add(destinationPreview, 0, 5);
        fields.SetColumnSpan(destinationPreview, 2);
        return fields;
    }

    {
        var result = MessageBox.Show(this,
            "3. 한글의 비트 수에 맞는 DLL을 전용 폴더에 보관하세요.\n" +
            "4. 같은 Windows 계정에서 ZIP의 레지스트리 안내를 따라 아래 문자열 값을 등록하세요.\n" +
            "    값 데이터  DLL의 전체 경로\n" +
            MessageBoxIcon.Information, MessageBoxDefaultButton.Button2);
        if (result == DialogResult.Yes)
    }

    {
        try
        {
            using (Process.Start(new ProcessStartInfo(url) { UseShellExecute = true })) { }
        }
        catch (Exception error)
        {
            MessageBox.Show(this, "주소를 열지 못했습니다.\n\n" + url + "\n\n" + error.Message,
                            Text, MessageBoxButtons.OK, MessageBoxIcon.Warning);
        }
    }

    private void ShowPage(int index)
    {
        page = index;
        Display(pages[index], index > 0, index == 3 ? "설치" : "다음 >", index != 1 || agreement.Checked);
        if (index == 2)
            location.Focus();
    }

    private void Browse()
    {
        using (var picker = new FolderBrowserDialog
        {
            Description = "magic-converter 폴더를 만들 상위 폴더를 선택하세요.",
            ShowNewFolderButton = false
        })
        {
            if (Directory.Exists(location.Text))
                picker.SelectedPath = location.Text;
            if (picker.ShowDialog(this) == DialogResult.OK)
                location.Text = picker.SelectedPath;
        }
    }

    private void UpdateDestinationPreview()
    {
        var parent = location.Text.Trim();
        destinationPreview.Text = parent.Length == 0
            ? ""
            : parent.TrimEnd('\\') + "\\" + InstallLocation.FolderName;
    }

    private void Advance()
    {
        if (page >= 5)
        {
            LaunchAfterInstall = ExitCode == 0 && launchApp.Checked;
            Close();
        }
        else if (page == 0 || page == 1 && agreement.Checked)
        {
            ShowPage(page + 1);
        }
        else if (page == 2)
        {
            try
            {
                Destination = InstallLocation.ValidateNewFolder(InstallLocation.FolderUnder(location.Text), root, version);
                confirmedLocation.Text = Destination;
                ShowPage(3);
            }
            catch (Exception error)
            {
                MessageBox.Show(this, error.Message, Text, MessageBoxButtons.OK, MessageBoxIcon.Warning);
                location.Focus();
            }
        }
        else if (page == 3)
        {
            page = 4;
            string warning = null;
            RunWork("MagicConverter 설치 중", "설치 준비 중", report => warning = install(Destination, report), error =>
            {
                page = 5;
                ExitCode = error == null ? 0 : 1;
                if (error == null)
                    Display(Welcome("설치 완료", "MagicConverter 설치를 완료했습니다." +
                        (warning == null ? "" : "\r\n\r\n" + warning) + "\r\n\r\n마침을 눌러 마법사를 닫으세요.", launchApp),
                        false, "마침", cancel: false);
                else
                    Display(Step("설치를 완료하지 못했습니다", "오류 내용과 설치 로그 경로를 확인하세요.",
                        ReadOnlyText(error.Message + "\r\n\r\n설치 로그\r\n" + logPath, true)), false, "닫기", cancel: false);
            });
        }
    }
}
