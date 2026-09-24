import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, Optional

import yt_dlp
from flask import (
    Flask,
    abort,
    jsonify,
    render_template,
    request,
    send_from_directory,
    session,
)
from werkzeug.utils import secure_filename

BASE_DIR = Path(__file__).resolve().parent

# Environment and storage paths (Supports AWS Lambda /tmp and local)
IS_LAMBDA = bool(os.environ.get("AWS_LAMBDA_FUNCTION_NAME") or os.environ.get("LAMBDA_TASK_ROOT"))
DEFAULT_STORAGE_ROOT = Path("/tmp/psp_converter") if IS_LAMBDA else (BASE_DIR / "storage")
STORAGE_ROOT = Path(os.environ.get("STORAGE_DIR", str(DEFAULT_STORAGE_ROOT)))

USERS_DIR = STORAGE_ROOT / "users"
JOBS_DIR = STORAGE_ROOT / "jobs"

for folder in (STORAGE_ROOT, USERS_DIR, JOBS_DIR):
    folder.mkdir(parents=True, exist_ok=True)

# Application constraints
MAX_UPLOAD_SIZE = 430 * 1024 * 1024  # 430 MB
MAX_DOWNLOADS_PER_USER = 3
MAX_CONVERSIONS_PER_USER = 5
SESSION_TIMEOUT_SECONDS = int(os.environ.get("SESSION_TIMEOUT_SECONDS", "300"))  # 5 minutes idle timeout
DISCONNECT_GRACE_SECONDS = 30  # Grace period to differentiate tab close from page refresh or download clicks

app = Flask(__name__, template_folder=str(BASE_DIR))
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_SIZE
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "novavideo-psp-super-secret-key-production-ready")
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_NAME"] = "novavideo_session"

job_executor = ThreadPoolExecutor(max_workers=2)
jobs: Dict[str, dict] = {}
jobs_lock = threading.Lock()

user_last_seen: Dict[str, float] = {}
disconnect_intents: Dict[str, float] = {}
user_lock = threading.Lock()
last_sweep_time = 0.0

with open(BASE_DIR / "psp-preset.json", "r", encoding="utf-8") as preset_file:
    PSP_PRESET = json.load(preset_file)


# ---------------------------------------------------------------------------
# Session & User Isolation Helpers
# ---------------------------------------------------------------------------
def get_user_id() -> str:
    """Retrieve or generate an isolated user session ID."""
    if "user_id" not in session:
        session["user_id"] = uuid.uuid4().hex
        session.permanent = True
    return session["user_id"]


def get_user_storage(user_id: str) -> Path:
    """Return user root directory, creating it if needed."""
    user_dir = USERS_DIR / user_id
    user_dir.mkdir(parents=True, exist_ok=True)
    return user_dir


def get_user_subdir(user_id: str, subfolder: str) -> Path:
    """Return an isolated user subdirectory (e.g. downloads, converted, uploads)."""
    folder = get_user_storage(user_id) / subfolder
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def record_user_activity(user_id: str):
    """Mark the user as active and clear any pending disconnect intent."""
    with user_lock:
        user_last_seen[user_id] = time.time()
        disconnect_intents.pop(user_id, None)


def mark_user_disconnect_intent(user_id: str):
    """Mark that a user may have disconnected (e.g. pagehide beacon)."""
    with user_lock:
        disconnect_intents[user_id] = time.time()


def user_has_active_jobs(user_id: str) -> bool:
    """Check if the user currently has running or queued jobs."""
    with jobs_lock:
        return any(
            j.get("user_id") == user_id and j.get("status") in ("queued", "running")
            for j in jobs.values()
        )


def delete_user_data(user_id: str):
    """Permanently delete all files (uploaded, converted, downloaded) and jobs for a user."""
    user_dir = USERS_DIR / user_id
    if user_dir.exists():
        shutil.rmtree(user_dir, ignore_errors=True)

    with jobs_lock:
        stale_jobs = [jid for jid, j in jobs.items() if j.get("user_id") == user_id]
        for jid in stale_jobs:
            jobs.pop(jid, None)
            job_file = JOBS_DIR / f"{jid}.json"
            if job_file.exists():
                try:
                    job_file.unlink(missing_ok=True)
                except OSError:
                    pass

    with user_lock:
        user_last_seen.pop(user_id, None)
        disconnect_intents.pop(user_id, None)


