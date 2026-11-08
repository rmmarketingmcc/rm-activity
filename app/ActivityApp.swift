// RM Activity: the app for RM Activity (scripts/activity_tracker.py). The menu bar shows today's active time
// next to the RM monogram (a red dot while on a call); a click opens a panel with the day, the stretch going
// on now, meetings, clients and the week. Opening the app itself (Dock, Launchpad, Spotlight, Finder) opens
// the same panel. Right-click the menu bar item: open the full page, put the widget on the desktop (behind
// windows) or floating on top, or quit.
// It only reads widget.json (written by the tracker every 30 s) and draws widget.html from its own bundle;
// it records nothing itself. Built by `activity_tracker.py install-widget`.
import AppKit
import WebKit

let navy = NSColor(srgbRed: 25 / 255, green: 34 / 255, blue: 48 / 255, alpha: 1)

final class ActivityApp: NSObject, NSApplicationDelegate, WKScriptMessageHandler, WKNavigationDelegate {
    let width: CGFloat = 340
    let dataURL: URL
    let htmlURL: URL
    let pageURL: URL
    let startedAtLogin = CommandLine.arguments.contains("--login")
    var statusItem: NSStatusItem!
    let popover = NSPopover()
    var popWeb: WKWebView!
    var panel: NSPanel?
    var panelWeb: WKWebView?
    var json = ""
    var launched = false

    override init() {
        let info = Bundle.main.infoDictionary ?? [:]
        let dataDir = (((info["RMDataDir"] as? String) ?? "~/google-ads-mcp/data/activity_tracker") as NSString).expandingTildeInPath
        dataURL = URL(fileURLWithPath: dataDir).appendingPathComponent("widget.json")
        pageURL = URL(fileURLWithPath: (((info["RMPage"] as? String) ?? (dataDir + "/index.html")) as NSString).expandingTildeInPath)
        htmlURL = Bundle.main.url(forResource: "widget", withExtension: "html") ?? URL(fileURLWithPath: "/dev/null")
        super.init()
    }

