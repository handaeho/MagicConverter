using System;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Reflection;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;

internal abstract class WizardWindow : Form
{
    private readonly Panel host = new Panel { Dock = DockStyle.Fill };
    private readonly Icon brandIcon;
    private readonly Font heading = new Font("Malgun Gothic", 15F);
    private readonly Font titleFont = new Font("Malgun Gothic", 10F, FontStyle.Bold);
    private readonly Font bodyFont = new Font("Malgun Gothic", 9F);
    private bool working;

    protected readonly Button Back = WizardButton("BackButton", "< 이전");
    protected readonly Button Next = WizardButton("NextButton", "다음 >");
    protected readonly Button Cancel = WizardButton("CancelButton", "취소");

    protected WizardWindow(string title)
    {
        Text = title;
        Font = bodyFont;
        AutoScaleDimensions = new SizeF(96F, 96F);
        AutoScaleMode = AutoScaleMode.Dpi;
        ClientSize = new Size(640, 450);
        StartPosition = FormStartPosition.CenterScreen;
        FormBorderStyle = FormBorderStyle.FixedDialog;
        MaximizeBox = false;
        MinimizeBox = false;
        using (var stream = Assembly.GetExecutingAssembly().GetManifestResourceStream("magic-converter-wizard.ico"))
            brandIcon = new Icon(stream, 64, 64);
        Icon = brandIcon;

        var footer = new FlowLayoutPanel
        {
            Dock = DockStyle.Fill, FlowDirection = FlowDirection.RightToLeft, WrapContents = false,
            Padding = new Padding(14, 10, 12, 8)
        };
        footer.Controls.AddRange(new Control[] { Cancel, Next, Back });
        Back.TabIndex = 0;
        Next.TabIndex = 1;
        Cancel.TabIndex = 2;
        Cancel.DialogResult = DialogResult.Cancel;
        AcceptButton = Next;
        CancelButton = Cancel;

        var layout = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 1, RowCount = 3, Margin = Padding.Empty };
        layout.RowStyles.Add(new RowStyle(SizeType.Percent, 100F));
        layout.RowStyles.Add(new RowStyle(SizeType.Absolute, 1F));
        layout.RowStyles.Add(new RowStyle(SizeType.Absolute, 54F));
        layout.Controls.Add(host, 0, 0);
        layout.Controls.Add(new Panel { Dock = DockStyle.Fill, Margin = Padding.Empty, BackColor = SystemColors.ControlDark }, 0, 1);
        layout.Controls.Add(footer, 0, 2);
        host.Margin = footer.Margin = Padding.Empty;
        Controls.Add(layout);
    }

    private static Button WizardButton(string name, string text)
    {
        return new Button { Name = name, Text = text, Size = new Size(90, 28), FlatStyle = FlatStyle.System };
    }

    protected void Display(Control view, bool back, string next, bool nextEnabled = true, bool cancel = true)
    {
        host.SuspendLayout();
        if (!host.Controls.Contains(view))
        {
            view.Dock = DockStyle.Fill;
            host.Controls.Add(view);
        }
        foreach (Control child in host.Controls)
            child.Visible = child == view;
        view.BringToFront();
        host.ResumeLayout();
        Back.Enabled = back;
        Next.Text = next;
        Next.Enabled = nextEnabled;
        Cancel.Enabled = cancel;
        if (nextEnabled)
            Next.Focus();
    }

    protected Control Welcome(string title, string description, Control extra = null)
    {
        var layout = new TableLayoutPanel { Dock = DockStyle.Fill, BackColor = SystemColors.Window, ColumnCount = 2, RowCount = 1 };
        layout.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 202F));
        layout.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100F));
        var banner = new Panel { Dock = DockStyle.Fill, Margin = Padding.Empty };
        banner.Paint += (sender, args) => PaintBanner(banner, args.Graphics);
        layout.Controls.Add(banner, 0, 0);
        var copy = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 1, RowCount = 3, Padding = new Padding(18, 29, 20, 12) };
        copy.RowStyles.Add(new RowStyle(SizeType.Absolute, 84F));
        copy.RowStyles.Add(new RowStyle(SizeType.Percent, 100F));
        copy.RowStyles.Add(new RowStyle(SizeType.Absolute, extra == null ? 0F : 102F));
        copy.Controls.Add(new Label { Text = title, Font = heading, Dock = DockStyle.Fill }, 0, 0);
        copy.Controls.Add(new Label { Text = description, Dock = DockStyle.Fill }, 0, 1);
        if (extra != null)
            copy.Controls.Add(extra, 0, 2);
        layout.Controls.Add(copy, 1, 0);
        return layout;
    }

    private void PaintBanner(Control banner, Graphics graphics)
    {
        var bounds = banner.ClientRectangle;
        if (bounds.Width == 0 || bounds.Height == 0)
            return;
        graphics.SmoothingMode = SmoothingMode.AntiAlias;
        using (var gradient = new LinearGradientBrush(bounds, Color.FromArgb(31, 24, 72), Color.FromArgb(89, 45, 147), 56F))
        using (var shape = new SolidBrush(Color.FromArgb(36, 208, 177, 255)))
        using (var ring = new Pen(Color.FromArgb(49, 217, 194, 255), 16F))
        {
            graphics.FillRectangle(gradient, bounds);
            graphics.FillEllipse(shape, -bounds.Width, bounds.Height / 4, bounds.Width * 2, bounds.Height);
            graphics.DrawEllipse(ring, -bounds.Width / 2, bounds.Height / 2, bounds.Width * 2, bounds.Width * 2);
            int size = bounds.Width * 2 / 5;
            graphics.DrawIcon(brandIcon, new Rectangle(bounds.Width - size - bounds.Width / 9, bounds.Width / 9, size, size));
        }
    }

    protected Control Step(string title, string subtitle, Control content)
    {
        var layout = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 1, RowCount = 3 };
        layout.RowStyles.Add(new RowStyle(SizeType.Absolute, 82F));
        layout.RowStyles.Add(new RowStyle(SizeType.Absolute, 1F));
        layout.RowStyles.Add(new RowStyle(SizeType.Percent, 100F));
        var header = new TableLayoutPanel
        {
            Dock = DockStyle.Fill, ColumnCount = 2, RowCount = 2, BackColor = SystemColors.Window,
            Margin = Padding.Empty, Padding = new Padding(21, 11, 17, 8)
        };
        header.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100F));
        header.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 53F));
        header.RowStyles.Add(new RowStyle(SizeType.Absolute, 24F));
        header.RowStyles.Add(new RowStyle(SizeType.Percent, 100F));
        header.Controls.Add(new Label { Text = title, Dock = DockStyle.Fill, Font = titleFont }, 0, 0);
        header.Controls.Add(new Label { Text = subtitle, Dock = DockStyle.Fill, Margin = new Padding(16, 0, 0, 0) }, 0, 1);
        var mark = new Panel { Dock = DockStyle.Fill };
        mark.Paint += (sender, args) =>
        {
            int size = Math.Min(mark.Width, mark.Height) - 4;
            if (size > 0)
                args.Graphics.DrawIcon(brandIcon, new Rectangle((mark.Width - size) / 2, (mark.Height - size) / 2, size, size));
        };
        header.Controls.Add(mark, 1, 0);
        header.SetRowSpan(mark, 2);
        layout.Controls.Add(header, 0, 0);
        layout.Controls.Add(new Panel { Dock = DockStyle.Fill, Margin = Padding.Empty, BackColor = SystemColors.ControlDark }, 0, 1);
        var body = new Panel { Dock = DockStyle.Fill, Margin = Padding.Empty, Padding = new Padding(34, 20, 34, 16) };
        content.Dock = DockStyle.Fill;
        body.Controls.Add(content);
        layout.Controls.Add(body, 0, 2);
        return layout;
    }

    protected static TextBox ReadOnlyText(string text, bool multiline = false)
    {
        return new TextBox
        {
            Text = text, Dock = DockStyle.Fill, ReadOnly = true, Multiline = multiline,
            ScrollBars = multiline ? ScrollBars.Vertical : ScrollBars.None
        };
    }

    protected async void RunWork(string title, string initial, Action<Action<string>> operation, Action<Exception> finished)
    {
        if (working)
            return;
        var content = new TableLayoutPanel { ColumnCount = 1, RowCount = 3 };
        content.RowStyles.Add(new RowStyle(SizeType.Absolute, 36F));
        content.RowStyles.Add(new RowStyle(SizeType.Absolute, 26F));
        var status = new Label { Text = initial, Dock = DockStyle.Fill, Name = "ProgressStatus" };
        var bar = new ProgressBar { Dock = DockStyle.Fill, Style = ProgressBarStyle.Marquee };
        content.Controls.Add(status, 0, 0);
        content.Controls.Add(bar, 0, 1);
        Display(Step(title, "작업이 끝날 때까지 기다려 주세요.", content), false, "다음 >", false, false);
        working = true;
        Exception failure = null;
        try
        {
            // Both operations can use Windows Shell COM for the Start menu shortcut.
            var completion = new TaskCompletionSource<object>();
            var worker = new Thread(() =>
            {
                try
                {
                    operation(message => BeginInvoke((Action)(() => status.Text = message)));
                    completion.SetResult(null);
                }
                catch (Exception error)
                {
                    completion.SetException(error);
                }
            }) { IsBackground = true };
            worker.SetApartmentState(ApartmentState.STA);
            worker.Start();
            await completion.Task;
        }
        catch (Exception error)
        {
            failure = error;
        }
        finally
        {
            working = false;
            bar.Style = ProgressBarStyle.Blocks;
        }
        finished(failure);
    }

    protected override void OnFormClosing(FormClosingEventArgs e)
    {
        base.OnFormClosing(e);
        if (working)
            e.Cancel = true;
    }

    protected override void Dispose(bool disposing)
    {
        base.Dispose(disposing);
        if (disposing)
        {
            brandIcon.Dispose();
            heading.Dispose();
            titleFont.Dispose();
            bodyFont.Dispose();
        }
    }
}
