"""Read-only local Flask console routes and artifact boundaries."""

from __future__ import annotations

from conftest import FIXTURES
from copybook_kit.store.ledger import Ledger
from copybook_kit.web.app import create_app

def client_for(path):
    app = create_app(path)
    app.config.update(TESTING=True)
    return app.test_client()

def test_runs_details_profiles_rejects_downloads_and_404(run_fixture, tmp_path):
    normal = run_fixture()
    rejected = run_fixture("customer_rejects.dat")
    truncated = run_fixture("customer_truncated.dat")
    client = client_for(tmp_path / "ledger.sqlite")
    index = client.get("/")
    assert index.status_code == 200
    assert normal.run_id.encode() in index.data and rejected.run_id.encode() in index.data
    detail = client.get(f"/runs/{normal.run_id}")
    assert detail.status_code == 200 and b"CUSTOMER-ID" in detail.data
    rejects_page = client.get(f"/runs/{rejected.run_id}/rejects")
    assert rejects_page.status_code == 200 and b"R008_UNIQUE" in rejects_page.data
    issues_page = client.get(f"/runs/{truncated.run_id}")
    assert b"RD002" in issues_page.data
    for name in ("summary", "html", "layout", "profile", "accepted", "rejects"):
        response = client.get(f"/runs/{normal.run_id}/artifacts/{name}")
        assert response.status_code == 200 and (response.data or name == "rejects")
    assert client.get(f"/runs/{normal.run_id}/artifacts/nope").status_code == 404
    assert client.get("/runs/run-missing").status_code == 404
    assert client.get("/missing").status_code == 404 and client.post("/").status_code == 405
    for response in (index, detail, rejects_page, issues_page):
        assert b"http://" not in response.data and b"https://" not in response.data

def test_reject_raw_download_matches_original_bytes(run_fixture, tmp_path):
    result = run_fixture("customer_rejects.dat")
    reject = Ledger(tmp_path / "ledger.sqlite").get_rejects(result.run_id)[0]
    response = client_for(tmp_path / "ledger.sqlite").get(
        f"/runs/{result.run_id}/rejects/{reject['reject_no']}/raw")
    assert response.status_code == 200
    data = (FIXTURES / "data" / "customer_rejects.dat").read_bytes()
    offset = reject["byte_offset"]
    assert response.data == data[offset:offset + 86]
    assert response.headers["Content-Disposition"].startswith("attachment")
