from __future__ import annotations


def test_healthz(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"]


def test_pages_render(client):
    assert client.get("/").status_code == 200
    assert "Runtime versions" in client.get("/").text
    for path in ("/channels", "/recordings", "/settings", "/logs"):
        response = client.get(path)
        assert response.status_code == 200, path
    settings = client.get("/settings").text
    assert "<title>Settings" in settings
    assert 'name="recordings_dir"' not in settings
    assert "docker-compose.yml" in settings


def test_channel_api_flow(client):
    response = client.post(
        "/api/channels",
        json={"platform": "youtube", "identifier": "@HasanAbi", "name": "Hasan"},
    )
    assert response.status_code == 201, response.text
    channel = response.json()["channel"]
    assert channel["name"] == "Hasan"
    assert channel["url"].endswith("/live")
    channel_id = channel["id"]

    duplicate = client.post(
        "/api/channels", json={"platform": "youtube", "identifier": "@hasanabi"}
    )
    assert duplicate.status_code == 409

    channels = client.get("/api/channels").json()["channels"]
    assert len(channels) == 1
    assert channels[0]["active"] is None

    toggle = client.post(f"/api/channels/{channel_id}/toggle")
    assert toggle.status_code == 200
    assert toggle.json()["enabled"] is False

    delete = client.delete(f"/api/channels/{channel_id}")
    assert delete.status_code == 200
    assert client.get("/api/channels").json()["channels"] == []


def test_channel_validation_and_missing(client):
    bad = client.post("/api/channels", json={"platform": "twitch", "identifier": "https://evil.test/x"})
    assert bad.status_code == 400
    assert client.delete("/api/channels/999").status_code == 404
    assert client.post("/api/channels/999/check").status_code == 404
    assert client.post("/api/channels/999/stop").status_code == 404


def test_bulk_add(client):
    response = client.post(
        "/api/channels/bulk",
        json={
            "platform": "twitch",
            "lines": "xqc\nhasanabi\nxqc\n\nhttps://evil.test/nope",
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["added"] == 2
    assert body["skipped"] == 1
    assert len(body["errors"]) == 1
    assert len(client.get("/api/channels").json()["channels"]) == 2


def test_status_and_settings_api(client):
    status = client.get("/api/status")
    assert status.status_code == 200
    body = status.json()
    assert body["monitor"]["state"] == "stopped"
    assert body["stats"]["channels_total"] == 0

    saved = client.post(
        "/api/settings",
        json={"values": {"check_interval": 90, "monitor_enabled": False}},
    )
    assert saved.status_code == 200, saved.text
    assert set(saved.json()["saved"]) == {"check_interval", "monitor_enabled"}

    settings = client.get("/api/settings").json()["settings"]
    assert settings["check_interval"] == 90
    assert settings["monitor_enabled"] is False
    assert "recordings_dir" not in settings

    invalid = client.post("/api/settings", json={"values": {"check_interval": 2}})
    assert invalid.status_code == 400
    assert "between" in invalid.json()["error"]

    pause = client.post("/api/monitor/pause")
    assert pause.status_code == 200
    assert pause.json()["monitor"]["enabled"] is False


def test_recordings_endpoints(client):
    assert client.get("/api/recordings/999/log").status_code == 404
    assert client.post("/api/recordings/999/stop").status_code == 404
    assert client.delete("/api/recordings/999").status_code == 404
    page = client.get("/recordings")
    assert page.status_code == 200
    assert "No recordings" in page.text


def test_api_404_shape(client):
    response = client.get("/api/does-not-exist")
    assert response.status_code == 404
    assert response.json()["ok"] is False


def test_security_headers(client):
    response = client.get("/")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert "content-security-policy" in response.headers


def test_monitor_stopped_state(client):
    state = client.get("/api/status").json()["monitor"]
    assert state["loop_running"] is False
    assert state["active_count"] == 0
    assert client.post("/api/monitor/check-all").json()["queued"] == 0


def test_active_recording_overlay():
    from app.routes.pages import _merge_active_recordings

    rows = [
        {
            "id": 1,
            "status": "recording",
            "filename": None,
            "filepath": None,
            "size_bytes": None,
            "duration_seconds": None,
        },
        {"id": 2, "status": "completed", "filename": "done.mp4", "size_bytes": 10},
    ]
    active = {
        1: {
            "filename": "live.mp4",
            "filepath": "/recordings/live.mp4.part",
            "size_bytes": 1234,
            "elapsed_seconds": 42,
        }
    }
    merged = _merge_active_recordings(rows, active)
    assert merged[0]["filename"] == "live.mp4"
    assert merged[0]["size_bytes"] == 1234
    assert merged[0]["duration_seconds"] == 42
    assert merged[1]["filename"] == "done.mp4"