def sweep_stale_sessions():
    """Remove user data for disconnected users or sessions idle past timeout."""
    global last_sweep_time
    now = time.time()
    last_sweep_time = now

    users_to_delete = []
    with user_lock:
        # Check users whose disconnect grace period expired without re-connecting
        for uid, intent_time in list(disconnect_intents.items()):
            if now - intent_time >= DISCONNECT_GRACE_SECONDS:
                users_to_delete.append(uid)

        # Check users who haven't sent a heartbeat/request past idle timeout
        for uid, last_time in list(user_last_seen.items()):
            if now - last_time >= SESSION_TIMEOUT_SECONDS:
                users_to_delete.append(uid)

    for uid in set(users_to_delete):
        # Do not purge data while a user still has active jobs in flight
        if not user_has_active_jobs(uid):
            delete_user_data(uid)

    # Clean orphaned directories on disk (older than timeout)
    if USERS_DIR.exists():
        try:
            for ufolder in USERS_DIR.iterdir():
                if ufolder.is_dir():
                    try:
                        mtime = ufolder.stat().st_mtime
                        if now - mtime >= SESSION_TIMEOUT_SECONDS:
                            shutil.rmtree(ufolder, ignore_errors=True)
                    except OSError:
                        pass
        except OSError:
            pass


@app.before_request
def cleanup_check():
    """Periodically sweep stale sessions on request activity (ideal for serverless Lambda)."""
    global last_sweep_time
    now = time.time()
    if now - last_sweep_time > 15:
        sweep_stale_sessions()


def start_background_cleaner():
    """Run background thread for local server execution."""
    def worker():
        while True:
            time.sleep(10)
            try:
                sweep_stale_sessions()
            except Exception:
                pass

    t = threading.Thread(target=worker, daemon=True)
    t.start()


start_background_cleaner()


# ---------------------------------------------------------------------------
# Job Management (In-Memory + Disk Persistence for Multi-Worker / Lambda)
# ---------------------------------------------------------------------------
def save_job_to_disk(job_id: str, job_data: dict):
    try:
        job_file = JOBS_DIR / f"{job_id}.json"
        temp_file = JOBS_DIR / f"{job_id}.json.tmp"
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(job_data, f)
        temp_file.replace(job_file)
    except OSError:
        pass


def load_job_from_disk(job_id: str) -> Optional[dict]:
    try:
        job_file = JOBS_DIR / f"{job_id}.json"
        if job_file.is_file():
            with open(job_file, "r", encoding="utf-8") as f:
                return json.load(f)
    except (OSError, json.JSONDecodeError):
        pass
    return None


def update_job(job_id: str, **changes):
    data = None
    with jobs_lock:
        if job_id in jobs:
            jobs[job_id].update(changes)
            data = dict(jobs[job_id])
    if data:
        save_job_to_disk(job_id, data)


def create_job(job_type: str, user_id: str):
    job_id = uuid.uuid4().hex
    job_data = {
        "id": job_id,
        "type": job_type,
        "user_id": user_id,
        "status": "queued",
        "percent": 0,
        "message": "Waiting to start...",
        "created_at": time.time(),
    }
    with jobs_lock:
        jobs[job_id] = job_data
    save_job_to_disk(job_id, job_data)
    return job_id


def get_job(job_id: str) -> Optional[dict]:
    with jobs_lock:
        if job_id in jobs:
            return dict(jobs[job_id])
    return load_job_from_disk(job_id)


def run_job(job_id: str, worker):
    update_job(job_id, status="running", message="Starting...")
    try:
        result = worker()
        update_job(job_id, status="completed", percent=100, message="Finished.", result=result)
    except Exception as exc:  # pragma: no cover - runtime failure path
        update_job(job_id, status="failed", message=str(exc), error=str(exc))


# ---------------------------------------------------------------------------
# FFmpeg Resolution (Supports Lambda Layers, FFMPEG_PATH, System PATH, imageio)
# ---------------------------------------------------------------------------
def resolve_ffmpeg():
    configured_path = os.environ.get("FFMPEG_PATH")
    if configured_path:
        configured_executable = Path(configured_path).expanduser()
        if configured_executable.is_file():
            return str(configured_executable)
        raise FileNotFoundError(f"FFMPEG_PATH does not point to a file: {configured_executable}")

    # Standard AWS Lambda Layer locations
    for layer_candidate in ("/opt/bin/ffmpeg", "/opt/ffmpeg/ffmpeg", "/opt/ffmpeg"):
        if Path(layer_candidate).is_file():
            return layer_candidate

    system_executable = shutil.which("ffmpeg")
    if system_executable:
        return system_executable

    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, RuntimeError) as exc:
        raise FileNotFoundError(
            "FFmpeg was not found. Install it, set FFMPEG_PATH, or attach an FFmpeg Lambda Layer."
        ) from exc


