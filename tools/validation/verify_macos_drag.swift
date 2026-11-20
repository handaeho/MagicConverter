import AppKit
import CoreGraphics
import WebKit

private final class SourceView: NSView, NSDraggingSource, NSFilePromiseProviderDelegate {
    let file: URL
    let delivery: String
    init(file: URL, delivery: String) {
        self.file = file
        self.delivery = delivery
        super.init(frame: NSRect(x: 0, y: 0, width: 260, height: 200))
    }
    required init?(coder: NSCoder) { nil }
    override func draw(_ rect: NSRect) {
        NSColor.white.setFill()
        bounds.fill()
        NSWorkspace.shared.icon(forFile: file.path).draw(in: NSRect(x: 90, y: 70, width: 80, height: 80))
    }
    override func mouseDown(with event: NSEvent) { }
    override func mouseDragged(with event: NSEvent) {
        if delivery == "plain" { return }
        let writer: NSPasteboardWriting
        if delivery == "promise" {
            writer = NSFilePromiseProvider(fileType: "public.png", delegate: self)
        } else if delivery == "raw" {
            let board = NSPasteboardItem()
            board.setData(try! Data(contentsOf: file), forType: .png)
            writer = board
        } else { writer = file as NSURL }
        let item = NSDraggingItem(pasteboardWriter: writer)
        item.setDraggingFrame(NSRect(x: 95, y: 75, width: 64, height: 64), contents: NSWorkspace.shared.icon(forFile: file.path))
        beginDraggingSession(with: [item], event: event, source: self)
    }
    func draggingSession(_ session: NSDraggingSession, sourceOperationMaskFor context: NSDraggingContext) -> NSDragOperation {
        [.copy, .generic, .link]
    }
    func filePromiseProvider(_ filePromiseProvider: NSFilePromiseProvider, fileNameForType fileType: String) -> String {
        "promised.png"
    }
    func filePromiseProvider(_ filePromiseProvider: NSFilePromiseProvider, writePromiseTo url: URL,
                             completionHandler: @escaping (Error?) -> Void) {
        do { try FileManager.default.copyItem(at: file, to: url); completionHandler(nil) }
        catch { completionHandler(error) }
    }
}

private func windows() -> [[String: Any]] {
    CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]] ?? []
}

private func wheelFrame() -> CGRect? {
    let applications = NSRunningApplication.runningApplications(withBundleIdentifier: "com.handaeho.magicconverter")
    let pids = Set(applications.map(\.processIdentifier))
    for window in windows() {
        guard let pid = (window[kCGWindowOwnerPID as String] as? NSNumber)?.int32Value, pids.contains(pid),
              let bounds = window[kCGWindowBounds as String] as? NSDictionary,
              let frame = CGRect(dictionaryRepresentation: bounds as CFDictionary),
              abs(frame.width - 350) < 2 && abs(frame.height - 350) < 2 else { continue }
        return frame
    }
    return nil
}

private func post(_ type: CGEventType, _ point: CGPoint, flags: CGEventFlags = []) {
    let event = CGEvent(mouseEventSource: nil, mouseType: type, mouseCursorPosition: point, mouseButton: .left)!
    event.flags = flags
    event.post(tap: .cghidEventTap)
}

private func key(_ code: CGKeyCode, down: Bool, flags: CGEventFlags) {
    let event = CGEvent(keyboardEventSource: nil, virtualKey: code, keyDown: down)!
    event.flags = flags
    event.post(tap: .cghidEventTap)
}

let arguments = CommandLine.arguments
let file = URL(fileURLWithPath: arguments[1])
let quick = arguments[2] == "control"
let finder = arguments.contains("--finder")
let browser = arguments.contains("--web")
let delivery = arguments.contains("--plain") ? "plain" : (arguments.contains("--promise") ? "promise" : (arguments.contains("--raw") ? "raw" : "file"))
let transient = browser || delivery != "file"
let resultPath = URL(fileURLWithPath: arguments[3])
let existing = Set(((try? FileManager.default.contentsOfDirectory(atPath: resultPath.path)) ?? []))
let cli = Process()
cli.executableURL = URL(fileURLWithPath: arguments[4])
cli.arguments = ["available", "--", file.path]
let cliOutput = Pipe()
cli.standardOutput = cliOutput
try cli.run()
let data = cliOutput.fileHandleForReading.readDataToEndOfFile()
cli.waitUntilExit()
let choices = (try JSONSerialization.jsonObject(with: data) as! [String: Any])["actions"] as! [[String: Any]]
let slots = choices.filter { action in
    let key = action["key"] as! String
    let tool = key.hasPrefix("tool:")
    return quick ? tool || ["strip", "zip"].contains(key) : !tool && key != "zip"
}
let wanted = quick ? "strip" : "image:jpg"
guard let slot = slots.firstIndex(where: { $0["key"] as? String == wanted }) else {
    fatalError("The engine did not offer \(wanted)")
}
let slotCount = min(12, slots.count + (quick ? 2 : 0))
precondition(slot < 12)
let app = NSApplication.shared
app.setActivationPolicy(.regular)
let source = NSWindow(contentRect: NSRect(x: 35, y: 120, width: 260, height: 200),
                      styleMask: [.titled], backing: .buffered, defer: false)
