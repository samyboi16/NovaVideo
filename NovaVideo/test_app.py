import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

# Set test storage directory before importing app
TEST_DIR = Path(tempfile.mkdtemp(prefix="novavideo_test_"))
os.environ["STORAGE_DIR"] = str(TEST_DIR)

from app import (
    app,
    get_user_storage,
    get_user_subdir,
    delete_user_data,
    MAX_DOWNLOADS_PER_USER,
    MAX_CONVERSIONS_PER_USER,
    MAX_UPLOAD_SIZE,
    create_job,
    jobs,
    jobs_lock,
)
import lambda_handler


class NovaVideoTestCase(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False
        self.client = app.test_client()

    def tearDown(self):
        pass

    @classmethod
    def tearDownClass(cls):
        if TEST_DIR.exists():
            shutil.rmtree(TEST_DIR, ignore_errors=True)

    def test_dashboard_loads_and_creates_session(self):
        """Dashboard should render cleanly and establish an isolated session."""
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"NovaVideo", response.data)
        self.assertIn(b"YouTube downloader", response.data)
        self.assertIn(b"PSP MP4 conversion", response.data)
        self.assertIn(b"0 / 3 files", response.data)
        self.assertIn(b"0 / 5 files", response.data)

    def test_user_file_isolation(self):
        """Files created by User A must not be visible or accessible by User B."""
        # User A session
        client_a = app.test_client()
        client_a.get("/")
        with client_a.session_transaction() as sess:
            user_a_id = sess["user_id"]

        # Place a mock file in User A's download directory
        a_downloads = get_user_subdir(user_a_id, "downloads")
        mock_file = a_downloads / "user_a_secret_video.mp4"
        mock_file.write_text("dummy content a")

        # User A sees the file on dashboard
        res_a = client_a.get("/")
        self.assertIn(b"user_a_secret_video.mp4", res_a.data)

        # User A can download the file
        file_res_a = client_a.get("/files/downloads/user_a_secret_video.mp4")
        self.assertEqual(file_res_a.status_code, 200)
        self.assertEqual(file_res_a.data, b"dummy content a")

        # User B session
        client_b = app.test_client()
        res_b = client_b.get("/")
        with client_b.session_transaction() as sess:
            user_b_id = sess["user_id"]

        self.assertNotEqual(user_a_id, user_b_id)

        # User B dashboard should NOT contain User A's file
        self.assertNotIn(b"user_a_secret_video.mp4", res_b.data)

        # User B attempting to download User A's file directly must get 404
        file_res_b = client_b.get("/files/downloads/user_a_secret_video.mp4")
        self.assertEqual(file_res_b.status_code, 404)

        # Path traversal attempts must be blocked
        traversal_res = client_b.get(f"/files/downloads/../../users/{user_a_id}/downloads/user_a_secret_video.mp4")
        self.assertEqual(traversal_res.status_code, 404)

    def test_job_isolation(self):
        """User B cannot view or poll User A's job progress."""
        client_a = app.test_client()
        client_a.get("/")
        with client_a.session_transaction() as sess:
            user_a_id = sess["user_id"]

        job_id = create_job("download", user_a_id)

        # User A can check progress
        res_a = client_a.get(f"/progress/{job_id}")
        self.assertEqual(res_a.status_code, 200)

        # User B attempting to check User A's job gets 404
        client_b = app.test_client()
        client_b.get("/")
        res_b = client_b.get(f"/progress/{job_id}")
        self.assertEqual(res_b.status_code, 404)

    def test_download_quota_limit(self):
        """Users can have at most 3 downloads."""
        client = app.test_client()
        client.get("/")
        with client.session_transaction() as sess:
            uid = sess["user_id"]

        downloads_dir = get_user_subdir(uid, "downloads")

        # Create 3 files
        for i in range(3):
            (downloads_dir / f"vid_{i}.mp4").write_text("test")

        # 4th download should be rejected
        res = client.post("/download", data={"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "quality": "720p"})
        self.assertEqual(res.status_code, 400)
        json_data = res.get_json()
        self.assertIn("Download limit reached", json_data["error"])

    def test_conversion_quota_limit(self):
        """Users can have at most 5 conversions."""
        client = app.test_client()
        client.get("/")
        with client.session_transaction() as sess:
            uid = sess["user_id"]

        converted_dir = get_user_subdir(uid, "converted")

        # Create 5 files
        for i in range(5):
            (converted_dir / f"conv_{i}.mp4").write_text("test")

        # 6th conversion should be rejected
        file_data = (io.BytesIO(b"dummy video content"), "test.mp4")
        res = client.post("/convert", data={"video_file": file_data}, content_type="multipart/form-data")
        self.assertEqual(res.status_code, 400)
        json_data = res.get_json()
        self.assertIn("Conversion limit reached", json_data["error"])

    def test_file_deletion_frees_quota(self):
        """Deleting a file via the delete endpoint frees up user quota."""
        client = app.test_client()
        client.get("/")
        with client.session_transaction() as sess:
            uid = sess["user_id"]

        downloads_dir = get_user_subdir(uid, "downloads")
        test_file = downloads_dir / "delete_me.mp4"
        test_file.write_text("content")

        self.assertTrue(test_file.exists())

        del_res = client.post("/files/downloads/delete_me.mp4/delete")
        self.assertEqual(del_res.status_code, 200)
        self.assertFalse(test_file.exists())

    def test_download_file_with_spaces_and_special_chars(self):
        """Files with spaces, brackets, and symbols can be downloaded and deleted without 404."""
        client = app.test_client()
        client.get("/")
        with client.session_transaction() as sess:
            uid = sess["user_id"]

        downloads_dir = get_user_subdir(uid, "downloads")
        complex_name = "Rick Astley - Never Gonna Give You Up (Official Video) [1080p].mp4"
        file_path = downloads_dir / complex_name
        file_path.write_bytes(b"sample video data")

        # Verify downloading with spaces and brackets encoded
        url_encoded_name = "Rick%20Astley%20-%20Never%20Gonna%20Give%20You%20Up%20(Official%20Video)%20[1080p].mp4"
        res = client.get(f"/files/downloads/{url_encoded_name}")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data, b"sample video data")
        res.close()

        # Verify deleting the file with spaces
        del_res = client.post(f"/files/downloads/{url_encoded_name}/delete")
        self.assertEqual(del_res.status_code, 200)
        self.assertFalse(file_path.exists())

    def test_disconnect_deletes_all_user_files(self):
        """When user disconnects, all uploaded, downloaded, and converted files are deleted."""
        client = app.test_client()
        client.get("/")
        with client.session_transaction() as sess:
            uid = sess["user_id"]

        user_storage = get_user_storage(uid)
        (get_user_subdir(uid, "downloads") / "down.mp4").write_text("down")
        (get_user_subdir(uid, "converted") / "conv.mp4").write_text("conv")
        (get_user_subdir(uid, "uploads") / "up.mp4").write_text("up")

        self.assertTrue(user_storage.exists())

        disconnect_res = client.post("/session/disconnect")
        self.assertEqual(disconnect_res.status_code, 200)

        # Storage directory should be completely removed
        self.assertFalse(user_storage.exists())

    def test_max_upload_size_configuration(self):
        """Max upload size is set to 430 MB."""
        self.assertEqual(app.config["MAX_CONTENT_LENGTH"], 430 * 1024 * 1024)

    def test_lambda_handler_v2_format(self):
        """AWS Lambda handler handles format 2.0 (Function URL / HTTP API)."""
        event = {
            "version": "2.0",
            "rawPath": "/",
            "rawQueryString": "",
            "headers": {
                "host": "test.lambda-url.us-east-1.on.aws",
            },
            "requestContext": {
                "http": {
                    "method": "GET",
                    "path": "/",
                    "sourceIp": "1.2.3.4",
                }
            },
            "isBase64Encoded": False,
        }
        res = lambda_handler.handler(event, None)
        self.assertEqual(res["statusCode"], 200)
        self.assertIn("cookies", res)
        self.assertIn("NovaVideo", res["body"])

    def test_lambda_handler_v1_and_binary_content(self):
        """AWS Lambda handler handles format 1.0 and base64-encodes binary video files."""
        # Create a mock file in a user's directory
        client = app.test_client()
        client.get("/")
        with client.session_transaction() as sess:
            uid = sess["user_id"]
        
        down_dir = get_user_subdir(uid, "downloads")
        (down_dir / "sample.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42")

        event = {
            "version": "1.0",
            "httpMethod": "GET",
            "path": "/files/downloads/sample.mp4",
            "headers": {
                "Cookie": f"novavideo_session={client.get_cookie('novavideo_session').value}",
            },
            "isBase64Encoded": False,
        }
        res = lambda_handler.handler(event, None)
        self.assertEqual(res["statusCode"], 200)
        self.assertTrue(res["isBase64Encoded"])
        decoded_bytes = lambda_handler.base64.b64decode(res["body"])
        self.assertEqual(decoded_bytes, b"\x00\x00\x00\x18ftypmp42")

    def test_413_error_handler(self):
        """Test that 413 error returns friendly JSON error."""
        from werkzeug.exceptions import RequestEntityTooLarge
        with app.test_request_context("/convert"):
            resp, code = app.handle_user_exception(RequestEntityTooLarge())
            self.assertEqual(code, 413)
            self.assertIn("430 MB", resp.get_json()["error"])

    def test_stale_session_sweeper(self):
        """Test that inactive sessions are purged when timeout expires."""
        from app import sweep_stale_sessions, mark_user_disconnect_intent, user_last_seen, disconnect_intents

        client = app.test_client()
        client.get("/")
        with client.session_transaction() as sess:
            uid = sess["user_id"]

        user_storage = get_user_storage(uid)
        (get_user_subdir(uid, "downloads") / "test.mp4").write_text("hello")
        self.assertTrue(user_storage.exists())

        # Simulate disconnect intent past grace period
        mark_user_disconnect_intent(uid)
        disconnect_intents[uid] = 0.0  # Set timestamp in past

        sweep_stale_sessions()
        self.assertFalse(user_storage.exists())


if __name__ == "__main__":
    unittest.main()

