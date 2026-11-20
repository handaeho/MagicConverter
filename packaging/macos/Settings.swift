import AppKit

private final class NumberField: NSStackView {
    let text: NSTextField
    private let stepper = NSStepper()
    init(_ value: Int, minimum: Int, maximum: Int) {
        text = NSTextField(string: String(value))
        super.init(frame: .zero)
        text.isEditable = false
        text.widthAnchor.constraint(equalToConstant: 100).isActive = true
        stepper.minValue = Double(minimum)
        stepper.maxValue = Double(maximum)
        stepper.integerValue = value
        stepper.target = self
        stepper.action = #selector(adjust)
        addArrangedSubview(text)
        addArrangedSubview(stepper)
        spacing = 6
    }
    required init?(coder: NSCoder) { nil }
    @objc private func adjust() { text.integerValue = stepper.integerValue }
}

final class MacProfile {
    let directory: URL
    let catalog: [String: Any]
    var values: [String: Any]
    var changed: (() -> Void)?

    init() {
        let environment = ProcessInfo.processInfo.environment
        directory = environment["MAGIC_CONVERTER_PROFILE_DIR"].map { URL(fileURLWithPath: $0) }
            ?? FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/MagicConverter/3.0/profile")
        let resource = Bundle.main.url(forResource: "catalog", withExtension: "json")!
        catalog = (try! JSONSerialization.jsonObject(with: Data(contentsOf: resource))) as! [String: Any]
        let defaults = catalog["default_preferences"] as! [String: Any]
        values = defaults
        for name in ["settings.json", "settings.json.bak"] {
            if let data = try? Data(contentsOf: directory.appendingPathComponent(name)),
               let stored = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
               let version = stored["version"] as? Int, (1...3).contains(version) {
                values.merge(stored) { _, saved in saved }
                break
            }
        }
    }

    var presets: [[String: Any]] { (values["presets"] as? [[String: Any]] ?? []).filter { $0["id"] is String && $0["action_key"] is String } }
    var profiles: [[String: Any]] { values["profiles"] as? [[String: Any]] ?? [] }
    var actions: [[String: Any]] { catalog["actions"] as! [[String: Any]] }

    func save() throws {
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let target = directory.appendingPathComponent("settings.json")
        if let old = try? Data(contentsOf: target), (try? JSONSerialization.jsonObject(with: old)) != nil {
            try old.write(to: directory.appendingPathComponent("settings.json.bak"), options: .atomic)
        }
        let data = try JSONSerialization.data(withJSONObject: values, options: [.prettyPrinted, .sortedKeys])
        try data.write(to: target, options: .atomic)
        changed?()
    }

    func slots(for mode: WheelMode, available: [WheelAction]) -> [WheelAction] {
        let id = values[mode == .convert ? "convert_profile" : "action_profile"] as? String ?? ""
        guard let group = profiles.first(where: { $0["id"] as? String == id }) else {
            return available.filter { mode.includes($0) }
        }
        let keys = Set(available.map(\.key))
        return (group["preset_ids"] as? [String] ?? []).compactMap { id in
            guard let preset = presets.first(where: { $0["id"] as? String == id }),
                  let key = preset["action_key"] as? String, keys.contains(key) else { return nil }
            return WheelAction(key: key, label: preset["name"] as? String ?? key, presetID: id)
        }
    }
}

