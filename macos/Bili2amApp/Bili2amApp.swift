import AppKit
import SwiftUI

final class AppDelegate: NSObject, NSApplicationDelegate {
    weak var backend: BackendManager?

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        NSApp.activate(ignoringOtherApps: true)
    }

    func applicationWillTerminate(_ notification: Notification) {
        if let backend {
            Task { @MainActor in
                backend.shutdownBackend()
            }
        }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
    }
}

@main
struct Bili2amApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var appDelegate
    @StateObject private var backend = BackendManager()

    var body: some Scene {
        WindowGroup("bili2am") {
            ContentView(backend: backend)
                .onAppear {
                    appDelegate.backend = backend
                }
        }
        .windowToolbarStyle(.unified)
        .defaultSize(width: 1060, height: 680)
    }
}