    var mode: String {  // "off" | "desktop" | "float"
        get { UserDefaults.standard.string(forKey: "widgetMode") ?? "off" }
        set { UserDefaults.standard.set(newValue, forKey: "widgetMode") }
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let button = statusItem.button {
            button.title = " RM"
            button.imagePosition = .imageLeading
            button.target = self
            button.action = #selector(statusClicked(_:))
            button.sendAction(on: [.leftMouseUp, .rightMouseUp])
        }
        popWeb = makeWeb()
        let controller = NSViewController()
        controller.view = popWeb
        popover.contentViewController = controller
        popover.contentSize = NSSize(width: width, height: 560)
        popover.behavior = .transient
        popover.appearance = NSAppearance(named: .darkAqua)
        applyMode()
        Timer.scheduledTimer(withTimeInterval: 15, repeats: true) { [weak self] _ in self?.refresh() }
        refresh()
        launched = true
        if !startedAtLogin {  // opened by hand: show the panel right away
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.4) { [weak self] in self?.showPopover() }
        }
    }

    // Clicking the app again in the Dock, Launchpad, Spotlight or Finder while it runs.
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        if launched { showPopover() }
        return false
    }

    func makeWeb() -> WKWebView {
        let config = WKWebViewConfiguration()
        config.userContentController.add(self, name: "rm")
        let web = WKWebView(frame: NSRect(x: 0, y: 0, width: width, height: 560), configuration: config)
        web.setValue(false, forKey: "drawsBackground")
        web.navigationDelegate = self
        web.loadFileURL(htmlURL, allowingReadAccessTo: htmlURL.deletingLastPathComponent())
        return web
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        if webView === panelWeb { webView.evaluateJavaScript("document.body.classList.add('panel')", completionHandler: nil) }
        push(webView)
    }

    func push(_ web: WKWebView?) {
        guard let web = web, !json.isEmpty else { return }
        web.evaluateJavaScript("window.render && window.render(\(json))", completionHandler: nil)
    }

    func refresh() {
        guard let data = try? Data(contentsOf: dataURL), let text = String(data: data, encoding: .utf8),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            setTitle("RM", image: symbol("exclamationmark.triangle"), tint: nil)
            return
        }
        json = text
        let generated = obj["generated"] as? Double ?? 0
        let stale = Date().timeIntervalSince1970 - generated > 180
        let state = stale ? "stopped" : (obj["state"] as? String ?? "active")
        let title = obj["menubar"] as? String ?? "RM"
        if state == "stopped" {
            setTitle(title, image: symbol("exclamationmark.triangle"), tint: nil)
        } else if obj["in_call"] as? Bool == true {
            setTitle(title, image: symbol("circle.fill", size: 8), tint: .systemRed)
        } else {
            setTitle(title, image: monogram(dim: state != "active"), tint: nil)
        }
        if popover.isShown { push(popWeb) }
        push(panelWeb)
    }

    func symbol(_ name: String, size: CGFloat = 13) -> NSImage? {
        let image = NSImage(systemSymbolName: name, accessibilityDescription: "RM Activity")?
            .withSymbolConfiguration(NSImage.SymbolConfiguration(pointSize: size, weight: .regular))
        image?.isTemplate = true
        return image
    }

    /// The RM monogram as a template image (macOS colours it for the menu bar); falls back to a timer symbol.
    func monogram(dim: Bool) -> NSImage? {
        guard let url = Bundle.main.url(forResource: "menubar", withExtension: "png"), let src = NSImage(contentsOf: url) else {
            return symbol(dim ? "pause.circle" : "timer")
        }
        let h: CGFloat = 15
        let image = NSImage(size: NSSize(width: (src.size.width / max(src.size.height, 1)) * h, height: h), flipped: false) { rect in
            src.draw(in: rect, from: .zero, operation: .sourceOver, fraction: dim ? 0.45 : 1)
            return true
        }
        image.isTemplate = true
        return image
    }

    func setTitle(_ text: String, image: NSImage?, tint: NSColor?) {
        guard let button = statusItem.button else { return }
        button.image = image
        button.contentTintColor = tint
        button.title = " " + text
        button.font = NSFont.monospacedDigitSystemFont(ofSize: NSFont.systemFontSize, weight: .regular)
    }

    func showPopover() {
        guard let button = statusItem.button else { return }
        refresh()
        if !popover.isShown { popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY) }
        push(popWeb)
        NSApp.activate(ignoringOtherApps: true)
    }

    @objc func statusClicked(_ sender: NSStatusBarButton) {
        let event = NSApp.currentEvent
        if event?.type == .rightMouseUp || event?.modifierFlags.contains(.control) == true {
            statusItem.menu = buildMenu()
            sender.performClick(nil)
            statusItem.menu = nil
            return
        }
        if popover.isShown { popover.performClose(nil) } else { showPopover() }
    }

    func buildMenu() -> NSMenu {
        let menu = NSMenu()
        let open = NSMenuItem(title: "Open full page", action: #selector(openPage), keyEquivalent: "")
        open.target = self
        menu.addItem(open)
        menu.addItem(.separator())
        for (title, key) in [("Widget on the desktop", "desktop"), ("Widget floating on top", "float"), ("No widget", "off")] {
            let item = NSMenuItem(title: title, action: #selector(chooseMode(_:)), keyEquivalent: "")
            item.target = self
            item.representedObject = key
            item.state = mode == key ? .on : .off
            menu.addItem(item)
        }
        menu.addItem(.separator())
        let quit = NSMenuItem(title: "Quit RM Activity", action: #selector(quitApp), keyEquivalent: "q")
        quit.target = self
        menu.addItem(quit)
        return menu
    }

    @objc func openPage() {
        popover.performClose(nil)
        NSWorkspace.shared.open(pageURL)
    }

    @objc func chooseMode(_ sender: NSMenuItem) {
        mode = sender.representedObject as? String ?? "off"
        applyMode()
    }

    @objc func quitApp() { NSApp.terminate(nil) }

    func applyMode() {
        if mode == "off" {
            panel?.orderOut(nil)
            panel = nil
            panelWeb = nil
            return
        }
        let p = panel ?? makePanel()
        panel = p
        if mode == "float" {
            p.level = .floating
            p.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        } else {  // on the desktop: above the wallpaper and icons, below every window
            p.level = NSWindow.Level(rawValue: Int(CGWindowLevelForKey(.desktopIconWindow)) + 1)
            p.collectionBehavior = [.canJoinAllSpaces, .stationary, .ignoresCycle]
        }
        p.orderFrontRegardless()
        push(panelWeb)
    }

    func makePanel() -> NSPanel {
        let p = NSPanel(contentRect: NSRect(x: 0, y: 0, width: width, height: 560),
                        styleMask: [.titled, .fullSizeContentView, .nonactivatingPanel],
                        backing: .buffered, defer: false)
        p.titlebarAppearsTransparent = true
        p.titleVisibility = .hidden
        for kind in [NSWindow.ButtonType.closeButton, .miniaturizeButton, .zoomButton] { p.standardWindowButton(kind)?.isHidden = true }
        p.isMovableByWindowBackground = true
        p.isOpaque = false
        p.backgroundColor = navy
        p.appearance = NSAppearance(named: .darkAqua)
        p.hasShadow = true
        p.hidesOnDeactivate = false
        p.isReleasedWhenClosed = false
        let web = makeWeb()
        web.autoresizingMask = [.width, .height]
        p.contentView = web
        panelWeb = web
        if !p.setFrameUsingName("RMActivityWidget"), let screen = NSScreen.screens.first {  // the menu bar screen
            let v = screen.visibleFrame
            p.setFrameTopLeftPoint(NSPoint(x: v.maxX - width - 24, y: v.maxY - 24))
        }
        p.setFrameAutosaveName("RMActivityWidget")
        keepOnScreen(p)
        return p
    }

    /// Keep the whole widget on the screen it is mostly on (two screens, a resize, a screen unplugged).
    func keepOnScreen(_ p: NSPanel) {
        let screen = NSScreen.screens.max(by: { a, b in
            area(a.frame.intersection(p.frame)) < area(b.frame.intersection(p.frame))
        }) ?? NSScreen.screens.first
        guard let v = screen?.visibleFrame else { return }
        var f = p.frame
        f.origin.x = min(max(f.origin.x, v.minX + 8), v.maxX - f.width - 8)
        f.origin.y = min(max(f.origin.y, v.minY + 8), v.maxY - f.height - 8)
        if f != p.frame { p.setFrame(f, display: true) }
    }

    func area(_ r: NSRect) -> CGFloat { r.isNull ? 0 : r.width * r.height }

    func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage) {
        guard let body = message.body as? [String: Any], let action = body["action"] as? String else { return }
        switch action {
        case "open":
            openPage()
        case "size":
            let h = min(max(CGFloat(body["h"] as? Double ?? 560), 180), 900)
            if message.webView === popWeb {
                popover.contentSize = NSSize(width: width, height: h)
            } else if let p = panel, message.webView === panelWeb {
                var frame = p.frame
                let top = frame.maxY
                frame.size.height = h
                frame.origin.y = top - h
                p.setFrame(frame, display: true)
                keepOnScreen(p)
            }
        default:
            break
        }
    }
}

let app = NSApplication.shared
let delegate = ActivityApp()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
