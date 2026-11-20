import AppKit
import UniformTypeIdentifiers

struct WheelAction: Equatable {
    let key: String
    let label: String
    var presetID: String? = nil
}

enum WheelMode: String {
    case convert, quick

    static func from(_ flags: NSEvent.ModifierFlags) -> WheelMode? {
        guard flags.intersection([.command, .option]).isEmpty else { return nil }
        if flags.contains(.control) { return .quick }
        return flags.contains(.shift) ? .convert : nil
    }

    func includes(_ action: WheelAction) -> Bool {
        let tool = action.key.hasPrefix("tool:")
        let quick = ["strip", "zip", "copy_path", "terminal"].contains(action.key)
        return self == .quick ? tool || quick : !tool && !["zip", "copy_path", "terminal"].contains(action.key)
    }
}

private func directFiles(_ board: NSPasteboard) -> [URL] {
    let urls = (board.readObjects(forClasses: [NSURL.self], options: nil) as? [URL] ?? [])
        .filter { $0.isFileURL || ["https", "http"].contains($0.scheme?.lowercased() ?? "") }
    if !urls.isEmpty { return urls }
    let paths = board.propertyList(forType: NSPasteboard.PasteboardType("NSFilenamesPboardType")) as? [String] ?? []
    return paths.map { URL(fileURLWithPath: $0) }
}

private func promises(_ board: NSPasteboard) -> [NSFilePromiseReceiver] {
    board.readObjects(forClasses: [NSFilePromiseReceiver.self], options: nil) as? [NSFilePromiseReceiver] ?? []
}

func dragInbox() -> URL {
    let profile = ProcessInfo.processInfo.environment["MAGIC_CONVERTER_PROFILE_DIR"].map { URL(fileURLWithPath: $0) }
        ?? FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/MagicConverter/3.0/profile")
    return profile.appendingPathComponent("DragInbox")
}

func draggedFiles(_ board: NSPasteboard) -> [URL] {
    let direct = directFiles(board)
    if !direct.isEmpty { return direct }
    let types = promises(board).flatMap(\.fileTypes)
    if !types.isEmpty {
        return types.enumerated().map { index, type in
            let suffix = UTType(type)?.preferredFilenameExtension ?? "bin"
            return dragInbox().appendingPathComponent("pending-\(index).\(suffix)")
        }
    }
    let suffix = board.types?.contains(.png) == true ? "png" : (board.types?.contains(.tiff) == true ? "tiff" : nil)
    return suffix.map { [dragInbox().appendingPathComponent("pending.\($0)")] } ?? []
}

func receiveDrag(_ board: NSPasteboard, completion: @escaping (Result<[URL], Error>) -> Void) {
    let direct = directFiles(board)
    if !direct.isEmpty { completion(.success(direct)); return }
    let folder = dragInbox().appendingPathComponent(UUID().uuidString)
    do { try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true) }
    catch { completion(.failure(error)); return }
    let receivers = promises(board)
    if !receivers.isEmpty {
        let count = receivers.reduce(0) { $0 + $1.fileTypes.count }
        guard (1...256).contains(count) else {
            completion(.failure(NSError(domain: "MagicConverter", code: 1, userInfo: [NSLocalizedDescriptionKey: "드래그 파일 수를 확인하세요."])))
            return
        }
        let queue = OperationQueue()
        queue.maxConcurrentOperationCount = 1
        var files: [URL] = []
        var errors: [Error] = []
        var received = 0
        for receiver in receivers {
            receiver.receivePromisedFiles(atDestination: folder, options: [:], operationQueue: queue) { file, error in
                received += 1
                if let error { errors.append(error) }
                else {
                    let path = file.standardizedFileURL
                    let valid = path.path.hasPrefix(folder.standardizedFileURL.path + "/")
                        && ((try? path.resourceValues(forKeys: [.isRegularFileKey, .isSymbolicLinkKey, .fileSizeKey]))
                            .map { $0.isRegularFile == true && $0.isSymbolicLink != true && ($0.fileSize ?? 0) <= 32 * 1024 * 1024 } ?? false)
                    if valid { files.append(path) }
                    else { errors.append(NSError(domain: "MagicConverter", code: 1, userInfo: [NSLocalizedDescriptionKey: "드래그 파일의 크기와 경로를 확인하세요."])) }
                }
                if received == count {
                    let result: Result<[URL], Error> = errors.first.map { .failure($0) } ?? .success(files)
                    DispatchQueue.main.async { completion(result) }
                }
            }
        }
        return
    }
    do {
        let type: NSPasteboard.PasteboardType = board.types?.contains(.png) == true ? .png : .tiff
        guard let data = board.data(forType: type), !data.isEmpty, data.count <= 32 * 1024 * 1024 else {
            throw NSError(domain: "MagicConverter", code: 1, userInfo: [NSLocalizedDescriptionKey: "드래그 이미지 데이터는 32 MiB까지 지원합니다."])
        }
        let file = folder.appendingPathComponent(type == .png ? "image.png" : "image.tiff")
        try data.write(to: file, options: .atomic)
        completion(.success([file]))
    } catch { completion(.failure(error)) }
}

