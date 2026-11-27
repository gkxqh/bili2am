import AVFoundation
import AppKit
import Foundation

@MainActor
final class BackendManager: ObservableObject {
    @Published var isBackendReady = false
    @Published var errorMessage: String?
    @Published var statusMessage: String?

    @Published var config: AppConfigModel?
    @Published var result: PipelineResultModel?
    @Published var activeHistoryId: String?
    @Published var activeFailedSummary: HistorySummaryModel?
    @Published var history: [HistorySummaryModel] = []
    @Published var historyQuery: String = ""

    @Published var analyzeStatus: AnalyzeJobStatusModel?
    @Published var ollamaStatus: OllamaStatusModel?
    @Published var recommendedModels: [RecommendedModelInfo] = []
    @Published var pullStatus: PullStatusModel?

    @Published var webImportStatus: WebImportStatusModel?
    @Published var playingTrackId: String?

    @Published var urlInput: String = ""
    @Published var selectedMode: String = "auto"
    @Published var selectedStorefront: String = "cn"
    @Published var playlistName: String = ""

    private var serverProcess: Process?
    private var baseURL: URL?
    private var previewPlayer: AVPlayer?
    private var analyzePollTimer: Timer?
    private var webPollTimer: Timer?
    private var pullPollTimer: Timer?

    private var initialTarget: String?
    private var initialMode: String = "auto"
    private var initialStorefront: String?

    var filteredHistory: [HistorySummaryModel] {
        let q = historyQuery.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        guard !q.isEmpty else { return history }
        return history.filter {
            $0.title.lowercased().contains(q)
            || $0.bvid.lowercased().contains(q)
            || $0.input_url.lowercased().contains(q)
            || $0.error_message.lowercased().contains(q)
        }
    }

    var importItemById: [String: WebTrackActionModel] {
        Dictionary(uniqueKeysWithValues: (webImportStatus?.items ?? []).map { ($0.id, $0) })
    }

    var isAnalyzing: Bool {
        analyzeStatus?.running == true
    }

    init() {
        parseLaunchArguments()
        startBackend()
    }

    private func parseLaunchArguments() {
        let args = CommandLine.arguments
        var i = 1
        while i < args.count {
            let a = args[i]
            if a == "--target", i + 1 < args.count {
                initialTarget = args[i + 1]
                urlInput = args[i + 1]
                i += 2
            } else if a == "--mode", i + 1 < args.count {
                initialMode = args[i + 1]
                selectedMode = args[i + 1]
                i += 2
            } else if a == "--storefront", i + 1 < args.count {
                initialStorefront = args[i + 1]
                selectedStorefront = args[i + 1]
                i += 2
            } else {
                i += 1
            }
        }
    }

