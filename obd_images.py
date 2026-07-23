#!/usr/bin/env python3
"""Photos for the chat assistant — attach, capture, downscale, encode.

At the car a picture settles things live data can't: is that hose cracked, which
connector is this, what does the dash say. This module gets an image from
wherever it is and hands the chat a small, correctly-rotated JPEG.

Sources (each returns a path, or raises ImageError explaining why it can't):
    from_path      an explicit file, ~ and globs allowed
    newest_image   the newest photo in the watch folders — phone -> sync -> /pic
    capture_camera a frame from a webcam (ffmpeg) or the device camera (Termux)
    pull_from_phone the newest DCIM photo off a USB-attached Android (adb)

Everything is platform-probed at call time rather than at import, so the same
code runs on a laptop today and inside Termux on Android later: the watch folders
include the Android DCIM paths, and camera capture prefers termux-camera-photo
when it exists.

Images are never embedded in the session JSON. `prepare()` writes a downscaled
copy into the session's media folder and the transcript keeps a path reference,
which `inflate_*` turns back into base64 only at the moment of an API call.
"""

import base64
import glob
import mimetypes
import os
import re
import shutil
import subprocess
import sys

try:
    from PIL import Image, ImageOps
except ImportError:  # optional — ffmpeg or a plain copy is used instead
    Image = ImageOps = None


class ImageError(Exception):
    """An image could not be obtained or prepared — message is user-facing."""


IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".heic")

# Long-edge cap. Claude downsamples above ~1568 px anyway, and every pixel past
# that is tokens spent for no extra detail.
MAX_EDGE = 1568
# Well under the 5 MB/image API ceiling once base64 inflates it by ~33%.
MAX_BYTES = 3_500_000

# Reference block kept in the transcript in place of base64 image data.
IMAGE_REF_TYPE = "obd_image"


# --------------------------------------------------------------------------- #
# Finding images
# --------------------------------------------------------------------------- #
def watch_dirs():
    """Folders scanned by a bare `/pic`, newest-photo-first.

    OBD_PHOTO_DIRS (colon-separated) wins, then the usual phone-sync targets on
    desktop, then the Android/Termux camera paths so a port keeps working.
    """
    env = os.getenv("OBD_PHOTO_DIRS")
    if env:
        dirs = [d for d in env.split(":") if d.strip()]
    else:
        home = os.path.expanduser("~")
        dirs = [
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "photos"),
            os.path.join(home, "Dropbox", "Camera Uploads"),
            os.path.join(home, "Pictures"),
            os.path.join(home, "Downloads"),
            # Android (Termux): ~/storage/* are the symlinks termux-setup-storage makes
            os.path.join(home, "storage", "dcim"),
            os.path.join(home, "storage", "pictures"),
            os.path.join(home, "storage", "downloads"),
            "/sdcard/DCIM/Camera",
        ]
    return [d for d in dirs if os.path.isdir(d)]


def _is_image(path):
    return path.lower().endswith(IMAGE_EXTS)


def recent_images(limit=10, dirs=None):
    """The `limit` most recently modified images across the watch folders."""
    found = []
    for d in (dirs if dirs is not None else watch_dirs()):
        try:
            for name in os.listdir(d):
                p = os.path.join(d, name)
                if _is_image(name) and os.path.isfile(p):
                    found.append((os.path.getmtime(p), p))
        except OSError:
            continue
    found.sort(reverse=True)
    return [p for _, p in found[:limit]]


def newest_image(dirs=None):
    """The most recent photo in the watch folders — the phone-sync workflow."""
    recent = recent_images(1, dirs)
    if not recent:
        searched = dirs if dirs is not None else watch_dirs()
        raise ImageError(
            "No images found in: " + (", ".join(searched) or "(no watch folders exist)")
            + "\n  Give a path instead (/pic <file>), or set OBD_PHOTO_DIRS to your photo folder.")
    return recent[0]


