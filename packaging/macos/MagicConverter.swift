import AppKit
import Darwin

private struct Conversion: Decodable {
    let key: String
    let label: String
}

private struct Choices: Decodable {
    let actions: [Conversion]
    let snapshot_recipes: Bool?
}

private struct ResultReport: Decodable {
    struct Job: Decodable {
        let outputs: [String]
        let errors: [String]
        let warnings: [String]
    }
    let ok: Bool
    let interrupted: Bool?
    let jobs: [Job]?
    let errors: [String]?
    let error: String?
}

private final class DropView: NSView {
    var onFiles: (([URL]) -> Void)?
    var onError: ((Error) -> Void)?

    override init(frame: NSRect) {
        super.init(frame: frame)
        registerForDraggedTypes(dragTypes)
    }

    required init?(coder: NSCoder) { return nil }

    private func files(_ info: NSDraggingInfo) -> [URL] {
        draggedFiles(info.draggingPasteboard)
    }

    override func draggingEntered(_ sender: NSDraggingInfo) -> NSDragOperation {
        onFiles != nil && !files(sender).isEmpty ? .copy : []
    }

    override func performDragOperation(_ sender: NSDraggingInfo) -> Bool {
        guard let onFiles else { return false }
        let urls = files(sender)
        guard !urls.isEmpty else { return false }
        receiveDrag(sender.draggingPasteboard) { [weak self] result in
            switch result {
            case .success(let files): self?.onFiles?(files)
            case .failure(let error): self?.onError?(error)
            }
        }
        return true
    }
}

private final class App: NSObject, NSApplicationDelegate {
    private let window = NSWindow(
        contentRect: NSRect(x: 0, y: 0, width: 760, height: 500),
        styleMask: [.titled, .closable, .miniaturizable, .resizable],
        backing: .buffered, defer: false)
    private let dropView = DropView(frame: .zero)
    private let input = NSTextField(wrappingLabelWithString: "Finder에서 Shift+드래그로 변환 · Ctrl+드래그로 빠른 작업")
    private let destination = NSTextField(labelWithString: "원본 파일과 같은 폴더")
    private let actions = NSPopUpButton(frame: .zero, pullsDown: false)
    private let detail = NSTextField(wrappingLabelWithString: "")
    private let run = NSButton(title: "변환 시작", target: nil, action: nil)
    private let reveal = NSButton(title: "Finder에서 결과 보기", target: nil, action: nil)
    private let clear = NSButton(title: "지우기", target: nil, action: nil)
    private let progress = NSProgressIndicator()
    private let wheel = DragWheel()
    private let profile = MacProfile()
    private lazy var settings = SettingsWindow(profile: profile)
    private let convertProfile = NSPopUpButton(frame: .zero, pullsDown: false)
    private let quickProfile = NSPopUpButton(frame: .zero, pullsDown: false)
    private var service: Process?
    private var conversionProcess: Process?
    private var progressTimer: Timer?
    private var shuttingDown = false
    private struct PendingConversion {
        let action: WheelAction
        let files: [URL]
        let recipe: [String: Any]
        let output: URL?
    }
    private var pending: [PendingConversion] = []
    private var snapshotRecipes = false
    private let queries = DispatchQueue(label: "com.handaeho.magicconverter.actions", qos: .userInitiated)
    private var statusItem: NSStatusItem!
    private let wheelEnabled = NSButton(checkboxWithTitle: "드래그 감지 사용", target: nil, action: nil)
    private let releaseModifier = NSButton(checkboxWithTitle: "휠 안에서 보조키를 놓아야 실행", target: nil, action: nil)
    private let notificationPanel = NSPanel(contentRect: NSRect(x: 0, y: 0, width: 340, height: 96),
                                           styleMask: [.titled, .nonactivatingPanel], backing: .buffered, defer: false)
    private let notificationText = NSTextField(wrappingLabelWithString: "")
    private var files: [URL] = []
    private var output: URL?
    private var results: [URL] = []
    private var options: [Conversion] = []
    private var query = UUID()
    private var busy = false
    private let work = DispatchQueue(label: "com.handaeho.magicconverter.engine", qos: .userInitiated)

    private var cli: URL { Bundle.main.bundleURL.appendingPathComponent("Contents/MacOS/magic_converter-cli") }

