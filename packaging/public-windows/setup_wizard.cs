using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Windows.Forms;

internal sealed class SetupWizard : Form
{
    private readonly Control[] pages;
    private readonly Button back;
    private readonly Button next;
    private readonly TextBox location;
    private readonly CheckBox agreement;
    private readonly string root;
    private readonly string version;
    private int page;

    internal string Destination { get; private set; }

    internal SetupWizard(string root, string version, string defaultPath, string source, string license)
    {
        this.root = root;
        this.version = version;
        Text = "MagicConverter " + version + " 설치";
        StartPosition = FormStartPosition.CenterScreen;
        ClientSize = new Size(650, 416);
        MinimumSize = new Size(590, 400);

        var layout = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 1, RowCount = 2, Padding = new Padding(16) };
        layout.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
        layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        var body = new Panel { Dock = DockStyle.Fill };
        layout.Controls.Add(body, 0, 0);

        var welcome = Page("1. 설치 파일 및 설치 안내");
        welcome.Controls.Add(new Label
        {
            Text = "MagicConverter-Setup.exe · " + version + " · Windows 64비트\r\n\r\n" + source +
            Dock = DockStyle.Fill
        }, 0, 1);

        var terms = Page("2. 프로젝트 라이선스");
        var licenseText = new TextBox
        {
            Name = "LicenseText", Dock = DockStyle.Fill, Multiline = true, ReadOnly = true,
            ScrollBars = ScrollBars.Vertical, Text = license.Replace("\r\n", "\n").Replace("\n", "\r\n")
        };
        terms.Controls.Add(licenseText, 0, 1);
        agreement = new CheckBox { Name = "LicenseAgreement", Text = "라이선스 내용을 읽었으며 동의합니다.", AutoSize = true };
        agreement.CheckedChanged += (sender, args) => UpdateButtons();
        terms.Controls.Add(agreement, 0, 2);

        var folder = Page("3. 설치 위치 지정");
        var folderFields = new TableLayoutPanel { Dock = DockStyle.Top, ColumnCount = 2, AutoSize = true };
        folderFields.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
        folderFields.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
        folderFields.Controls.Add(new Label
        {
            Text = "MagicConverter를 설치할 전용 폴더를 입력하세요.\r\n새 폴더나 비어 있는 폴더를 사용할 수 있습니다.",
            AutoSize = true
        }, 0, 0);
        folderFields.SetColumnSpan(folderFields.GetControlFromPosition(0, 0), 2);
        location = new TextBox { Name = "InstallFolder", Text = defaultPath, Dock = DockStyle.Fill };
        var browse = new Button { Text = "찾아보기", AutoSize = true };
        browse.Click += (sender, args) => Browse();
        folderFields.Controls.Add(location, 0, 1);
        folderFields.Controls.Add(browse, 1, 1);
        folder.Controls.Add(folderFields, 0, 1);

        pages = new Control[] { welcome, terms, folder };
        foreach (var item in pages)
            body.Controls.Add(item);
        var actions = new FlowLayoutPanel { Dock = DockStyle.Fill, FlowDirection = FlowDirection.RightToLeft, AutoSize = true };
        var cancel = new Button { Text = "취소", DialogResult = DialogResult.Cancel, AutoSize = true };
        next = new Button { Name = "NextButton", AutoSize = true };
        back = new Button { Name = "BackButton", Text = "이전", AutoSize = true };
        next.Click += (sender, args) => Advance();
        back.Click += (sender, args) => ShowPage(page - 1);
        actions.Controls.AddRange(new Control[] { cancel, next, back });
        layout.Controls.Add(actions, 0, 1);
        Controls.Add(layout);
        AcceptButton = next;
        CancelButton = cancel;
        ShowPage(0);
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

    private static TableLayoutPanel Page(string title)
    {
        var result = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 1, RowCount = 3 };
        result.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        result.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
        result.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        result.Controls.Add(new Label { Text = title, AutoSize = true, Margin = new Padding(0, 0, 0, 12) }, 0, 0);
        return result;
    }

    private void ShowPage(int index)
    {
        page = index;
        for (int i = 0; i < pages.Length; i++)
            pages[i].Visible = i == page;
        UpdateButtons();
        if (page == 2)
            location.Focus();
    }

    private void UpdateButtons()
    {
        if (next == null)
            return;
        back.Enabled = page > 0;
        next.Text = page == 2 ? "설치" : "다음";
        next.Enabled = page != 1 || agreement.Checked;
    }

    private void Browse()
    {
        using (var picker = new FolderBrowserDialog
        {
            Description = "MagicConverter 전용 설치 폴더를 선택하세요.",
            ShowNewFolderButton = false
        })
        {
            if (Directory.Exists(location.Text))
                picker.SelectedPath = location.Text;
            if (picker.ShowDialog(this) == DialogResult.OK)
                location.Text = picker.SelectedPath;
        }
    }

    private void Advance()
    {
        if (page == 1 && !agreement.Checked)
            return;
        if (page < 2)
        {
            ShowPage(page + 1);
            return;
        }
        try
        {
            var folder = InstallLocation.ValidateEmptyFolder(location.Text, root, version);
            if (MessageBox.Show(this, "다음 폴더에 설치합니다.\n\n" + folder + "\n\n설치를 시작할까요?", Text,
                                MessageBoxButtons.YesNo, MessageBoxIcon.Question,
                                MessageBoxDefaultButton.Button2) != DialogResult.Yes)
                return;
            Destination = folder;
            DialogResult = DialogResult.OK;
            Close();
        }
        catch (Exception error)
        {
            MessageBox.Show(this, error.Message, Text, MessageBoxButtons.OK, MessageBoxIcon.Warning);
            location.Focus();
        }
    }
}
