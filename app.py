import base64
import binascii
import io
import ipaddress
import logging
import os
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass

from flask import Flask, jsonify, request, send_from_directory, url_for
from PIL import Image, UnidentifiedImageError
from werkzeug.exceptions import RequestEntityTooLarge

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

UPLOAD_FOLDER = os.environ.get("UPLOAD_FOLDER", "/app/uploads")
MAX_FILE_SIZE = 100 * 1024 * 1024  # 100MB（按解码后文件大小计算）
# multipart/form-data 等请求体总大小限制。这里放宽到 140MB，避免 Base64 膨胀后
# 100MB 左右的图片在 /upload 接口被请求体大小提前拦截。
app.config["MAX_CONTENT_LENGTH"] = 140 * 1024 * 1024
ALLOWED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
DOWNLOAD_TIMEOUT_SECONDS = 30
DOWNLOAD_CHUNK_SIZE = 1024 * 1024
SUPPORTED_IMAGE_FORMATS = {
    "PNG": ("image/png", ".png"),
    "JPEG": ("image/jpeg", ".jpg"),
    "GIF": ("image/gif", ".gif"),
    "WEBP": ("image/webp", ".webp"),
}

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
logger.info("上传目录已创建: %s", UPLOAD_FOLDER)


@dataclass(frozen=True)
class ImageMetadata:
    actual_format: str
    mime_type: str
    extension: str


class ImageProcessingError(Exception):
    """图片处理异常基类"""


class FileTooLargeError(ImageProcessingError):
    """文件过大异常"""


class InvalidImageError(ImageProcessingError):
    """无效图片异常"""


class InvalidRemoteUrlError(ImageProcessingError):
    """远程 URL 非法"""


class UnsafeRemoteAddressError(ImageProcessingError):
    """远程地址不安全"""


class RemoteDownloadError(ImageProcessingError):
    """远程下载异常"""


def format_size(size: int) -> str:
    """格式化文件大小"""
    return f"{size / 1024 / 1024:.2f}MB"


def get_supported_extensions_message() -> str:
    return ", ".join(sorted(ALLOWED_EXTENSIONS))


def get_uploaded_file_size(file) -> int:
    """获取上传文件大小，不影响后续读取/保存"""
    current_position = file.stream.tell()
    file.stream.seek(0, os.SEEK_END)
    size = file.stream.tell()
    file.stream.seek(current_position)
    return size


def ensure_uploaded_file_size(file) -> int:
    """校验上传文件大小（按单文件限制）"""
    size = get_uploaded_file_size(file)
    if size > MAX_FILE_SIZE:
        filename = file.filename or "unknown"
        raise FileTooLargeError(
            f"文件 {filename} 大小不能超过100MB，当前文件大小: {format_size(size)}"
        )
    return size


def estimate_base64_decoded_size(base64_data: str) -> int:
    """估算 Base64 解码后的字节大小"""
    if not base64_data:
        return 0
    padding = base64_data[-2:].count("=")
    return (len(base64_data) * 3) // 4 - padding


def get_image_metadata(image_data: bytes) -> ImageMetadata:
    """读取图片元信息，并确保只接受支持的图片格式"""
    try:
        with Image.open(io.BytesIO(image_data)) as image:
            image.verify()
        with Image.open(io.BytesIO(image_data)) as image:
            actual_format = (image.format or "").upper()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise InvalidImageError("无效的图片数据") from exc

    supported = SUPPORTED_IMAGE_FORMATS.get(actual_format)
    if not supported:
        raise InvalidImageError(
            f"不支持的图片格式，仅支持: {get_supported_extensions_message()}"
        )

    mime_type, extension = supported
    return ImageMetadata(
        actual_format=actual_format,
        mime_type=mime_type,
        extension=extension,
    )


def read_uploaded_image(file, *, validate_extension: bool) -> tuple[bytes, ImageMetadata]:
    """读取并校验上传图片"""
    if validate_extension:
        ext = os.path.splitext(file.filename)[1].lower()
        if ext not in ALLOWED_EXTENSIONS:
            raise InvalidImageError(
                f"不支持的文件类型，仅支持: {get_supported_extensions_message()}"
            )

    ensure_uploaded_file_size(file)
    file.stream.seek(0)

    image_data = file.read()
    metadata = get_image_metadata(image_data)
    return image_data, metadata


def build_uploaded_file_payload(
    filename: str,
    size: int,
    *,
    original_name: str | None = None,
) -> dict:
    payload = {
        "url": url_for("get_image", filename=filename, _external=True),
        "filename": filename,
        "size": size,
    }
    if original_name is not None:
        payload["original_name"] = original_name
    return payload


def save_uploaded_image(image_data: bytes, extension: str) -> tuple[str, int]:
    """保存图片并返回文件名和大小"""
    filename = f"{uuid.uuid4().hex}{extension}"
    filepath = os.path.join(UPLOAD_FOLDER, filename)

    with open(filepath, "wb") as file_obj:
        file_obj.write(image_data)

    return filename, len(image_data)