let dragTypes: [NSPasteboard.PasteboardType] = [.fileURL, .URL, .png, .tiff, NSPasteboard.PasteboardType("NSFilenamesPboardType")]
    + NSFilePromiseReceiver.readableDraggedTypes.map { NSPasteboard.PasteboardType($0) }

final class WheelView: NSView {
    var actions: [WheelAction] = [] { didSet { needsDisplay = true } }
    var heading = "Shift 변환" { didSet { needsDisplay = true } }
    var message = "파일을\n가져오세요" { didSet { needsDisplay = true } }
    var requireRelease = false
    var modifierReleased = false
    var onFiles: (([URL]) -> Void)?
    var onChoose: ((WheelAction, [URL]) -> Void)?
    var onFinish: (() -> Void)?
    var onError: ((Error) -> Void)?
    var selected: Int? { didSet { needsDisplay = true } }
    private var received: [URL] = []

    override init(frame: NSRect) {
        super.init(frame: frame)
        registerForDraggedTypes(dragTypes)
        setAccessibilityIdentifier("magicconverter-wheel")
        setAccessibilityLabel("변환 작업 휠")
    }

    required init?(coder: NSCoder) { nil }

    func slot(at point: NSPoint) -> Int? {
        let dx = point.x - bounds.midX
        let dy = point.y - bounds.midY
        guard !actions.isEmpty, (64...168).contains(hypot(dx, dy)) else { return nil }
        let step = 2 * Double.pi / Double(actions.count)
        var angle = Double.pi / 2 - atan2(dy, dx) + step / 2
        angle = angle.truncatingRemainder(dividingBy: 2 * .pi)
        if angle < 0 { angle += 2 * .pi }
        return min(Int(angle / step), actions.count - 1)
    }

