import AppKit
import ApplicationServices

func attribute(_ element: AXUIElement, _ name: String) -> CFTypeRef? {
    var value: CFTypeRef?
    AXUIElementCopyAttributeValue(element, name as CFString, &value)
    return value
}
func children(_ element: AXUIElement) -> [AXUIElement] {
    attribute(element, kAXChildrenAttribute) as? [AXUIElement] ?? []
}
func find(_ element: AXUIElement, title: String, depth: Int = 0) -> AXUIElement? {
    if attribute(element, kAXTitleAttribute) as? String == title { return element }
    guard depth < 10 else { return nil }
    for child in children(element) {
        if let result = find(child, title: title, depth: depth + 1) { return result }
    }
    return nil
}
func press(_ element: AXUIElement) {
    precondition(AXUIElementPerformAction(element, kAXPressAction as CFString) == .success)
}

precondition(AXIsProcessTrusted(), "Native UI test requires the CI runner's Accessibility permission")
let bundle = URL(fileURLWithPath: CommandLine.arguments[1]).standardizedFileURL
let application = NSRunningApplication.runningApplications(withBundleIdentifier: "com.handaeho.magicconverter")
    .first { $0.bundleURL?.standardizedFileURL == bundle }!
application.activate(options: [.activateAllWindows, .activateIgnoringOtherApps])
Thread.sleep(forTimeInterval: 1)
let element = AXUIElementCreateApplication(application.processIdentifier)
let menu = attribute(element, kAXMenuBarAttribute) as! AXUIElement
let appMenu = children(menu).first { (attribute($0, kAXTitleAttribute) as? String)?.contains("MagicConverter") == true }!
press(appMenu)
Thread.sleep(forTimeInterval: 0.3)
press(find(appMenu, title: "MagicConverter 제거…")!)
let deadline = Date().addingTimeInterval(10)
var confirmed = false
while Date() < deadline {
    if !confirmed, let button = find(element, title: "제거") {
        press(button)
        confirmed = true
    }
    if confirmed && !FileManager.default.fileExists(atPath: bundle.path) && application.isTerminated {
        print("PASS: native uninstall confirmation removed the installed app and exited normally.")
        exit(0)
    }
    Thread.sleep(forTimeInterval: 0.2)
}
print("FAILED: native uninstall did not finish; appExists=\(FileManager.default.fileExists(atPath: bundle.path)), confirmed=\(confirmed)")
exit(1)