    private func findFreePort() -> Int {
        let sock = Darwin.socket(AF_INET, SOCK_STREAM, 0)
        guard sock >= 0 else { return 18765 }
        defer { Darwin.close(sock) }
        var addr = sockaddr_in()
        addr.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_port = 0
        addr.sin_addr = in_addr(s_addr: inet_addr("127.0.0.1"))
        let bindResult = withUnsafePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                Darwin.bind(sock, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
        guard bindResult == 0 else { return 18765 }
        var len = socklen_t(MemoryLayout<sockaddr_in>.size)
        withUnsafeMutablePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                _ = Darwin.getsockname(sock, $0, &len)
            }
        }
        return Int(UInt16(bigEndian: addr.sin_port))
    }

    private func resolvePythonPath() -> String {
        let hintFile = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".config/bili2am/python_path")
        if let saved = try? String(contentsOf: hintFile, encoding: .utf8)
            .trimmingCharacters(in: .whitespacesAndNewlines),
           !saved.isEmpty,
           FileManager.default.isExecutableFile(atPath: saved) {
            return saved
        }
        for candidate in [
            "/opt/anaconda3/bin/python3",
            "/opt/homebrew/bin/python3",
            "/usr/local/bin/python3",
            "/usr/bin/python3",
        ] where FileManager.default.isExecutableFile(atPath: candidate) {
            return candidate
        }
        return "/usr/bin/python3"
    }

    func startBackend() {
        let port = findFreePort()
        guard let url = URL(string: "http://127.0.0.1:\(port)") else { return }
        baseURL = url

        let pythonPath = resolvePythonPath()
        let pythonBinDir = URL(fileURLWithPath: pythonPath).deletingLastPathComponent().path
        var env = ProcessInfo.processInfo.environment
        let existingPath = env["PATH"] ?? "/usr/bin:/bin:/usr/sbin:/sbin"
        let extraBins = [pythonBinDir, "/opt/homebrew/bin", "/opt/anaconda3/bin", "/usr/local/bin"]
        let mergedPath = (extraBins + existingPath.split(separator: ":").map(String.init))
            .reduce(into: [String]()) { acc, item in
                if !item.isEmpty && !acc.contains(item) { acc.append(item) }
            }
            .joined(separator: ":")
        env["PATH"] = mergedPath

        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: pythonPath)
        proc.environment = env
        proc.arguments = ["-m", "bili2am.cli", "run", "--api-only", "--port", "\(port)"]
        proc.standardOutput = FileHandle.nullDevice
        proc.standardError = FileHandle.nullDevice

        do {
            try proc.run()
            serverProcess = proc
            Task { await waitForBackendAndInit() }
        } catch {
            errorMessage = "无法启动本地解析服务：\(error.localizedDescription)"
        }
    }

    func shutdownBackend() {
        analyzePollTimer?.invalidate()
        webPollTimer?.invalidate()
        pullPollTimer?.invalidate()
        previewPlayer?.pause()
        if let proc = serverProcess, proc.isRunning {
            proc.terminate()
        }
        serverProcess = nil
    }

    private func waitForBackendAndInit() async {
        for _ in 0..<40 {
            if await refreshState() {
                isBackendReady = true
                if let target = initialTarget, !target.isEmpty {
                    initialTarget = nil
                    await startAnalyze(url: target, mode: initialMode, storefront: initialStorefront, useCache: true)
                }
                return
            }
            try? await Task.sleep(nanoseconds: 200_000_000)
        }
        errorMessage = "本地服务启动超时，请运行 `bili2am doctor` 检查环境。"
    }

    func refreshState() async -> Bool {
        guard let baseURL else { return false }
        do {
            let (data, resp) = try await URLSession.shared.data(from: baseURL.appendingPathComponent("api/state"))
            guard (resp as? HTTPURLResponse)?.statusCode == 200 else { return false }
            let state = try JSONDecoder().decode(ServerStateResponse.self, from: data)
            config = state.config
            history = state.history
            if result == nil, let initial = state.result {
                result = initial
                playlistName = initial.playlist_name
                activeHistoryId = state.active_history_id
            }
            if initialStorefront == nil {
                selectedStorefront = state.config.storefront
            }
            ollamaStatus = state.ollama
            recommendedModels = state.recommended_models
            pullStatus = state.pull_status
            return true
        } catch {
            return false
        }
    }

    func refreshHistory() async {
        guard let baseURL else { return }
        do {
            let (data, resp) = try await URLSession.shared.data(from: baseURL.appendingPathComponent("api/history"))
            guard (resp as? HTTPURLResponse)?.statusCode == 200 else { return }
            let decoded = try JSONDecoder().decode(HistoryListResponse.self, from: data)
            history = decoded.items
        } catch {
            // ignore transient error
        }
    }

    func showHome() {
        activeHistoryId = nil
        activeFailedSummary = nil
        result = nil
        webImportStatus = nil
        analyzeStatus = nil
        errorMessage = nil
        statusMessage = nil
        urlInput = ""
    }

    func openHistoryRecord(_ id: String) async {
        guard let baseURL else { return }
        do {
            let (data, resp) = try await URLSession.shared.data(
                from: baseURL.appendingPathComponent("api/history/\(id)")
            )
            guard (resp as? HTTPURLResponse)?.statusCode == 200 else { return }
            let rec = try JSONDecoder().decode(HistoryRecordModel.self, from: data)
            activeHistoryId = rec.summary.id
            urlInput = rec.summary.input_url
            selectedMode = rec.summary.mode
            selectedStorefront = rec.summary.storefront
            errorMessage = nil
            statusMessage = nil

            if let res = rec.result {
                result = res
                playlistName = res.playlist_name
                activeFailedSummary = nil
            } else {
                result = nil
                activeFailedSummary = rec.summary
            }

            if !rec.import_items.isEmpty || !rec.import_logs.isEmpty {
                let added = rec.import_items.filter { $0.status == "added" }.count
                let failed = rec.import_items.filter { $0.status == "failed" }.count
                webImportStatus = WebImportStatusModel(
                    ok: failed == 0 && added > 0,
                    success: failed == 0 && added > 0,
                    running: false,
                    waiting_for_login: false,
                    total_count: rec.import_items.count,
                    processed_count: rec.import_items.count,
                    added_count: added,
                    failed_count: failed,
                    history_id: rec.summary.id,
                    message: rec.import_message.isEmpty ? rec.summary.error_message : rec.import_message,
                    log_path: nil,
                    logs: rec.import_logs,
                    items: rec.import_items
                )
            } else {
                webImportStatus = nil
            }
        } catch {
            errorMessage = "读取历史记录失败：\(error.localizedDescription)"
        }
    }

    func deleteHistoryRecord(_ id: String) async {
        guard let baseURL else { return }
        var req = URLRequest(url: baseURL.appendingPathComponent("api/history/\(id)"))
        req.httpMethod = "DELETE"
        _ = try? await URLSession.shared.data(for: req)
        if activeHistoryId == id {
            showHome()
        }
        await refreshHistory()
    }

    func clearAllHistory() async {
        guard let baseURL else { return }
        var req = URLRequest(url: baseURL.appendingPathComponent("api/history"))
        req.httpMethod = "DELETE"
        _ = try? await URLSession.shared.data(for: req)
        showHome()
        await refreshHistory()
    }

    func startAnalyze(
        url: String? = nil,
        mode: String? = nil,
        storefront: String? = nil,
        useCache: Bool = false,
        retryHistoryId: String? = nil
    ) async {
        guard let baseURL else { return }
        let target = (url ?? urlInput).trimmingCharacters(in: .whitespacesAndNewlines)
        guard !target.isEmpty else { return }

        errorMessage = nil
        statusMessage = nil
        activeFailedSummary = nil
        webImportStatus = nil

        var payload: [String: Any] = [
            "url": target,
            "mode": mode ?? selectedMode,
            "storefront": storefront ?? selectedStorefront,
            "use_cache": useCache,
        ]
        if let retryHistoryId {
            payload["history_id"] = retryHistoryId
        }

        do {
            var req = URLRequest(url: baseURL.appendingPathComponent("api/analyze/start"))
            req.httpMethod = "POST"
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = try JSONSerialization.data(withJSONObject: payload)

            let (data, resp) = try await URLSession.shared.data(for: req)
            guard (resp as? HTTPURLResponse)?.statusCode == 200 else {
                throw NSError(domain: "bili2am", code: 400, userInfo: [NSLocalizedDescriptionKey: "启动解析失败"])
            }
            let st = try JSONDecoder().decode(AnalyzeJobStatusModel.self, from: data)
            analyzeStatus = st
            activeHistoryId = st.history_id
            await refreshHistory()

            if st.running {
                startAnalyzePollTimer()
            } else {
                await handleAnalyzeFinished(st)
            }
        } catch {
            errorMessage = error.localizedDescription
            NotificationHelper.send(title: "bili2am 解析失败", body: error.localizedDescription)
        }
    }

    private func startAnalyzePollTimer() {
        analyzePollTimer?.invalidate()
        analyzePollTimer = Timer.scheduledTimer(withTimeInterval: 0.4, repeats: true) { [weak self] _ in
            Task { @MainActor in
                await self?.pollAnalyzeStatus()
            }
        }
    }

    private func pollAnalyzeStatus() async {
        guard let baseURL else { return }
        do {
            let (data, resp) = try await URLSession.shared.data(from: baseURL.appendingPathComponent("api/analyze/status"))
            guard (resp as? HTTPURLResponse)?.statusCode == 200 else { return }
            let st = try JSONDecoder().decode(AnalyzeJobStatusModel.self, from: data)
            analyzeStatus = st
            if !st.running {
                analyzePollTimer?.invalidate()
                await handleAnalyzeFinished(st)
            }
        } catch {
            // keep polling
        }
    }

    private func handleAnalyzeFinished(_ st: AnalyzeJobStatusModel) async {
        await refreshHistory()
        if st.stage == "failed" || !st.error.isEmpty {
            result = nil
            if let hid = st.history_id, let found = history.first(where: { $0.id == hid }) {
                activeFailedSummary = found
            }
            errorMessage = st.error.isEmpty ? "解析失败" : st.error
            NotificationHelper.send(
                title: "bili2am 解析失败",
                body: st.error.isEmpty ? "请检查视频链接或网络后重试" : st.error
            )
            return
        }

        if let res = st.result {
            result = res
            playlistName = res.playlist_name
            activeHistoryId = st.history_id
            let matchedN = res.tracks.filter { $0.status != "missing" && !$0.candidates.isEmpty }.count
            let missingN = res.tracks.filter { $0.status == "missing" || $0.candidates.isEmpty }.count
            NotificationHelper.send(
                title: "歌单解析完成",
                subtitle: res.video.title,
                body: "共 \(res.tracks.count) 首：可导入 \(matchedN) 首，未收录 \(missingN) 首"
            )
        }
    }

    func rematchTrack(id: String, artist: String, title: String) async {
        guard let baseURL, let currentResult = result,
              let idx = currentResult.tracks.firstIndex(where: { $0.id == id }) else { return }

        do {
            var bodyObj: [String: Any] = [
                "item": try JSONSerialization.jsonObject(with: JSONEncoder().encode(currentResult.tracks[idx])),
                "artist": artist,
                "title": title,
                "country": selectedStorefront,
            ]
            if let activeHistoryId {
                bodyObj["history_id"] = activeHistoryId
            }
            var req = URLRequest(url: baseURL.appendingPathComponent("api/rematch"))
            req.httpMethod = "POST"
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = try JSONSerialization.data(withJSONObject: bodyObj)

            let (data, resp) = try await URLSession.shared.data(for: req)
            guard (resp as? HTTPURLResponse)?.statusCode == 200 else { return }
            let updated = try JSONDecoder().decode(TrackMatchItemModel.self, from: data)
            result?.tracks[idx] = updated
            await refreshHistory()
        } catch {
            errorMessage = "重新检索失败：\(error.localizedDescription)"
        }
    }

    func startWebImport(retryTrackIds: [String]? = nil) async {
        guard let baseURL, let currentResult = result else { return }
        errorMessage = nil
        statusMessage = nil

        do {
            var body: [String: Any] = [
                "playlist_name": playlistName.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                    ? currentResult.video.title
                    : playlistName,
                "method": "web_apple_music",
                "tracks": try JSONSerialization.jsonObject(with: JSONEncoder().encode(currentResult.tracks)),
            ]
            if let activeHistoryId {
                body["history_id"] = activeHistoryId
            }
            if let retryTrackIds, !retryTrackIds.isEmpty {
                body["retry_track_ids"] = retryTrackIds
            }
            var req = URLRequest(url: baseURL.appendingPathComponent("api/import"))
            req.httpMethod = "POST"
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = try JSONSerialization.data(withJSONObject: body)

            let (data, resp) = try await URLSession.shared.data(for: req)
            let code = (resp as? HTTPURLResponse)?.statusCode ?? 500
            if code != 200 {
                let errObj = (try? JSONSerialization.jsonObject(with: data) as? [String: Any]) ?? [:]
                throw NSError(domain: "bili2am", code: code, userInfo: [
                    NSLocalizedDescriptionKey: (errObj["detail"] as? String) ?? "启动导入失败",
                ])
            }
            let status = try JSONDecoder().decode(WebImportStatusModel.self, from: data)
            webImportStatus = status
            if status.running {
                startWebPollTimer()
            }
        } catch {
            errorMessage = error.localizedDescription
            NotificationHelper.send(title: "Apple Music 导入失败", body: error.localizedDescription)
        }
    }

    func retryAllFailedImports() async {
        let failedIds = (webImportStatus?.items ?? [])
            .filter { $0.status == "failed" }
            .map(\.id)
        guard !failedIds.isEmpty else { return }
        await startWebImport(retryTrackIds: failedIds)
    }

    private func startWebPollTimer() {
        webPollTimer?.invalidate()
        webPollTimer = Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { [weak self] _ in
            Task { @MainActor in
                await self?.pollWebImport()
            }
        }
    }

    func pollWebImport() async {
        guard let baseURL else { return }
        do {
            let (data, resp) = try await URLSession.shared.data(from: baseURL.appendingPathComponent("api/web-am/status"))
            guard (resp as? HTTPURLResponse)?.statusCode == 200 else { return }
            let st = try JSONDecoder().decode(WebImportStatusModel.self, from: data)
            webImportStatus = st
            if !st.running {
                webPollTimer?.invalidate()
                await refreshHistory()
                let msg = st.message ?? "导入结束"
                if st.ok == false || st.success == false || (st.failed_count ?? 0) > 0 {
                    if (st.added_count ?? 0) == 0 {
                        errorMessage = msg
                    } else {
                        statusMessage = msg
                    }
                    NotificationHelper.send(
                        title: (st.added_count ?? 0) > 0 ? "Apple Music 部分导入完成" : "Apple Music 导入失败",
                        subtitle: playlistName,
                        body: msg
                    )
                } else {
                    statusMessage = msg
                    NotificationHelper.send(
                        title: "Apple Music 导入成功",
                        subtitle: playlistName,
                        body: msg
                    )
                }
            }
        } catch {
            // Keep polling on transient read failure
        }
    }

    func stopWebImport() async {
        guard let baseURL else { return }
        var req = URLRequest(url: baseURL.appendingPathComponent("api/web-am/stop"))
        req.httpMethod = "POST"
        _ = try? await URLSession.shared.data(for: req)
    }

    func copyConfirmedTxt() async {
        guard let text = await fetchExportPreview(method: "txt") else { return }
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
        statusMessage = "已复制已匹配曲目到剪贴板"
    }

    func saveExportFile(method: String) async {
        guard let text = await fetchExportPreview(method: method) else { return }
        let panel = NSSavePanel()
        let baseName = playlistName.trimmingCharacters(in: .whitespacesAndNewlines)
        panel.nameFieldStringValue = "\(baseName.isEmpty ? "bili2am_playlist" : baseName).\(method)"
        if panel.runModal() == .OK, let dest = panel.url {
            let content = method == "csv" ? "\u{FEFF}\(text)" : text
            do {
                try content.write(to: dest, atomically: true, encoding: .utf8)
                statusMessage = "已保存至 \(dest.lastPathComponent)"
            } catch {
                errorMessage = "保存失败：\(error.localizedDescription)"
            }
        }
    }

    private func fetchExportPreview(method: String) async -> String? {
        guard let baseURL, let currentResult = result else { return nil }
        do {
            let body: [String: Any] = [
                "playlist_name": playlistName,
                "method": method,
                "tracks": try JSONSerialization.jsonObject(with: JSONEncoder().encode(currentResult.tracks)),
            ]
            var req = URLRequest(url: baseURL.appendingPathComponent("api/export-preview"))
            req.httpMethod = "POST"
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = try JSONSerialization.data(withJSONObject: body)
            let (data, resp) = try await URLSession.shared.data(for: req)
            guard (resp as? HTTPURLResponse)?.statusCode == 200 else { return nil }
            let text = String(data: data, encoding: .utf8) ?? ""
            if text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                errorMessage = "没有可导出的已匹配曲目"
                return nil
            }
            return text
        } catch {
            errorMessage = "生成导出内容失败：\(error.localizedDescription)"
            return nil
        }
    }

    func togglePreview(trackId: String, previewURL: String) {
        if playingTrackId == trackId {
            previewPlayer?.pause()
            previewPlayer = nil
            playingTrackId = nil
            return
        }
        guard let url = URL(string: previewURL) else { return }
        previewPlayer?.pause()
        previewPlayer = AVPlayer(url: url)
        previewPlayer?.play()
        playingTrackId = trackId
    }

    func saveConfig(_ nextCfg: AppConfigModel) async {
        guard let baseURL else { return }
        do {
            var req = URLRequest(url: baseURL.appendingPathComponent("api/config"))
            req.httpMethod = "POST"
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = try JSONEncoder().encode(nextCfg)
            let (_, resp) = try await URLSession.shared.data(for: req)
            if (resp as? HTTPURLResponse)?.statusCode == 200 {
                config = nextCfg
                selectedStorefront = nextCfg.storefront
            }
        } catch {
            errorMessage = "保存配置失败：\(error.localizedDescription)"
        }
    }

    func pullOllamaModel(_ model: String) async {
        guard let baseURL else { return }
        do {
            var req = URLRequest(url: baseURL.appendingPathComponent("api/ollama/pull"))
            req.httpMethod = "POST"
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = try JSONSerialization.data(withJSONObject: ["model": model])
            let (data, _) = try await URLSession.shared.data(for: req)
            pullStatus = try JSONDecoder().decode(PullStatusModel.self, from: data)
            pullPollTimer?.invalidate()
            pullPollTimer = Timer.scheduledTimer(withTimeInterval: 2.0, repeats: true) { [weak self] _ in
                Task { @MainActor in
                    guard let self else { return }
                    _ = await self.refreshState()
                    if self.pullStatus?.running == false {
                        self.pullPollTimer?.invalidate()
                    }
                }
            }
        } catch {
            errorMessage = "拉取模型失败：\(error.localizedDescription)"
        }
    }
}
