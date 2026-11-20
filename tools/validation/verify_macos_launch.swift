import AppKit
import CoreGraphics

let appURL = URL(fileURLWithPath: CommandLine.arguments[1])
let applicationID = Bundle(url: appURL)!.bundleIdentifier!
let deadline = Date().addingTimeInterval(20)

while Date() < deadline {
    let windows = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID)
        as? [[String: Any]] ?? []
    let applications = NSRunningApplication.runningApplications(withBundleIdentifier: applicationID)
    for application in applications where application.isFinishedLaunching {
        let mainWindowVisible = windows.contains { window in
            guard (window[kCGWindowOwnerPID as String] as? NSNumber)?.int32Value == application.processIdentifier,
                  (window[kCGWindowLayer as String] as? NSNumber)?.intValue == 0,
                  let bounds = window[kCGWindowBounds as String] as? NSDictionary,
                  let frame = CGRect(dictionaryRepresentation: bounds as CFDictionary) else { return false }
            return frame.width >= 500 && frame.height >= 350
        }
        if mainWindowVisible {
            print("Launch Services started MagicConverter and WindowServer reported its main window.")
            if !CommandLine.arguments.contains("--keep-running") { application.terminate() }
            exit(0)
        }
    }
    RunLoop.current.run(until: Date().addingTimeInterval(0.2))
}

let running = NSRunningApplication.runningApplications(withBundleIdentifier: applicationID)
print("No visible MagicConverter window. Running process IDs: \(running.map(\.processIdentifier))")
exit(1)
