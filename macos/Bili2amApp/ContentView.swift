import SwiftUI

struct CleanInputField: View {
    let placeholder: String
    @Binding var text: String
    var onSubmit: (() -> Void)? = nil

    var body: some View {
        TextField(placeholder, text: $text)
            .textFieldStyle(.plain)
            .focusEffectDisabled()
            .onSubmit {
                onSubmit?()
            }
    }
}

struct ContentView: View {
    @ObservedObject var backend: BackendManager
    @State private var filter: TrackFilter = .all
    @State private var showSettings = false
    @State private var showImportLog = false
    @State private var confirmClearHistory = false

    private var tracks: [TrackMatchItemModel] {
        backend.result?.tracks ?? []
    }

    private var filteredTracks: [TrackMatchItemModel] {
        tracks.filter { item in
            switch filter {
            case .all: return true
            case .matched: return item.status == "matched"
            case .uncertain: return item.status == "uncertain"
            case .missing: return item.status == "missing"
            }
        }
    }

    private var matchedCount: Int { tracks.filter { $0.status == "matched" }.count }
    private var uncertainCount: Int { tracks.filter { $0.status == "uncertain" }.count }
    private var missingCount: Int { tracks.filter { $0.status == "missing" }.count }
    private var selectedImportCount: Int {
        tracks.filter { $0.selected && $0.status != "missing" && !$0.candidates.isEmpty }.count
    }

    var body: some View {
        NavigationSplitView {
            sidebarView
                .navigationSplitViewColumnWidth(min: 220, ideal: 245, max: 300)
        } detail: {
            detailArea
        }
        .frame(minWidth: 960, minHeight: 620)
        .toolbar {
            ToolbarItem(placement: .navigation) {
                Button {
                    backend.showHome()
                } label: {
                    Image(systemName: "plus")
                }
                .help("新建解析")
            }

            if backend.result != nil || backend.activeFailedSummary != nil {
                ToolbarItem(placement: .principal) {
                    HStack(spacing: 8) {
                        HStack(spacing: 6) {
                            Image(systemName: "link")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                            CleanInputField(placeholder: "B 站链接或 BV 号", text: $backend.urlInput) {
                                Task { await backend.startAnalyze(useCache: false) }
                            }
                            .frame(minWidth: 210, idealWidth: 290)
                        }
                        .padding(.horizontal, 10)
                        .padding(.vertical, 5)
                        .background(
                            RoundedRectangle(cornerRadius: 8)
                                .fill(Color(nsColor: .controlBackgroundColor))
                        )
                        .overlay(
                            RoundedRectangle(cornerRadius: 8)
                                .strokeBorder(Color(nsColor: .separatorColor), lineWidth: 1)
                        )

                        Button("解析") {
                            Task { await backend.startAnalyze(useCache: false) }
                        }
                        .buttonStyle(.bordered)
                        .disabled(!backend.isBackendReady || backend.isAnalyzing || backend.urlInput.trimmingCharacters(in: .whitespaces).isEmpty)
                    }
                }
            }

            ToolbarItem(placement: .primaryAction) {
                Button {
                    showSettings = true
                } label: {
                    Image(systemName: "gearshape")
                }
                .help("设置")
            }
        }
        .sheet(isPresented: $showSettings) {
            SettingsView(backend: backend)
        }
        .alert("清空全部历史记录", isPresented: $confirmClearHistory) {
            Button("取消", role: .cancel) {}
            Button("清空", role: .destructive) {
                Task { await backend.clearAllHistory() }
            }
        } message: {
            Text("将移除侧边栏中的全部记录，此操作不可撤销。")
        }
        .alert(
            "提示",
            isPresented: Binding(
                get: { backend.errorMessage != nil },
                set: { if !$0 { backend.errorMessage = nil } }
            )
        ) {
            Button("好", role: .cancel) { backend.errorMessage = nil }
        } message: {
            Text(backend.errorMessage ?? "")
        }
    }

    // MARK: - Sidebar