def is_forbidden_ip(ip_obj: ipaddress._BaseAddress) -> bool:
    return (
        ip_obj.is_loopback
        or ip_obj.is_private
        or ip_obj.is_link_local
        or ip_obj.is_multicast
        or ip_obj.is_unspecified
        or ip_obj.is_reserved
    )


def validate_resolved_addresses(hostname: str, port: int) -> None:
    """拒绝解析到本地或私有网络的地址"""
    if hostname.lower() == "localhost":
        raise UnsafeRemoteAddressError("不允许访问本地或私有网络地址")

    try:
        address_info = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise InvalidRemoteUrlError("无法解析远程地址") from exc

    for _, _, _, _, sockaddr in address_info:
        ip_text = sockaddr[0].split("%", 1)[0]
        ip_obj = ipaddress.ip_address(ip_text)
        if is_forbidden_ip(ip_obj):
            raise UnsafeRemoteAddressError("不允许访问本地或私有网络地址")


def validate_remote_url(url: str) -> urllib.parse.ParseResult:
    """校验远程图片 URL 是否安全可访问"""
    if not isinstance(url, str):
        raise InvalidRemoteUrlError("url参数必须是字符串")

    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError as exc:
        raise InvalidRemoteUrlError("URL格式不正确") from exc

    if parsed.scheme not in {"http", "https"}:
        raise InvalidRemoteUrlError("仅支持http和https协议")
    if not parsed.hostname:
        raise InvalidRemoteUrlError("URL缺少主机名")

    try:
        port = parsed.port
    except ValueError as exc:
        raise InvalidRemoteUrlError("URL端口不合法") from exc

    if port is None:
        port = 443 if parsed.scheme == "https" else 80

    validate_resolved_addresses(parsed.hostname, port)
    return parsed


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """在跟随跳转前再次校验目标地址"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_remote_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def create_url_opener() -> urllib.request.OpenerDirector:
    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE
    return urllib.request.build_opener(
        SafeRedirectHandler(),
        urllib.request.HTTPSHandler(context=ssl_context),
    )


def download_image(url: str) -> tuple[bytes, ImageMetadata]:
    """从远程 URL 安全下载图片"""
    validate_remote_url(url)

    opener = create_url_opener()
    request_obj = urllib.request.Request(url, headers={"User-Agent": "URLapi/1.0"})

    try:
        with opener.open(request_obj, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
            validate_remote_url(response.geturl())

            content_length_header = response.headers.get("Content-Length")
            if content_length_header:
                try:
                    content_length = int(content_length_header)
                except ValueError:
                    content_length = None
                if content_length and content_length > MAX_FILE_SIZE:
                    raise FileTooLargeError(
                        "文件大小不能超过100MB，当前文件大小约为: "
                        f"{format_size(content_length)}"
                    )

            content_type_header = response.headers.get("Content-Type")
            if content_type_header:
                content_type = response.headers.get_content_type()
            else:
                content_type = None

            if content_type and content_type != "application/octet-stream":
                if not content_type.startswith("image/"):
                    raise InvalidImageError("远程资源不是图片")

            chunks = []
            total_size = 0
            while True:
                chunk = response.read(DOWNLOAD_CHUNK_SIZE)
                if not chunk:
                    break
                total_size += len(chunk)
                if total_size > MAX_FILE_SIZE:
                    raise FileTooLargeError(
                        "文件大小不能超过100MB，当前文件大小约为: "
                        f"{format_size(total_size)}"
                    )
                chunks.append(chunk)
    except urllib.error.HTTPError as exc:
        raise RemoteDownloadError(f"下载图片失败: HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        reason_text = str(reason)
        if isinstance(reason, TimeoutError) or "timed out" in reason_text.lower():
            raise RemoteDownloadError("下载图片超时") from exc
        if isinstance(reason, ssl.SSLError):
            raise RemoteDownloadError("下载图片失败: SSL证书校验失败") from exc
        raise RemoteDownloadError(f"下载图片失败: {reason_text}") from exc
    except ssl.SSLError as exc:
        raise RemoteDownloadError("下载图片失败: SSL证书校验失败") from exc
    except TimeoutError as exc:
        raise RemoteDownloadError("下载图片超时") from exc

    image_data = b"".join(chunks)
    metadata = get_image_metadata(image_data)
    return image_data, metadata


@app.errorhandler(RequestEntityTooLarge)
def handle_request_entity_too_large(e):
    """请求体过大时返回统一 JSON 错误"""
    return jsonify({"error": "请求体不能超过140MB"}), 413


@app.route("/url-to-base64", methods=["POST"])
def url_to_base64():
    """网络图片URL转Base64"""
    data = request.get_json(silent=True)
    if not data or "url" not in data:
        return jsonify({"error": "缺少url参数"}), 400

    try:
        image_data, metadata = download_image(data["url"])
        b64_data = base64.b64encode(image_data).decode("utf-8")
        return jsonify({"base64": b64_data, "mime_type": metadata.mime_type})
    except ImageProcessingError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception:
        logger.exception("网络图片转Base64失败")
        return jsonify({"error": "转换失败: 内部服务器错误"}), 500


@app.route("/file-to-base64", methods=["POST"])
def file_to_base64():
    """上传文件转Base64（multipart/form-data）"""
    if "file" not in request.files:
        return jsonify({"error": "缺少file参数"}), 400

    file = request.files["file"]
    if file.filename == "":
        return jsonify({"error": "文件名为空"}), 400

    try:
        image_data, metadata = read_uploaded_image(file, validate_extension=False)
        b64_data = base64.b64encode(image_data).decode("utf-8")
        return jsonify({"base64": b64_data, "mime_type": metadata.mime_type})
    except ImageProcessingError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception:
        logger.exception("上传文件转Base64失败")
        return jsonify({"error": "转换失败: 内部服务器错误"}), 500


@app.route("/upload", methods=["POST"])
def upload():
    """上传Base64图片数据，返回访问URL"""
    data = request.get_json(silent=True)
    if not data or "base64" not in data:
        return jsonify({"error": "缺少base64参数"}), 400

    base64_data = data["base64"]
    if not isinstance(base64_data, str):
        return jsonify({"error": "base64参数必须是字符串"}), 400

    try:
        if "," in base64_data:
            base64_data = base64_data.split(",", 1)[1]

        base64_data = "".join(base64_data.split())

        estimated_size = estimate_base64_decoded_size(base64_data)
        if estimated_size > MAX_FILE_SIZE:
            raise FileTooLargeError(
                "文件大小不能超过100MB，当前文件大小约为: "
                f"{format_size(estimated_size)}"
            )

        image_data = base64.b64decode(base64_data, validate=True)
        if len(image_data) > MAX_FILE_SIZE:
            raise FileTooLargeError(
                f"文件大小不能超过100MB，当前文件大小: {format_size(len(image_data))}"
            )

        metadata = get_image_metadata(image_data)
        filename, size = save_uploaded_image(image_data, metadata.extension)

        return jsonify(build_uploaded_file_payload(filename, size))
    except binascii.Error:
        return jsonify({"error": "base64格式不正确"}), 400
    except ImageProcessingError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception:
        logger.exception("Base64上传失败")
        return jsonify({"error": "上传失败: 内部服务器错误"}), 500


@app.route("/uploads/<filename>", methods=["GET"])
def get_image(filename):
    """访问上传的图片"""
    return send_from_directory(UPLOAD_FOLDER, filename)


@app.route("/health", methods=["GET"])
def health():
    """健康检查"""
    return jsonify({"status": "ok"})


@app.route("/upload-file", methods=["POST"])
def upload_file():
    """上传本地图片文件，返回访问URL"""
    if "file" not in request.files:
        return jsonify({"error": "缺少file参数"}), 400

    file = request.files["file"]
    if file.filename == "":
        return jsonify({"error": "文件名为空"}), 400

    try:
        image_data, metadata = read_uploaded_image(file, validate_extension=True)
        filename, size = save_uploaded_image(image_data, metadata.extension)
        return jsonify(build_uploaded_file_payload(filename, size))
    except ImageProcessingError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception:
        logger.exception("上传本地文件失败")
        return jsonify({"error": "上传失败: 内部服务器错误"}), 500


@app.route("/upload-directory", methods=["POST"])
def upload_directory():
    """上传目录下的图片文件，返回访问URL列表"""
    if "files" not in request.files:
        return jsonify({"error": "缺少files参数"}), 400

    files = request.files.getlist("files")
    if not files:
        return jsonify({"error": "未选择文件"}), 400

    uploaded_files = []
    failed_files = []

    for file in files:
        original_name = file.filename or ""
        if original_name == "":
            failed_files.append({"original_name": original_name, "error": "文件名为空"})
            continue

        try:
            image_data, metadata = read_uploaded_image(file, validate_extension=True)
            filename, size = save_uploaded_image(image_data, metadata.extension)
            uploaded_files.append(
                build_uploaded_file_payload(
                    filename,
                    size,
                    original_name=original_name,
                )
            )
        except ImageProcessingError as exc:
            failed_files.append({"original_name": original_name, "error": str(exc)})
        except Exception:
            logger.exception("批量上传失败: %s", original_name)
            failed_files.append(
                {"original_name": original_name, "error": "内部服务器错误"}
            )

    response = {
        "files": uploaded_files,
        "failed": failed_files,
        "count": len(uploaded_files),
        "failed_count": len(failed_files),
    }
    status_code = 200 if uploaded_files else 400
    return jsonify(response), status_code


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
