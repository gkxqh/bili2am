"""FastAPI local confirmation server for reviewing, tracking history, and importing tracks."""

from __future__ import annotations

import shutil
import subprocess
import threading
import webbrowser
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel

from bili2am.config import AppConfig, load_config, save_config
from bili2am.exporters.file_export import (
    export_tracks_to_csv,
    export_tracks_to_txt,
    filter_confirmed_tracks,
    format_tracks_csv,
    format_tracks_txt,
)
from bili2am.exporters.web_apple_music import WebAppleMusicJobManager, WebAppleMusicResult
from bili2am.history import HistoryRecord, HistoryStore
from bili2am.matcher.cleaner import check_ollama_status
from bili2am.matcher.itunes_search import rematch_single_track
from bili2am.models import ImportRequest, MatchStatus, PipelineResult, TrackMatchItem
from bili2am.pipeline import run_pipeline

STATIC_HTML = Path(__file__).parent / "static" / "index.html"

RECOMMENDED_OLLAMA_MODELS = [
    {
        "name": "qwen2.5:1.5b",
        "size_hint": "~1.0 GB",
        "desc": "最小可用档：适合内存有限设备，可处理基础中英文歌单清洗",
    },
    {
        "name": "qwen2.5:3b",
        "size_hint": "~1.9 GB",
        "desc": "推荐甜点档：中英日歌名理解稳定、JSON 结构准确且速度极快",
    },
    {
        "name": "qwen3:4b",
        "size_hint": "~2.6 GB",
        "desc": "高精度轻量档：更擅长处理杂乱热评与画面 OCR 碎片文本",
    },
]


class AnalyzeRequest(BaseModel):
    url: str
    mode: Literal["auto", "text", "ocr", "shazam"] = "auto"
    storefront: str | None = None
    use_cache: bool = True
    history_id: str | None = None


class RematchRequest(BaseModel):
    item: TrackMatchItem
    artist: str
    title: str
    country: str = "cn"
    history_id: str | None = None


class OllamaPullRequest(BaseModel):
    model: str


class AnalyzeJobState(BaseModel):
    running: bool = False
    stage: str = "idle"  # idle | extract | match | done | failed
    progress: float = 0.0
    current: int = 0
    total: int = 0
    message: str = ""
    error: str = ""
    history_id: str | None = None
    result: PipelineResult | None = None


class _ServerState:
    def __init__(self, cfg: AppConfig, initial_result: PipelineResult | None = None) -> None:
        self.cfg = cfg
        self.result = initial_result
        self.active_history_id: str | None = None
        self.history = HistoryStore(Path(cfg.cache_dir))
        self.pull_status: dict[str, Any] = {"running": False, "model": "", "message": "", "ok": None}
        self.web_am_jobs = WebAppleMusicJobManager()
        self.analyze_job = AnalyzeJobState()
        self._lock = threading.Lock()


def _save_result_to_history(
    history: HistoryStore,
    rec: HistoryRecord,
    res: PipelineResult,
) -> HistoryRecord:
    matched_n = sum(1 for t in res.tracks if t.status != MatchStatus.MISSING and t.candidates)
    rec.summary.bvid = res.video.bvid
    rec.summary.title = res.video.title or res.video.bvid
    rec.summary.storefront = res.storefront
    rec.summary.status = "success" if matched_n > 0 else "partial"
    rec.summary.stage = "匹配完成"
    rec.summary.total_tracks = len(res.tracks)
    rec.summary.matched_count = matched_n
    rec.summary.error_message = ""
    rec.result = res
    return history.save_record(rec)


