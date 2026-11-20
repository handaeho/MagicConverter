import AppKit

func descendants(_ view: NSView) -> [NSView] {
    [view] + view.subviews.flatMap { descendants($0) }
}
func grid() -> NSGridView {
    let window = NSApp.windows.first { $0.title == "MagicConverter 설정" }!
    window.contentView!.layoutSubtreeIfNeeded()
    return descendants(window.contentView!).compactMap { $0 as? NSGridView }.first!
}
func control(_ title: String) -> NSView {
    let form = grid()
    for row in 0..<form.numberOfRows {
        if (form.cell(atColumnIndex: 0, rowIndex: row).contentView as? NSTextField)?.stringValue == title {
            return form.cell(atColumnIndex: 1, rowIndex: row).contentView!
        }
    }
    fatalError("Missing settings control: \(title)")
}
func select(_ label: String, _ value: String) {
    let popup = control(label) as! NSPopUpButton
    guard let item = popup.itemArray.first(where: { $0.representedObject as? String == value }) else {
        fatalError("Missing choice \(value) for \(label)")
    }
    popup.select(item)
    if let action = popup.action { NSApp.sendAction(action, to: popup.target, from: popup) }
}
func click(_ title: String) {
    let button = descendants(grid()).compactMap { $0 as? NSButton }.first { $0.title == title }!
    button.performClick(nil)
}

let app = NSApplication.shared
app.setActivationPolicy(.regular)
let root = URL(fileURLWithPath: CommandLine.arguments[1])
let cli = URL(fileURLWithPath: CommandLine.arguments[2])
let image = URL(fileURLWithPath: CommandLine.arguments[3])
let output = root.appendingPathComponent("output")
try FileManager.default.createDirectory(at: output, withIntermediateDirectories: true)
let profile = MacProfile()
let settings = SettingsWindow(profile: profile)
settings.show(0)
select("감지 주기 (ms)", "40")
select("드래그 거리 (px)", "25")
select("결과 기록 보관 수", "50")
click("설정 저장")
precondition(MacProfile().values["threshold"] as? Int == 25)
precondition(MacProfile().values["interval_ms"] as? Int == 40)
settings.show(1)
click("새 프리셋")
(control("이름") as! NSTextField).stringValue = "UI검증"
select("작업", "image:jpg")
select("이미지 품질", "75")
select("최대 이미지 변 (px)", "640")
select("프리셋 저장 위치", "folder")
(control("프리셋 출력 폴더") as! NSTextField).stringValue = output.path
select("프리셋 출력 이름", "{stem}_{preset}")
click("저장")
let preset = MacProfile().presets.last!
precondition(preset["name"] as? String == "UI검증")
let process = Process()
process.executableURL = cli
process.arguments = ["convert", "--data-dir", profile.directory.path, "--preset", preset["id"] as! String, "--", image.path]
try process.run()
process.waitUntilExit()
precondition(process.terminationStatus == 0, "The engine rejected a preset saved through the settings UI")
precondition(FileManager.default.fileExists(atPath: output.appendingPathComponent("drag_UI검증.jpg").path))
settings.show(2)
click("새 구성")
(control("이름") as! NSTextField).stringValue = "UI 휠"
select("항목 1", preset["id"] as! String)
click("저장")
precondition(MacProfile().profiles.last?["preset_ids"] as? [String] == [preset["id"] as! String])
settings.show(3)
(control("감시 폴더") as! NSTextField).stringValue = image.deletingLastPathComponent().path
(control("출력 폴더") as! NSTextField).stringValue = output.path
click("규칙 저장")
let rules = try JSONSerialization.jsonObject(with: Data(contentsOf: profile.directory.appendingPathComponent("automation.json"))) as! [String: Any]
precondition((rules["rules"] as! [[String: Any]]).count == 1)
settings.show(4)
let buttons = descendants(grid()).compactMap { $0 as? NSButton }
precondition(buttons.contains { $0.title == "결과 위치" && $0.isEnabled })
precondition(buttons.contains { $0.title == "자세히" })
print("PASS: advanced choices, preset conversion, wheel slots, watch rules and real output history UI.")
exit(0)
