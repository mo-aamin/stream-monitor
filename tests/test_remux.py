from __future__ import annotations

import asyncio
import os
import stat

import pytest

from app.services import remux


def _fake_tool(directory, name, body):
    path = directory / name
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


@pytest.fixture
def tools(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    def make(fmt: str, ffmpeg_ok: bool = True):
        ffprobe = _fake_tool(bin_dir, "ffprobe", f'echo "{fmt}"\n')
        if ffmpeg_ok:
            # Last argument is the output path; copy the input there.
            body = 'for a; do last="$a"; done\ncp "$7" "$last"\n'
        else:
            body = 'echo "Invalid data found" >&2\nexit 1\n'
        ffmpeg = _fake_tool(bin_dir, "ffmpeg", body)
        return {"merge_output_format": "mp4", "ffprobe_path": ffprobe, "ffmpeg_path": ffmpeg}

    return make


def test_mpegts_part_is_remuxed(tmp_path, tools):
    cfg = tools("mpegts")
    src = tmp_path / "a - Live.mp4.part"
    src.write_bytes(b"ts-data")
    path, note = asyncio.run(remux.ensure_playable(str(src), cfg))
    assert path == str(tmp_path / "a - Live.mp4")
    assert (tmp_path / "a - Live.mp4").read_bytes() == b"ts-data"
    assert not src.exists()
    assert "mpegts" in note


def test_mpegts_in_mp4_is_remuxed_in_place(tmp_path, tools):
    cfg = tools("mpegts")
    src = tmp_path / "Live.mp4"
    src.write_bytes(b"ts-data")
    path, note = asyncio.run(remux.ensure_playable(str(src), cfg))
    assert path == str(src)
    assert src.exists()
    assert note
    assert not any(name.endswith(".tmp.mp4") for name in os.listdir(tmp_path))


def test_real_mp4_untouched(tmp_path, tools):
    cfg = tools("mov,mp4,m4a,3gp,3g2,mj2")
    src = tmp_path / "Live.mp4"
    src.write_bytes(b"mp4")
    assert asyncio.run(remux.ensure_playable(str(src), cfg)) == (str(src), None)


def test_finished_mp4_part_is_renamed(tmp_path, tools):
    cfg = tools("mov,mp4,m4a,3gp,3g2,mj2")
    src = tmp_path / "Live.mp4.part"
    src.write_bytes(b"mp4")
    path, _ = asyncio.run(remux.ensure_playable(str(src), cfg))
    assert path == str(tmp_path / "Live.mp4")
    assert not src.exists()


def test_failed_remux_keeps_original(tmp_path, tools):
    cfg = tools("mpegts", ffmpeg_ok=False)
    src = tmp_path / "Live.mp4.part"
    src.write_bytes(b"ts-data")
    path, note = asyncio.run(remux.ensure_playable(str(src), cfg))
    assert path == str(src)
    assert src.read_bytes() == b"ts-data"
    assert "failed" in note
    assert sorted(os.listdir(tmp_path)) == ["Live.mp4.part", "bin"]


def test_skipped_for_other_containers(tmp_path, tools):
    cfg = tools("mpegts") | {"merge_output_format": "mkv"}
    src = tmp_path / "Live.mkv"
    src.write_bytes(b"x")
    assert asyncio.run(remux.ensure_playable(str(src), cfg)) == (str(src), None)
