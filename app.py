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

from flask import Flask, jsonify, request, send_from_directory, url_for, session
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

# 图库密码认证（通过环境变量设置，默认为空表示不需要密码）
GALLERY_PASSWORD = os.environ.get("GALLERY_PASSWORD", "")
SESSION_SECRET = os.environ.get("SESSION_SECRET", uuid.uuid4().hex)

app.secret_key = SESSION_SECRET


def check_gallery_auth():
    """检查图库认证状态"""
    if not GALLERY_PASSWORD:
        return True
    return session.get("gallery_authenticated", False)


def require_gallery_auth(f):
    """图库认证装饰器"""
    from functools import wraps

    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not check_gallery_auth():
            return jsonify({"error": "未授权访问", "require_auth": True}), 401
        return f(*args, **kwargs)

    return decorated_function


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


@app.route("/images", methods=["GET"])
@require_gallery_auth
def list_images():
    """获取图片列表"""
    try:
        files = os.listdir(UPLOAD_FOLDER)
        images = []
        for filename in files:
            filepath = os.path.join(UPLOAD_FOLDER, filename)
            if os.path.isfile(filepath):
                stat = os.stat(filepath)
                width, height = 0, 0
                try:
                    with Image.open(filepath) as img:
                        width, height = img.size
                except Exception:
                    pass
                images.append({
                    "filename": filename,
                    "url": url_for("get_image", filename=filename, _external=True),
                    "size": stat.st_size,
                    "width": width,
                    "height": height,
                    "created_time": stat.st_ctime
                })
        # 按创建时间倒序排列
        images.sort(key=lambda x: x["created_time"], reverse=True)
        return jsonify({"images": images, "total": len(images)})
    except Exception as e:
        logger.error("获取图片列表失败: %s", str(e))
        return jsonify({"error": f"获取图片列表失败: {str(e)}"}), 500


@app.route("/gallery-auth", methods=["POST"])
def gallery_auth():
    """图库登录认证"""
    if not GALLERY_PASSWORD:
        return jsonify({"success": True, "message": "无需认证"})
    
    data = request.get_json()
    password = data.get("password", "")
    
    if password == GALLERY_PASSWORD:
        session["gallery_authenticated"] = True
        return jsonify({"success": True})
    else:
        return jsonify({"success": False, "error": "密码错误"}), 401


@app.route("/gallery-logout", methods=["POST"])
def gallery_logout():
    """图库登出"""
    session.pop("gallery_authenticated", None)
    return jsonify({"success": True})