    private var sidebarView: some View {
        VStack(spacing: 0) {
            HStack(spacing: 6) {
                Image(systemName: "magnifyingglass")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                CleanInputField(placeholder: "搜索历史", text: $backend.historyQuery)
                if !backend.historyQuery.isEmpty {
                    Button {
                        backend.historyQuery = ""
                    } label: {
                        Image(systemName: "xmark.circle.fill")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                    .buttonStyle(.borderless)
                }
            }
            .padding(.horizontal, 8)
            .padding(.vertical, 6)
            .background(
                RoundedRectangle(cornerRadius: 7)
                    .fill(Color(nsColor: .controlBackgroundColor))
            )
            .overlay(
                RoundedRectangle(cornerRadius: 7)
                    .strokeBorder(Color(nsColor: .separatorColor), lineWidth: 0.8)
            )
            .padding(.horizontal, 10)
            .padding(.top, 8)
            .padding(.bottom, 6)

            if backend.filteredHistory.isEmpty {
                VStack(spacing: 6) {
                    Spacer()
                    Text(backend.history.isEmpty ? "暂无记录" : "无匹配记录")
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                    Spacer()
                }
                .frame(maxWidth: .infinity)
            } else {
                List(selection: Binding(
                    get: { backend.activeHistoryId },
                    set: { newId in
                        if let newId {
                            Task { await backend.openHistoryRecord(newId) }
                        }
                    }
                )) {
                    ForEach(backend.filteredHistory) { item in
                        HistoryRowView(item: item)
                            .tag(item.id)
                            .contextMenu {
                                Button("重新解析") {
                                    Task {
                                        await backend.startAnalyze(
                                            url: item.input_url,
                                            mode: item.mode,
                                            storefront: item.storefront,
                                            useCache: false,
                                            retryHistoryId: item.id
                                        )
                                    }
                                }
                                Divider()
                                Button("删除", role: .destructive) {
                                    Task { await backend.deleteHistoryRecord(item.id) }
                                }
                            }
                    }
                }
                .listStyle(.sidebar)
            }

            Divider()
            HStack {
                Text("共 \(backend.history.count) 条")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Spacer()
                if !backend.history.isEmpty {
                    Button {
                        confirmClearHistory = true
                    } label: {
                        Image(systemName: "trash")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                    .buttonStyle(.borderless)
                    .help("清空历史记录")
                }
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 7)
        }
    }

    // MARK: - Detail Area

    @ViewBuilder
    private var detailArea: some View {
        VStack(spacing: 0) {
            if backend.isAnalyzing, let st = backend.analyzeStatus {
                analyzingProgressView(st)
            } else if let res = backend.result {
                statusHeader(res)
                Divider()
                trackListSection
                if let webStatus = backend.webImportStatus {
                    Divider()
                    importProgressBar(webStatus)
                }
                Divider()
                bottomActionBar
            } else if let failed = backend.activeFailedSummary {
                failedRecordView(failed)
            } else {
                homeSpotlightView
            }
        }
    }

    private var homeSpotlightView: some View {
        VStack(spacing: 22) {
            Spacer()

            VStack(spacing: 8) {
                Image(systemName: "music.note.list")
                    .font(.system(size: 38, weight: .light))
                    .foregroundStyle(Color.accentColor)
                Text("bili2am")
                    .font(.title2.weight(.semibold))
            }

            VStack(spacing: 10) {
                HStack(spacing: 10) {
                    Image(systemName: "link")
                        .foregroundStyle(.secondary)
                    CleanInputField(placeholder: "粘贴 B 站合集链接、b23.tv 短链或 BV 号", text: $backend.urlInput) {
                        Task { await backend.startAnalyze(useCache: false) }
                    }
                    .font(.body)
                }
                .padding(.horizontal, 14)
                .padding(.vertical, 10)
                .background(
                    RoundedRectangle(cornerRadius: 10)
                        .fill(Color(nsColor: .controlBackgroundColor))
                )
                .overlay(
                    RoundedRectangle(cornerRadius: 10)
                        .strokeBorder(Color(nsColor: .separatorColor), lineWidth: 1)
                )

                HStack(spacing: 10) {
                    Picker("提取方式", selection: $backend.selectedMode) {
                        Text("自动提取").tag("auto")
                        Text("仅文本").tag("text")
                        Text("画面 OCR").tag("ocr")
                        Text("听歌识曲").tag("shazam")
                    }
                    .frame(width: 150)

                    Picker("曲库地区", selection: $backend.selectedStorefront) {
                        Text("中国大陆 (CN)").tag("cn")
                        Text("美国 (US)").tag("us")
                        Text("日本 (JP)").tag("jp")
                        Text("中国香港 (HK)").tag("hk")
                        Text("中国台湾 (TW)").tag("tw")
                    }
                    .frame(width: 170)

                    Spacer()

                    Button {
                        Task { await backend.startAnalyze(useCache: false) }
                    } label: {
                        Text("开始解析")
                            .frame(minWidth: 76)
                    }
                    .buttonStyle(.borderedProminent)
                    .disabled(!backend.isBackendReady || backend.urlInput.trimmingCharacters(in: .whitespaces).isEmpty)
                }
            }
            .frame(maxWidth: 540)

            if !backend.history.isEmpty {
                HStack(spacing: 8) {
                    ForEach(backend.history.prefix(3)) { item in
                        Button {
                            Task { await backend.openHistoryRecord(item.id) }
                        } label: {
                            HStack(spacing: 5) {
                                HistoryStatusDot(status: item.status)
                                Text(item.title)
                                    .lineLimit(1)
                                    .frame(maxWidth: 145)
                            }
                            .font(.caption)
                            .padding(.horizontal, 10)
                            .padding(.vertical, 5)
                            .background(
                                Capsule().fill(Color(nsColor: .controlBackgroundColor))
                            )
                            .overlay(
                                Capsule().strokeBorder(Color(nsColor: .separatorColor), lineWidth: 0.7)
                            )
                        }
                        .buttonStyle(.plain)
                    }
                }
                .padding(.top, 4)
            }

            if !backend.isBackendReady {
                HStack(spacing: 6) {
                    ProgressView().controlSize(.small)
                    Text("正在初始化本地服务…")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }

            Spacer()
        }
        .padding(24)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private func analyzingProgressView(_ st: AnalyzeJobStatusModel) -> some View {
        VStack(spacing: 14) {
            Spacer()
            VStack(spacing: 12) {
                HStack {
                    Text(st.stage == "match" ? "正在匹配 Apple Music 曲库" : "正在识别并提取歌单")
                        .font(.headline)
                    Spacer()
                    Text("\(Int(st.progress * 100))%")
                        .font(.subheadline.monospacedDigit())
                        .foregroundStyle(.secondary)
                }
                ProgressView(value: st.progress, total: 1.0)
                    .progressViewStyle(.linear)
                Text(st.message.isEmpty ? "处理中…" : st.message)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            .padding(20)
            .frame(maxWidth: 460)
            .background(
                RoundedRectangle(cornerRadius: 12)
                    .fill(Color(nsColor: .controlBackgroundColor))
            )
            .overlay(
                RoundedRectangle(cornerRadius: 12)
                    .strokeBorder(Color(nsColor: .separatorColor), lineWidth: 1)
            )
            Spacer()
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private func failedRecordView(_ item: HistorySummaryModel) -> some View {
        VStack(spacing: 14) {
            Spacer()
            Image(systemName: "exclamationmark.triangle")
                .font(.system(size: 36, weight: .light))
                .foregroundStyle(.orange)
            Text(item.title)
                .font(.headline)
                .lineLimit(1)
            Text(item.error_message.isEmpty ? "解析失败" : item.error_message)
                .font(.subheadline)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .frame(maxWidth: 460)
            Button("重试解析") {
                Task {
                    await backend.startAnalyze(
                        url: item.input_url,
                        mode: item.mode,
                        storefront: item.storefront,
                        useCache: false,
                        retryHistoryId: item.id
                    )
                }
            }
            .buttonStyle(.borderedProminent)
            Spacer()
        }
        .padding(24)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private func statusHeader(_ res: PipelineResultModel) -> some View {
        HStack(spacing: 12) {
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 8) {
                    Text(res.video.title)
                        .font(.headline)
                        .lineLimit(1)
                    Text(res.video.bvid)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                HStack(spacing: 10) {
                    Text("提取: \(res.extraction_method)")
                    Text("·")
                    Text(res.cookie_info.selected_browser != nil
                         ? "Cookie: \(res.cookie_info.selected_browser!.capitalized)"
                         : "匿名模式")
                    if let msg = backend.statusMessage, !msg.isEmpty {
                        Text("·")
                        Text(msg).foregroundStyle(.green)
                    }
                }
                .font(.caption)
                .foregroundStyle(.secondary)
                .lineLimit(1)
            }

            Spacer()

            Picker("", selection: $filter) {
                Text("全部 \(tracks.count)").tag(TrackFilter.all)
                Text("已匹配 \(matchedCount)").tag(TrackFilter.matched)
                Text("待确认 \(uncertainCount)").tag(TrackFilter.uncertain)
                Text("未收录 \(missingCount)").tag(TrackFilter.missing)
            }
            .pickerStyle(.segmented)
            .frame(width: 310)
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 10)
        .background(.bar)
    }

    private var trackListSection: some View {
        let actionMap = backend.importItemById
        return Group {
            if tracks.isEmpty {
                VStack(spacing: 12) {
                    Spacer()
                    Image(systemName: "music.note.list")
                        .font(.system(size: 30))
                        .foregroundStyle(.secondary)
                    Text("未提取到有效曲目")
                        .font(.headline)
                    if let notes = backend.result?.extraction_notes, !notes.isEmpty {
                        VStack(alignment: .leading, spacing: 6) {
                            ForEach(notes, id: \.self) { note in
                                Text("• \(note)")
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                                    .textSelection(.enabled)
                            }
                        }
                        .padding(12)
                        .background(
                            RoundedRectangle(cornerRadius: 8, style: .continuous)
                                .fill(Color(nsColor: .controlBackgroundColor))
                        )
                        .frame(maxWidth: 520)
                    }
                    Spacer()
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .padding()
            } else {
                List {
                    ForEach(filteredTracks) { item in
                        if let idx = tracks.firstIndex(where: { $0.id == item.id }) {
                            TrackRowView(
                                item: Binding(
                                    get: { backend.result!.tracks[idx] },
                                    set: { backend.result!.tracks[idx] = $0 }
                                ),
                                importAction: actionMap[item.id],
                                isPlaying: backend.playingTrackId == item.id,
                                isImportRunning: backend.webImportStatus?.running == true,
                                onRematch: { artist, title in
                                    Task { await backend.rematchTrack(id: item.id, artist: artist, title: title) }
                                },
                                onRetryImport: {
                                    Task { await backend.startWebImport(retryTrackIds: [item.id]) }
                                },
                                onTogglePreview: { url in
                                    backend.togglePreview(trackId: item.id, previewURL: url)
                                }
                            )
                        }
                    }
                }
                .listStyle(.inset(alternatesRowBackgrounds: true))
            }
        }
    }

    private func importProgressBar(_ st: WebImportStatusModel) -> some View {
        let total = max(1, st.total_count ?? st.items.count)
        let processed = min(total, st.processed_count ?? ((st.added_count ?? 0) + (st.failed_count ?? 0)))
        let failedCount = st.failed_count ?? st.items.filter { $0.status == "failed" }.count

        return VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 10) {
                if st.running {
                    Text(
                        st.waiting_for_login == true
                        ? "请在弹出的 Chrome 窗口完成 Apple Music 登录…"
                        : "正在导入 Apple Music（\(processed) / \(total)）"
                    )
                    .font(.subheadline)
                    .monospacedDigit()
                } else {
                    Image(systemName: failedCount > 0 || st.ok == false ? "exclamationmark.triangle.fill" : "checkmark.circle.fill")
                        .foregroundStyle(failedCount > 0 || st.ok == false ? .orange : .green)
                    Text(st.message ?? "完成")
                        .font(.subheadline)
                        .lineLimit(1)
                }

                Spacer()

                if !st.running && failedCount > 0 {
                    Button("重试失败项 (\(failedCount))") {
                        Task { await backend.retryAllFailedImports() }
                    }
                    .controlSize(.small)
                    .buttonStyle(.borderedProminent)
                }

                if st.running {
                    Button("停止") {
                        Task { await backend.stopWebImport() }
                    }
                    .controlSize(.small)
                }

                if !st.logs.isEmpty {
                    Button(showImportLog ? "收起日志" : "查看日志") {
                        showImportLog.toggle()
                    }
                    .controlSize(.small)
                }
            }

            if st.running {
                if st.waiting_for_login == true {
                    ProgressView()
                        .progressViewStyle(.linear)
                } else {
                    ProgressView(value: Double(processed), total: Double(total))
                        .progressViewStyle(.linear)
                }
            }

            if showImportLog {
                ScrollView {
                    Text(st.logs.joined(separator: "\n"))
                        .font(.system(.caption, design: .monospaced))
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .textSelection(.enabled)
                }
                .frame(height: 120)
                .padding(8)
                .background(Color(nsColor: .textBackgroundColor))
                .clipShape(RoundedRectangle(cornerRadius: 6))
            }
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 8)
        .background(.regularMaterial)
    }

    private var bottomActionBar: some View {
        HStack(spacing: 10) {
            Text("歌单")
                .font(.subheadline)
                .foregroundStyle(.secondary)
            CleanInputField(placeholder: "目标歌单名称", text: $backend.playlistName)
                .padding(.horizontal, 9)
                .padding(.vertical, 5)
                .background(
                    RoundedRectangle(cornerRadius: 7)
                        .fill(Color(nsColor: .controlBackgroundColor))
                )
                .overlay(
                    RoundedRectangle(cornerRadius: 7)
                        .strokeBorder(Color(nsColor: .separatorColor), lineWidth: 0.8)
                )
                .frame(minWidth: 190, maxWidth: 320)

            Text("已选 \(selectedImportCount) 首")
                .font(.caption)
                .foregroundStyle(.secondary)
                .monospacedDigit()

            Spacer()

            Button {
                Task { await backend.copyConfirmedTxt() }
            } label: {
                Label("复制歌单", systemImage: "doc.on.clipboard")
            }
            .disabled(selectedImportCount == 0)

            Menu {
                Button("导出 CSV") {
                    Task { await backend.saveExportFile(method: "csv") }
                }
                Button("导出 TXT") {
                    Task { await backend.saveExportFile(method: "txt") }
                }
            } label: {
                Label("导出", systemImage: "square.and.arrow.up")
            }
            .disabled(selectedImportCount == 0)

            Button {
                Task { await backend.startWebImport() }
            } label: {
                Text("导入 Apple Music")
            }
            .buttonStyle(.borderedProminent)
            .disabled(selectedImportCount == 0 || backend.webImportStatus?.running == true)
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 10)
        .background(.regularMaterial)
    }
}

private struct HistoryStatusDot: View {
    let status: String

    var body: some View {
        switch status {
        case "running":
            ProgressView().controlSize(.mini)
        case "success":
            Image(systemName: "checkmark.circle.fill")
                .font(.caption2)
                .foregroundStyle(.green)
        case "partial":
            Image(systemName: "exclamationmark.triangle.fill")
                .font(.caption2)
                .foregroundStyle(.orange)
        default:
            Image(systemName: "xmark.circle.fill")
                .font(.caption2)
                .foregroundStyle(.red)
        }
    }
}

private struct HistoryRowView: View {
    let item: HistorySummaryModel

    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            HistoryStatusDot(status: item.status)
                .padding(.top, 3)
            VStack(alignment: .leading, spacing: 2) {
                Text(item.title.isEmpty ? item.input_url : item.title)
                    .font(.subheadline)
                    .lineLimit(1)
                HStack(spacing: 4) {
                    if item.status == "failed" && item.total_tracks == 0 {
                        Text("解析失败")
                    } else if item.failed_import_count > 0 {
                        Text("已导 \(item.imported_count) · 失败 \(item.failed_import_count)")
                    } else if item.imported_count > 0 {
                        Text("已导 \(item.imported_count) / \(item.total_tracks)")
                    } else {
                        Text("\(item.matched_count) / \(item.total_tracks) 首")
                    }
                    Text("·")
                    Text(shortTime(item.updated_at))
                }
                .font(.caption2)
                .foregroundStyle(.secondary)
                .lineLimit(1)
            }
        }
        .padding(.vertical, 2)
    }

    private func shortTime(_ ts: String) -> String {
        let parts = ts.split(separator: " ")
        if parts.count == 2 {
            return String(parts[1].prefix(5))
        }
        return ts
    }
}

private struct TrackRowView: View {
    @Binding var item: TrackMatchItemModel
    let importAction: WebTrackActionModel?
    let isPlaying: Bool
    let isImportRunning: Bool
    let onRematch: (String, String) -> Void
    let onRetryImport: () -> Void
    let onTogglePreview: (String) -> Void

    @State private var draftArtist = ""
    @State private var draftTitle = ""
    @State private var isEditing = false

    var body: some View {
        HStack(spacing: 10) {
            Toggle("", isOn: $item.selected)
                .toggleStyle(.checkbox)
                .labelsHidden()
                .disabled(item.status == "missing" || item.candidates.isEmpty)

            Text(item.timestamp ?? "--:--")
                .font(.caption)
                .monospacedDigit()
                .foregroundStyle(.secondary)
                .frame(width: 56, alignment: .leading)

            VStack(alignment: .leading, spacing: 2) {
                if isEditing {
                    HStack(spacing: 6) {
                        TextField("歌手", text: $draftArtist)
                            .textFieldStyle(.roundedBorder)
                            .frame(width: 100)
                        TextField("歌名", text: $draftTitle)
                            .textFieldStyle(.roundedBorder)
                            .frame(width: 140)
                        Button {
                            onRematch(draftArtist, draftTitle)
                            isEditing = false
                        } label: {
                            Image(systemName: "arrow.clockwise")
                        }
                        .buttonStyle(.borderless)
                        Button {
                            isEditing = false
                        } label: {
                            Image(systemName: "xmark")
                        }
                        .buttonStyle(.borderless)
                    }
                } else {
                    HStack(spacing: 6) {
                        Text(item.artist.isEmpty ? item.title : "\(item.artist) - \(item.title)")
                            .font(.body)
                            .lineLimit(1)
                        Button {
                            draftArtist = item.artist
                            draftTitle = item.title
                            isEditing = true
                        } label: {
                            Image(systemName: "pencil")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                        .buttonStyle(.borderless)
                        .help("修改并重搜")
                    }
                }
            }
            .frame(minWidth: 200, maxWidth: 270, alignment: .leading)

            Spacer(minLength: 6)

            if item.candidates.isEmpty {
                Text(item.note.isEmpty ? "曲库未收录" : item.note)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            } else {
                HStack(spacing: 8) {
                    if item.candidates.count > 1 {
                        Picker("", selection: $item.selected_candidate_index) {
                            ForEach(Array(item.candidates.enumerated()), id: \.offset) { idx, c in
                                Text("\(c.artist_name) - \(c.track_name) (\(Int(c.confidence * 100))%)")
                                    .tag(idx)
                            }
                        }
                        .labelsHidden()
                        .frame(maxWidth: 240)
                    } else if let c = item.activeCandidate {
                        Text("\(c.artist_name) - \(c.track_name)")
                            .font(.subheadline)
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                    }

                    if let c = item.activeCandidate, !c.preview_url.isEmpty {
                        Button {
                            onTogglePreview(c.preview_url)
                        } label: {
                            Image(systemName: isPlaying ? "stop.circle.fill" : "play.circle")
                        }
                        .buttonStyle(.borderless)
                        .help("试听 30 秒")
                    }

                    if let c = item.activeCandidate, let url = URL(string: c.track_view_url), !c.track_view_url.isEmpty {
                        Link(destination: url) {
                            Image(systemName: "arrow.up.right.square")
                                .foregroundStyle(.secondary)
                        }
                        .help("在 Apple Music 网页打开")
                    }
                }
            }

            trailingStatusView
                .frame(minWidth: 74, alignment: .trailing)
        }
        .padding(.vertical, 3)
    }

    @ViewBuilder
    private var trailingStatusView: some View {
        if let act = importAction, act.status == "failed" {
            Button {
                onRetryImport()
            } label: {
                Label("重试", systemImage: "arrow.clockwise")
                    .font(.caption)
                    .foregroundStyle(.red)
            }
            .buttonStyle(.bordered)
            .controlSize(.mini)
            .disabled(isImportRunning)
            .help(act.detail)
        } else if let act = importAction, act.status == "added" {
            Label("已导入", systemImage: "checkmark.seal.fill")
                .font(.caption)
                .foregroundStyle(.green)
        } else {
            switch item.status {
            case "matched":
                Label("已匹配", systemImage: "checkmark.circle.fill")
                    .font(.caption)
                    .foregroundStyle(.green)
            case "uncertain":
                Label("待确认", systemImage: "exclamationmark.triangle.fill")
                    .font(.caption)
                    .foregroundStyle(.orange)
            default:
                Label("未收录", systemImage: "xmark.circle.fill")
                    .font(.caption)
                    .foregroundStyle(.red)
            }
        }
    }
}