def from_path(spec):
    """Resolve a user-typed path: ~ expansion, globs, quotes, file:// URLs."""
    spec = spec.strip().strip('"').strip("'")
    if spec.startswith("file://"):
        spec = spec[7:]
    spec = os.path.expanduser(spec)
    matches = sorted(glob.glob(spec)) if any(c in spec for c in "*?[") else [spec]
    matches = [m for m in matches if os.path.isfile(m)]
    if not matches:
        raise ImageError(f"No such file: {spec}")
    path = matches[-1]
    if not _is_image(path):
        raise ImageError(f"Not an image file: {path} (expected {', '.join(IMAGE_EXTS)})")
    return path


# A bare path typed inside an ordinary sentence, quoted or escaped-space form.
_INLINE_PATH = re.compile(
    r"(?:'[^']+'|\"[^\"]+\"|(?:[~./][^\s]*|/[^\s]*|[\w.-]+/[^\s]*)(?:\\ [^\s]*)*)")


def find_inline_images(text):
    """Image paths mentioned in ordinary typed text, in order, deduplicated."""
    out = []
    for raw in _INLINE_PATH.findall(text or ""):
        cand = raw.strip("'\"").replace("\\ ", " ")
        if not _is_image(cand):
            continue
        cand = os.path.expanduser(cand)
        if os.path.isfile(cand) and cand not in out:
            out.append(cand)
    return out


# --------------------------------------------------------------------------- #
# Capturing
# --------------------------------------------------------------------------- #
def _run(cmd, timeout=30):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "timed out"
    except OSError as e:
        return 127, str(e)


def video_devices():
    return sorted(glob.glob("/dev/video*"))


def capture_camera(dest, device=None, size="1280x720"):
    """Grab a still from the camera. Returns `dest`.

    Termux's camera is used when present (the Android port), otherwise a V4L2
    webcam via ffmpeg. Several /dev/video* nodes are metadata-only, so each is
    tried in turn until one yields a frame.
    """
    if shutil.which("termux-camera-photo"):
        code, out = _run(["termux-camera-photo", "-c", str(device or 0), dest], timeout=60)
        if code == 0 and os.path.exists(dest) and os.path.getsize(dest) > 0:
            return dest
        raise ImageError(f"termux-camera-photo failed: {out.strip() or 'no image written'}")

    if not shutil.which("ffmpeg"):
        raise ImageError("No camera capture available — ffmpeg is not installed "
                         "(`sudo apt install ffmpeg`), or attach a photo with /pic <file>.")

    devices = [device] if device else video_devices()
    if not devices:
        raise ImageError("No camera found (no /dev/video* devices). "
                         "Plug in a webcam, or use /pic <file> / /phone.")

    errors = []
    for dev in devices:
        for extra in (["-vf", "select=gte(n\\,4)"], []):   # skip dark warm-up frames
            code, out = _run(["ffmpeg", "-y", "-f", "v4l2", "-video_size", size,
                              "-i", dev, *extra, "-frames:v", "1", "-q:v", "3", dest],
                             timeout=30)
            if code == 0 and os.path.exists(dest) and os.path.getsize(dest) > 0:
                return dest
        errors.append(f"{dev}: {out.strip().splitlines()[-1] if out.strip() else 'no frame'}")
    raise ImageError("Camera capture failed.\n  " + "\n  ".join(errors))


ANDROID_DCIM = ["/sdcard/DCIM/Camera", "/sdcard/DCIM", "/sdcard/Pictures"]


