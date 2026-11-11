using System;
using System.Collections.Generic;
using System.Windows.Forms;

internal sealed class UninstallWizard : WizardWindow
{
    private readonly Control welcome;
    private readonly Control review;
    private readonly Func<IList<string>> remove;
    private int page;

    internal int ExitCode { get; private set; }

    internal UninstallWizard(string version, string folder, Func<IList<string>> remove)
        : base("MagicConverter " + version + " 제거 마법사")
    {
        this.remove = remove;
        ExitCode = 2;
        welcome = Welcome("MagicConverter " + version + "\r\n제거 마법사에 오신 것을 환영합니다",
            "이 마법사는 현재 Windows 계정의 MagicConverter를 제거합니다.\r\n\r\n진행 중인 변환을 마치고 MagicConverter를 종료한 뒤 다음을 누르세요.");

        var content = new TableLayoutPanel { ColumnCount = 1, RowCount = 3 };
        content.RowStyles.Add(new RowStyle(SizeType.Absolute, 25F));
        content.RowStyles.Add(new RowStyle(SizeType.Absolute, 44F));
        content.Controls.Add(new Label { Text = "설치 위치", AutoSize = true }, 0, 0);
        content.Controls.Add(ReadOnlyText(folder), 0, 1);
        content.Controls.Add(new Label
        {
            Text = "설치 파일, 앱 전용 Python, 기본 설정, 변환 기록, 감시 설정과 로그를 제거합니다.\r\n\r\n원본 문서, 변환 결과와 별도로 설치한 도구는 보존합니다.",
            Dock = DockStyle.Fill
        }, 0, 2);
        review = Step("제거 준비 완료", "제거할 위치와 항목을 확인하세요.", content);
        Back.Click += (sender, args) => ShowWelcome();
        Next.Click += (sender, args) => Advance();
        ShowWelcome();
    }

    private void ShowWelcome()
    {
        page = 0;
        Display(welcome, false, "다음 >");
    }

    private void Advance()
    {
        if (page == 0)
        {
            page = 1;
            Display(review, true, "제거");
        }
        else if (page == 1)
        {
            page = 2;
            IList<string> kept = null;
            RunWork("MagicConverter 제거 중", "설치 파일과 설정을 정리하는 중", report => kept = remove(), error =>
            {
                page = 3;
                ExitCode = error == null ? 0 : 1;
                if (error != null)
                    Display(Step("제거를 완료하지 못했습니다", "오류 내용을 확인하세요.",
                        ReadOnlyText(error.Message, true)), false, "닫기", cancel: false);
                else if (kept.Count > 0)
                    Display(Step("제거 완료", "사용자 파일 또는 링크가 있는 아래 폴더를 보존했습니다.",
                        ReadOnlyText(String.Join("\r\n", kept), true)), false, "마침", cancel: false);
                else
                    Display(Welcome("제거 완료", "MagicConverter 제거를 완료했습니다.\r\n\r\n마침을 눌러 마법사를 닫으세요."),
                        false, "마침", cancel: false);
            });
        }
        else if (page == 3)
            Close();
    }
}
