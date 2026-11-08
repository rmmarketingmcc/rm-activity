// RM Activity: the app for RM Activity (scripts/activity_tracker.py), built like Sales Coach: a Dock icon and a
// normal window (close, minimise, quit) with the day, the stretch going on now, meetings, clients and the week,
// plus today's time in the menu bar (a red dot while on a call). The window can stay on top or sit on the
// desktop behind other windows. Code on GitHub and "Report a problem" are in the menus.
// It only reads widget.json (written by the tracker every 30 s) and draws widget.html from its own bundle;
// it records nothing itself. Started at login with --login (menu bar only, unless pinned to the desktop).
// RM's Mac builds it with `activity_tracker.py install-widget`; the public app with build.sh.
import AppKit
import WebKit

let navy = NSColor(srgbRed: 25 / 255, green: 34 / 255, blue: 48 / 255, alpha: 1)
let repoURL = URL(string: "https://github.com/rmmarketingmcc/rm-activity")!
let websiteURL = URL(string: "https://rmmarketingmcc.github.io/rm-activity/")!
let issuesURL = URL(string: "https://github.com/rmmarketingmcc/rm-activity/issues/new")!
let appVersion = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "?"

final class ActivityApp: NSObject, NSApplicationDelegate, NSWindowDelegate, NSMenuDelegate, WKScriptMessageHandler, WKNavigationDelegate {
    let width: CGFloat = 340
    let dataURL: URL
    let htmlURL: URL
    let pageURL: URL
    let atLogin = CommandLine.arguments.contains("--login")
    var item: NSStatusItem!
    var window: NSWindow!
    var web: WKWebView!
    var json = ""
    var summary = ""   // "Today 5h 12m, 1h 10m in meetings" for the menu
    var state = "stopped"

    override init() {
        let info = Bundle.main.infoDictionary ?? [:]
        let dataDir = (((info["RMDataDir"] as? String) ?? "~/google-ads-mcp/data/activity_tracker") as NSString).expandingTildeInPath
        dataURL = URL(fileURLWithPath: dataDir).appendingPathComponent("widget.json")
        pageURL = URL(fileURLWithPath: (((info["RMPage"] as? String) ?? (dataDir + "/index.html")) as NSString).expandingTildeInPath)
        htmlURL = Bundle.main.url(forResource: "widget", withExtension: "html") ?? URL(fileURLWithPath: "/dev/null")
        super.init()
    }

    /// "normal" | "top" (floats over other windows) | "desktop" (behind every window, on the wallpaper)
    var level: String {
        get { UserDefaults.standard.string(forKey: "windowLevel") ?? "normal" }
        set { UserDefaults.standard.set(newValue, forKey: "windowLevel") }
    }

    // MARK: start