@app.route("/tuku", methods=["GET"])
def tuku():
    """图库页面"""
    require_auth = bool(GALLERY_PASSWORD)
    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>图片图库</title>
    <style>
        :root {{
            --primary: #2563eb;
            --primary-hover: #1d4ed8;
            --danger: #dc2626;
            --danger-hover: #b91c1c;
            --bg: #f8fafc;
            --card-bg: #ffffff;
            --text: #1e293b;
            --text-muted: #64748b;
            --border: #e2e8f0;
        }}
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: 'PingFang SC', 'Microsoft YaHei', -apple-system, sans-serif;
            background: var(--bg);
            color: var(--text);
            min-height: 100vh;
            padding: 20px;
        }}
        .header {{
            max-width: 1400px;
            margin: 0 auto 24px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
            gap: 16px;
        }}
        .header h1 {{
            font-size: 28px;
            font-weight: 700;
            color: #1e293b;
        }}
        .header h1 span {{
            color: var(--primary);
        }}
        .header-actions {{
            display: flex;
            gap: 12px;
            align-items: center;
        }}
        .btn {{
            padding: 10px 20px;
            border: none;
            border-radius: 8px;
            cursor: pointer;
            font-size: 14px;
            font-weight: 500;
            transition: all 0.2s;
            display: inline-flex;
            align-items: center;
            gap: 6px;
        }}
        .btn-primary {{
            background: var(--primary);
            color: white;
        }}
        .btn-primary:hover {{ background: var(--primary-hover); }}
        .btn-danger {{
            background: var(--danger);
            color: white;
        }}
        .btn-danger:hover {{ background: var(--danger-hover); }}
        .btn:disabled {{
            opacity: 0.5;
            cursor: not-allowed;
        }}
        .toolbar {{
            max-width: 1400px;
            margin: 0 auto 20px;
            padding: 16px 20px;
            background: var(--card-bg);
            border-radius: 12px;
            display: flex;
            gap: 12px;
            flex-wrap: wrap;
            align-items: center;
            box-shadow: 0 1px 3px rgba(0,0,0,0.1);
        }}
        .toolbar .stats {{
            color: var(--text-muted);
            font-size: 14px;
        }}
        .search-box {{
            flex: 1;
            min-width: 200px;
            max-width: 400px;
        }}
        .search-box input {{
            width: 100%;
            padding: 10px 16px;
            background: var(--bg);
            border: 1px solid var(--border);
            border-radius: 8px;
            color: var(--text);
            font-size: 14px;
        }}
        .search-box input:focus {{
            outline: none;
            border-color: var(--primary);
        }}
        .filter-group {{
            display: flex;
            gap: 8px;
            flex-wrap: wrap;
        }}
        .filter-btn {{
            padding: 8px 14px;
            background: var(--bg);
            border: 1px solid var(--border);
            border-radius: 6px;
            color: var(--text-muted);
            cursor: pointer;
            font-size: 13px;
            transition: all 0.2s;
        }}
        .filter-btn:hover {{
            border-color: var(--primary);
            color: var(--primary);
        }}
        .filter-btn.active {{
            background: var(--primary);
            border-color: var(--primary);
            color: white;
        }}
        .sort-select {{
            padding: 8px 12px;
            border: 1px solid var(--border);
            border-radius: 6px;
            font-size: 13px;
            color: var(--text);
            background: var(--card-bg);
            cursor: pointer;
        }}
        .sort-select:focus {{
            outline: none;
            border-color: var(--primary);
        }}
        .gallery {{
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
            gap: 20px;
            max-width: 1400px;
            margin: 0 auto;
        }}
        .image-card {{
            background: var(--card-bg);
            border-radius: 12px;
            overflow: hidden;
            border: 2px solid transparent;
            transition: all 0.2s;
            cursor: pointer;
            position: relative;
            box-shadow: 0 1px 3px rgba(0,0,0,0.1);
        }}
        .image-card:hover {{
            transform: translateY(-4px);
            box-shadow: 0 8px 25px rgba(0,0,0,0.15);
        }}
        .image-card.selected {{
            border-color: var(--primary);
        }}
        .image-card .checkbox {{
            position: absolute;
            top: 12px;
            left: 12px;
            width: 22px;
            height: 22px;
            background: white;
            border: 2px solid #cbd5e1;
            border-radius: 6px;
            display: flex;
            align-items: center;
            justify-content: center;
            z-index: 10;
            opacity: 0;
            transition: opacity 0.2s;
        }}
        .image-card:hover .checkbox,
        .image-card.selected .checkbox {{
            opacity: 1;
        }}
        .image-card.selected .checkbox {{
            background: var(--primary);
            border-color: var(--primary);
            color: white;
        }}
        .image-card img {{
            width: 100%;
            height: 200px;
            object-fit: cover;
            display: block;
            background: #f1f5f9;
        }}
        .image-info {{
            padding: 14px;
        }}
        .image-info .filename {{
            font-size: 13px;
            font-weight: 500;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
            margin-bottom: 8px;
            color: var(--text);
        }}
        .image-info .meta {{
            display: flex;
            flex-direction: column;
            gap: 4px;
            font-size: 12px;
            color: var(--text-muted);
        }}
        .image-info .meta-row {{
            display: flex;
            justify-content: space-between;
        }}
        .loading, .empty {{
            text-align: center;
            padding: 80px 20px;
            color: var(--text-muted);
            font-size: 16px;
            grid-column: 1 / -1;
        }}
        /* 模态框 */
        .modal {{
            display: none;
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background: rgba(0,0,0,0.9);
            z-index: 1000;
        }}
        .modal.show {{ display: flex; align-items: center; justify-content: center; }}
        .modal img {{
            max-width: 90%;
            max-height: 90%;
            object-fit: contain;
            border-radius: 8px;
        }}
        .modal-close {{
            position: absolute;
            top: 20px;
            right: 30px;
            color: white;
            font-size: 40px;
            cursor: pointer;
            opacity: 0.7;
        }}
        .modal-close:hover {{ opacity: 1; }}
        .modal-nav {{
            position: absolute;
            top: 50%;
            transform: translateY(-50%);
            color: white;
            font-size: 48px;
            cursor: pointer;
            opacity: 0.7;
            padding: 20px;
        }}
        .modal-nav:hover {{ opacity: 1; }}
        .modal-nav.prev {{ left: 20px; }}
        .modal-nav.next {{ right: 20px; }}
        /* 登录弹窗 */
        .login-overlay {{
            display: none;
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background: rgba(0,0,0,0.5);
            z-index: 2000;
            align-items: center;
            justify-content: center;
        }}
        .login-overlay.show {{ display: flex; }}
        .login-box {{
            background: var(--card-bg);
            padding: 40px;
            border-radius: 16px;
            text-align: center;
            max-width: 360px;
            width: 90%;
            box-shadow: 0 20px 40px rgba(0,0,0,0.2);
        }}
        .login-box h2 {{ margin-bottom: 24px; color: var(--text); }}
        .login-box input {{
            width: 100%;
            padding: 12px 16px;
            background: var(--bg);
            border: 1px solid var(--border);
            border-radius: 8px;
            color: var(--text);
            font-size: 16px;
            margin-bottom: 16px;
        }}
        .login-box .btn {{
            width: 100%;
            justify-content: center;
        }}
        .login-error {{
            color: var(--danger);
            font-size: 14px;
            margin-top: 12px;
            display: none;
        }}
        /* 底部操作栏 */
        .action-bar {{
            position: fixed;
            bottom: -60px;
            left: 0;
            right: 0;
            background: var(--card-bg);
            padding: 16px 20px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            transition: bottom 0.3s;
            z-index: 100;
        }}
        .action-bar.show {{ bottom: 0; }}
        .action-bar .info {{ color: var(--text-muted); }}
        .action-bar .actions {{ display: flex; gap: 12px; }}
    </style>
