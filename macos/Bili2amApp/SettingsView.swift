import SwiftUI

struct SettingsView: View {
    @ObservedObject var backend: BackendManager
    @Environment(\.dismiss) private var dismiss

    @State private var cookieEnabled = true
    @State private var cookiePriority = "safari, edge, chrome"
    @State private var storefront = "cn"
    @State private var llmProvider = "none"
    @State private var ollamaModel = "qwen2.5:3b"
    @State private var openaiUrl = "https://api.openai.com/v1"
    @State private var openaiModel = "gpt-4o-mini"
    @State private var openaiKey = ""

    var body: some View {
        NavigationStack {
            Form {
                Section("Apple Music 地区") {
                    Picker("默认曲库", selection: $storefront) {
                        Text("中国大陆 (CN)").tag("cn")
                        Text("美国 (US)").tag("us")
                        Text("日本 (JP)").tag("jp")
                        Text("中国香港 (HK)").tag("hk")
                        Text("中国台湾 (TW)").tag("tw")
                    }
                }

                Section {
                    Toggle("自动读取浏览器 Cookie", isOn: $cookieEnabled)
                    TextField("读取优先级", text: $cookiePriority)
                        .disabled(!cookieEnabled)
                } header: {
                    Text("B 站登录态")
                } footer: {
                    Text("仅在本地内存用于抓取热评与清晰度流，按逗号分隔，例如 safari, edge, chrome。")
                }

                Section {
                    Picker("清洗方式", selection: $llmProvider) {
                        Text("仅本地规则").tag("none")
                        Text("本地 Ollama 模型").tag("ollama")
                        Text("OpenAI 兼容 API").tag("openai")
                    }

                    if llmProvider == "ollama" {
                        TextField("模型名称", text: $ollamaModel)
                    } else if llmProvider == "openai" {
                        TextField("Base URL", text: $openaiUrl)
                        TextField("Model", text: $openaiModel)
                        SecureField("API Key", text: $openaiKey)
                    }
                } header: {
                    Text("歌单清洗")
                }

                Section {
                    HStack {
                        Label(
                            backend.ollamaStatus?.reachable == true ? "Ollama 服务运行中" : "未检测到运行中的 Ollama",
                            systemImage: backend.ollamaStatus?.reachable == true ? "circle.fill" : "circle"
                        )
                        .foregroundStyle(backend.ollamaStatus?.reachable == true ? .green : .secondary)
                        Spacer()
                        if let models = backend.ollamaStatus?.models, !models.isEmpty {
                            Text(models.joined(separator: ", "))
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                    }

                    ForEach(backend.recommendedModels) { m in
                        let installed = backend.ollamaStatus?.models.contains(where: { $0.hasPrefix(m.name) }) ?? false
                        HStack {
                            VStack(alignment: .leading, spacing: 2) {
                                HStack(spacing: 6) {
                                    Text(m.name).fontWeight(.medium)
                                    Text(m.size_hint)
                                        .font(.caption)
                                        .foregroundStyle(.secondary)
                                }
                                Text(m.desc)
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                            }
                            Spacer()
                            if installed {
                                Button("选用") {
                                    llmProvider = "ollama"
                                    ollamaModel = m.name
                                }
                            } else {
                                Button("下载") {
                                    Task { await backend.pullOllamaModel(m.name) }
                                }
                                .disabled(backend.ollamaStatus?.reachable != true || backend.pullStatus?.running == true)
                            }
                        }
                    }

                    if let ps = backend.pullStatus, !ps.message.isEmpty {
                        Text(ps.message)
                            .font(.caption)
                            .foregroundStyle(ps.ok == false ? .red : .secondary)
                    }
                } header: {
                    Text("可选本地模型（按需下载）")
                }
            }
            .formStyle(.grouped)
            .navigationTitle("设置")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("取消") { dismiss() }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("保存") {
                        Task {
                            await saveChanges()
                            dismiss()
                        }
                    }
                }
            }
        }
        .frame(width: 520, height: 540)
        .onAppear {
            loadFromBackend()
            Task { _ = await backend.refreshState() }
        }
    }

    private func loadFromBackend() {
        guard let cfg = backend.config else { return }
        storefront = cfg.storefront
        cookieEnabled = cfg.cookie.enabled
        cookiePriority = cfg.cookie.priority.joined(separator: ", ")
        llmProvider = cfg.llm.provider
        ollamaModel = cfg.llm.ollama_model
        openaiUrl = cfg.llm.openai_base_url
        openaiModel = cfg.llm.openai_model
        openaiKey = cfg.llm.openai_api_key
    }

    private func saveChanges() async {
        guard var next = backend.config else { return }
        next.storefront = storefront
        next.cookie.enabled = cookieEnabled
        next.cookie.priority = cookiePriority
            .split(separator: ",")
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines).lowercased() }
            .filter { !$0.isEmpty }
        next.llm.provider = llmProvider
        next.llm.ollama_model = ollamaModel.trimmingCharacters(in: .whitespacesAndNewlines)
        next.llm.openai_base_url = openaiUrl.trimmingCharacters(in: .whitespacesAndNewlines)
        next.llm.openai_model = openaiModel.trimmingCharacters(in: .whitespacesAndNewlines)
        next.llm.openai_api_key = openaiKey.trimmingCharacters(in: .whitespacesAndNewlines)
        await backend.saveConfig(next)
    }
}
