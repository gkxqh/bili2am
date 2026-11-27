import Foundation

struct CookieConfigModel: Codable, Equatable {
    var enabled: Bool
    var priority: [String]
}

struct LLMConfigModel: Codable, Equatable {
    var provider: String
    var ollama_base_url: String
    var ollama_model: String
    var openai_base_url: String
    var openai_model: String
    var openai_api_key: String
}

struct AppConfigModel: Codable, Equatable {
    var storefront: String
    var cache_dir: String
    var cookie: CookieConfigModel
    var llm: LLMConfigModel
}

struct CookieResolutionModel: Codable {
    var enabled: Bool
    var selected_browser: String?
    var bilibili_cookie_count: Int
    var notice: String
}

struct VideoInfoModel: Codable {
    var bvid: String
    var title: String
    var owner: String
    var duration_sec: Int
    var url: String
}

struct AppleMusicCandidateModel: Codable, Identifiable, Hashable {
    var track_id: Int
    var track_name: String
    var artist_name: String
    var collection_name: String
    var track_view_url: String
    var preview_url: String
    var artwork_url: String
    var is_streamable: Bool
    var confidence: Double
    var country: String

    var id: Int { track_id }
}

struct TrackMatchItemModel: Codable, Identifiable {
    var id: String
    var raw_text: String
    var title: String
    var artist: String
    var timestamp: String?
    var source: String
    var status: String
    var selected: Bool
    var selected_candidate_index: Int
    var candidates: [AppleMusicCandidateModel]
    var note: String

    var activeCandidate: AppleMusicCandidateModel? {
        guard selected_candidate_index >= 0, selected_candidate_index < candidates.count else {
            return nil
        }
        return candidates[selected_candidate_index]
    }
}

struct PipelineResultModel: Codable {
    var cache_version: Int?
    var video: VideoInfoModel
    var cookie_info: CookieResolutionModel
    var extraction_method: String
    var extraction_notes: [String]
    var storefront: String
    var playlist_name: String
    var tracks: [TrackMatchItemModel]
    var llm_used: String
}

struct HistorySummaryModel: Codable, Identifiable, Hashable {
    var id: String
    var created_at: String
    var updated_at: String
    var input_url: String
    var bvid: String
    var title: String
    var mode: String
    var storefront: String
    var status: String // "running" | "success" | "partial" | "failed"
    var stage: String
    var total_tracks: Int
    var matched_count: Int
    var imported_count: Int
    var failed_import_count: Int
    var error_message: String
}

struct HistoryRecordModel: Codable {
    var summary: HistorySummaryModel
    var result: PipelineResultModel?
    var import_items: [WebTrackActionModel]
    var import_logs: [String]
    var import_message: String
}

struct HistoryListResponse: Codable {
    var items: [HistorySummaryModel]
}

struct AnalyzeJobStatusModel: Codable {
    var running: Bool
    var stage: String
    var progress: Double
    var current: Int
    var total: Int
    var message: String
    var error: String
    var history_id: String?
    var result: PipelineResultModel?
}

struct OllamaStatusModel: Codable {
    var reachable: Bool
    var models: [String]
    var error: String
}

struct RecommendedModelInfo: Codable, Identifiable {
    var name: String
    var size_hint: String
    var desc: String
    var id: String { name }
}

struct PullStatusModel: Codable {
    var running: Bool
    var model: String
    var message: String
    var ok: Bool?
}

struct ServerStateResponse: Codable {
    var config: AppConfigModel
    var result: PipelineResultModel?
    var active_history_id: String?
    var history: [HistorySummaryModel]
    var ollama: OllamaStatusModel
    var recommended_models: [RecommendedModelInfo]
    var pull_status: PullStatusModel
}

struct WebTrackActionModel: Codable, Identifiable {
    var id: String
    var title: String
    var artist: String
    var track_id: Int
    var web_url: String
    var status: String
    var detail: String
}

struct WebImportStatusModel: Codable {
    var ok: Bool?
    var success: Bool?
    var running: Bool
    var waiting_for_login: Bool?
    var total_count: Int?
    var processed_count: Int?
    var added_count: Int?
    var failed_count: Int?
    var history_id: String?
    var message: String?
    var log_path: String?
    var logs: [String]
    var items: [WebTrackActionModel]
}

enum TrackFilter: String, CaseIterable, Identifiable {
    case all = "全部"
    case matched = "已匹配"
    case uncertain = "待确认"
    case missing = "未收录"

    var id: String { rawValue }
}