    override func draw(_ dirtyRect: NSRect) {
        let center = NSPoint(x: bounds.midX, y: bounds.midY)
        NSColor(calibratedWhite: 0.14, alpha: 0.98).setFill()
        NSBezierPath(ovalIn: bounds.insetBy(dx: 3, dy: 3)).fill()
        let count = actions.count
        if count > 0 {
            let step = 360.0 / Double(count)
            for (index, action) in actions.enumerated() {
                let angle = 90 - Double(index) * step
                let path = NSBezierPath()
                path.move(to: center)
                path.appendArc(withCenter: center, radius: 168, startAngle: CGFloat(angle + step / 2 - 0.7),
                               endAngle: CGFloat(angle - step / 2 + 0.7), clockwise: true)
                path.close()
                (index == selected ? NSColor.systemOrange : NSColor(calibratedRed: 0.53, green: 0.28, blue: 0.17, alpha: 1)).setFill()
                path.fill()
                let radians = angle * .pi / 180
                let x = center.x + cos(radians) * 117
                let y = center.y + sin(radians) * 117
                let width = min(104, max(32, 2 * 112 * sin(.pi / Double(count)) - 18))
                let label = NSRect(x: x - width / 2, y: y - 20, width: width, height: 40)
                let paragraph = NSMutableParagraphStyle()
                paragraph.alignment = .center
                paragraph.lineBreakMode = .byTruncatingTail
                let attributes: [NSAttributedString.Key: Any] = [
                    .font: NSFont.systemFont(ofSize: count > 9 ? 11 : 12, weight: .medium),
                    .foregroundColor: NSColor.white, .paragraphStyle: paragraph
                ]
                NSGraphicsContext.saveGraphicsState()
                path.addClip()
                (action.label as NSString).draw(with: label, options: [.usesLineFragmentOrigin], attributes: attributes)
                NSGraphicsContext.restoreGraphicsState()
            }
        }
        NSColor(calibratedWhite: 0.14, alpha: 1).setFill()
        NSBezierPath(ovalIn: NSRect(x: center.x - 61, y: center.y - 61, width: 122, height: 122)).fill()
        let paragraph = NSMutableParagraphStyle()
        paragraph.alignment = .center
        let caption = selected.flatMap { actions.indices.contains($0) ? actions[$0].label : nil } ?? message
        let text = heading + "\n" + caption
        (text as NSString).draw(with: NSRect(x: center.x - 53, y: center.y - 35, width: 106, height: 70),
                              options: [.usesLineFragmentOrigin], attributes: [
                                .font: NSFont.systemFont(ofSize: 12), .foregroundColor: NSColor.white,
                                .paragraphStyle: paragraph
                              ])
    }

    override func draggingEntered(_ sender: NSDraggingInfo) -> NSDragOperation {
        received = draggedFiles(sender.draggingPasteboard)
        guard !received.isEmpty else { return [] }
        onFiles?(received)
        return update(sender)
    }

    override func draggingUpdated(_ sender: NSDraggingInfo) -> NSDragOperation { update(sender) }
    override func wantsPeriodicDraggingUpdates() -> Bool { true }

    private func update(_ sender: NSDraggingInfo) -> NSDragOperation {
        selected = slot(at: convert(sender.draggingLocation, from: nil))
        let reserved = !NSEvent.modifierFlags.intersection([.command, .option]).isEmpty
        guard !reserved, !received.isEmpty else { return [] }
        if requireRelease && !modifierReleased { return [] }
        // Generic preserves the drag's source files even when Finder maps Control to a link.
        let mask = sender.draggingSourceOperationMask
        if mask.contains(.copy) { return .copy }
        if mask.contains(.generic) { return .generic }
        return mask.contains(.link) ? .link : []
    }

    override func prepareForDragOperation(_ sender: NSDraggingInfo) -> Bool {
        !update(sender).isEmpty && selected != nil
    }

    override func performDragOperation(_ sender: NSDraggingInfo) -> Bool {
        guard !update(sender).isEmpty, let selected, actions.indices.contains(selected) else { return false }
        let action = actions[selected]
        onFinish?()
        receiveDrag(sender.draggingPasteboard) { [weak self] result in
            switch result {
            case .success(let files): self?.onChoose?(action, files)
            case .failure(let error): self?.onError?(error)
            }
        }
        return true
    }

    override func draggingExited(_ sender: NSDraggingInfo?) { selected = nil }
    override func draggingEnded(_ sender: NSDraggingInfo) { onFinish?() }
}