def create_app(
    cfg: AppConfig | None = None,
    initial_result: PipelineResult | None = None,
) -> FastAPI:
    app = FastAPI(title="bili2am Confirmation UI", version="0.1.0")
    state = _ServerState(cfg or load_config(), initial_result)

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        if not STATIC_HTML.exists():
            raise HTTPException(status_code=500, detail="Missing index.html asset")
        return HTMLResponse(STATIC_HTML.read_text(encoding="utf-8"))

    @app.get("/api/state")
    def get_state() -> dict[str, Any]:
        return {
            "config": state.cfg.model_dump(mode="json"),
            "result": state.result.model_dump(mode="json") if state.result else None,
            "active_history_id": state.active_history_id,
            "history": [h.model_dump(mode="json") for h in state.history.list_summaries()],
            "ollama": check_ollama_status(state.cfg.llm.ollama_base_url),
            "recommended_models": RECOMMENDED_OLLAMA_MODELS,
            "pull_status": state.pull_status,
        }

    @app.post("/api/config")
    def update_config(new_cfg: AppConfig) -> dict[str, Any]:
        state.cfg = new_cfg
        state.history = HistoryStore(Path(new_cfg.cache_dir))
        saved_path = save_config(new_cfg)
        return {"ok": True, "path": str(saved_path), "config": new_cfg.model_dump(mode="json")}

    @app.post("/api/analyze")
    def analyze_video(req: AnalyzeRequest) -> dict[str, Any]:
        cfg_copy = state.cfg.model_copy(deep=True)
        if req.storefront:
            cfg_copy.storefront = req.storefront.strip().lower()
        rec = (
            state.history.get_record(req.history_id)
            if req.history_id
            else None
        ) or state.history.new_record(req.url, mode=req.mode, storefront=cfg_copy.storefront)
        try:
            res = run_pipeline(
                url_or_bvid=req.url,
                cfg=cfg_copy,
                mode=req.mode,
                use_cache=req.use_cache,
            )
        except Exception as exc:  # noqa: BLE001
            rec.summary.status = "failed"
            rec.summary.stage = "解析失败"
            rec.summary.error_message = str(exc)
            state.history.save_record(rec)
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        _save_result_to_history(state.history, rec, res)
        state.result = res
        state.active_history_id = rec.summary.id
        return {**res.model_dump(mode="json"), "history_id": rec.summary.id}

    @app.post("/api/analyze/start")
    def analyze_start(req: AnalyzeRequest) -> dict[str, Any]:
        with state._lock:
            if state.analyze_job.running:
                return state.analyze_job.model_dump(mode="json")
            cfg_copy = state.cfg.model_copy(deep=True)
            if req.storefront:
                cfg_copy.storefront = req.storefront.strip().lower()
            rec = (
                state.history.get_record(req.history_id)
                if req.history_id
                else None
            ) or state.history.new_record(req.url, mode=req.mode, storefront=cfg_copy.storefront)
            rec.summary.status = "running"
            rec.summary.stage = "准备解析"
            rec.summary.error_message = ""
            state.history.save_record(rec)

            state.analyze_job = AnalyzeJobState(
                running=True,
                stage="extract",
                progress=0.02,
                current=0,
                total=1,
                message="正在启动解析…",
                history_id=rec.summary.id,
            )
            state.active_history_id = rec.summary.id

        def _on_progress(stage: str, pct: float, cur: int, tot: int, msg: str) -> None:
            with state._lock:
                state.analyze_job.stage = stage
                state.analyze_job.progress = max(0.0, min(1.0, pct))
                state.analyze_job.current = cur
                state.analyze_job.total = tot
                state.analyze_job.message = msg

        def _worker() -> None:
            try:
                res = run_pipeline(
                    url_or_bvid=req.url,
                    cfg=cfg_copy,
                    mode=req.mode,
                    use_cache=req.use_cache,
                    progress_cb=_on_progress,
                )
                _save_result_to_history(state.history, rec, res)
                with state._lock:
                    state.result = res
                    state.analyze_job = AnalyzeJobState(
                        running=False,
                        stage="done",
                        progress=1.0,
                        current=len(res.tracks),
                        total=len(res.tracks),
                        message=f"完成：提取 {len(res.tracks)} 首曲目",
                        history_id=rec.summary.id,
                        result=res,
                    )
            except Exception as exc:  # noqa: BLE001
                rec.summary.status = "failed"
                rec.summary.stage = "解析失败"
                rec.summary.error_message = str(exc)
                state.history.save_record(rec)
                with state._lock:
                    state.analyze_job = AnalyzeJobState(
                        running=False,
                        stage="failed",
                        progress=0.0,
                        message="解析失败",
                        error=str(exc),
                        history_id=rec.summary.id,
                    )

        threading.Thread(target=_worker, daemon=True).start()
        return state.analyze_job.model_dump(mode="json")

    @app.get("/api/analyze/status")
    def analyze_status() -> dict[str, Any]:
        with state._lock:
            return state.analyze_job.model_dump(mode="json")

    @app.get("/api/history")
    def list_history(q: str = "") -> dict[str, Any]:
        return {
            "items": [h.model_dump(mode="json") for h in state.history.list_summaries(q)],
        }

    @app.get("/api/history/{record_id}")
    def get_history(record_id: str) -> dict[str, Any]:
        rec = state.history.get_record(record_id)
        if rec is None:
            raise HTTPException(status_code=404, detail="未找到该历史记录")
        if rec.result is not None:
            state.result = rec.result
        state.active_history_id = rec.summary.id
        return rec.model_dump(mode="json")

    @app.delete("/api/history/{record_id}")
    def delete_history(record_id: str) -> dict[str, Any]:
        ok = state.history.delete_record(record_id)
        if state.active_history_id == record_id:
            state.active_history_id = None
            state.result = None
        return {"ok": ok}

    @app.delete("/api/history")
    def clear_history() -> dict[str, Any]:
        cnt = state.history.clear_all()
        state.active_history_id = None
        state.result = None
        return {"ok": True, "deleted": cnt}

    @app.post("/api/rematch")
    def rematch(req: RematchRequest) -> dict[str, Any]:
        with httpx.Client(timeout=12.0) as client:
            updated = rematch_single_track(
                item=req.item,
                artist=req.artist,
                title=req.title,
                country=req.country or state.cfg.storefront,
                client=client,
            )
        hist_id = req.history_id or state.active_history_id
        if hist_id:
            rec = state.history.get_record(hist_id)
            if rec and rec.result:
                for i, tr in enumerate(rec.result.tracks):
                    if tr.id == updated.id:
                        rec.result.tracks[i] = updated
                        break
                _save_result_to_history(state.history, rec, rec.result)
        return updated.model_dump(mode="json")

    @app.post("/api/import")
    def do_import(req: ImportRequest) -> dict[str, Any]:
        confirmed = filter_confirmed_tracks(req.tracks)
        if not confirmed:
            raise HTTPException(status_code=400, detail="没有可导入的已匹配曲目。")

        if req.method == "web_apple_music":
            hist_id = req.history_id or state.active_history_id
            prev_items: list[dict[str, Any]] | None = None
            if hist_id:
                rec = state.history.get_record(hist_id)
                if rec:
                    prev_items = rec.import_items or None
                    if rec.result:
                        rec.result.tracks = req.tracks
                        rec.result.playlist_name = req.playlist_name
                        state.history.save_record(rec)

            def _on_import_finish(final_st: WebAppleMusicResult) -> None:
                if not hist_id:
                    return
                rec2 = state.history.get_record(hist_id)
                if not rec2:
                    return
                rec2.import_items = [asdict(x) for x in final_st.items]
                rec2.import_logs = list(final_st.logs)
                rec2.import_message = final_st.message
                rec2.summary.imported_count = final_st.added_count
                rec2.summary.failed_import_count = final_st.failed_count
                if final_st.failed_count == 0 and final_st.added_count > 0:
                    rec2.summary.status = "success"
                    rec2.summary.stage = "全部导入完成"
                    rec2.summary.error_message = ""
                elif final_st.added_count > 0:
                    rec2.summary.status = "partial"
                    rec2.summary.stage = "部分导入失败"
                    rec2.summary.error_message = final_st.message
                else:
                    rec2.summary.status = "failed"
                    rec2.summary.stage = "导入失败"
                    rec2.summary.error_message = final_st.message
                state.history.save_record(rec2)

            job_dict = state.web_am_jobs.start(
                playlist_name=req.playlist_name,
                tracks=req.tracks,
                country=state.cfg.storefront,
                cache_dir=Path(state.cfg.cache_dir).expanduser(),
                history_id=hist_id,
                retry_track_ids=req.retry_track_ids,
                previous_items=prev_items,
                on_finish=_on_import_finish,
            )
            return {"ok": True, "async": True, **job_dict}

        out_dir = Path.home() / "Downloads"
        if req.method == "csv":
            out_path = export_tracks_to_csv(req.tracks, out_dir, req.playlist_name)
            return {
                "ok": True,
                "count": len(confirmed),
                "path": str(out_path),
                "content": format_tracks_csv(req.tracks),
                "message": f"已导出 {len(confirmed)} 首可导入曲目到 {out_path}",
            }

        out_path = export_tracks_to_txt(req.tracks, out_dir, req.playlist_name)
        return {
            "ok": True,
            "count": len(confirmed),
            "path": str(out_path),
            "content": format_tracks_txt(req.tracks),
            "message": f"已导出 {len(confirmed)} 首可导入曲目到 {out_path}",
        }

    @app.get("/api/web-am/status")
    def web_am_status() -> dict[str, Any]:
        return state.web_am_jobs.status() or {"running": False, "logs": [], "items": []}

    @app.post("/api/web-am/stop")
    def web_am_stop() -> dict[str, Any]:
        return state.web_am_jobs.stop() or {"running": False, "logs": [], "items": []}

    @app.post("/api/export-preview", response_class=PlainTextResponse)
    def export_preview(req: ImportRequest) -> PlainTextResponse:
        if req.method == "csv":
            return PlainTextResponse(format_tracks_csv(req.tracks))
        return PlainTextResponse(format_tracks_txt(req.tracks))

    @app.post("/api/ollama/pull")
    def pull_ollama_model(req: OllamaPullRequest) -> dict[str, Any]:
        model = req.model.strip()
        allowed = {m["name"] for m in RECOMMENDED_OLLAMA_MODELS}
        if model not in allowed:
            raise HTTPException(status_code=400, detail=f"仅支持预设轻量模型: {', '.join(sorted(allowed))}")

        ollama_bin = shutil.which("ollama")
        if not ollama_bin:
            raise HTTPException(
                status_code=400,
                detail="未检测到 ollama 命令，请先安装 Ollama (https://ollama.com)。",
            )

        with state._lock:
            if state.pull_status["running"]:
                return state.pull_status
            state.pull_status = {
                "running": True,
                "model": model,
                "message": f"正在后台下载 {model}，请稍候...",
                "ok": None,
            }

        def _bg_pull() -> None:
            proc = subprocess.run(
                [ollama_bin, "pull", model],
                capture_output=True,
                text=True,
                check=False,
            )
            with state._lock:
                if proc.returncode == 0:
                    state.pull_status = {
                        "running": False,
                        "model": model,
                        "message": f"模型 {model} 下载完成！可在上方切换启用。",
                        "ok": True,
                    }
                else:
                    err = proc.stderr.strip() or proc.stdout.strip() or "未知错误"
                    state.pull_status = {
                        "running": False,
                        "model": model,
                        "message": f"下载 {model} 失败: {err[:240]}",
                        "ok": False,
                    }

        threading.Thread(target=_bg_pull, daemon=True).start()
        return state.pull_status

    return app


def launch_confirm_server(
    cfg: AppConfig,
    initial_result: PipelineResult | None = None,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = True,
) -> None:
    """Start local FastAPI confirmation server and optionally open browser."""
    app = create_app(cfg=cfg, initial_result=initial_result)
    url = f"http://{host}:{port}"
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=host, port=port, log_level="warning")
