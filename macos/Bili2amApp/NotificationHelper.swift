import Foundation

enum NotificationHelper {
    /// Send a native macOS notification banner (works reliably in ad-hoc .app bundles).
    static func send(title: String, subtitle: String = "", body: String) {
        let script = """
        on run argv
            set nTitle to item 1 of argv
            set nSub to item 2 of argv
            set nBody to item 3 of argv
            if nSub is "" then
                display notification nBody with title nTitle
            else
                display notification nBody with title nTitle subtitle nSub
            end if
        end run
        """
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
        proc.arguments = ["-e", script, title, subtitle, body]
        proc.standardOutput = FileHandle.nullDevice
        proc.standardError = FileHandle.nullDevice
        try? proc.run()
    }
}
