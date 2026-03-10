import base64
import io
import os
import ssl
import tempfile
import unittest
import urllib.error
import urllib.parse
import urllib.request
from email.message import Message
from unittest import mock

from PIL import Image

import app as app_module


class FakeResponse:
    def __init__(
        self,
        body: bytes,
        *,
        url: str = "https://example.com/image.png",
        content_type: str | None = "image/png",
        content_length: int | None = None,
    ) -> None:
        self._buffer = io.BytesIO(body)
        self._url = url
        self.headers = Message()
        if content_type is not None:
            self.headers["Content-Type"] = content_type
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)

    def read(self, size: int = -1) -> bytes:
        return self._buffer.read(size)

    def geturl(self) -> str:
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class FakeOpener:
    def __init__(self, *, response=None, error=None) -> None:
        self._response = response
        self._error = error

    def open(self, request, timeout=None):
        del request
        del timeout
        if self._error is not None:
            raise self._error
        return self._response


class UrlApiTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.upload_dir = tempfile.TemporaryDirectory()
        self.original_upload_folder = app_module.UPLOAD_FOLDER
        app_module.UPLOAD_FOLDER = self.upload_dir.name
        os.makedirs(app_module.UPLOAD_FOLDER, exist_ok=True)

        app_module.app.config["TESTING"] = True
        self.client = app_module.app.test_client()

    def tearDown(self) -> None:
        app_module.UPLOAD_FOLDER = self.original_upload_folder
        self.upload_dir.cleanup()

    def make_image_bytes(self, image_format: str) -> bytes:
        buffer = io.BytesIO()
        image = Image.new("RGB", (2, 2), color=(255, 0, 0))
        image.save(buffer, format=image_format)
        return buffer.getvalue()

    def test_upload_uses_actual_format_when_mime_is_spoofed(self) -> None:
        png_bytes = self.make_image_bytes("PNG")
        response = self.client.post(
            "/upload",
            json={
                "base64": base64.b64encode(png_bytes).decode("ascii"),
                "mime_type": "image/jpeg",
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["filename"].endswith(".png"))
        self.assertEqual(payload["size"], len(png_bytes))
        self.assertTrue(
            os.path.exists(os.path.join(app_module.UPLOAD_FOLDER, payload["filename"]))
        )

    def test_upload_rejects_non_image_base64(self) -> None:
        response = self.client.post(
            "/upload",
            json={"base64": base64.b64encode(b"not an image").decode("ascii")},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "无效的图片数据")

    def test_file_to_base64_returns_detected_mime_type(self) -> None:
        jpeg_bytes = self.make_image_bytes("JPEG")
        response = self.client.post(
            "/file-to-base64",
            data={"file": (io.BytesIO(jpeg_bytes), "sample.jpg")},
            content_type="multipart/form-data",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["mime_type"], "image/jpeg")
        self.assertEqual(base64.b64decode(payload["base64"]), jpeg_bytes)

    def test_upload_file_rejects_disguised_non_image(self) -> None:
        response = self.client.post(
            "/upload-file",
            data={"file": (io.BytesIO(b"not image"), "fake.png")},
            content_type="multipart/form-data",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "无效的图片数据")

    def test_upload_directory_reports_partial_success(self) -> None:
        png_bytes = self.make_image_bytes("PNG")
        response = self.client.post(
            "/upload-directory",
            data={
                "files": [
                    (io.BytesIO(png_bytes), "good.png"),
                    (io.BytesIO(b"not image"), "bad.png"),
                ]
            },
            content_type="multipart/form-data",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["failed_count"], 1)
        self.assertEqual(payload["files"][0]["original_name"], "good.png")
        self.assertEqual(payload["failed"][0]["original_name"], "bad.png")

    def test_upload_directory_returns_400_when_all_fail(self) -> None:
        response = self.client.post(
            "/upload-directory",
            data={"files": [(io.BytesIO(b"not image"), "bad.png")]},
            content_type="multipart/form-data",
        )

        self.assertEqual(response.status_code, 400)
        payload = response.get_json()
        self.assertEqual(payload["count"], 0)
        self.assertEqual(payload["failed_count"], 1)
        self.assertEqual(payload["failed"][0]["original_name"], "bad.png")

    def test_url_to_base64_returns_detected_mime_type(self) -> None:
        png_bytes = self.make_image_bytes("PNG")
        metadata = app_module.ImageMetadata("PNG", "image/png", ".png")

        with mock.patch.object(
            app_module,
            "download_image",
            return_value=(png_bytes, metadata),
        ):
            response = self.client.post(
                "/url-to-base64",
                json={"url": "https://example.com/photo.png?version=1"},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["mime_type"], "image/png")
        self.assertEqual(base64.b64decode(payload["base64"]), png_bytes)

    def test_url_to_base64_rejects_private_address(self) -> None:
        response = self.client.post(
            "/url-to-base64",
            json={"url": "http://127.0.0.1/image.png"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "不允许访问本地或私有网络地址")

    def test_url_to_base64_rejects_non_string_url(self) -> None:
        response = self.client.post("/url-to-base64", json={"url": 123})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "url参数必须是字符串")

    def test_download_image_rejects_non_image_content_type(self) -> None:
        response = FakeResponse(b"plain text", content_type="text/plain")

        with mock.patch.object(
            app_module,
            "validate_remote_url",
            return_value=urllib.parse.urlparse("https://example.com/file.txt"),
        ), mock.patch.object(
            app_module,
            "create_url_opener",
            return_value=FakeOpener(response=response),
        ):
            with self.assertRaises(app_module.InvalidImageError):
                app_module.download_image("https://example.com/file.txt")

    def test_download_image_accepts_missing_content_type_if_image_is_valid(self) -> None:
        png_bytes = self.make_image_bytes("PNG")
        response = FakeResponse(png_bytes, content_type=None)

        with mock.patch.object(
            app_module,
            "validate_remote_url",
            return_value=urllib.parse.urlparse("https://example.com/image"),
        ), mock.patch.object(
            app_module,
            "create_url_opener",
            return_value=FakeOpener(response=response),
        ):
            image_data, metadata = app_module.download_image("https://example.com/image")

        self.assertEqual(image_data, png_bytes)
        self.assertEqual(metadata.mime_type, "image/png")

    def test_download_image_rejects_oversized_content_length(self) -> None:
        response = FakeResponse(
            b"",
            content_length=app_module.MAX_FILE_SIZE + 1,
        )

        with mock.patch.object(
            app_module,
            "validate_remote_url",
            return_value=urllib.parse.urlparse("https://example.com/big.png"),
        ), mock.patch.object(
            app_module,
            "create_url_opener",
            return_value=FakeOpener(response=response),
        ):
            with self.assertRaises(app_module.FileTooLargeError):
                app_module.download_image("https://example.com/big.png")

    def test_validate_remote_url_rejects_invalid_port(self) -> None:
        with self.assertRaises(app_module.InvalidRemoteUrlError) as ctx:
            app_module.validate_remote_url("https://example.com:99999/image.png")

        self.assertEqual(str(ctx.exception), "URL端口不合法")

    def test_download_image_maps_timeout_to_user_error(self) -> None:
        timeout_error = urllib.error.URLError(TimeoutError())

        with mock.patch.object(
            app_module,
            "validate_remote_url",
            return_value=urllib.parse.urlparse("https://example.com/slow.png"),
        ), mock.patch.object(
            app_module,
            "create_url_opener",
            return_value=FakeOpener(error=timeout_error),
        ):
            with self.assertRaises(app_module.RemoteDownloadError) as ctx:
                app_module.download_image("https://example.com/slow.png")

        self.assertEqual(str(ctx.exception), "下载图片超时")

    def test_download_image_maps_ssl_error_to_user_error(self) -> None:
        ssl_error = urllib.error.URLError(ssl.SSLError("certificate verify failed"))

        with mock.patch.object(
            app_module,
            "validate_remote_url",
            return_value=urllib.parse.urlparse("https://example.com/secure.png"),
        ), mock.patch.object(
            app_module,
            "create_url_opener",
            return_value=FakeOpener(error=ssl_error),
        ):
            with self.assertRaises(app_module.RemoteDownloadError) as ctx:
                app_module.download_image("https://example.com/secure.png")

        self.assertEqual(str(ctx.exception), "下载图片失败: SSL证书校验失败")

    def test_create_url_opener_disables_ssl_verification(self) -> None:
        opener = app_module.create_url_opener()
        https_handler = next(
            handler
            for handler in opener.handlers
            if isinstance(handler, urllib.request.HTTPSHandler)
        )

        self.assertFalse(https_handler._context.check_hostname)
        self.assertEqual(https_handler._context.verify_mode, ssl.CERT_NONE)


if __name__ == "__main__":
    unittest.main()