final class DragWheel {
    let view = WheelView(frame: NSRect(x: 0, y: 0, width: 350, height: 350))
    let panel = NSPanel(contentRect: NSRect(x: 0, y: 0, width: 350, height: 350),
                        styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
    var enabled = true
    var requireRelease = false
    var threshold: CGFloat = 15
    var interval: TimeInterval = 0.035
    var onChoose: ((WheelAction, [URL]) -> Void)?
    var onQuery: (([URL], WheelMode, @escaping ([WheelAction]) -> Void) -> Void)?
    private var timer: Timer?
    private var mouseDownMonitor: Any?
    private var origin: NSPoint?
    private var mode: WheelMode?
    private var source: [URL] = []
    private var consumed = false
    private var reserved = false
    private var request = UUID()
    private var releaseDeadline: Date?

    init() {
        panel.contentView = view
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = true
        panel.level = .statusBar
        panel.hidesOnDeactivate = false
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        panel.isReleasedWhenClosed = false
        view.onFiles = { [weak self] files in self?.query(files) }
        view.onFinish = { [weak self] in self?.finish() }
        view.onChoose = { [weak self] action, files in self?.onChoose?(action, files) }
    }

    func start() {
        if mouseDownMonitor == nil {
            mouseDownMonitor = NSEvent.addGlobalMonitorForEvents(matching: .leftMouseDown) { [weak self] event in
                if let point = event.cgEvent?.location, let top = NSScreen.screens.first?.frame.maxY {
                    self?.origin = NSPoint(x: point.x, y: top - point.y)
                } else { self?.origin = NSEvent.mouseLocation }
            }
        }
        timer?.invalidate()
        timer = Timer(timeInterval: interval, repeats: true) { [weak self] _ in self?.poll() }
        RunLoop.main.add(timer!, forMode: .common)
    }

    deinit {
        timer?.invalidate()
        if let mouseDownMonitor { NSEvent.removeMonitor(mouseDownMonitor) }
    }

    func finish() {
        panel.orderOut(nil)
        consumed = true
        request = UUID()
        source = []
        mode = nil
    }

    private func poll() {
        let pressed = NSEvent.pressedMouseButtons & 1 != 0
        let flags = NSEvent.modifierFlags
        let point = NSEvent.mouseLocation
        if !flags.intersection([.command, .option]).isEmpty { reserved = true }
        if !pressed {
            if panel.isVisible {
                if releaseDeadline == nil { releaseDeadline = Date().addingTimeInterval(0.35) }
                if Date() >= releaseDeadline! { finish() }
            }
            origin = nil
            if WheelMode.from(flags) == nil { reserved = false }
            consumed = false
            return
        }
        releaseDeadline = nil
        if origin == nil { origin = point }
        guard enabled, !reserved, !consumed else {
            if panel.isVisible { finish() }
            return
        }
        let current = WheelMode.from(flags)
        if current == nil {
            if panel.isVisible && requireRelease && view.selected != nil {
                view.modifierReleased = true
            } else if panel.isVisible { finish() }
            return
        }
        guard let current, let origin, hypot(point.x - origin.x, point.y - origin.y) >= threshold else { return }
        if !panel.isVisible {
            let board = NSPasteboard(name: .drag)
            guard board.types?.contains(where: { dragTypes.contains($0) }) == true else { return }
            mode = current
            source = []
            view.actions = []
            view.message = "확인 중…"
            view.modifierReleased = false
            view.requireRelease = requireRelease
            view.heading = current == .convert ? "Shift 변환" : "Ctrl 빠른 작업"
            let screen = NSScreen.screens.first { NSMouseInRect(point, $0.frame, false) } ?? NSScreen.main!
            let frame = screen.visibleFrame
            let x = min(max(point.x - 175, frame.minX), frame.maxX - 350)
            let y = min(max(point.y - 175, frame.minY), frame.maxY - 350)
            panel.setFrameOrigin(NSPoint(x: x, y: y))
            panel.orderFrontRegardless()
            query(draggedFiles(board))
        } else if mode != current {
            mode = current
            view.modifierReleased = false
            view.heading = current == .convert ? "Shift 변환" : "Ctrl 빠른 작업"
            let files = source
            source = []
            query(files)
        }
    }

    private func query(_ files: [URL]) {
        guard !files.isEmpty, let mode, source != files else { return }
        source = files
        let token = UUID()
        request = token
        view.actions = []
        onQuery?(files, mode) { [weak self] actions in
            guard let self, self.request == token, self.panel.isVisible else { return }
            self.view.actions = Array(actions.prefix(12))
            self.view.message = self.view.actions.isEmpty ? "가능한 작업\n없음" : "항목 위에서\n놓으세요"
        }
    }
}
