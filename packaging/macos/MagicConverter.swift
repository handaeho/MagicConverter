import AppKit

private struct Conversion: Decodable {
    let key: String
    let label: String
}

private struct Choices: Decodable {
    let actions: [Conversion]
}

private struct ResultReport: Decodable {
    struct Job: Decodable {
        let outputs: [String]
        let errors: [String]
        let warnings: [String]
    }
    let ok: Bool
    let jobs: [Job]?
    let errors: [String]?
    let error: String?
}

private final class DropView: NSView {
    var onFiles: (([URL]) -> Void)?

    override init(frame: NSRect) {
        super.init(frame: frame)
        registerForDraggedTypes([.fileURL])
    }

    required init?(coder: NSCoder) { return nil }

    private func files(_ info: NSDraggingInfo) -> [URL] {
        let options: [NSPasteboard.ReadingOptionKey: Any] = [.urlReadingFileURLsOnly: true]
        return info.draggingPasteboard.readObjects(forClasses: [NSURL.self], options: options) as? [URL] ?? []
    }

    override func draggingEntered(_ sender: NSDraggingInfo) -> NSDragOperation {
        onFiles != nil && !files(sender).isEmpty ? .copy : []
    }

    override func performDragOperation(_ sender: NSDraggingInfo) -> Bool {
        guard let onFiles else { return false }
        let urls = files(sender)
        guard !urls.isEmpty else { return false }
        onFiles(urls)
        return true
    }
}

private final class App: NSObject, NSApplicationDelegate {
    private let window = NSWindow(
        contentRect: NSRect(x: 0, y: 0, width: 640, height: 420),
        styleMask: [.titled, .closable, .miniaturizable, .resizable],
        backing: .buffered, defer: false)
    private let dropView = DropView(frame: .zero)
    private let input = NSTextField(wrappingLabelWithString: "Finder에서 파일을 이 창에 놓거나 파일을 선택하세요.")
    private let destination = NSTextField(labelWithString: "원본 파일과 같은 폴더")
    private let actions = NSPopUpButton(frame: .zero, pullsDown: false)
    private let detail = NSTextField(wrappingLabelWithString: "")
    private let run = NSButton(title: "변환 시작", target: nil, action: nil)
    private let reveal = NSButton(title: "Finder에서 결과 보기", target: nil, action: nil)
    private let clear = NSButton(title: "지우기", target: nil, action: nil)
    private let progress = NSProgressIndicator()
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
        let fileItem = NSMenuItem()
        fileItem.title = "파일"
        let fileMenu = NSMenu(title: "파일")
        let open = fileMenu.addItem(withTitle: "파일 선택…", action: #selector(chooseFiles), keyEquivalent: "o")
        open.target = self
        fileItem.submenu = fileMenu
        menu.addItem(fileItem)
        NSApp.mainMenu = menu

        window.title = "MagicConverter 3.0.5"
        window.minSize = NSSize(width: 560, height: 400)
        window.contentView = dropView
        window.center()
        dropView.onFiles = { [weak self] in self?.setFiles($0) }

        let title = NSTextField(labelWithString: "MagicConverter")
        title.font = .systemFont(ofSize: 25, weight: .semibold)
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
            row([button("파일 선택…", #selector(chooseFiles)), clear]),
            NSTextField(labelWithString: "변환 작업"), actions,
            NSTextField(labelWithString: "저장 위치"), destination,
            row([button("폴더 선택…", #selector(chooseFolder)), button("원본 폴더 사용", #selector(useSourceFolder))]),
            row([run, reveal, progress]), detail
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
        if ProcessInfo.processInfo.arguments.contains("--smoke-test") {
            DispatchQueue.main.async {
                exit(self.window.isVisible && self.checkVersion() ? 0 : 1)
            }
        }
    }

    func application(_ application: NSApplication, open urls: [URL]) {
        setFiles(urls)
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { !busy }
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        busy ? .terminateCancel : .terminateNow
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
        guard !busy else { return }
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
            }
        }
    }

    @objc private func useSourceFolder() {
        guard !busy else { return }
        output = nil
        destination.stringValue = "원본 파일과 같은 폴더"
    }

    @objc private func clearFiles() { setFiles([]) }

    private func setFiles(_ urls: [URL]) {
        guard !busy else { return }
        var seen = Set<URL>()
        files = urls.filter { $0.isFileURL && seen.insert($0.standardizedFileURL).inserted }
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
        work.async {
            let result = Result { try Self.invoke(cli, ["available", "--"] + files.map(\.path)) }
            DispatchQueue.main.async {
                guard self.query == token else { return }
                self.actions.removeAllItems()
                do {
                    let data = try result.get()
                    self.options = try JSONDecoder().decode(Choices.self, from: data).actions
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
        guard !busy, options.indices.contains(actions.indexOfSelectedItem) else { return }
        let action = options[actions.indexOfSelectedItem]
        let cli = cli
        var arguments = ["convert", "--action", action.key]
        if let output { arguments += ["--output", output.path] }
        arguments += ["--"] + files.map(\.path)
        busy = true
        run.isEnabled = false
        reveal.isEnabled = false
        detail.stringValue = "변환 중…"
        progress.startAnimation(nil)
        let request = arguments
        work.async {
            let response = Result { try JSONDecoder().decode(ResultReport.self, from: Self.invoke(cli, request)) }
            DispatchQueue.main.async {
                self.progress.stopAnimation(nil)
                self.busy = false
                self.run.isEnabled = true
                do {
                    let report = try response.get()
                    let jobs = report.jobs ?? []
                    self.results = jobs.flatMap(\.outputs).map { URL(fileURLWithPath: $0) }
                    let notices = ([report.error].compactMap { $0 } + (report.errors ?? [])
                                   + jobs.flatMap(\.errors) + jobs.flatMap(\.warnings))
                    self.detail.stringValue = ([report.ok ? "변환 완료 · 결과 \(self.results.count)개" : "변환 실패"]
                                               + Array(notices.prefix(2))).joined(separator: "\n")
                    self.reveal.isEnabled = !self.results.isEmpty
                } catch {
                    self.detail.stringValue = "변환 결과를 읽지 못했습니다. \(error.localizedDescription)"
                }
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

    private static func invoke(_ cli: URL, _ arguments: [String]) throws -> Data {
        let process = Process()
        process.executableURL = cli
        process.arguments = arguments
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = FileHandle.nullDevice
        try process.run()
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        return data
    }
}

let application = NSApplication.shared
let delegate = App()
application.delegate = delegate
application.setActivationPolicy(.regular)
application.run()