final class SettingsWindow: NSObject {
    private let profile: MacProfile
    private let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 650, height: 560),
                                  styleMask: [.titled, .closable, .resizable], backing: .buffered, defer: false)
    private let tabs = NSSegmentedControl(labels: ["고급 설정", "프리셋", "휠 구성", "작업 관리", "결과 기록"], trackingMode: .selectOne, target: nil, action: nil)
    private let content = NSScrollView()
    private let status = NSTextField(wrappingLabelWithString: "")
    private var fields: [String: NSView] = [:]
    private var presetIndex = 0
    private var groupIndex = 0
    private var ruleIndex = 0
    private var jobs: [[String: Any]] = []
    private var historyOutputs: [String: [URL]] = [:]
    var command: (([String]) throws -> [String: Any])?
    var restartService: (() -> Void)?
    var cancelConversion: (() -> Void)?
    var currentConversion: (() -> String)?

    init(profile: MacProfile) {
        self.profile = profile
        super.init()
        window.title = "MagicConverter 설정"
        window.isReleasedWhenClosed = false
        window.minSize = NSSize(width: 620, height: 500)
        tabs.target = self
        tabs.action = #selector(render)
        tabs.selectedSegment = 0
        content.hasVerticalScroller = true
        content.drawsBackground = false
        let layout = NSStackView(views: [tabs, content, status])
        layout.orientation = .vertical
        layout.alignment = .leading
        layout.spacing = 16
        layout.translatesAutoresizingMaskIntoConstraints = false
        window.contentView!.addSubview(layout)
        NSLayoutConstraint.activate([
            layout.topAnchor.constraint(equalTo: window.contentView!.topAnchor, constant: 18),
            layout.leadingAnchor.constraint(equalTo: window.contentView!.leadingAnchor, constant: 18),
            layout.trailingAnchor.constraint(equalTo: window.contentView!.trailingAnchor, constant: -18),
            layout.bottomAnchor.constraint(equalTo: window.contentView!.bottomAnchor, constant: -18),
            content.widthAnchor.constraint(equalTo: layout.widthAnchor),
            status.widthAnchor.constraint(equalTo: layout.widthAnchor),
            content.heightAnchor.constraint(greaterThanOrEqualToConstant: 350)
        ])
        render()
    }

    func show(_ tab: Int = 0) {
        tabs.selectedSegment = tab
        render()
        window.center()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    private func button(_ title: String, _ selector: Selector) -> NSButton {
        NSButton(title: title, target: self, action: selector)
    }

    private func text(_ key: String, _ value: String) -> NSTextField {
        let field = NSTextField(string: value)
        fields[key] = field
        field.widthAnchor.constraint(greaterThanOrEqualToConstant: 240).isActive = true
        return field
    }

    private func choice(_ key: String, _ options: [(String, String)], _ value: String) -> NSPopUpButton {
        let field = NSPopUpButton(frame: .zero, pullsDown: false)
        for (label, value) in options {
            field.addItem(withTitle: label)
            field.lastItem?.representedObject = value
        }
        if !options.contains(where: { $0.1 == value }) {
            field.addItem(withTitle: value.isEmpty ? "기본값" : value)
            field.lastItem?.representedObject = value
        }
        field.selectItem(at: field.itemArray.firstIndex(where: { $0.representedObject as? String == value }) ?? 0)
        fields[key] = field
        return field
    }

    private func numbers(_ key: String, _ options: [Int], _ value: Int) -> NSPopUpButton {
        choice(key, options.map { (String($0), String($0)) }, String(value))
    }

    private func value(_ key: String) -> String {
        if let number = fields[key] as? NumberField { return number.text.stringValue }
        if let popup = fields[key] as? NSPopUpButton { return popup.selectedItem?.representedObject as? String ?? "" }
        return (fields[key] as? NSTextField)?.stringValue ?? ""
    }

    private func number(_ key: String, _ value: Int, minimum: Int = 0, maximum: Int = 100000) -> NSView {
        let field = NumberField(value, minimum: minimum, maximum: maximum)
        fields[key] = field
        return field
    }

    private func row(_ label: String, _ control: NSView) -> [NSView] {
        let title = NSTextField(labelWithString: label)
        title.toolTip = label + "에 적용할 값의 선택"
        control.toolTip = title.toolTip
        return [title, control]
    }

    @objc private func render() {
        fields = [:]
        let rows: [[NSView]]
        switch tabs.selectedSegment {
        case 1: rows = presetRows()
        case 2: rows = groupRows()
        case 3: rows = taskRows()
        case 4: rows = historyRows()
        default: rows = advancedRows()
        }
        let grid = NSGridView(views: rows)
        grid.rowSpacing = 12
        grid.columnSpacing = 16
        grid.column(at: 0).xPlacement = .leading
        grid.column(at: 1).xPlacement = .fill
        grid.translatesAutoresizingMaskIntoConstraints = false
        let document = NSView()
        document.addSubview(grid)
        content.documentView = document
        document.translatesAutoresizingMaskIntoConstraints = false
        NSLayoutConstraint.activate([
            document.widthAnchor.constraint(equalTo: content.contentView.widthAnchor),
            grid.topAnchor.constraint(equalTo: document.topAnchor, constant: 8),
            grid.leadingAnchor.constraint(equalTo: document.leadingAnchor, constant: 4),
            grid.trailingAnchor.constraint(equalTo: document.trailingAnchor, constant: -20),
            grid.bottomAnchor.constraint(equalTo: document.bottomAnchor, constant: -18)
        ])
    }

    private func advancedRows() -> [[NSView]] {
        let output = profile.values["output"] as? [String: Any] ?? [:]
        return [
            row("감지 주기 (ms)", numbers("interval_ms", Array(30...50), profile.values["interval_ms"] as? Int ?? 35)),
            row("드래그 거리 (px)", numbers("threshold", Array(stride(from: 5, through: 80, by: 5)), profile.values["threshold"] as? Int ?? 15)),
            row("출력 이름", choice("template", [("원본 이름 + 변환 종류", "{stem}_{tag}"), ("원본 이름", "{stem}"), ("원본 이름 + 프리셋", "{stem}_{preset}")], output["template"] as? String ?? "{stem}_{tag}")),
            row("이름 충돌", choice("collision", [("번호를 붙여 저장", "rename"), ("오류 안내 후 중지", "error")], output["collision"] as? String ?? "rename")),
            row("결과 기록 보관 수", numbers("history_limit", [0, 50, 100, 200, 500, 1000], profile.values["history_limit"] as? Int ?? 200)),
            row("FFmpeg 경로", button("실행 파일 선택…", #selector(selectFFmpeg))),
            row("설정 파일", NSStackView(views: [button("가져오기…", #selector(importSettings)), button("내보내기…", #selector(exportSettings))])),
            row("설정 복원", NSStackView(views: [button("백업 복구", #selector(restoreBackup)), button("기본값 복원", #selector(resetSettings))])),
            row("", button("설정 저장", #selector(saveAdvanced)))
        ]
    }

    private func presetRows() -> [[NSView]] {
        let presets = profile.presets
        presetIndex = min(presetIndex, max(presets.count - 1, 0))
        let preset = presets.indices.contains(presetIndex) ? presets[presetIndex] : [:]
        let options = preset["options"] as? [String: Any] ?? [:]
        let tools = preset["tools"] as? [String: Any] ?? [:]
        let selector = choice("preset", presets.enumerated().map { ($0.element["name"] as? String ?? "", String($0.offset)) }, String(presetIndex))
        selector.target = self
        selector.action = #selector(selectPreset)
        var rows = [
            row("프리셋", selector),
            row("이름", text("name", preset["name"] as? String ?? "")),
            row("작업", choice("action", profile.actions.map { ($0["label"] as! String, $0["key"] as! String) }, preset["action_key"] as? String ?? "image:jpg")),
            row("이미지 품질", numbers("quality", Array(stride(from: 10, through: 100, by: 5)), options["image_quality"] as? Int ?? 90)),
            row("최대 이미지 변 (px)", numbers("edge", [0, 640, 1280, 1920, 2560, 3840], options["image_max_edge"] as? Int ?? 0)),
            row("동영상 CRF", choice("crf", [("자동", "")] + [0, 18, 20, 23, 26, 28, 32].map { (String($0), String($0)) }, (options["video_crf"] as? Int).map(String.init) ?? "")),
            row("오디오 비트레이트", choice("bitrate", [("자동", "")] + [64, 96, 128, 160, 192, 256, 320].map { (String($0), String($0)) }, (options["audio_bitrate"] as? Int).map(String.init) ?? "")),
            row("PDF 페이지", text("pages", tools["pdf_pages"] as? String ?? "")),
            row("회전", numbers("rotation", [90, 180, 270], tools["rotation"] as? Int ?? 90)),
            row("반전", choice("flip", [("좌우", "horizontal"), ("상하", "vertical")], tools["flip"] as? String ?? "horizontal")),
            row("자막 이동 (ms)", number("offset", tools["subtitle_offset_ms"] as? Int ?? 1000, minimum: -604800000, maximum: 604800000))
        ]
        for (key, title, fallback, maximum) in [
            ("crop_x", "자르기 시작 X", 0, 100000), ("crop_y", "자르기 시작 Y", 0, 100000),
            ("crop_width", "자르기 너비", 0, 100000), ("crop_height", "자르기 높이", 0, 100000),
            ("start_ms", "구간 시작 (ms)", 0, 604800000), ("duration_ms", "구간 길이 (ms)", 0, 604800000),
            ("frame_interval", "프레임 추출 간격", 1, 10000), ("max_frames", "최대 추출 프레임", 10000, 100000)
        ] { rows.append(row(title, number(key, tools[key] as? Int ?? fallback, minimum: ["frame_interval", "max_frames"].contains(key) ? 1 : 0, maximum: maximum))) }
        rows.append(row("", NSStackView(views: [button("저장", #selector(savePreset)), button("새 프리셋", #selector(addPreset)), button("삭제", #selector(deletePreset))])))
        return rows
    }

    private func groupRows() -> [[NSView]] {
        let groups = profile.profiles
        groupIndex = min(groupIndex, max(groups.count - 1, 0))
        let group = groups.indices.contains(groupIndex) ? groups[groupIndex] : [:]
        let selector = choice("group", groups.enumerated().map { ($0.element["name"] as? String ?? "", String($0.offset)) }, String(groupIndex))
        selector.target = self
        selector.action = #selector(selectGroup)
        var rows = [row("휠 구성", selector), row("이름", text("group-name", group["name"] as? String ?? "")),
                    row("용도", choice("group-mode", [("Shift 변환", "convert"), ("Ctrl 빠른 작업", "action")], group["mode"] as? String ?? "convert"))]
        let ids = group["preset_ids"] as? [String] ?? []
        let options = [("비움", "")] + profile.presets.map { ($0["name"] as? String ?? "", $0["id"] as! String) }
        for index in 0..<12 {
            rows.append(row("항목 \(index + 1)", choice("slot-\(index)", options, index < ids.count ? ids[index] : "")))
        }
        rows.append(row("", NSStackView(views: [button("저장", #selector(saveGroup)), button("새 구성", #selector(addGroup)), button("삭제", #selector(deleteGroup))])))
        return rows
    }

    private func taskRows() -> [[NSView]] {
        let path = profile.directory.appendingPathComponent("automation.json")
        let automation = (try? JSONSerialization.jsonObject(with: Data(contentsOf: path))) as? [String: Any] ?? ["version": 1, "rules": []]
        let rules = automation["rules"] as? [[String: Any]] ?? []
        if ruleIndex >= rules.count { ruleIndex = max(0, rules.count - 1) }
        let rule = rules.indices.contains(ruleIndex) ? rules[ruleIndex] : [:]
        let recipe = rule["recipe"] as? [String: Any] ?? [:]
        let selector = choice("rule", rules.enumerated().map { ($0.element["folder"] as? String ?? "", String($0.offset)) }, String(ruleIndex))
        selector.target = self
        selector.action = #selector(selectRule)
        var rows = [
            row("폴더 감시 규칙", selector),
            row("감시 폴더", text("watch-folder", rule["folder"] as? String ?? "")),
            row("", button("감시 폴더 선택…", #selector(chooseWatchFolder))),
            row("출력 폴더", text("watch-output", rule["output"] as? String ?? "")),
            row("", button("출력 폴더 선택…", #selector(chooseWatchOutput))),
            row("포함 파일", text("watch-include", rule["include"] as? String ?? "*")),
            row("작업", choice("watch-action", profile.actions.map { ($0["label"] as! String, $0["key"] as! String) }, recipe["action_key"] as? String ?? "image:jpg")),
            row("하위 폴더", choice("recursive", [("포함", "true"), ("현재 폴더만", "false")], rule["recursive"] as? Bool == true ? "true" : "false")),
            row("사용 여부", choice("watch-enabled", [("사용", "true"), ("중지", "false")], rule["enabled"] as? Bool == false ? "false" : "true")),
            row("", NSStackView(views: [button("규칙 저장", #selector(saveRule)), button("새 규칙", #selector(addRule)), button("규칙 삭제", #selector(deleteRule))]))
        ]
        if let result = try? command?(["status"]), let active = result["jobs"] as? [[String: Any]] {
            jobs = active
            for job in active {
                rows.append(row("실행 작업 \(job["job_id"] ?? "")", NSTextField(labelWithString: job["action"] as? String ?? "")))
            }
        }
        rows.append(row("실행 작업", NSStackView(views: [button("새로고침", #selector(render)), button("전체 취소", #selector(cancelJobs))])))
        if let text = currentConversion?(), !text.isEmpty { rows.append(row("앱 변환", NSTextField(wrappingLabelWithString: text))) }
        return rows
    }

    private func historyRows() -> [[NSView]] {
        let path = profile.directory.appendingPathComponent("history.json")
        let data = (try? JSONSerialization.jsonObject(with: Data(contentsOf: path))) as? [String: Any]
        let entries = data?["entries"] as? [[String: Any]] ?? []
        if entries.isEmpty { return [row("결과 기록", NSTextField(labelWithString: "아직 변환 기록이 없습니다."))] }
        historyOutputs = [:]
        return entries.reversed().prefix(200).map { entry in
            let files = entry["files"] as? [[String: Any]] ?? []
            let sources = files.compactMap { $0["source"] as? String }
            let id = entry["id"] as? String ?? UUID().uuidString
            historyOutputs[id] = files.flatMap { $0["outputs"] as? [String] ?? [] }.map { URL(fileURLWithPath: $0) }
            let reveal = button("결과 위치", #selector(revealHistory))
            reveal.identifier = NSUserInterfaceItemIdentifier(id)
            reveal.isEnabled = !(historyOutputs[id] ?? []).isEmpty
            return row(entry["label"] as? String ?? "변환", NSStackView(views: [NSTextField(wrappingLabelWithString: sources.map { URL(fileURLWithPath: $0).lastPathComponent }.joined(separator: ", ")), reveal]))
        }
    }

    @objc private func revealHistory(_ sender: NSButton) {
        NSWorkspace.shared.activateFileViewerSelecting(historyOutputs[sender.identifier?.rawValue ?? ""] ?? [])
    }

    private func persist() {
        do { try profile.save(); status.stringValue = "저장했습니다." }
        catch { status.stringValue = "저장 실패 · \(error.localizedDescription)" }
    }
    @objc private func saveAdvanced() {
        for key in ["interval_ms", "threshold", "history_limit"] { profile.values[key] = Int(value(key)) }
        var output = profile.values["output"] as? [String: Any] ?? [:]
        output["template"] = value("template")
        output["collision"] = value("collision")
        profile.values["output"] = output
        persist()
    }
    @objc private func selectPreset() { presetIndex = Int(value("preset")) ?? 0; render() }
    @objc private func savePreset() {
        var presets = profile.presets
        guard presets.indices.contains(presetIndex), !value("name").trimmingCharacters(in: .whitespaces).isEmpty,
              let offset = Int(value("offset")) else { status.stringValue = "이름과 자막 이동 값을 확인하세요."; return }
        var preset = presets[presetIndex]
        preset["name"] = value("name")
        preset["action_key"] = value("action")
        var options = preset["options"] as? [String: Any] ?? [:]
        for (field, key) in [("quality", "image_quality"), ("edge", "image_max_edge"), ("crf", "video_crf"), ("bitrate", "audio_bitrate")] { options[key] = Int(value(field)) }
        preset["options"] = options
        var tools = preset["tools"] as? [String: Any] ?? [:]
        tools["pdf_pages"] = value("pages")
        tools["rotation"] = Int(value("rotation"))
        tools["flip"] = value("flip")
        tools["subtitle_offset_ms"] = offset
        for key in ["crop_x", "crop_y", "crop_width", "crop_height", "start_ms", "duration_ms", "frame_interval", "max_frames"] { tools[key] = Int(value(key)) }
        preset["tools"] = tools
        let action = value("action")
        let toolFields: [String: [String]] = [
            "tool:pdf_select": ["pdf_pages"], "tool:pdf_split": ["pdf_pages"], "tool:pdf_rotate": ["pdf_pages", "rotation"],
            "tool:image_rotate": ["rotation"], "tool:image_flip": ["flip"],
            "tool:image_crop": ["crop_x", "crop_y", "crop_width", "crop_height"],
            "tool:image_frames": ["frame_interval", "max_frames"], "tool:media_trim": ["start_ms", "duration_ms"],
            "tool:media_frames": ["start_ms", "duration_ms", "frame_interval", "max_frames"], "tool:subtitle_shift": ["subtitle_offset_ms"]
        ]
        let defaults = (profile.catalog["default_preferences"] as! [String: Any])["presets"] as! [[String: Any]]
        let defaultTools = defaults[0]["tools"] as! [String: Any]
        for (key, defaultValue) in defaultTools where !(toolFields[action] ?? []).contains(key) { tools[key] = defaultValue }
        preset["tools"] = tools
        let video = ["media:mp4", "media:mov", "media:mkv", "media:webm"].contains(action)
        let audio = ["media:mp4", "media:mov", "media:mkv", "media:webm", "media:avi", "media:wmv", "media:mp3", "audio:m4a", "audio:ogg", "audio:opus", "audio:wma"].contains(action)
        if !action.hasPrefix("image:") { options["image_quality"] = 90; options["image_max_edge"] = 0 }
        if !video { options["video_crf"] = NSNull() }
        if !audio { options["audio_bitrate"] = NSNull() }
        preset["options"] = options
        presets[presetIndex] = preset
        profile.values["presets"] = presets
        persist()
    }
    @objc private func addPreset() {
        var presets = profile.presets
        presets.append(["id": UUID().uuidString.lowercased(), "name": "새 프리셋", "action_key": "image:jpg"])
        profile.values["presets"] = presets
        presetIndex = presets.count - 1
        persist(); render()
    }
    @objc private func deletePreset() {
        var presets = profile.presets
        guard presets.indices.contains(presetIndex) else { return }
        let id = presets.remove(at: presetIndex)["id"] as? String ?? ""
        profile.values["presets"] = presets
        profile.values["profiles"] = profile.profiles.map { group in
            var group = group
            group["preset_ids"] = (group["preset_ids"] as? [String] ?? []).filter { $0 != id }
            return group
        }
        persist(); render()
    }
    @objc private func selectGroup() { groupIndex = Int(value("group")) ?? 0; render() }
    @objc private func saveGroup() {
        var groups = profile.profiles
        guard groups.indices.contains(groupIndex) else { return }
        groups[groupIndex]["name"] = value("group-name")
        groups[groupIndex]["mode"] = value("group-mode")
        groups[groupIndex]["preset_ids"] = (0..<12).map { value("slot-\($0)") }.filter { !$0.isEmpty }
        profile.values["profiles"] = groups
        persist()
    }
    @objc private func addGroup() {
        var groups = profile.profiles
        groups.append(["id": UUID().uuidString.lowercased(), "name": "새 휠", "mode": "convert", "preset_ids": []])
        profile.values["profiles"] = groups
        groupIndex = groups.count - 1
        persist(); render()
    }
    @objc private func deleteGroup() {
        var groups = profile.profiles
        guard groups.indices.contains(groupIndex) else { return }
        let id = groups.remove(at: groupIndex)["id"] as? String ?? ""
        for key in ["convert_profile", "action_profile"] where profile.values[key] as? String == id { profile.values[key] = "" }
        profile.values["profiles"] = groups
        persist(); render()
    }
    @objc private func selectRule() { ruleIndex = Int(value("rule")) ?? 0; render() }
    private func saveRules(_ rules: [[String: Any]]) {
        do {
            try FileManager.default.createDirectory(at: profile.directory, withIntermediateDirectories: true)
            let path = profile.directory.appendingPathComponent("automation.json")
            var root = (try? JSONSerialization.jsonObject(with: Data(contentsOf: path))) as? [String: Any] ?? ["version": 1]
            root["rules"] = rules
            try JSONSerialization.data(withJSONObject: root, options: .prettyPrinted).write(to: path, options: .atomic)
            restartService?()
            status.stringValue = "감시 규칙을 저장했습니다."
        } catch { status.stringValue = error.localizedDescription }
    }
    private func rules() -> [[String: Any]] {
        let path = profile.directory.appendingPathComponent("automation.json")
        let root = (try? JSONSerialization.jsonObject(with: Data(contentsOf: path))) as? [String: Any]
        return root?["rules"] as? [[String: Any]] ?? []
    }
    @objc private func saveRule() {
        var rules = rules()
        let folder = URL(fileURLWithPath: value("watch-folder")).standardizedFileURL
        let output = URL(fileURLWithPath: value("watch-output")).standardizedFileURL
        guard !value("watch-folder").isEmpty, !value("watch-output").isEmpty,
              FileManager.default.fileExists(atPath: folder.path), FileManager.default.fileExists(atPath: output.path),
              folder != output, !folder.path.hasPrefix(output.path + "/") else { status.stringValue = "감시·출력 폴더를 확인하세요."; return }
        let old = rules.indices.contains(ruleIndex) ? rules[ruleIndex] : [:]
        let rule: [String: Any] = ["id": old["id"] as? String ?? UUID().uuidString.lowercased(), "folder": folder.path,
                                   "output": output.path, "include": value("watch-include"), "recursive": value("recursive") == "true",
                                   "enabled": value("watch-enabled") == "true", "settle": 2.0,
                                   "recipe": ["id": "watch-rule", "name": "폴더 감시", "action_key": value("watch-action")]]
        if rules.indices.contains(ruleIndex) { rules[ruleIndex] = rule } else { rules.append(rule) }
        saveRules(rules)
    }
    @objc private func addRule() { ruleIndex = -1; status.stringValue = "폴더와 작업을 선택한 뒤 규칙을 저장하세요."; render() }
    @objc private func deleteRule() {
        var rules = rules()
        guard rules.indices.contains(ruleIndex) else { return }
        rules.remove(at: ruleIndex)
        saveRules(rules); render()
    }
    @objc private func cancelJobs() {
        cancelConversion?()
        do {
            let result = try command?(["status"])
            if let instance = result?["instance"] as? String { _ = try command?(["cancel", "--instance", instance]) }
            status.stringValue = "작업 취소를 요청했습니다."
        } catch { status.stringValue = error.localizedDescription }
    }
    private func selectFolder(_ key: String) {
        let panel = NSOpenPanel()
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        if panel.runModal() == .OK { (fields[key] as? NSTextField)?.stringValue = panel.url!.path }
    }
    @objc private func chooseWatchFolder() { selectFolder("watch-folder") }
    @objc private func chooseWatchOutput() { selectFolder("watch-output") }
    @objc private func selectFFmpeg() {
        let panel = NSOpenPanel()
        if panel.runModal() == .OK { profile.values["ffmpeg_path"] = panel.url!.path; persist() }
    }
    @objc private func exportSettings() {
        let panel = NSSavePanel()
        panel.nameFieldStringValue = "MagicConverter-settings.json"
        if panel.runModal() == .OK {
            do { try JSONSerialization.data(withJSONObject: profile.values, options: .prettyPrinted).write(to: panel.url!, options: .atomic) }
            catch { status.stringValue = error.localizedDescription }
        }
    }
    @objc private func importSettings() {
        let panel = NSOpenPanel()
        guard panel.runModal() == .OK, let url = panel.url else { return }
        do {
            guard let data = try JSONSerialization.jsonObject(with: Data(contentsOf: url)) as? [String: Any],
                  let version = data["version"] as? Int, (1...3).contains(version), data["presets"] is [[String: Any]] else { throw NSError(domain: "MagicConverter", code: 1, userInfo: [NSLocalizedDescriptionKey: "설정 파일의 형식을 확인하세요."]) }
            profile.values = data; persist(); render()
        } catch { status.stringValue = error.localizedDescription }
    }
    @objc private func restoreBackup() {
        let path = profile.directory.appendingPathComponent("settings.json.bak")
        if let data = try? Data(contentsOf: path), let values = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            profile.values = values; persist(); render()
        } else { status.stringValue = "복구할 백업을 찾지 못했습니다." }
    }
    @objc private func resetSettings() {
        profile.values = profile.catalog["default_preferences"] as! [String: Any]
        persist(); render()
    }
}
