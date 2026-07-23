#!/usr/bin/env python3
"""Tests for photo attachments in the chat (obd_images + obd_chat's turn loop).

Spec asserted: the obd_images docstring contract — a photo can be attached from a
path, the watch folders, a camera, or a phone; it is downscaled and stored beside
the transcript; the session JSON holds a path reference and never base64; and a
photo that cannot be obtained produces a clear message with nothing queued,
never a crash and never a silently-empty message.

Run:  venv/bin/python -m pytest test_obd_images.py -q
"""

import json
import os

import pytest
from PIL import Image

import obd_chat
import obd_images


def make_image(path, size=(80, 60), color=(200, 30, 30), fmt=None, exif_orientation=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    img = Image.new("RGB", size, color)
    kwargs = {}
    if exif_orientation is not None:
        exif = Image.Exif()
        exif[0x0112] = exif_orientation
        kwargs["exif"] = exif
    img.save(path, fmt, **kwargs)
    return path


@pytest.fixture
def media(tmp_path):
    return str(tmp_path / "media")


# --------------------------------------------------------------------------- #
# Finding images
# --------------------------------------------------------------------------- #
def test_watch_dirs_honours_env_override(tmp_path, monkeypatch):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    monkeypatch.setenv("OBD_PHOTO_DIRS", f"{a}:{b}")
    assert obd_images.watch_dirs() == [str(a)]      # non-existent dirs dropped


def test_recent_images_is_newest_first(tmp_path):
    old = make_image(str(tmp_path / "old.jpg"))
    new = make_image(str(tmp_path / "new.jpg"))
    os.utime(old, (1000, 1000))
    os.utime(new, (2000, 2000))
    assert obd_images.recent_images(5, [str(tmp_path)]) == [new, old]


def test_newest_image_explains_when_nothing_found(tmp_path):
    with pytest.raises(obd_images.ImageError) as e:
        obd_images.newest_image([str(tmp_path)])
    assert "/pic <file>" in str(e.value)            # tells the user what to do instead


def test_from_path_expands_user_and_globs(tmp_path, monkeypatch):
    make_image(str(tmp_path / "one.jpg"))
    two = make_image(str(tmp_path / "two.jpg"))
    monkeypatch.setenv("HOME", str(tmp_path))
    assert obd_images.from_path(str(tmp_path / "*.jpg")) == two    # newest-sorted last
    assert obd_images.from_path("~/one.jpg") == str(tmp_path / "one.jpg")
    assert obd_images.from_path(f'"{tmp_path}/one.jpg"') == str(tmp_path / "one.jpg")


def test_from_path_rejects_missing_and_non_image(tmp_path):
    with pytest.raises(obd_images.ImageError, match="No such file"):
        obd_images.from_path(str(tmp_path / "nope.jpg"))
    doc = tmp_path / "notes.txt"
    doc.write_text("hi")
    with pytest.raises(obd_images.ImageError, match="Not an image"):
        obd_images.from_path(str(doc))


def test_inline_paths_only_match_real_images(tmp_path):
    pic = make_image(str(tmp_path / "coil.jpg"))
    text = f"whats wrong with {pic} vs {tmp_path}/ghost.jpg and /etc/hostname"
    assert obd_images.find_inline_images(text) == [pic]


def test_inline_paths_handle_spaces_when_quoted(tmp_path):
    pic = make_image(str(tmp_path / "engine bay.jpg"))
    assert obd_images.find_inline_images(f"look at '{pic}'") == [pic]


# --------------------------------------------------------------------------- #
# Preparing
# --------------------------------------------------------------------------- #
def test_prepare_downscales_to_the_token_cap(tmp_path, media):
    src = make_image(str(tmp_path / "huge.jpg"), size=(4000, 3000))
    att = obd_images.prepare(src, media)
    assert max(att["size"]) == obd_images.MAX_EDGE
    assert att["bytes"] < obd_images.MAX_BYTES
    assert att["media_type"] == "image/jpeg"
    assert os.path.dirname(att["path"]) == media       # copied beside the transcript


def test_prepare_applies_exif_rotation(tmp_path, media):
    """Phone photos carry rotation in EXIF; an unrotated engine bay is unreadable."""
    src = make_image(str(tmp_path / "sideways.jpg"), size=(100, 50), exif_orientation=6)
    att = obd_images.prepare(src, media)
    assert att["size"] == (50, 100)


def test_prepare_numbers_photos_sequentially(tmp_path, media):
    a = obd_images.prepare(make_image(str(tmp_path / "a.jpg")), media)
    b = obd_images.prepare(make_image(str(tmp_path / "b.png"), fmt="PNG"), media)
    assert os.path.basename(a["path"]) == "pic_01.jpg"
    assert os.path.basename(b["path"]) == "pic_02.jpg"    # PNG normalised to JPEG


def test_prepare_rejects_a_file_that_is_not_really_an_image(tmp_path, media):
    fake = tmp_path / "fake.jpg"
    fake.write_text("this is not a JPEG")
    with pytest.raises(obd_images.ImageError, match="could not read"):
        obd_images.prepare(str(fake), media)


def test_prepare_missing_source(tmp_path, media):
    with pytest.raises(obd_images.ImageError, match="No such file"):
        obd_images.prepare(str(tmp_path / "gone.jpg"), media)


# --------------------------------------------------------------------------- #
# Transcript: references on disk, base64 only on the wire
# --------------------------------------------------------------------------- #
def test_text_only_turn_stays_a_plain_string(tmp_path, media):
    assert obd_images.user_turn("hello", []) == {"role": "user", "content": "hello"}


def test_turn_with_photo_keeps_text_after_the_image(tmp_path, media):
    att = obd_images.prepare(make_image(str(tmp_path / "a.jpg")), media)
    turn = obd_images.user_turn("is this cracked?", [att])
    assert [b["type"] for b in turn["content"]] == [obd_images.IMAGE_REF_TYPE, "text"]
    assert turn["content"][-1]["text"] == "is this cracked?"


def test_empty_text_with_photo_gets_a_usable_prompt(tmp_path, media):
    att = obd_images.prepare(make_image(str(tmp_path / "a.jpg")), media)
    turn = obd_images.user_turn("", [att])
    assert turn["content"][-1]["text"] == "(see attached photo)"


def test_session_json_holds_a_path_not_base64(tmp_path, media):
    att = obd_images.prepare(make_image(str(tmp_path / "a.jpg")), media)
    messages = [obd_images.user_turn("look", [att])]
    blob = json.dumps(messages)
    assert att["path"] in blob
    assert "base64" not in blob
    assert len(blob) < 2000            # a photo must not bloat the transcript


def test_inflate_for_claude_embeds_the_image(tmp_path, media):
    att = obd_images.prepare(make_image(str(tmp_path / "a.jpg")), media)
    messages = [obd_images.user_turn("look", [att])]
    sent = obd_images.inflate_for_claude(messages)
    block = sent[0]["content"][0]
    assert block["type"] == "image"
    assert block["source"]["media_type"] == "image/jpeg"
    assert len(block["source"]["data"]) > 100
    # the stored transcript is untouched — still a reference
    assert messages[0]["content"][0]["type"] == obd_images.IMAGE_REF_TYPE


def test_inflate_for_openai_uses_a_data_uri(tmp_path, media):
    att = obd_images.prepare(make_image(str(tmp_path / "a.jpg")), media)
    sent = obd_images.inflate_for_openai([obd_images.user_turn("look", [att])])
    url = sent[0]["content"][0]["image_url"]["url"]
    assert url.startswith("data:image/jpeg;base64,")


def test_inflate_leaves_ordinary_messages_alone():
    messages = [{"role": "user", "content": "plain"},
                {"role": "user", "content": [{"type": "tool_result", "content": "x"}]}]
    assert obd_images.inflate_for_claude(messages) == messages


def test_resuming_after_the_photo_was_deleted_does_not_crash(tmp_path, media):
    att = obd_images.prepare(make_image(str(tmp_path / "a.jpg")), media)
    messages = [obd_images.user_turn("look", [att])]
    os.remove(att["path"])
    obd_images._b64.clear()
    sent = obd_images.inflate_for_claude(messages)
    assert sent[0]["content"][0]["type"] == "text"
    assert "no longer available" in sent[0]["content"][0]["text"]


def test_count_images(tmp_path, media):
    att = obd_images.prepare(make_image(str(tmp_path / "a.jpg")), media)
    messages = [obd_images.user_turn("a", [att]), obd_images.user_turn("b", [])]
    assert obd_images.count_images(messages) == 1


# --------------------------------------------------------------------------- #
# Capture sources: unavailable hardware must explain itself, not throw
# --------------------------------------------------------------------------- #
def test_camera_without_ffmpeg_says_how_to_proceed(monkeypatch, media):
    monkeypatch.setattr(obd_images.shutil, "which", lambda n: None)
    with pytest.raises(obd_images.ImageError, match="ffmpeg is not installed"):
        obd_images.capture_camera(os.path.join(media, "x.jpg"))


def test_camera_with_no_video_devices(monkeypatch, media):
    monkeypatch.setattr(obd_images.shutil, "which",
                        lambda n: "/usr/bin/ffmpeg" if n == "ffmpeg" else None)
    monkeypatch.setattr(obd_images, "video_devices", lambda: [])
    with pytest.raises(obd_images.ImageError, match="No camera found"):
        obd_images.capture_camera(os.path.join(media, "x.jpg"))


def test_termux_camera_is_preferred_on_android(monkeypatch, media):
    """The Android port has no /dev/video*; termux-camera-photo is the camera."""
    monkeypatch.setattr(obd_images.shutil, "which",
                        lambda n: "/data/.../termux-camera-photo" if n == "termux-camera-photo" else None)
    dest = os.path.join(media, "snap.jpg")

    def fake_run(cmd, timeout=30):
        assert cmd[0] == "termux-camera-photo"
        make_image(cmd[-1])
        return 0, ""

    monkeypatch.setattr(obd_images, "_run", fake_run)
    assert obd_images.capture_camera(dest) == dest


def test_phone_without_adb(monkeypatch, media):
    monkeypatch.setattr(obd_images.shutil, "which", lambda n: None)
    with pytest.raises(obd_images.ImageError, match="adb is not installed"):
        obd_images.pull_from_phone(media)


def test_phone_not_attached(monkeypatch, media):
    monkeypatch.setattr(obd_images.shutil, "which", lambda n: "/usr/bin/adb")
    monkeypatch.setattr(obd_images, "_run",
                        lambda cmd, timeout=30: (0, "List of devices attached\n\n"))
    with pytest.raises(obd_images.ImageError, match="USB debugging"):
        obd_images.pull_from_phone(media)


def test_phone_unauthorized_is_called_out(monkeypatch, media):
    monkeypatch.setattr(obd_images.shutil, "which", lambda n: "/usr/bin/adb")
    monkeypatch.setattr(obd_images, "_run", lambda cmd, timeout=30:
                        (0, "List of devices attached\nABC123\tunauthorized\n"))
    with pytest.raises(obd_images.ImageError, match="unauthorized"):
        obd_images.pull_from_phone(media)


def test_phone_pulls_newest_dcim_photo(monkeypatch, tmp_path, media):
    monkeypatch.setattr(obd_images.shutil, "which", lambda n: "/usr/bin/adb")
    src = make_image(str(tmp_path / "IMG_2026.jpg"))

    def fake_run(cmd, timeout=30):
        if cmd[:2] == ["adb", "devices"]:
            return 0, "List of devices attached\nABC123\tdevice\n"
        if cmd[:2] == ["adb", "shell"]:
            return 0, "IMG_2026.jpg\nolder.jpg\n"
        if cmd[:2] == ["adb", "pull"]:
            import shutil as sh
            sh.copy2(src, cmd[3])
            return 0, ""
        return 1, "?"

    monkeypatch.setattr(obd_images, "_run", fake_run)
    got = obd_images.pull_from_phone(media)
    assert os.path.basename(got) == "IMG_2026.jpg"


# --------------------------------------------------------------------------- #
# The chat turn loop
# --------------------------------------------------------------------------- #
def _state(media):
    return {"media_dir": media, "pending": [], "listing": []}


def _scripted_input(monkeypatch, lines):
    it = iter(lines)
    monkeypatch.setattr("builtins.input", lambda *a: next(it))


def test_pic_command_queues_photo_and_sends_with_next_message(monkeypatch, tmp_path, media):
    pic = make_image(str(tmp_path / "hose.jpg"))
    state = _state(media)
    _scripted_input(monkeypatch, [f"/pic {pic}", "is this cracked?"])
    text, attachments = obd_chat.collect_turn(state)
    assert text == "is this cracked?"
    assert len(attachments) == 1
    assert state["pending"] == []        # queue is handed over, not duplicated


def test_bare_pic_takes_the_newest_watch_folder_photo(monkeypatch, tmp_path, media):
    make_image(str(tmp_path / "old.jpg"))
    new = make_image(str(tmp_path / "new.jpg"))
    os.utime(str(tmp_path / "old.jpg"), (1000, 1000))
    monkeypatch.setenv("OBD_PHOTO_DIRS", str(tmp_path))
    _scripted_input(monkeypatch, ["/pic", "what is this"])
    _, attachments = obd_chat.collect_turn(_state(media))
    assert attachments[0]["source"] == new


def test_photos_listing_then_pick_by_number(monkeypatch, tmp_path, media):
    make_image(str(tmp_path / "a.jpg"))
    b = make_image(str(tmp_path / "b.jpg"))
    os.utime(str(tmp_path / "a.jpg"), (1000, 1000))
    monkeypatch.setenv("OBD_PHOTO_DIRS", str(tmp_path))
    state = _state(media)
    _scripted_input(monkeypatch, ["/photos", "/pic 1", "this one"])
    text, attachments = obd_chat.collect_turn(state)
    assert text == "this one"
    assert attachments[0]["source"] == b          # 1 = newest in the listing


def test_bad_pic_path_reports_and_queues_nothing(monkeypatch, capsys, tmp_path, media):
    state = _state(media)
    _scripted_input(monkeypatch, ["/pic /nope/missing.jpg", "hello"])
    text, attachments = obd_chat.collect_turn(state)
    assert attachments == []
    assert text == "hello"                        # the turn still goes through
    assert "Could not attach" in capsys.readouterr().out


def test_drop_clears_the_queue(monkeypatch, tmp_path, media):
    pic = make_image(str(tmp_path / "a.jpg"))
    state = _state(media)
    _scripted_input(monkeypatch, [f"/pic {pic}", "/drop", "never mind"])
    text, attachments = obd_chat.collect_turn(state)
    assert (text, attachments) == ("never mind", [])


def test_empty_line_sends_a_queued_photo_alone(monkeypatch, tmp_path, media):
    pic = make_image(str(tmp_path / "a.jpg"))
    state = _state(media)
    _scripted_input(monkeypatch, [f"/pic {pic}", ""])
    text, attachments = obd_chat.collect_turn(state)
    assert text == "" and len(attachments) == 1


def test_empty_line_with_nothing_queued_just_reprompts(monkeypatch, media):
    _scripted_input(monkeypatch, ["", "  ", "hello"])
    text, _ = obd_chat.collect_turn(_state(media))
    assert text == "hello"


def test_inline_path_is_attached_automatically(monkeypatch, tmp_path, media):
    pic = make_image(str(tmp_path / "coil.jpg"))
    _scripted_input(monkeypatch, [f"whats this {pic}"])
    text, attachments = obd_chat.collect_turn(_state(media))
    assert len(attachments) == 1 and pic in text


def test_help_is_not_sent_as_a_message(monkeypatch, capsys, media):
    _scripted_input(monkeypatch, ["/help", "hi"])
    text, _ = obd_chat.collect_turn(_state(media))
    assert text == "hi"
    assert "/snap" in capsys.readouterr().out


def test_quit_ends_the_chat(monkeypatch, media):
    _scripted_input(monkeypatch, ["quit"])
    assert obd_chat.collect_turn(_state(media)) == (None, [])


def test_unknown_slash_command_is_sent_as_text(monkeypatch, media):
    """Only photo commands are intercepted — anything else is a real question."""
    _scripted_input(monkeypatch, ["/dev/ttyUSB0 is what port?"])
    text, _ = obd_chat.collect_turn(_state(media))
    assert text.startswith("/dev/ttyUSB0")


def test_turn_count_includes_photo_turns_but_not_tool_results(tmp_path, media):
    att = obd_images.prepare(make_image(str(tmp_path / "a.jpg")), media)
    messages = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
        obd_images.user_turn("look at this", [att]),
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": "x"}]},
    ]
    assert obd_chat._turn_count(messages) == 2