    func applicationDidFinishLaunching(_ notification: Notification) {
        let menu = NSMenu()
        let appItem = NSMenuItem()
        menu.addItem(appItem)
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "MagicConverter 종료", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu
        appMenu.insertItem(withTitle: "설정 창 열기", action: #selector(showWindow), keyEquivalent: ",", at: 0).target = self
        appMenu.insertItem(withTitle: "MagicConverter 제거…", action: #selector(uninstall), keyEquivalent: "", at: 1).target = self
        let fileItem = NSMenuItem()
        fileItem.title = "파일"
        let fileMenu = NSMenu(title: "파일")
        let open = fileMenu.addItem(withTitle: "파일 선택…", action: #selector(chooseFiles), keyEquivalent: "o")
        open.target = self
        fileItem.submenu = fileMenu
        menu.addItem(fileItem)
        NSApp.mainMenu = menu

        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        if let image = NSImage(named: "AppIcon") {
            let small = image.copy() as! NSImage
            small.size = NSSize(width: 18, height: 18)
            statusItem.button?.image = small
            NSApp.applicationIconImage = image
        } else { statusItem.button?.title = "MC" }
        statusItem.button?.toolTip = "MagicConverter · Shift 변환 / Ctrl 빠른 작업"
        let statusMenu = NSMenu()
        statusMenu.addItem(withTitle: "설정 창 열기", action: #selector(showWindow), keyEquivalent: "").target = self
        statusMenu.addItem(withTitle: "MagicConverter 종료", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "")
        statusItem.menu = statusMenu

        wheelEnabled.state = profile.values["enabled"] as? Bool == false ? .off : .on
        releaseModifier.state = profile.values["release_modifier_inside"] as? Bool == true ? .on : .off
        for control in [wheelEnabled, releaseModifier] {
            control.target = self
            control.action = #selector(saveDragSettings)
        }
        wheelEnabled.toolTip = "Finder에서 Shift 또는 Ctrl을 누른 드래그의 휠 표시 여부"
        releaseModifier.toolTip = "휠 항목 안에서 보조키를 놓은 후 파일을 놓아야 실행하는 설정"
        for popup in [convertProfile, quickProfile] {
            popup.target = self
            popup.action = #selector(saveDragSettings)
        }
        profile.changed = { [weak self] in self?.applyProfile() }
        applyProfile()
        settings.command = { [weak self] arguments in
            guard let self else { return [:] }
            return try JSONSerialization.jsonObject(with: Self.invoke(self.cli, arguments)) as? [String: Any] ?? [:]
        }
        settings.restartService = { [weak self] in self?.reloadService() }
        settings.cancelConversion = { [weak self] in
            self?.pending.removeAll()
            if let process = self?.conversionProcess, process.isRunning { kill(process.processIdentifier, SIGINT) }
        }
        settings.currentConversion = { [weak self] in
            guard let self, self.busy else { return "" }
            return self.detail.stringValue + " · 대기 \(self.pending.count)건"
        }
        wheel.onQuery = { [weak self] files, mode, completion in
            self?.available(files) { [weak self] actions in
                completion(self?.profile.slots(for: mode, available: actions) ?? [])
            }
        }
        wheel.onChoose = { [weak self] action, files in self?.execute(action, files: files) }
        wheel.view.onError = { [weak self] error in self?.notify(error.localizedDescription) }
        wheel.start()

        window.title = "MagicConverter 3.0.5"
        window.isReleasedWhenClosed = false
        window.minSize = NSSize(width: 720, height: 490)
        window.contentView = dropView
        window.center()
        dropView.onFiles = { [weak self] in self?.setFiles($0) }
        dropView.onError = { [weak self] error in self?.notify(error.localizedDescription) }

        let title = NSTextField(labelWithString: "MagicConverter 3.0.5")
        title.font = .systemFont(ofSize: 18, weight: .semibold)
        input.maximumNumberOfLines = 5
        detail.maximumNumberOfLines = 5
        detail.textColor = .secondaryLabelColor
        destination.lineBreakMode = .byTruncatingMiddle
        actions.addItem(withTitle: "파일을 선택하세요")
        actions.isEnabled = false
        run.target = self
        run.action = #selector(convert)
        run.keyEquivalent = "\r"
        run.isEnabled = false
        reveal.target = self
        reveal.action = #selector(revealResults)
        reveal.isEnabled = false
        clear.target = self
        clear.action = #selector(clearFiles)
        progress.style = .spinning
        progress.controlSize = .small
        progress.isDisplayedWhenStopped = false

        let stack = NSStackView(views: [
            title, input,
            row([wheelEnabled, releaseModifier]),
            row([NSTextField(labelWithString: "Shift 변환 휠"), convertProfile, NSTextField(labelWithString: "Ctrl 작업 휠"), quickProfile]),
            row([button("파일 선택…", #selector(chooseFiles)), clear]),
            NSTextField(labelWithString: "변환 작업"), actions,
            NSTextField(labelWithString: "저장 위치"), destination,
            row([button("폴더 선택…", #selector(chooseFolder)), button("원본 폴더 사용", #selector(useSourceFolder))]),
            row([run, reveal, progress]), detail,
            row([button("작업 관리…", #selector(showTasks)), button("결과 기록…", #selector(showHistory)),
                   button("프리셋 편집…", #selector(showPresets)), button("고급 설정…", #selector(showAdvanced))])
        ])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 11
        stack.translatesAutoresizingMaskIntoConstraints = false
        dropView.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.topAnchor.constraint(equalTo: dropView.topAnchor, constant: 22),
            stack.leadingAnchor.constraint(equalTo: dropView.leadingAnchor, constant: 24),
            stack.trailingAnchor.constraint(equalTo: dropView.trailingAnchor, constant: -24),
            stack.bottomAnchor.constraint(lessThanOrEqualTo: dropView.bottomAnchor, constant: -18),
            actions.widthAnchor.constraint(lessThanOrEqualTo: stack.widthAnchor),
            detail.widthAnchor.constraint(equalTo: stack.widthAnchor)
        ])
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        notificationPanel.title = "MagicConverter 작업 진행"
        notificationPanel.isReleasedWhenClosed = false
        notificationPanel.level = .floating
        notificationPanel.hidesOnDeactivate = false
        notificationPanel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        notificationText.frame = NSRect(x: 16, y: 16, width: 308, height: 64)
        notificationPanel.contentView?.addSubview(notificationText)
        if !ProcessInfo.processInfo.arguments.contains("--smoke-test") { startService() }
        if ProcessInfo.processInfo.arguments.contains("--smoke-test") {
            DispatchQueue.main.async {
                exit(self.window.isVisible && self.checkVersion() ? 0 : 1)
            }
        }
    }

    func application(_ application: NSApplication, open urls: [URL]) {
        setFiles(urls)
        showWindow()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows: Bool) -> Bool {
        showWindow()
        return true
    }
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if busy { notify("변환 작업이 진행 중입니다. 완료 후 종료할 수 있습니다."); return .terminateCancel }
        guard service?.isRunning == true else { return .terminateNow }
        if !shuttingDown {
            shuttingDown = true
            let cli = cli
            work.async {
                if let data = try? Self.invoke(cli, ["status"]),
                   let result = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                   let instance = result["instance"] as? String {
                    _ = try? Self.invoke(cli, ["stop", "--instance", instance])
                    self.service?.waitUntilExit()
                }
                DispatchQueue.main.async { NSApp.reply(toApplicationShouldTerminate: true) }
            }
        }
        return .terminateLater
    }

    @objc private func showWindow() {
        window.makeKeyAndOrderFront(nil)
        window.deminiaturize(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    @objc private func saveDragSettings() {
        profile.values["enabled"] = wheelEnabled.state == .on
        profile.values["release_modifier_inside"] = releaseModifier.state == .on
        profile.values["convert_profile"] = convertProfile.selectedItem?.representedObject as? String ?? ""
        profile.values["action_profile"] = quickProfile.selectedItem?.representedObject as? String ?? ""
        do { try profile.save() } catch { detail.stringValue = "설정 저장 실패 · \(error.localizedDescription)" }
    }

    private func applyProfile() {
        wheelEnabled.state = profile.values["enabled"] as? Bool == false ? .off : .on
        releaseModifier.state = profile.values["release_modifier_inside"] as? Bool == true ? .on : .off
        wheel.enabled = wheelEnabled.state == .on
        wheel.requireRelease = releaseModifier.state == .on
        wheel.threshold = CGFloat(max(5, min(80, profile.values["threshold"] as? Int ?? 15)))
        wheel.interval = Double(max(30, min(50, profile.values["interval_ms"] as? Int ?? 35))) / 1000
        wheel.start()
        for (popup, mode, key) in [(convertProfile, "convert", "convert_profile"), (quickProfile, "action", "action_profile")] {
            popup.removeAllItems()
            popup.addItem(withTitle: "자동")
            popup.lastItem?.representedObject = ""
            for group in profile.profiles where group["mode"] as? String == mode {
                popup.addItem(withTitle: group["name"] as? String ?? "휠")
                popup.lastItem?.representedObject = group["id"] as? String ?? ""
            }
            let selected = profile.values[key] as? String ?? ""
            popup.selectItem(at: popup.itemArray.firstIndex(where: { $0.representedObject as? String == selected }) ?? 0)
        }
    }

    @objc private func showAdvanced() { settings.show(0) }
    @objc private func showPresets() { settings.show(1) }
    @objc private func showTasks() { settings.show(3) }
    @objc private func showHistory() { settings.show(4) }

    private func startService() {
        guard service?.isRunning != true else { return }
        do {
            try FileManager.default.createDirectory(at: profile.directory, withIntermediateDirectories: true)
            let log = profile.directory.appendingPathComponent("service.log")
            FileManager.default.createFile(atPath: log.path, contents: nil)
            let output = try FileHandle(forWritingTo: log)
            let process = Process()
            process.executableURL = cli
            process.arguments = ["serve"]
            process.standardOutput = output
            process.standardError = output
            try process.run()
            service = process
        } catch { detail.stringValue = "자동화 서비스 시작 실패 · \(error.localizedDescription)" }
    }

    private func reloadService() {
        guard !busy else { notify("현재 변환이 끝난 뒤 감시 규칙을 적용하세요."); return }
        let cli = cli
        work.async {
            let result = try? Self.invoke(cli, ["status"])
            let status = result.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
            if let jobs = status?["jobs"] as? [Any], !jobs.isEmpty {
                DispatchQueue.main.async { self.notify("감시 작업이 실행 중입니다. 완료 후 규칙을 적용하세요.") }
                return
            }
            if let instance = status?["instance"] as? String { _ = try? Self.invoke(cli, ["stop", "--instance", instance]) }
            self.service?.waitUntilExit()
            DispatchQueue.main.async { self.startService() }
        }
    }

    private func notify(_ text: String) {
        notificationText.stringValue = text
        if let frame = NSScreen.main?.visibleFrame {
            notificationPanel.setFrameOrigin(NSPoint(x: frame.maxX - 356, y: frame.minY + 18))
        }
        notificationPanel.orderFrontRegardless()
    }

    @objc private func uninstall() {
        let alert = NSAlert()
        if busy {
            alert.messageText = "변환 작업이 실행 중입니다."
            alert.informativeText = "작업이 끝난 뒤 제거할 수 있습니다."
            alert.runModal()
            return
        }
        alert.messageText = "MagicConverter를 제거하시겠습니까?"
        alert.informativeText = "앱을 휴지통으로 옮기고 종료합니다. 변환 결과는 보관됩니다."
        alert.addButton(withTitle: "제거")
        alert.addButton(withTitle: "취소")
        guard alert.runModal() == .alertFirstButtonReturn else { return }
        NSWorkspace.shared.recycle([Bundle.main.bundleURL]) { _, error in
            DispatchQueue.main.async {
                if let error {
                    let failure = NSAlert(error: error)
                    failure.messageText = "앱을 휴지통으로 옮기지 못했습니다."
                    failure.runModal()
                } else { NSApp.terminate(nil) }
            }
        }
    }

    private func available(_ files: [URL], completion: @escaping ([WheelAction]) -> Void) {
        let cli = cli
        queries.async {
            let data = try? Self.invoke(cli, ["available", "--"] + files.map(Self.argument))
            let choices = data.flatMap { try? JSONDecoder().decode(Choices.self, from: $0) }
            var actions = choices?.actions.map { WheelAction(key: $0.key, label: $0.label) } ?? []
            if files.allSatisfy({ $0.isFileURL && FileManager.default.fileExists(atPath: $0.path) }) {
                actions += [WheelAction(key: "copy_path", label: "경로 복사"), WheelAction(key: "terminal", label: "터미널 열기")]
            }
            let result = actions
            let snapshots = choices?.snapshot_recipes == true
            DispatchQueue.main.async { self.snapshotRecipes = snapshots; completion(result) }
        }
    }

    private func row(_ views: [NSView]) -> NSStackView {
        let row = NSStackView(views: views)
        row.spacing = 9
        return row
    }

    private func button(_ title: String, _ action: Selector) -> NSButton {
        NSButton(title: title, target: self, action: action)
    }

    @objc private func chooseFiles() {
        let panel = NSOpenPanel()
        panel.allowsMultipleSelection = true
        panel.canChooseDirectories = true
        panel.beginSheetModal(for: window) { [weak self] answer in
            if answer == .OK { self?.setFiles(panel.urls) }
        }
    }

    @objc private func chooseFolder() {
        guard !busy else { return }
        let panel = NSOpenPanel()
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.beginSheetModal(for: window) { [weak self] answer in
            if answer == .OK {
                self?.output = panel.url
                self?.destination.stringValue = panel.url?.path ?? "원본 파일과 같은 폴더"
                self?.saveOutput()
            }
        }
    }

    @objc private func useSourceFolder() {
        guard !busy else { return }
        output = nil
        destination.stringValue = "원본 파일과 같은 폴더"
        saveOutput()
    }

    private func saveOutput() {
        var rule = profile.values["output"] as? [String: Any] ?? [:]
        rule["location"] = output == nil ? "source" : "folder"
        rule["directory"] = output?.path ?? ""
        profile.values["output"] = rule
        do { try profile.save() } catch { detail.stringValue = error.localizedDescription }
    }

    @objc private func clearFiles() { setFiles([]) }

    private func setFiles(_ urls: [URL]) {
        var seen = Set<URL>()
        files = urls.filter { ($0.isFileURL || ["https", "http"].contains($0.scheme ?? "")) && seen.insert($0).inserted }
        let files = files
        input.stringValue = files.isEmpty ? "Finder에서 파일을 이 창에 놓거나 파일을 선택하세요." :
            files.prefix(4).map(\.lastPathComponent).joined(separator: "\n") +
            (files.count > 4 ? "\n외 \(files.count - 4)개" : "")
        query = UUID()
        let token = query
        options = []
        actions.removeAllItems()
        actions.addItem(withTitle: files.isEmpty ? "파일을 선택하세요" : "가능한 변환 확인 중…")
        actions.isEnabled = false
        run.isEnabled = false
        reveal.isEnabled = false
        detail.stringValue = ""
        guard !files.isEmpty else { return }
        let cli = cli
        queries.async {
            let result = Result { try Self.invoke(cli, ["available", "--"] + files.map(Self.argument)) }
            DispatchQueue.main.async {
                guard self.query == token else { return }
                self.actions.removeAllItems()
                do {
                    let data = try result.get()
                    let choices = try JSONDecoder().decode(Choices.self, from: data)
                    self.snapshotRecipes = choices.snapshot_recipes == true
                    self.options = choices.actions
                    for action in self.options { self.actions.addItem(withTitle: action.label) }
                } catch {
                    self.detail.stringValue = "변환 작업을 확인하지 못했습니다. \(error.localizedDescription)"
                }
                if self.options.isEmpty { self.actions.addItem(withTitle: "공통 변환 작업 없음") }
                self.actions.isEnabled = !self.options.isEmpty
                self.run.isEnabled = !self.options.isEmpty
            }
        }
    }

    @objc private func convert() {
        guard options.indices.contains(actions.indexOfSelectedItem) else { return }
        let action = options[actions.indexOfSelectedItem]
        execute(WheelAction(key: action.key, label: action.label), files: files)
    }

    private func execute(_ action: WheelAction, files: [URL]) {
        guard !files.isEmpty else { return }
        if action.key == "copy_path" {
            NSPasteboard.general.clearContents()
            NSPasteboard.general.setString(files.map(\.path).joined(separator: "\n"), forType: .string)
            notify("경로 \(files.count)개 복사 완료")
            return
        }
        if action.key == "terminal" {
            var seen = Set<URL>()
            for file in files {
                var directory: ObjCBool = false
                FileManager.default.fileExists(atPath: file.path, isDirectory: &directory)
                let folder = directory.boolValue ? file : file.deletingLastPathComponent()
                guard seen.insert(folder).inserted else { continue }
                let process = Process()
                process.executableURL = URL(fileURLWithPath: "/usr/bin/open")
                process.arguments = ["-a", "Terminal", folder.path]
                do { try process.run() } catch { notify("터미널을 열지 못했습니다. \(error.localizedDescription)") }
            }
            return
        }
        var recipe = profile.presets.first { $0["id"] as? String == action.presetID } ?? [
            "id": "cli", "name": action.label, "action_key": action.key
        ]
        if recipe["output"] == nil || recipe["output"] is NSNull { recipe["output"] = profile.values["output"] }
        let job = PendingConversion(action: action, files: files, recipe: recipe, output: output)
        if busy {
            guard pending.count < 100 else { notify("대기 작업이 100건을 초과했습니다."); return }
            pending.append(job)
            notify("\(action.label) · 대기 작업 \(pending.count)건")
            return
        }
        begin(job)
    }

    private func begin(_ job: PendingConversion) {
        let action = job.action
        let files = job.files
        let cli = cli
        let requestFile = profile.directory.appendingPathComponent("request-\(UUID().uuidString).json")
        var arguments = ["convert", "--action", action.key]
        if snapshotRecipes {
            do {
                try JSONSerialization.data(withJSONObject: job.recipe).write(to: requestFile, options: .atomic)
                arguments += ["--recipe-file", requestFile.path]
            } catch { notify(error.localizedDescription); return }
        } else if let preset = action.presetID {
            arguments = ["convert", "--preset", preset]
        }
        if let output = job.output { arguments += ["--output", output.path] }
        else if files.allSatisfy({ $0.path.hasPrefix(dragInbox().path + "/") }) {
            let downloads = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Downloads/MagicConverter")
            do { try FileManager.default.createDirectory(at: downloads, withIntermediateDirectories: true) }
            catch { notify(error.localizedDescription); return }
            arguments += ["--output", downloads.path]
        }
        arguments += ["--"] + files.map(Self.argument)
        busy = true
        run.isEnabled = false
        reveal.isEnabled = false
        detail.stringValue = "변환 중…"
        notify("\(action.label) · \(files.count)개 파일 변환 중…")
        progress.startAnimation(nil)
        let request = arguments
        let progressFile = profile.directory.appendingPathComponent("progress-\(UUID().uuidString).json")
        progressTimer = Timer.scheduledTimer(withTimeInterval: 0.2, repeats: true) { [weak self] _ in
            guard let self, let data = try? Data(contentsOf: progressFile),
                  let value = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let finished = value["finished"] as? Int, let total = value["total"] as? Int else { return }
            let file = value["file"] as? String ?? ""
            let message = "\(action.label) · \(finished)/\(total)개 완료\n\(file)"
            self.detail.stringValue = message
            self.notificationText.stringValue = message
        }
        work.async {
            let response = Result {
                try JSONDecoder().decode(ResultReport.self, from: Self.invoke(cli, request,
                    environment: ["MAGIC_CONVERTER_PROGRESS": progressFile.path], started: { process in
                        DispatchQueue.main.async { self.conversionProcess = process }
                    }))
            }
            DispatchQueue.main.async {
                self.progressTimer?.invalidate()
                self.progressTimer = nil
                self.conversionProcess = nil
                try? FileManager.default.removeItem(at: progressFile)
                try? FileManager.default.removeItem(at: requestFile)
                self.progress.stopAnimation(nil)
                self.busy = false
                self.run.isEnabled = true
                do {
                    let report = try response.get()
                    let jobs = report.jobs ?? []
                    self.results = jobs.flatMap(\.outputs).map { URL(fileURLWithPath: $0) }
                    let notices = ([report.error].compactMap { $0 } + (report.errors ?? [])
                                   + jobs.flatMap(\.errors) + jobs.flatMap(\.warnings))
                    self.detail.stringValue = ([report.interrupted == true ? "변환을 취소했습니다." : (report.ok ? "변환 완료 · 결과 \(self.results.count)개" : "변환 실패")]
                                               + Array(notices.prefix(2))).joined(separator: "\n")
                    self.reveal.isEnabled = !self.results.isEmpty
                    self.notify(self.detail.stringValue)
                } catch {
                    self.detail.stringValue = "변환 결과를 읽지 못했습니다. \(error.localizedDescription)"
                    self.notify(self.detail.stringValue)
                }
                if !self.pending.isEmpty { self.begin(self.pending.removeFirst()) }
            }
        }
    }

    @objc private func revealResults() {
        NSWorkspace.shared.activateFileViewerSelecting(results)
    }

    private func checkVersion() -> Bool {
        guard let data = try? Self.invoke(cli, ["--version"]) else { return false }
        return String(decoding: data, as: UTF8.self).contains("3.0.5")
    }

    private static func argument(_ url: URL) -> String { url.isFileURL ? url.path : url.absoluteString }

    private static func invoke(_ cli: URL, _ arguments: [String], environment: [String: String] = [:], started: ((Process) -> Void)? = nil) throws -> Data {
        let process = Process()
        process.executableURL = cli
        process.arguments = arguments
        process.environment = ProcessInfo.processInfo.environment.merging(environment) { _, new in new }
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = FileHandle.nullDevice
        try process.run()
        started?(process)
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        return data
    }
}

let application = NSApplication.shared
private let delegate = App()
application.delegate = delegate
application.setActivationPolicy(.regular)
application.run()