source.title = "Native drag test source"
source.contentView = SourceView(file: file, delivery: delivery)
if browser {
    let view = WKWebView(frame: NSRect(x: 0, y: 0, width: 260, height: 200))
    source.contentView = view
    source.title = "WebKit browser image drag"
    let bytes = try Data(contentsOf: file).base64EncodedString()
    view.loadHTMLString("<html><body style='margin:0'><img draggable='true' style='position:absolute;left:90px;top:60px;width:80px;height:80px' src='data:image/png;base64,\(bytes)'></body></html>", baseURL: nil)
}
if finder {
    let automation = Process()
    automation.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
    automation.arguments = ["-e", """
        on run argv
            tell application "Finder"
                activate
                set sourceFolder to POSIX file (item 1 of argv) as alias
                open sourceFolder
                set w to front Finder window
                set current view of w to icon view
                set toolbar visible of w to false
                set bounds of w to {40, 80, 400, 400}
                set icon size of icon view options of w to 64
                set arrangement of icon view options of w to not arranged
                set sourceFile to item (item 2 of argv) of sourceFolder
                set position of sourceFile to {100, 100}
                set selection to {sourceFile}
            end tell
        end run
        """, file.deletingLastPathComponent().path, file.lastPathComponent]
    try automation.run()
    automation.waitUntilExit()
    precondition(automation.terminationStatus == 0, "Finder could not prepare the test file")
} else {
    source.makeKeyAndOrderFront(nil)
    app.activate(ignoringOtherApps: true)
}

DispatchQueue.global().async {
    Thread.sleep(forTimeInterval: browser ? 3 : 1)
    let height = CGDisplayBounds(CGMainDisplayID()).height
    let start = finder ? CGPoint(x: 140, y: 202) : CGPoint(x: 165, y: height - 220)
    let cursor = CGPoint(x: 510, y: height - 420)
    let flags: CGEventFlags = quick ? .maskControl : .maskShift
    let code: CGKeyCode = quick ? 59 : 56
    post(.mouseMoved, start)
    post(.leftMouseDown, start)
    Thread.sleep(forTimeInterval: 0.2)
    for index in 1...20 {
        let fraction = CGFloat(index) / 20
        post(.leftMouseDragged, CGPoint(x: start.x + (cursor.x - start.x) * fraction,
                                       y: start.y + (cursor.y - start.y) * fraction))
        Thread.sleep(forTimeInterval: 0.025)
    }
    key(code, down: true, flags: flags)
    post(.leftMouseDragged, cursor, flags: flags)
    if delivery == "plain" {
        Thread.sleep(forTimeInterval: 2)
        let displayed = wheelFrame() != nil
        post(.leftMouseUp, cursor)
        key(code, down: false, flags: [])
        precondition(!displayed, "An ordinary modifier drag reused a previous file drag")
        print("PASS: ordinary Shift text-selection gestures did not display the file wheel.")
        exit(0)
    }
    var wheel: CGRect?
    for _ in 0..<100 {
        Thread.sleep(forTimeInterval: 0.1)
        if let frame = wheelFrame() { wheel = frame; break }
    }
    guard let wheel else {
        post(.leftMouseUp, cursor)
        key(code, down: false, flags: [])
        print("FAILED: no wheel appeared during a real cross-process \(arguments[2]) file drag. Accessibility trusted: \(AXIsProcessTrusted())")
        exit(1)
    }
    let angle = Double(slot) * 2 * .pi / Double(slotCount)
    let target = CGPoint(x: wheel.midX + sin(angle) * 117, y: wheel.midY - cos(angle) * 117)
    for index in 1...12 {
        let fraction = CGFloat(index) / 12
        post(.leftMouseDragged, CGPoint(x: cursor.x + (target.x - cursor.x) * fraction,
                                       y: cursor.y + (target.y - cursor.y) * fraction), flags: flags)
        Thread.sleep(forTimeInterval: 0.05)
    }
    Thread.sleep(forTimeInterval: 1.5)
    post(.leftMouseUp, target, flags: flags)
    key(code, down: false, flags: [])
    for _ in 0..<50 {
        Thread.sleep(forTimeInterval: 0.1)
        let created = transient
            ? ((try? FileManager.default.contentsOfDirectory(at: resultPath, includingPropertiesForKeys: nil)) ?? []).contains { $0.pathExtension == "jpg" && !existing.contains($0.lastPathComponent) }
            : FileManager.default.fileExists(atPath: resultPath.path)
        if created {
            print("PASS: \(arguments[2]) cross-process file drag displayed the wheel and created \(resultPath.lastPathComponent).")
            exit(0)
        }
    }
    print("FAILED: wheel appeared but no output was created at \(resultPath.path).")
    print((try? FileManager.default.contentsOfDirectory(atPath: file.deletingLastPathComponent().path)) ?? [])
    exit(1)
}
app.run()
