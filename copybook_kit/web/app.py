"""Read-only local Flask console for runs, rejects and field profiles."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from flask import Flask, abort, current_app, render_template, send_file

from ..store.ledger import Ledger

ARTIFACT_FIELDS = {"summary": "summary_path", "report": "report_path", "html": "html_path",
                   "profile": "profile_path", "accepted": "accepted_path",
                   "rejects": "rejects_path"}

def create_app(db_path: str | Path) -> Flask:
    app = Flask(__name__, template_folder="templates")
    app.config["LEDGER_PATH"] = str(db_path)
    app.config["JSON_AS_ASCII"] = False

    def ledger() -> Ledger:
        return Ledger(current_app.config["LEDGER_PATH"])

    def require_run(run_id: str) -> dict[str, Any]:
        run = ledger().get_run(run_id)
        if run is None:
            abort(404)
        return run

    @app.get("/")
    def runs_page():
        return render_template("runs.html", runs=ledger().list_runs(), nav="runs")

    @app.get("/runs/<run_id>")
    def run_detail(run_id: str):
        store = ledger()
        run = store.get_run(run_id)
        if run is None:
            abort(404)
        profile = None
        profile_path = run.get("profile_path")
        if profile_path and Path(profile_path).is_file():
            try:
                profile = json.loads(Path(profile_path).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                profile = None
        return render_template(
            "run_detail.html", run=run, issues=store.get_file_issues(run_id),
            rejects=store.get_rejects(run_id), children=store.list_children(run_id),
            events=store.get_events(run_id),
            reject_codes=json.loads(run.get("reject_codes_json") or "{}"),
            profile=profile, nav="runs")

    @app.get("/runs/<run_id>/rejects")
    def rejects_page(run_id: str):
        store = ledger()
        run = store.get_run(run_id)
        if run is None:
            abort(404)
        return render_template("rejects.html", run=run, rejects=store.get_rejects(run_id), nav="rejects")

    @app.get("/runs/<run_id>/artifacts/<name>")
    def artifact(run_id: str, name: str):
        run = require_run(run_id)
        if name == "layout":
            path = Path(run["out_dir"]) / "layout.json"
        else:
            field = ARTIFACT_FIELDS.get(name)
            if field is None or not run.get(field):
                abort(404)
            path = Path(run[field])
        if not path.is_file():
            abort(404)
        return send_file(path, as_attachment=False)

    @app.get("/runs/<run_id>/rejects/<int:reject_no>/raw")
    def reject_raw(run_id: str, reject_no: int):
        run = require_run(run_id)
        match = next((item for item in ledger().get_rejects(run_id)
                      if item["reject_no"] == reject_no), None)
        if match is None:
            abort(404)
        path = (Path(run["out_dir"]) / match["raw_file"]).resolve()
        if Path(run["out_dir"]).resolve() not in path.parents or not path.is_file():
            abort(404)
        return send_file(path, as_attachment=True, download_name=path.name)

    @app.errorhandler(404)
    def not_found(error):
        return ("<!doctype html><html lang='zh-CN'><meta charset='utf-8'><title>404</title>"
                "<body style='font-family:system-ui;padding:40px'><h1>404</h1><p>资源不存在。</p>"
                "<p><a href='/'>返回运行列表</a></p></body></html>", 404)

    return app