def get_video_duration(ffmpeg_executable: str, input_file: Path):
    probe = subprocess.run(
        [ffmpeg_executable, "-hide_banner", "-i", str(input_file)],
        capture_output=True,
        text=True,
    )
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", probe.stderr)
    if not match:
        return None
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def list_recent_files(folder: Path):
    if not folder.exists():
        return []
    files = sorted(folder.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
    return [p.name for p in files if p.is_file()]


def get_user_quota(user_id: str) -> dict:
    """Return user file counts and remaining limits."""
    downloads = list_recent_files(get_user_subdir(user_id, "downloads"))
    converted = list_recent_files(get_user_subdir(user_id, "converted"))

    with jobs_lock:
        active_downloads = sum(
            1
            for j in jobs.values()
            if j.get("user_id") == user_id
            and j.get("type") == "download"
            and j.get("status") in ("queued", "running")
        )
        active_conversions = sum(
            1
            for j in jobs.values()
            if j.get("user_id") == user_id
            and j.get("type") == "convert"
            and j.get("status") in ("queued", "running")
        )

    total_downloads = len(downloads) + active_downloads
    total_conversions = len(converted) + active_conversions

    return {
        "downloads_count": total_downloads,
        "downloads_max": MAX_DOWNLOADS_PER_USER,
        "downloads_remaining": max(0, MAX_DOWNLOADS_PER_USER - total_downloads),
        "conversions_count": total_conversions,
        "conversions_max": MAX_CONVERSIONS_PER_USER,
        "conversions_remaining": max(0, MAX_CONVERSIONS_PER_USER - total_conversions),
        "downloads_list": downloads,
        "converted_list": converted,
    }


def resolve_download_quality(quality: str):
    quality_map = {
        "720p": "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best",
        "1080p": "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080][ext=mp4]/best",
        "audio_m4a": "bestaudio[ext=m4a]/bestaudio/best",
    }
    return quality_map.get(quality, quality_map["1080p"])


def download_from_youtube(url: str, quality: str, output_dir: Path, progress_callback=None):
    safe_name = url.rsplit("/", 1)[-1] or "video"
    output_template = str(output_dir / "%(title)s.%(ext)s")

    def download_progress(progress):
        if not progress_callback:
            return
        if progress.get("status") == "downloading":
            downloaded = progress.get("downloaded_bytes", 0)
            total = progress.get("total_bytes") or progress.get("total_bytes_estimate")
            percent = round(downloaded * 100 / total, 1) if total else 0
            eta = progress.get("eta")
            message = f"Downloading... {percent:.1f}%"
            if eta is not None:
                message += f" (about {eta}s remaining)"
            progress_callback(percent, message)
        elif progress.get("status") == "finished":
            progress_callback(100, "Download complete. Processing file...")

    ydl_opts = {
        "format": resolve_download_quality(quality),
        "outtmpl": output_template,
        "noplaylist": True,
        "ffmpeg_location": resolve_ffmpeg(),
        "quiet": True,
        "no_warnings": True,
        "merge_output_format": "mp4",
        "paths": {"home": str(output_dir)},
        "progress_hooks": [download_progress],
    }

    if quality == "audio_m4a":
        ydl_opts.update(
            {
                "postprocessors": [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "m4a",
                        "preferredquality": "0",
                    }
                ]
            }
        )

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)

    # 1. Check requested_downloads from yt-dlp info
    requested = info.get("requested_downloads") if info else None
    if requested and len(requested) > 0 and requested[0].get("filepath"):
        saved_file = Path(requested[0]["filepath"])
        if saved_file.is_file():
            return saved_file.name

    # 2. Check prepared filename
    try:
        prepared = Path(ydl.prepare_filename(info))
        if prepared.is_file():
            return prepared.name
        for ext in (".mp4", ".m4a", ".webm", ".mkv", ".mp3"):
            cand = prepared.with_suffix(ext)
            if cand.is_file():
                return cand.name
    except Exception:
        pass

    # 3. Find most recently modified file in output_dir
    files = sorted(output_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
    for p in files:
        if p.is_file():
            return p.name

    return f"{safe_name}.mp4"


def psp_preset_settings():
    preset = PSP_PRESET.get("PresetList", [{}])[0]
    audio = preset.get("AudioList", [{}])[0]
    return {
        "width": int(preset.get("PictureWidth", 480)),
        "height": int(preset.get("PictureHeight", 272)),
        "fps": int(float(preset.get("VideoFramerate", 30))),
        "video_preset": preset.get("VideoPreset", "medium"),
        "video_profile": preset.get("VideoProfile", "baseline"),
        "video_level": preset.get("VideoLevel", "1.3"),
        "audio_bitrate": int(audio.get("AudioBitrate", 160)),
        "audio_samplerate": 48000,
    }


def convert_to_psp_mp4(input_path: str, output_dir: Path, progress_callback=None, output_stem=None):
    settings = psp_preset_settings()
    input_file = Path(input_path).resolve()
    if not input_file.is_file():
        raise FileNotFoundError(f"Uploaded video was not saved: {input_file}")

    ffmpeg_executable = resolve_ffmpeg()
    output_name_stem = output_stem or input_file.stem
    output_file = output_dir / f"{output_name_stem}_to_psp.mp4"

    ffmpeg_cmd = [
        ffmpeg_executable,
        "-y",
        "-i",
        str(input_file),
        "-vf",
        (
            f"scale={settings['width']}:{settings['height']}:force_original_aspect_ratio=decrease,"
            f"pad={settings['width']}:{settings['height']}:(ow-iw)/2:(oh-ih)/2:color=black,"
            f"fps={settings['fps']}"
        ),
        "-c:v",
        "libx264",
        "-preset",
        settings["video_preset"],
        "-profile:v",
        settings["video_profile"],
        "-level",
        settings["video_level"],
        "-pix_fmt",
        "yuv420p",
        "-r",
        str(settings["fps"]),
        "-c:a",
        "aac",
        "-b:a",
        f"{settings['audio_bitrate']}k",
        "-ac",
        "2",
        "-ar",
        str(settings["audio_samplerate"]),
        "-movflags",
        "+faststart",
        str(output_file),
    ]

    try:
        duration = get_video_duration(ffmpeg_executable, input_file)
    except OSError:
        duration = None

    ffmpeg_cmd[1:1] = ["-progress", "pipe:1", "-nostats"]
    process = subprocess.Popen(
        ffmpeg_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    stderr_output = []
    for line in process.stdout:
        stderr_output.append(line)
        if line.startswith("out_time_ms=") and progress_callback and duration:
            elapsed = int(line.split("=", 1)[1]) / 1_000_000
            progress_callback(min(round(elapsed * 100 / duration, 1), 99.9), "Converting video...")

    return_code = process.wait()
    if return_code != 0:
        raise RuntimeError("\n".join(stderr_output).strip() or "FFmpeg conversion failed.")

    return output_file.name


# ---------------------------------------------------------------------------
# HTTP Endpoints & Routes
# ---------------------------------------------------------------------------
@app.errorhandler(413)
def request_entity_too_large(error):
    """Handle uploads exceeding 430 MB."""
    return jsonify(error="Uploaded file exceeds the maximum allowed size of 430 MB."), 413


@app.route("/")
def index():
    user_id = get_user_id()
    record_user_activity(user_id)
    quota = get_user_quota(user_id)
    return render_template(
        "dashboard.html",
        downloads=quota["downloads_list"],
        converted=quota["converted_list"],
        quota=quota,
    )


@app.route("/download", methods=["POST"])
def download_video():
    user_id = get_user_id()
    record_user_activity(user_id)

    quota = get_user_quota(user_id)
    if quota["downloads_remaining"] <= 0:
        return (
            jsonify(
                error=f"Download limit reached. You can download a maximum of {MAX_DOWNLOADS_PER_USER} files per session. "
                "Delete an existing download or disconnect to reset."
            ),
            400,
        )

    url = (request.form.get("url") or "").strip()
    quality = request.form.get("quality") or "1080p"

    if not url:
        return jsonify(error="Please add a valid video URL before downloading."), 400

    job_id = create_job("download", user_id)
    user_download_dir = get_user_subdir(user_id, "downloads")

    job_executor.submit(
        run_job,
        job_id,
        lambda: download_from_youtube(
            url,
            quality,
            user_download_dir,
            lambda percent, message: update_job(job_id, percent=percent, message=message),
        ),
    )
    return jsonify(job_id=job_id)


@app.route("/convert", methods=["POST"])
def convert_video():
    user_id = get_user_id()
    record_user_activity(user_id)

    quota = get_user_quota(user_id)
    if quota["conversions_remaining"] <= 0:
        return (
            jsonify(
                error=f"Conversion limit reached. You can convert a maximum of {MAX_CONVERSIONS_PER_USER} files per session. "
                "Delete an existing converted video or disconnect to reset."
            ),
            400,
        )

    uploaded_file = request.files.get("video_file")
    if not uploaded_file or not uploaded_file.filename:
        return jsonify(error="Please select a video file to convert."), 400

    safe_name = secure_filename(uploaded_file.filename)
    if not safe_name:
        return jsonify(error="The selected video has an invalid filename."), 400

    user_upload_dir = get_user_subdir(user_id, "uploads")
    source_path = user_upload_dir / f"{uuid.uuid4().hex}_{safe_name}"
    uploaded_file.save(source_path)

    user_converted_dir = get_user_subdir(user_id, "converted")
    job_id = create_job("convert", user_id)

    def worker():
        try:
            return convert_to_psp_mp4(
                str(source_path),
                user_converted_dir,
                lambda percent, message: update_job(job_id, percent=percent, message=message),
                Path(safe_name).stem,
            )
        finally:
            # Immediate cleanup: delete raw uploaded video after conversion finishes or fails
            if source_path.exists():
                try:
                    source_path.unlink(missing_ok=True)
                except OSError:
                    pass

    job_executor.submit(run_job, job_id, worker)
    return jsonify(job_id=job_id)


@app.route("/progress/<job_id>")
def job_progress(job_id: str):
    user_id = get_user_id()
    record_user_activity(user_id)

    job = get_job(job_id)
    if job is None or job.get("user_id") != user_id:
        abort(404)
    return jsonify(job)


@app.route("/files/<folder>/<path:filename>")
def serve_file(folder: str, filename: str):
    """Serve downloaded or converted files with strict user isolation checks."""
    user_id = get_user_id()
    record_user_activity(user_id)

    if folder not in ("downloads", "converted"):
        abort(404)

    user_folder = get_user_subdir(user_id, folder)
    # Sanitize to prevent path traversal without stripping spaces, parentheses, or unicode
    clean_filename = os.path.basename(filename)
    target_file = (user_folder / clean_filename).resolve()

    # Security check: ensure path traversal is prevented and file strictly belongs to user
    try:
        target_file.relative_to(user_folder.resolve())
    except ValueError:
        abort(404)

    if not target_file.is_file():
        abort(404)

    return send_from_directory(user_folder, clean_filename, as_attachment=True)


@app.route("/files/<folder>/<path:filename>/delete", methods=["POST"])
def delete_file(folder: str, filename: str):
    """Allow user to delete a specific file to free up quota."""
    user_id = get_user_id()
    record_user_activity(user_id)

    if folder not in ("downloads", "converted"):
        abort(404)

    user_folder = get_user_subdir(user_id, folder)
    clean_filename = os.path.basename(filename)
    target_file = (user_folder / clean_filename).resolve()

    try:
        target_file.relative_to(user_folder.resolve())
    except ValueError:
        abort(404)

    if target_file.is_file():
        target_file.unlink(missing_ok=True)
        return jsonify(success=True, message=f"{clean_filename} deleted successfully.")
    abort(404)


@app.route("/session/heartbeat", methods=["POST"])
def heartbeat():
    """Client heartbeat to indicate active tab presence."""
    user_id = get_user_id()
    record_user_activity(user_id)
    return jsonify(status="ok")


@app.route("/session/disconnect-intent", methods=["POST"])
def disconnect_intent():
    """Beacon sent on tab/page close; marks disconnect with grace period."""
    user_id = session.get("user_id")
    if user_id:
        mark_user_disconnect_intent(user_id)
    return jsonify(status="ok")


@app.route("/session/disconnect", methods=["POST"])
def disconnect():
    """Explicit disconnect; immediately deletes all user files and resets session."""
    user_id = session.get("user_id")
    if user_id:
        delete_user_data(user_id)
        session.clear()
    return jsonify(success=True, message="Session ended and all files permanently deleted.")


@app.route("/session/quota")
def session_quota():
    """Return user quota information."""
    user_id = get_user_id()
    record_user_activity(user_id)
    return jsonify(get_user_quota(user_id))


if __name__ == "__main__":
    app.run(debug=True, host="127.0.0.1", port=5000)