def pull_from_phone(dest_dir):
    """Copy the newest photo off a USB-attached Android phone. Returns the path."""
    if not shutil.which("adb"):
        raise ImageError("adb is not installed (`sudo apt install android-tools-adb`) — "
                         "use /pic <file> once the photo has synced instead.")
    code, out = _run(["adb", "devices"], timeout=15)
    attached = [l.split()[0] for l in out.splitlines()[1:]
                if l.strip() and l.split()[-1] == "device"]
    if not attached:
        unauthorized = "unauthorized" in out
        raise ImageError(
            "No phone attached over USB." +
            (" The phone shows as 'unauthorized' — unlock it and accept the "
             "'Allow USB debugging' prompt." if unauthorized else
             " Plug it in, unlock it, and turn on USB debugging (Developer options)."))

    remote = None
    for d in ANDROID_DCIM:
        code, out = _run(["adb", "shell", f"ls -t {d} 2>/dev/null | head -20"], timeout=20)
        names = [n.strip() for n in out.splitlines() if _is_image(n.strip())]
        if names:
            remote = f"{d}/{names[0]}"
            break
    if not remote:
        raise ImageError("No photos found on the phone under " + ", ".join(ANDROID_DCIM))

    local = os.path.join(dest_dir, os.path.basename(remote))
    os.makedirs(dest_dir, exist_ok=True)
    code, out = _run(["adb", "pull", remote, local], timeout=120)
    if code != 0 or not os.path.exists(local):
        raise ImageError(f"adb pull failed for {remote}: {out.strip()}")
    return local


# --------------------------------------------------------------------------- #
# Preparing (downscale + copy into the session's media folder)
# --------------------------------------------------------------------------- #
def _media_type(path):
    mt, _ = mimetypes.guess_type(path)
    if mt in ("image/jpeg", "image/png", "image/gif", "image/webp"):
        return mt
    return "image/jpeg"


def _shrink_pillow(src, dest):
    with Image.open(src) as img:
        img = ImageOps.exif_transpose(img)      # phone photos carry rotation in EXIF
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        img.thumbnail((MAX_EDGE, MAX_EDGE))
        img.save(dest, "JPEG", quality=85, optimize=True)
        return img.size


def _shrink_ffmpeg(src, dest):
    code, out = _run(["ffmpeg", "-y", "-i", src, "-vf",
                      f"scale='min({MAX_EDGE},iw)':-2", "-q:v", "3", dest], timeout=60)
    if code != 0 or not os.path.exists(dest):
        raise ImageError(f"could not downscale {src}: {out.strip().splitlines()[-1] if out else ''}")
    return None


def prepare(src, media_dir, label=None):
    """Downscale `src` into `media_dir` and describe the result.

    Returns {"path", "media_type", "size", "bytes", "source"} — `path` is what the
    transcript stores. Raises ImageError if the image cannot be made sendable.
    """
    if not os.path.isfile(src):
        raise ImageError(f"No such file: {src}")
    os.makedirs(media_dir, exist_ok=True)
    n = len(glob.glob(os.path.join(media_dir, "pic_*"))) + 1
    dest = os.path.join(media_dir, f"pic_{n:02d}.jpg")

    size = None
    if Image is not None:
        try:
            size = _shrink_pillow(src, dest)
        except ImageError:
            raise
        except Exception as e:
            raise ImageError(f"could not read {os.path.basename(src)} as an image: {e}") from e
    elif shutil.which("ffmpeg"):
        _shrink_ffmpeg(src, dest)
    else:
        # No image tooling at all: send the original, but only if it is small
        # enough that the API will accept it.
        if os.path.getsize(src) > MAX_BYTES:
            raise ImageError(
                f"{os.path.basename(src)} is {os.path.getsize(src) // 1024} KB — too big to send, "
                "and neither Pillow nor ffmpeg is available to shrink it "
                "(`pip install pillow`).")
        dest = os.path.join(media_dir, f"pic_{n:02d}" + os.path.splitext(src)[1].lower())
        shutil.copy2(src, dest)

    nbytes = os.path.getsize(dest)
    if nbytes > MAX_BYTES:
        raise ImageError(f"{os.path.basename(dest)} is still {nbytes // 1024} KB after "
                         "downscaling — too large to send.")
    return {"path": dest, "media_type": _media_type(dest), "size": size,
            "bytes": nbytes, "source": label or src}