    func applicationDidFinishLaunching(_ notification: Notification) {
        if let url = Bundle.main.url(forResource: "AppIcon", withExtension: "icns"), let icon = NSImage(contentsOf: url) {
            NSApp.applicationIconImage = icon
        }
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: width, height: 560),
                          styleMask: [.titled, .closable, .miniaturizable], backing: .buffered, defer: false)
        window.title = "RM Activity"
        window.titlebarAppearsTransparent = true
        window.titleVisibility = .hidden   // the panel has its own header
        window.backgroundColor = navy
        window.appearance = NSAppearance(named: .darkAqua)
        window.isReleasedWhenClosed = false
        window.delegate = self
        web = makeWeb()
        web.autoresizingMask = [.width, .height]
        window.contentView = web
        if !window.setFrameUsingName("RMActivityWindow"), let screen = NSScreen.screens.first {  // first time: top right
            let v = screen.visibleFrame
            window.setFrameTopLeftPoint(NSPoint(x: v.maxX - width - 24, y: v.maxY - 24))
        }
        window.setFrameAutosaveName("RMActivityWindow")
        applyLevel()

        NSApp.mainMenu = mainMenu()
        item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        let menu = NSMenu()
        menu.delegate = self
        item.menu = menu
        Timer.scheduledTimer(withTimeInterval: 15, repeats: true) { [weak self] _ in self?.refresh() }
        refresh()
        if !atLogin || level == "desktop" { showWindow() }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }

    // Clicking the app in the Dock, Launchpad, Spotlight or Finder while it runs.
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        showWindow()
        return false
    }

    func makeWeb() -> WKWebView {
        let config = WKWebViewConfiguration()
        config.userContentController.add(self, name: "rm")
        let w = WKWebView(frame: NSRect(x: 0, y: 0, width: width, height: 560), configuration: config)
        w.setValue(false, forKey: "drawsBackground")
        w.navigationDelegate = self
        w.loadFileURL(htmlURL, allowingReadAccessTo: htmlURL.deletingLastPathComponent())
        return w
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) { push() }

    func push() {
        guard !json.isEmpty else { return }
        web.evaluateJavaScript("window.render && window.render(\(json))", completionHandler: nil)
    }

    // MARK: data

    func refresh() {
        guard let data = try? Data(contentsOf: dataURL), let text = String(data: data, encoding: .utf8),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            state = "stopped"
            setTitle("RM Activity", symbol: "exclamationmark.triangle", tint: nil)
            return
        }
        json = text
        let generated = obj["generated"] as? Double ?? 0
        state = Date().timeIntervalSince1970 - generated > 180 ? "stopped" : (obj["state"] as? String ?? "active")
        let title = obj["menubar"] as? String ?? "0m"
        let day = obj["day"] as? [String: Any] ?? [:]
        let cats = day["cats"] as? [Double] ?? []
        let mi = obj["meetings_cat"] as? Int
        let meet = mi.flatMap { $0 < cats.count ? cats[$0] : nil } ?? 0
        summary = "Today \(title)" + (meet >= 60 ? ", \(dur(meet)) in meetings" : "")
        if state == "stopped" {
            setTitle(title, symbol: "exclamationmark.triangle", tint: nil)
        } else if obj["in_call"] as? Bool == true {
            setTitle(title, symbol: "circle.fill", tint: .systemRed)
        } else {
            setTitle(title, symbol: nil, tint: nil)
        }
        if window.isVisible { push() }
    }

    func dur(_ secs: Double) -> String {
        let m = Int((secs / 60).rounded())
        return m < 60 ? "\(m)m" : (m % 60 == 0 ? "\(m / 60)h" : "\(m / 60)h \(m % 60)m")
    }

    /// Text only in the menu bar; a small red dot while on a call, a warning sign when the tracker stopped.
    func setTitle(_ text: String, symbol: String?, tint: NSColor?) {
        guard let button = item.button else { return }
        if let symbol = symbol {
            let size: CGFloat = symbol == "circle.fill" ? 8 : 13
            let image = NSImage(systemSymbolName: symbol, accessibilityDescription: "RM Activity")?
                .withSymbolConfiguration(NSImage.SymbolConfiguration(pointSize: size, weight: .regular))
            image?.isTemplate = tint == nil
            button.image = image
            button.imagePosition = .imageLeading
            button.title = " " + text
        } else {
            button.image = nil
            button.title = text
        }
        button.contentTintColor = tint
        button.font = NSFont.monospacedDigitSystemFont(ofSize: NSFont.systemFontSize, weight: .regular)
    }

    // MARK: window

    @objc func showWindow() {
        if window.isMiniaturized { window.deminiaturize(nil) }
        refresh()
        if level == "desktop" {
            window.orderFrontRegardless()
        } else {
            NSApp.activate(ignoringOtherApps: true)
            window.makeKeyAndOrderFront(nil)
        }
        keepOnScreen()
        push()
    }

    @objc func hideWindow() { window.orderOut(nil) }
    @objc func toggleWindow() { window.isVisible && !window.isMiniaturized ? hideWindow() : showWindow() }
    @objc func minimize() { window.miniaturize(nil) }

    func applyLevel() {
        switch level {
        case "top":
            window.level = .floating
            window.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        case "desktop":
            window.level = NSWindow.Level(rawValue: Int(CGWindowLevelForKey(.desktopIconWindow)) + 1)
            window.collectionBehavior = [.canJoinAllSpaces, .stationary]
        default:
            window.level = .normal
            window.collectionBehavior = []
        }
    }

    @objc func toggleTop() { level = level == "top" ? "normal" : "top"; applyLevel(); showWindow() }
    @objc func toggleDesktop() { level = level == "desktop" ? "normal" : "desktop"; applyLevel(); showWindow() }

    /// Keep the whole window on the screen it is mostly on (two screens, a resize, a screen unplugged).
    func keepOnScreen() {
        let f0 = window.frame
        let screen = NSScreen.screens.max(by: { area($0.frame.intersection(f0)) < area($1.frame.intersection(f0)) }) ?? NSScreen.screens.first
        guard let v = screen?.visibleFrame else { return }
        var f = f0
        f.origin.x = min(max(f.origin.x, v.minX + 8), v.maxX - f.width - 8)
        f.origin.y = min(max(f.origin.y, v.minY + 8), v.maxY - f.height - 8)
        if f != f0 { window.setFrame(f, display: true) }
    }

    func area(_ r: NSRect) -> CGFloat { r.isNull ? 0 : r.width * r.height }

    // MARK: menus

    func entry(_ title: String, _ action: Selector?, _ key: String = "", on: Bool = false) -> NSMenuItem {
        let it = NSMenuItem(title: title, action: action, keyEquivalent: key)
        it.target = self
        it.state = on ? .on : .off
        return it
    }

    func mainMenu() -> NSMenu {
        let bar = NSMenu()
        let app = NSMenu()
        app.addItem(entry("RM Activity website", #selector(openWebsite)))
        app.addItem(entry("Code on GitHub", #selector(openGitHub)))
        app.addItem(entry("Report a problem...", #selector(reportProblem)))
        app.addItem(.separator())
        app.addItem(entry("Hide RM Activity", #selector(hideWindow), "h"))
        app.addItem(.separator())
        app.addItem(entry("Quit RM Activity", #selector(quitApp), "q"))
        let view = NSMenu(title: "View")
        view.addItem(entry("Open full page", #selector(openPage), "o"))
        view.addItem(.separator())
        view.addItem(entry("Keep on top", #selector(toggleTop), "t"))
        view.addItem(entry("Pin to the desktop", #selector(toggleDesktop)))
        view.delegate = self
        let win = NSMenu(title: "Window")
        win.addItem(entry("Minimize", #selector(minimize), "m"))
        win.addItem(entry("Close", #selector(hideWindow), "w"))
        for (title, sub) in [("RM Activity", app), ("View", view), ("Window", win)] {
            let top = NSMenuItem(title: title, action: nil, keyEquivalent: "")
            top.submenu = sub
            bar.addItem(top)
        }
        return bar
    }

    // The menu bar item's menu (and the View menu's ticks) are built fresh each time they open.
    func menuNeedsUpdate(_ menu: NSMenu) {
        if menu.title == "View" {
            menu.items.first { $0.action == #selector(toggleTop) }?.state = level == "top" ? .on : .off
            menu.items.first { $0.action == #selector(toggleDesktop) }?.state = level == "desktop" ? .on : .off
            return
        }
        refresh()
        menu.removeAllItems()
        let head = entry(state == "stopped" ? "Tracking is not running" : summary, nil)
        head.isEnabled = false
        menu.addItem(head)
        menu.addItem(.separator())
        menu.addItem(entry(window.isVisible && !window.isMiniaturized ? "Hide window" : "Show window", #selector(toggleWindow)))
        menu.addItem(entry("Open full page", #selector(openPage)))
        menu.addItem(.separator())
        menu.addItem(entry("Keep on top", #selector(toggleTop), on: level == "top"))
        menu.addItem(entry("Pin to the desktop", #selector(toggleDesktop), on: level == "desktop"))
        menu.addItem(.separator())
        menu.addItem(entry("RM Activity website", #selector(openWebsite)))
        menu.addItem(entry("Code on GitHub", #selector(openGitHub)))
        menu.addItem(entry("Report a problem...", #selector(reportProblem)))
        let about = entry("RM Activity \(appVersion), made by RM Marketing", nil)
        about.isEnabled = false
        menu.addItem(about)
        menu.addItem(.separator())
        menu.addItem(entry("Quit RM Activity", #selector(quitApp)))
    }

    @objc func openPage() { NSWorkspace.shared.open(pageURL) }
    @objc func openGitHub() { NSWorkspace.shared.open(repoURL) }
    @objc func openWebsite() { NSWorkspace.shared.open(websiteURL) }
    @objc func quitApp() { NSApp.terminate(nil) }

    /// A GitHub issue with the technical facts filled in. Issues are public: never names, sites or clients.
    @objc func reportProblem() {
        let os = ProcessInfo.processInfo.operatingSystemVersionString
        let body = "What happened:\n\n\nWhat did you expect:\n\n\n---\nRM Activity \(appVersion), macOS \(os)\nTracker: \(state)\n"
        var parts = URLComponents(url: issuesURL, resolvingAgainstBaseURL: false)!
        parts.queryItems = [URLQueryItem(name: "body", value: body)]
        if let url = parts.url { NSWorkspace.shared.open(url) }
    }

    // MARK: messages from the panel

    func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage) {
        guard let body = message.body as? [String: Any], let action = body["action"] as? String else { return }
        switch action {
        case "open":
            openPage()
        case "github":
            openGitHub()
        case "website":
            openWebsite()
        case "report":
            reportProblem()
        case "size":  // the window follows the panel's height, keeping its top edge where it is
            let h = min(max(CGFloat(body["h"] as? Double ?? 560), 180), 900)
            let content = window.contentRect(forFrameRect: window.frame)
            if abs(content.height - h) > 1 {
                var f = window.frameRect(forContentRect: NSRect(x: content.minX, y: content.maxY - h, width: width, height: h))
                f.origin.y = window.frame.maxY - f.height
                window.setFrame(f, display: true)
                keepOnScreen()
            }
        default:
            break
        }
    }
}

let app = NSApplication.shared
let delegate = ActivityApp()
app.delegate = delegate
app.setActivationPolicy(.regular)   // a Dock icon: open, minimise, close like any app
app.run()