</head>
<body>
    <div class="header">
        <h1><span>🖼️</span> 图片图库</h1>
        <div class="header-actions">
            <button class="btn btn-primary" onclick="loadImages()">🔄 刷新</button>
            <button class="btn btn-danger" id="logoutBtn" onclick="logout()" style="display:none;">🚪 登出</button>
        </div>
    </div>
    
    <div class="toolbar">
        <span class="stats" id="stats">共 0 张图片</span>
        <div class="search-box">
            <input type="text" id="searchInput" placeholder="搜索文件名..." oninput="filterImages()">
        </div>
        <div class="filter-group">
            <button class="filter-btn active" data-filter="all" onclick="setFilter('all')">全部</button>
            <button class="filter-btn" data-filter="jpg" onclick="setFilter('jpg')">JPG</button>
            <button class="filter-btn" data-filter="png" onclick="setFilter('png')">PNG</button>
            <button class="filter-btn" data-filter="gif" onclick="setFilter('gif')">GIF</button>
            <button class="filter-btn" data-filter="webp" onclick="setFilter('webp')">WebP</button>
        </div>
        <div class="filter-group">
            <button class="filter-btn active" data-time="all" onclick="setTimeFilter('all')">全部时间</button>
            <button class="filter-btn" data-time="today" onclick="setTimeFilter('today')">今天</button>
            <button class="filter-btn" data-time="week" onclick="setTimeFilter('week')">本周</button>
            <button class="filter-btn" data-time="month" onclick="setTimeFilter('month')">本月</button>
        </div>
        <div class="filter-group">
            <button class="filter-btn active" data-size="all" onclick="setSizeFilter('all')">全部大小</button>
            <button class="filter-btn" data-size="large" onclick="setSizeFilter('large')">大图(&gt;5MB)</button>
            <button class="filter-btn" data-size="medium" onclick="setSizeFilter('medium')">中图(1-5MB)</button>
            <button class="filter-btn" data-size="small" onclick="setSizeFilter('small')">小图(&lt;1MB)</button>
        </div>
        <select class="sort-select" id="sortSelect" onchange="filterImages()">
            <option value="date-desc">最新优先</option>
            <option value="date-asc">最旧优先</option>
            <option value="size-desc">大图优先</option>
            <option value="size-asc">小图优先</option>
            <option value="name-asc">名称A-Z</option>
            <option value="name-desc">名称Z-A</option>
        </select>
    </div>

    <div class="gallery" id="gallery">
        <div class="loading">加载中...</div>
    </div>

    <div class="action-bar" id="actionBar">
        <span class="info" id="selectedInfo">已选择 0 张图片</span>
        <div class="actions">
            <button class="btn btn-danger" onclick="deleteSelected()">🗑️ 删除选中</button>
        </div>
    </div>

    <div class="modal" id="modal">
        <span class="modal-close" onclick="closeModal()">&times;</span>
        <span class="modal-nav prev" onclick="prevImage()">‹</span>
        <img id="modal-img" src="" alt="">
        <span class="modal-nav next" onclick="nextImage()">›</span>
    </div>

    <div class="login-overlay" id="loginOverlay">
        <div class="login-box">
            <h2>🔐 请输入访问密码</h2>
            <input type="password" id="passwordInput" placeholder="请输入密码" onkeydown="if(event.key==='Enter')login()">
            <button class="btn btn-primary" onclick="login()">确认</button>
            <p class="login-error" id="loginError">密码错误，请重试</p>
        </div>
    </div>

    <script>
        let allImages = [];
        let filteredImages = [];
        let selectedImages = new Set();
        let currentFilter = 'all';
        let currentTimeFilter = 'all';
        let currentSizeFilter = 'all';
        let currentIndex = 0;
        let needAuth = {require_auth};

        function formatSize(bytes) {{
            if (bytes < 1024) return bytes + ' B';
            if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
            return (bytes / 1024 / 1024).toFixed(2) + ' MB';
        }}

        function formatDate(timestamp) {{
            return new Date(timestamp * 1000).toLocaleDateString('zh-CN');
        }}

        async function checkAuth() {{
            try {{
                const resp = await fetch('/images');
                if (resp.status === 401) {{
                    needAuth = true;
                    showLogin();
                }} else {{
                    loadImages();
                }}
            }} catch(e) {{
                loadImages();
            }}
        }}

        function showLogin() {{
            document.getElementById('loginOverlay').classList.add('show');
            document.getElementById('passwordInput').focus();
        }}

        function hideLogin() {{
            document.getElementById('loginOverlay').classList.remove('show');
            document.getElementById('logoutBtn').style.display = 'inline-flex';
        }}

        async function login() {{
            const password = document.getElementById('passwordInput').value;
            const resp = await fetch('/gallery-auth', {{
                method: 'POST',
                headers: {{'Content-Type': 'application/json'}},
                body: JSON.stringify({{password}})
            }});
            const data = await resp.json();
            if (data.success) {{
                hideLogin();
                loadImages();
            }} else {{
                document.getElementById('loginError').style.display = 'block';
            }}
        }}

        async function logout() {{
            await fetch('/gallery-logout', {{method: 'POST'}});
            document.getElementById('logoutBtn').style.display = 'none';
            if (needAuth) showLogin();
        }}

        async function loadImages() {{
            const gallery = document.getElementById('gallery');
            gallery.innerHTML = '<div class="loading">加载中...</div>';
            
            try {{
                const resp = await fetch('/images');
                const data = await resp.json();
                
                if (resp.status === 401) {{
                    showLogin();
                    return;
                }}
                
                if (data.error) {{
                    gallery.innerHTML = '<div class="empty">加载失败: ' + data.error + '</div>';
                    return;
                }}
                
                allImages = data.images || [];
                filteredImages = [...allImages];
                selectedImages.clear();
                updateActionBar();
                renderGallery();
            }} catch (e) {{
                gallery.innerHTML = '<div class="empty">加载失败: ' + e.message + '</div>';
            }}
        }}

        function filterImages() {{
            const keyword = document.getElementById('searchInput').value.toLowerCase();
            const now = new Date();
            const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
            const weekAgo = new Date(today.getTime() - 7 * 24 * 60 * 60 * 1000);
            const monthAgo = new Date(today.getFullYear(), today.getMonth() - 1, today.getDate());
            
            filteredImages = allImages.filter(img => {{
                const matchKeyword = img.filename.toLowerCase().includes(keyword);
                const matchFilter = currentFilter === 'all' || img.filename.toLowerCase().endsWith('.' + currentFilter);
                
                let matchTime = true;
                const imgDate = new Date(img.created_time * 1000);
                if (currentTimeFilter === 'today') {{
                    matchTime = imgDate >= today;
                }} else if (currentTimeFilter === 'week') {{
                    matchTime = imgDate >= weekAgo;
                }} else if (currentTimeFilter === 'month') {{
                    matchTime = imgDate >= monthAgo;
                }}
                
                let matchSize = true;
                const sizeMB = img.size / 1024 / 1024;
                if (currentSizeFilter === 'large') {{
                    matchSize = sizeMB > 5;
                }} else if (currentSizeFilter === 'medium') {{
                    matchSize = sizeMB >= 1 && sizeMB <= 5;
                }} else if (currentSizeFilter === 'small') {{
                    matchSize = sizeMB < 1;
                }}
                
                return matchKeyword && matchFilter && matchTime && matchSize;
            }});
            
            // 排序
            const sortValue = document.getElementById('sortSelect').value;
            filteredImages.sort((a, b) => {{
                if (sortValue === 'date-desc') return b.created_time - a.created_time;
                if (sortValue === 'date-asc') return a.created_time - b.created_time;
                if (sortValue === 'size-desc') return b.size - a.size;
                if (sortValue === 'size-asc') return a.size - b.size;
                if (sortValue === 'name-asc') return a.filename.localeCompare(b.filename);
                if (sortValue === 'name-desc') return b.filename.localeCompare(a.filename);
                return 0;
            }});
            
            renderGallery();
        }}

        function setFilter(filter) {{
            currentFilter = filter;
            document.querySelectorAll('.filter-btn[data-filter]').forEach(btn => {{
                btn.classList.toggle('active', btn.dataset.filter === filter);
            }});
            filterImages();
        }}

        function setTimeFilter(filter) {{
            currentTimeFilter = filter;
            document.querySelectorAll('.filter-btn[data-time]').forEach(btn => {{
                btn.classList.toggle('active', btn.dataset.time === filter);
            }});
            filterImages();
        }}

        function setSizeFilter(filter) {{
            currentSizeFilter = filter;
            document.querySelectorAll('.filter-btn[data-size]').forEach(btn => {{
                btn.classList.toggle('active', btn.dataset.size === filter);
            }});
            filterImages();
        }}

        function renderGallery() {{
            const gallery = document.getElementById('gallery');
            const stats = document.getElementById('stats');
            
            stats.textContent = `共 ${{filteredImages.length}} 张图片`;
            
            if (filteredImages.length === 0) {{
                gallery.innerHTML = '<div class="empty">暂无图片</div>';
                return;
            }}
            
            gallery.innerHTML = filteredImages.map((img, idx) => `
                <div class="image-card ${{selectedImages.has(img.filename) ? 'selected' : ''}}" 
                     data-filename="${{img.filename}}" data-index="${{idx}}"
                     onclick="toggleSelect('${{img.filename}}', event); showImage(${{idx}})">
                    <div class="checkbox">${{selectedImages.has(img.filename) ? '✓' : ''}}</div>
                    <img src="${{img.url}}" loading="lazy" alt="${{img.filename}}">
                    <div class="image-info">
                        <div class="filename" title="${{img.filename}}">${{img.filename}}</div>
                        <div class="meta">
                            <div class="meta-row">
                                <span>${{formatSize(img.size)}}</span>
                                <span>${{img.width && img.height ? img.width + '×' + img.height : ''}}</span>
                            </div>
                            <span>${{formatDate(img.created_time)}}</span>
                        </div>
                    </div>
                </div>
            `).join('');
        }}

        function toggleSelect(filename, event) {{
            event.stopPropagation();
            if (selectedImages.has(filename)) {{
                selectedImages.delete(filename);
            }} else {{
                selectedImages.add(filename);
            }}
            updateActionBar();
            renderGallery();
        }}

        function updateActionBar() {{
            const bar = document.getElementById('actionBar');
            const info = document.getElementById('selectedInfo');
            if (selectedImages.size > 0) {{
                bar.classList.add('show');
                info.textContent = `已选择 ${{selectedImages.size}} 张图片`;
            }} else {{
                bar.classList.remove('show');
            }}
        }}

        function showImage(idx) {{
            if (event && event.target.closest('.checkbox')) return;
            currentIndex = idx;
            document.getElementById('modal-img').src = filteredImages[idx].url;
            document.getElementById('modal').classList.add('show');
        }}

        function closeModal() {{
            document.getElementById('modal').classList.remove('show');
        }}

        function prevImage() {{
            currentIndex = (currentIndex - 1 + filteredImages.length) % filteredImages.length;
            document.getElementById('modal-img').src = filteredImages[currentIndex].url;
        }}

        function nextImage() {{
            currentIndex = (currentIndex + 1) % filteredImages.length;
            document.getElementById('modal-img').src = filteredImages[currentIndex].url;
        }}

        async function deleteSelected() {{
            if (selectedImages.size === 0) return;
            if (!confirm(`确定要删除选中的 ${{selectedImages.size}} 张图片吗？`)) return;
            
            const filenames = Array.from(selectedImages);
            for (const filename of filenames) {{
                await fetch('/image-delete', {{
                    method: 'DELETE',
                    headers: {{'Content-Type': 'application/json'}},
                    body: JSON.stringify({{filename}})
                }});
            }}
            selectedImages.clear();
            loadImages();
        }}

        document.addEventListener('keydown', function(e) {{
            if (e.key === 'Escape') closeModal();
            if (e.key === 'ArrowLeft') prevImage();
            if (e.key === 'ArrowRight') nextImage();
        }});

        if (needAuth) {{
            checkAuth();
        }} else {{
            loadImages();
        }}
    </script>
</body>
</html>"""
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/image-delete", methods=["DELETE"])
@require_gallery_auth
def delete_image():
    """删除指定图片"""
    data = request.get_json()
    filename = data.get("filename", "")
    
    if not filename:
        return jsonify({"error": "缺少文件名"}), 400
    
    # 防止路径遍历攻击
    filename = os.path.basename(filename)
    filepath = os.path.join(UPLOAD_FOLDER, filename)
    
    if not os.path.exists(filepath):
        return jsonify({"error": "文件不存在"}), 404
    
    try:
        os.remove(filepath)
        logger.info(f"删除图片: {filename}")
        return jsonify({"success": True, "filename": filename})
    except Exception as e:
        logger.error(f"删除图片失败: {str(e)}")
        return jsonify({"error": f"删除失败: {str(e)}"}), 500


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