def describe(att):
    """One-line human summary of a prepared attachment."""
    where = att.get("source") or att["path"]
    dims = f", {att['size'][0]}x{att['size'][1]}" if att.get("size") else ""
    return f"{os.path.basename(where)} ({att['bytes'] // 1024} KB{dims})"


# --------------------------------------------------------------------------- #
# Transcript blocks: reference in the session file, base64 only on the wire
# --------------------------------------------------------------------------- #
def ref_block(att):
    """The block stored in the transcript — a path, never image data."""
    return {"type": IMAGE_REF_TYPE, "path": att["path"],
            "media_type": att["media_type"], "source": att.get("source", "")}


def user_turn(text, attachments):
    """Build a user message from typed text plus any attachments."""
    if not attachments:
        return {"role": "user", "content": text}
    blocks = [ref_block(a) for a in attachments]
    blocks.append({"type": "text", "text": text or "(see attached photo)"})
    return {"role": "user", "content": blocks}


class _B64Cache(dict):
    def load(self, path):
        if path not in self:
            with open(path, "rb") as f:
                self[path] = base64.standard_b64encode(f.read()).decode()
        return self[path]


_b64 = _B64Cache()


def _missing_note(block):
    return {"type": "text",
            "text": f"[photo no longer available at {block.get('path')} — "
                    "it was attached earlier in this session but the file is gone]"}


def _convert(messages, image_block):
    """Copy `messages` with every image reference replaced by `image_block(ref)`."""
    out = []
    for m in messages:
        content = m.get("content")
        if not isinstance(content, list) or not any(
                isinstance(b, dict) and b.get("type") == IMAGE_REF_TYPE for b in content):
            out.append(m)
            continue
        blocks = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == IMAGE_REF_TYPE:
                try:
                    blocks.append(image_block(b))
                except OSError:
                    blocks.append(_missing_note(b))
            else:
                blocks.append(b)
        out.append({**m, "content": blocks})
    return out


def inflate_for_claude(messages):
    def block(ref):
        return {"type": "image", "source": {"type": "base64",
                                            "media_type": ref.get("media_type", "image/jpeg"),
                                            "data": _b64.load(ref["path"])}}
    return _convert(messages, block)


def inflate_for_openai(messages):
    def block(ref):
        mt = ref.get("media_type", "image/jpeg")
        return {"type": "image_url",
                "image_url": {"url": f"data:{mt};base64,{_b64.load(ref['path'])}"}}
    return _convert(messages, block)


def count_images(messages):
    return sum(1 for m in messages if isinstance(m.get("content"), list)
               for b in m["content"]
               if isinstance(b, dict) and b.get("type") == IMAGE_REF_TYPE)


# --------------------------------------------------------------------------- #
# Standalone check: what photo sources work on this machine?
# --------------------------------------------------------------------------- #
def main():
    dirs = watch_dirs()
    print("Watch folders (bare /pic takes the newest image here):")
    for d in dirs:
        print(f"  {d}")
    if not dirs:
        print("  (none — set OBD_PHOTO_DIRS)")

    print("\nMost recent photos:")
    for p in recent_images(5):
        print(f"  {p}")

    print("\nCamera:")
    if shutil.which("termux-camera-photo"):
        print("  termux-camera-photo available (Android)")
    devs = video_devices()
    print(f"  video devices: {', '.join(devs) if devs else '(none)'}")
    print(f"  ffmpeg: {shutil.which('ffmpeg') or '(not installed)'}")

    print("\nPhone over USB:")
    if not shutil.which("adb"):
        print("  adb not installed")
    else:
        _, out = _run(["adb", "devices"], timeout=15)
        attached = [l for l in out.splitlines()[1:] if l.strip()]
        print("  " + ("\n  ".join(attached) if attached else "no device attached"))

    print("\nImage processing:")
    print(f"  Pillow: {'yes' if Image is not None else 'no (install: pip install pillow)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
