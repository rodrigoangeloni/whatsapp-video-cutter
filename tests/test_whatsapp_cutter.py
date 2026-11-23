"""Suite de tests unitarios y de integracion para whatsapp_cutter."""

from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import whatsapp_cutter as wc


@pytest.fixture(scope="module")
def sample_video(tmp_path_factory) -> Path:
    """Genera un MP4 sintetico de 4 segundos con video + audio para tests."""
    root = tmp_path_factory.mktemp("videos_root")
    sub = root / "TestGame"
    sub.mkdir(parents=True, exist_ok=True)
    out = sub / "sample_clip.mp4"
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "testsrc=duration=4:size=640x360:rate=30",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:duration=4",
        "-pix_fmt",
        "yuv420p",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-c:a",
        "aac",
        "-b:a",
        "96k",
        str(out),
    ]
    subprocess.check_call(cmd)
    return out


@pytest.fixture()
def live_server(sample_video: Path, monkeypatch):
    """Levanta un ThreadingHTTPServer real apuntando a la carpeta temporal."""
    videos_root = sample_video.parent.parent
    monkeypatch.setattr(wc, "VIDEOS_ROOT", videos_root)
    monkeypatch.setattr(wc, "DEFAULT_FOLDER", sample_video.parent.name)
    monkeypatch.setattr(wc, "SUPPORTS_DESKTOP_ACTIONS", False)

    server = wc.ThreadingHTTPServer(("127.0.0.1", 0), wc.RequestHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{port}"
    try:
        yield base_url, videos_root, sample_video
    finally:
        server.shutdown()
        server.server_close()


def test_version_and_unit_helpers(tmp_path: Path, monkeypatch):
    assert wc.__version__ == "1.0.0"
    assert wc.parse_fps("60/1") == 60.0
    assert wc.parse_fps("41203125/686702") == 60.0
    assert wc.parse_fps("0/0") == 0.0
    assert wc.format_time_tag(125.4) == "02m05s"
    assert wc.format_time_tag(3661) == "01h01m01s"
    assert wc.compute_scale_filter(1920, 1080, "720p") == "scale=1280:720"
    assert wc.compute_scale_filter(1280, 720, "1080p") is None
    assert wc.compute_scale_filter(1080, 1920, "720p") == "scale=720:1280"

    monkeypatch.setattr(wc, "VIDEOS_ROOT", tmp_path)
    inside = tmp_path / "demo.mp4"
    inside.write_bytes(b"123")
    assert wc.resolve_safe_path("demo.mp4") == inside.resolve()
    with pytest.raises(ValueError):
        wc.resolve_safe_path("../outside.mp4", must_exist=False)

    p1 = wc.unique_output_path(tmp_path, "clip")
    assert p1.name == "clip.mp4"
    p1.write_bytes(b"a")
    p2 = wc.unique_output_path(tmp_path, "clip")
    assert p2.name == "clip_v2.mp4"


def test_http_endpoints_and_streaming(live_server):
    base_url, _, sample_video = live_server

    with urllib.request.urlopen(f"{base_url}/api/health") as r:
        assert r.status == 200
        health = json.loads(r.read().decode("utf-8"))
        assert health["status"] == "ok"
        assert health["version"] == "1.0.0"

    with urllib.request.urlopen(f"{base_url}/") as r:
        assert r.status == 200
        html = r.read().decode("utf-8")
        assert "Cortador & Compresor WhatsApp" in html

    with urllib.request.urlopen(f"{base_url}/favicon.ico") as r:
        assert r.status in (200, 204)

    with urllib.request.urlopen(f"{base_url}/api/folders") as r:
        folders = json.loads(r.read().decode("utf-8"))
        assert folders["default_folder"] == "TestGame"
        assert folders["version"] == "1.0.0"

    with urllib.request.urlopen(f"{base_url}/api/videos?folder=TestGame") as r:
        vids = json.loads(r.read().decode("utf-8"))
        assert len(vids["videos"]) == 1
        v0 = vids["videos"][0]
        assert v0["name"] == "sample_clip.mp4"
        assert v0["width"] == 640 and v0["height"] == 360

    vq = urllib.parse.urlencode({"path": v0["path"]})
    req = urllib.request.Request(f"{base_url}/api/video?{vq}", headers={"Range": "bytes=0-511"})
    with urllib.request.urlopen(req) as r:
        assert r.status == 206
        assert len(r.read()) == 512
        assert r.headers.get("Accept-Ranges") == "bytes"

    req_bad = urllib.request.Request(f"{base_url}/api/video?{vq}", headers={"Range": "bytes=999999999-"})
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(req_bad)
    assert exc_info.value.code == 416

    with urllib.request.urlopen(f"{base_url}/api/download?{vq}") as r:
        assert r.status == 200
        disp = r.headers.get("Content-Disposition", "")
        assert "attachment" in disp
        assert len(r.read()) == sample_video.stat().st_size

    # Path traversal debe devolver HTTP 400
    bad_q = urllib.parse.urlencode({"path": "../../secret.mp4"})
    with pytest.raises(urllib.error.HTTPError) as exc_trav:
        urllib.request.urlopen(f"{base_url}/api/video?{bad_q}")
    assert exc_trav.value.code == 400


def test_process_modes_and_disconnect_resilience(live_server):
    base_url, videos_root, _ = live_server
    rel_path = "TestGame/sample_clip.mp4"

    def post(endpoint: str, body: dict) -> dict:
        req = urllib.request.Request(
            f"{base_url}{endpoint}",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode("utf-8"))

    # 1. Modo copy
    res_copy = post(
        "/api/process",
        {"path": rel_path, "start_time": 0.5, "end_time": 2.5, "mode": "copy", "target_mb": 50},
    )
    assert res_copy["ok"] is True
    assert len(res_copy["outputs"]) == 1
    out_copy = videos_root / res_copy["outputs"][0]["path"]
    assert out_copy.exists() and out_copy.stat().st_size > 0

    # 2. Modo compress
    res_comp = post(
        "/api/process",
        {"path": rel_path, "start_time": 0.0, "end_time": 3.5, "mode": "compress", "target_mb": 10},
    )
    assert res_comp["ok"] is True
    assert res_comp["total_output_mb"] <= 10.0
    out_comp = videos_root / res_comp["outputs"][0]["path"]
    assert out_comp.exists()

    # 3. Modo split
    res_split = post(
        "/api/process",
        {"path": rel_path, "start_time": 0.0, "end_time": 4.0, "mode": "split", "target_mb": 5},
    )
    assert res_split["ok"] is True
    assert len(res_split["outputs"]) >= 2

    # 4. Verificar que la desconexion abrupta por socket no rompa el servidor
    host_port = base_url.replace("http://", "").split(":")
    addr = (host_port[0], int(host_port[1]))
    for _ in range(3):
        s = socket.create_connection(addr, timeout=2)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        s.sendall(b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n")
        s.recv(256)
        s.close()

    # Servidor sigue respondiendo normalmente tras desconexiones
    with urllib.request.urlopen(f"{base_url}/api/health") as r:
        assert r.status == 200
