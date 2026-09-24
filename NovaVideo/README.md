# NovaVideo Downloader and PSP Converter

A production-ready Flask web application for downloading videos from YouTube and converting video files into PSP-compatible MP4 files using custom presets, with built-in support for AWS Lambda serverless hosting.

## Features

- **YouTube Downloader**: Download videos directly from YouTube with 720p, 1080p, or M4A audio options using `yt-dlp`.
- **PSP MP4 Converter**: Convert local video files into PSP-compatible format (480×272, H.264 baseline 1.3, 30 fps, AAC audio 160 kbps) using FFmpeg.
- **430 MB Upload Limit**: Supports uploads up to 430 MB with both frontend client-side validation and server-side `MAX_CONTENT_LENGTH` enforcement.
- **Strict User Isolation**: Each visitor is assigned an isolated session. Downloads, conversions, and uploaded files are saved in private per-user directories and cannot be accessed or viewed by any other user.
- **Per-User Quotas**: Limits each user to a maximum of **3 downloads** and **5 conversions** per session. Visual quota badges and individual file deletion buttons allow users to manage their quota.
- **Automatic Disconnect Cleanup**: When a user leaves or closes their browser tab, a disconnect beacon marks the session for immediate cleanup. Idle sessions are automatically purged after a timeout, and raw uploaded videos are deleted immediately after conversion to conserve disk space.
- **AWS Lambda & Production Ready**: Fully configured for AWS Lambda container deployments with `/tmp` storage handling, a universal WSGI-to-Lambda adapter (`lambda_handler.py`), a production `Dockerfile` with static FFmpeg, and an AWS SAM template (`template.yaml`).

---

## Architecture & Security

### 1. Isolated Storage
All user files are stored under isolated directory trees:
```text
STORAGE_DIR/
├── jobs/
│   └── <job_id>.json
└── users/
    └── <user_session_id>/
        ├── downloads/
        ├── converted/
        └── uploads/
```
In AWS Lambda, `STORAGE_DIR` automatically defaults to `/tmp/psp_converter`. Paths are validated with strict directory containment checks to prevent path traversal attacks.

### 2. Disconnect & Cleanup Lifecycle
1. **Tab Close / Navigation**: Browser fires `navigator.sendBeacon('/session/disconnect-intent')` on `pagehide`. If the user does not reload within a 5-second grace window, the background cleaner permanently deletes all their files.
2. **Explicit Disconnect**: The user can click the **Disconnect & Clear** button in the header at any time to instantly delete all their files and reset their session.
3. **Heartbeat & Inactivity Sweeper**: Active browser tabs send a heartbeat every 25 seconds. If a tab closes without firing events (e.g. system crash or loss of connection), sessions idle for more than 5 minutes (`SESSION_TIMEOUT_SECONDS`) are purged.
4. **Immediate Upload Cleanup**: Raw uploaded video files in `uploads/` are immediately removed as soon as FFmpeg completes or fails, freeing up temporary storage.

---

## Local Development

### Requirements
- Python 3.10+
- FFmpeg (or the bundled fallback from `imageio-ffmpeg`)

### Setup

```bash
# Clone the repository
git clone <your-repository-url>
cd VidDownPSPConvert

# Create and activate virtual environment
python -m venv .venv
source .venv/bin/activate  # On Windows PowerShell: .\.venv\Scripts\Activate.ps1

# Install dependencies
pip install -r requirements.txt
```

### Run Locally

```bash
python app.py
```
Or run with auto-browser launch:
```bash
python run.py
```
Open [http://127.0.0.1:5000](http://127.0.0.1:5000) in your browser.

### Run Tests

Run the comprehensive unit test suite:
```bash
python test_app.py
```

---

## Deploying to AWS Lambda

Deploying video processing and downloading to AWS Lambda requires handling storage, execution time, and binary dependencies properly.

### AWS Lambda Configuration Requirements
- **Memory**: Set to **2048 MB - 3008 MB** (allocates 2 vCPUs to the Lambda function for fast FFmpeg encoding).
- **Ephemeral Storage (`/tmp`)**: Set to **4096 MB - 10240 MB** (AWS Lambda allows up to 10 GB of `/tmp` disk space; this ensures 430 MB uploads and converted videos fit comfortably).
- **Timeout**: Set to **900 seconds** (15 minutes maximum Lambda timeout for long video encodings).
- **Function URL / API Gateway**: Use a Lambda Function URL or Application Load Balancer (ALB). Note: Standard API Gateway REST APIs have a 10 MB payload limit, while Lambda Function URLs or Application Load Balancers support larger requests and streaming.

### Deploy with AWS SAM (Serverless Application Model)

1. **Build the container image**:
   ```bash
   sam build
   ```
2. **Deploy to AWS**:
   ```bash
   sam deploy --guided
   ```
3. SAM will output your public **NovaVideoFunctionUrl**. Open the URL in your browser.

### Deploy with Docker to AWS ECR / Lambda

1. **Build Docker image**:
   ```bash
   docker build -t novavideo-psp .
   ```
2. **Tag and push to Amazon ECR**:
   ```bash
   aws ecr get-login-password --region <region> | docker login --username AWS --password-stdin <account_id>.dkr.ecr.<region>.amazonaws.com
   docker tag novavideo-psp:latest <account_id>.dkr.ecr.<region>.amazonaws.com/novavideo-psp:latest
   docker push <account_id>.dkr.ecr.<region>.amazonaws.com/novavideo-psp:latest
   ```
3. **Create or update Lambda function**:
   - In AWS Lambda Console, choose **Container Image**.
   - Select the ECR image.
   - Configure **Ephemeral storage**: 5120 MB.
   - Configure **Timeout**: 15 min.
   - Configure **Memory**: 3008 MB.
   - Enable **Function URL** with auth type `NONE`.

---

## Project Structure

```text
.
├── app.py                 # Core Flask application & API endpoints
├── dashboard.html         # Frontend user interface
├── lambda_handler.py      # Universal WSGI adapter for AWS Lambda
├── Dockerfile             # Production container definition with static FFmpeg
├── template.yaml          # AWS SAM deployment template
├── psp-preset.json        # Handbrake/FFmpeg preset for Sony PSP
├── test_app.py            # Test suite (isolation, quotas, limits, cleanup)
├── run.py                 # Local launcher script
├── requirements.txt       # Python dependencies
└── README.md              # Project documentation
```

---

## Environment Variables

| Variable | Default | Description |
| :--- | :--- | :--- |
| `STORAGE_DIR` | `/tmp/psp_converter` (Lambda) or `./storage` (local) | Base directory for user directories and jobs |
| `SECRET_KEY` | (Default key) | Secret key for signing Flask session cookies |
| `SESSION_TIMEOUT_SECONDS` | `300` (5 minutes) | Inactivity duration before purging disconnected user sessions |
| `FFMPEG_PATH` | (Auto-detected) | Custom path to the FFmpeg executable |
| `HOST` | `127.0.0.1` | Local bind address for `run.py` |
| `PORT` | `5000` | Local port for `run.py` |
