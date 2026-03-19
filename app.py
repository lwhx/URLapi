import base64
import binascii
import hmac
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
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import wraps, lru_cache

from flask import Flask, jsonify, request, send_from_directory, url_for, session
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from PIL import Image, UnidentifiedImageError
from werkzeug.datastructures import FileStorage
from werkzeug.exceptions import RequestEntityTooLarge

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# 优化 1: 配置环境变量化
UPLOAD_FOLDER = os.environ.get("UPLOAD_FOLDER", "/app/uploads")
THUMBNAIL_FOLDER = os.path.join(UPLOAD_FOLDER, ".thumbnails")
MAX_FILE_SIZE = int(os.environ.get("MAX_FILE_SIZE", str(100 * 1024 * 1024)))
DOWNLOAD_TIMEOUT_SECONDS = int(os.environ.get("DOWNLOAD_TIMEOUT_SECONDS", "30"))
DOWNLOAD_CHUNK_SIZE = int(os.environ.get("DOWNLOAD_CHUNK_SIZE", str(1024 * 1024)))
MAX_CONTENT_LENGTH = int(os.environ.get("MAX_CONTENT_LENGTH", str(140 * 1024 * 1024)))
THREAD_POOL_SIZE = int(os.environ.get("THREAD_POOL_SIZE", "4"))
THUMBNAIL_SIZE = int(os.environ.get("THUMBNAIL_SIZE", "300"))
CACHE_TTL = int(os.environ.get("CACHE_TTL", "60"))

# 确保缩略图目录存在
os.makedirs(THUMBNAIL_FOLDER, exist_ok=True)

app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH

ALLOWED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
SUPPORTED_IMAGE_FORMATS = {
    "PNG": ("image/png", ".png"),
    "JPEG": ("image/jpeg", ".jpg"),
    "GIF": ("image/gif", ".gif"),
    "WEBP": ("image/webp", ".webp"),
}

# 优化 3: 简单内存缓存（用于图片列表）
cache = {"images": None, "timestamp": 0}


def generate_thumbnail(filepath: str, filename: str) -> bool:
    """生成缩略图"""
    try:
        thumbnail_path = os.path.join(THUMBNAIL_FOLDER, filename)
        if os.path.exists(thumbnail_path):
            return True

        with Image.open(filepath) as img:
            img.thumbnail((THUMBNAIL_SIZE, THUMBNAIL_SIZE), Image.Resampling.LANCZOS)
            img.save(thumbnail_path, optimize=True, quality=85)
        return True
    except Exception as exc:
        logger.warning("生成缩略图失败 %s: %s", filename, str(exc))
        return False

# 图库密码认证（通过环境变量设置，默认为空表示不需要密码）
GALLERY_PASSWORD = os.environ.get("GALLERY_PASSWORD", "")
SESSION_SECRET = os.environ.get("SESSION_SECRET", uuid.uuid4().hex)

app.secret_key = SESSION_SECRET

# 优化 2: 添加速率限制
limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=["200 per day", "50 per hour"]
)

# 优化 3: 线程池用于并发处理
executor = ThreadPoolExecutor(max_workers=THREAD_POOL_SIZE)


def check_gallery_auth():
    """检查图库认证状态"""
    if not GALLERY_PASSWORD:
        return True
    return session.get("gallery_authenticated", False)


def require_gallery_auth(f):
    """图库认证装饰器"""
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


@lru_cache(maxsize=1)
def get_supported_extensions_message() -> str:
    """获取支持的扩展名列表（缓存）"""
    return ", ".join(sorted(ALLOWED_EXTENSIONS))


def get_uploaded_file_size(file: FileStorage) -> int:
    """获取上传文件大小，不影响后续读取/保存"""
    current_position = file.stream.tell()
    file.stream.seek(0, os.SEEK_END)
    size = file.stream.tell()
    file.stream.seek(current_position)
    return size


def ensure_uploaded_file_size(file: FileStorage) -> int:
    """校验上传文件大小（按单文件限制）"""
    size = get_uploaded_file_size(file)
    if size > MAX_FILE_SIZE:
        filename = file.filename or "unknown"
        raise FileTooLargeError(
            f"文件 {filename} 大小不能超过{format_size(MAX_FILE_SIZE)}，当前文件大小: {format_size(size)}"
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
        logger.warning("无效的图片数据: %s", str(exc))
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


def read_uploaded_image(file: FileStorage, *, validate_extension: bool) -> tuple[bytes, ImageMetadata]:
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
    """构建上传文件响应负载"""
    payload = {
        "url": url_for("get_image", filename=filename, _external=True),
        "filename": filename,
        "size": size,
    }
    if original_name is not None:
        payload["original_name"] = original_name
    return payload


def save_uploaded_image(image_data: bytes, extension: str) -> tuple[str, int]:
    """保存图片并返回文件名和大小，同时生成缩略图"""
    filename = f"{uuid.uuid4().hex}{extension}"
    filepath = os.path.join(UPLOAD_FOLDER, filename)
    with open(filepath, "wb") as f:
        f.write(image_data)
    # 生成缩略图
    generate_thumbnail(filepath, filename)
    logger.info("图片已保存: %s (大小: %s)", filename, format_size(len(image_data)))
    return filename, len(image_data)


def validate_remote_url(url: str) -> urllib.parse.ParseResult:
    """验证远程 URL 的合法性"""
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception as exc:
        raise InvalidRemoteUrlError("URL 格式不合法") from exc

    if not parsed.scheme or not parsed.netloc:
        raise InvalidRemoteUrlError("URL 格式不合法")

    if parsed.scheme not in ("http", "https"):
        raise InvalidRemoteUrlError("仅支持 HTTP 和 HTTPS 协议")

    # 验证端口
    if parsed.port is not None:
        if not (1 <= parsed.port <= 65535):
            raise InvalidRemoteUrlError("URL端口不合法")

    # 验证主机名是否为私有地址
    hostname = parsed.hostname
    if not hostname:
        raise InvalidRemoteUrlError("URL 格式不合法")

    try:
        ip = ipaddress.ip_address(hostname)
        if ip.is_private or ip.is_loopback or ip.is_link_local:
            raise UnsafeRemoteAddressError("不允许访问本地或私有网络地址")
    except ValueError:
        # 不是 IP 地址，是域名，继续处理
        pass

    return parsed


def create_url_opener() -> urllib.request.OpenerDirector:
    """创建 URL 打开器，禁用 SSL 验证"""
    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE

    https_handler = urllib.request.HTTPSHandler(context=ssl_context)
    return urllib.request.build_opener(https_handler)


def download_image(url: str) -> tuple[bytes, ImageMetadata]:
    """从 URL 下载图片"""
    parsed_url = validate_remote_url(url)

    try:
        opener = create_url_opener()
        request_obj = urllib.request.Request(url)
        request_obj.add_header(
            "User-Agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        )

        with opener.open(request_obj, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
            # 检查 Content-Length
            content_length = response.headers.get("Content-Length")
            if content_length:
                try:
                    content_length = int(content_length)
                    if content_length > MAX_FILE_SIZE:
                        raise FileTooLargeError(
                            f"远程文件过大，不能超过 {format_size(MAX_FILE_SIZE)}"
                        )
                except ValueError:
                    pass

            # 分块读取
            chunks = []
            total_size = 0
            while True:
                chunk = response.read(DOWNLOAD_CHUNK_SIZE)
                if not chunk:
                    break
                chunks.append(chunk)
                total_size += len(chunk)
                if total_size > MAX_FILE_SIZE:
                    raise FileTooLargeError(
                        f"远程文件过大，不能超过 {format_size(MAX_FILE_SIZE)}"
                    )

            image_data = b"".join(chunks)

            # 检查 Content-Type
            content_type = response.headers.get("Content-Type", "").lower()
            if content_type and not content_type.startswith("image/"):
                raise InvalidImageError("远程文件不是图片")

            metadata = get_image_metadata(image_data)
            logger.info("图片已从 URL 下载: %s (大小: %s)", url, format_size(len(image_data)))
            return image_data, metadata

    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise RemoteDownloadError("下载图片超时") from exc
        elif isinstance(exc.reason, ssl.SSLError):
            raise RemoteDownloadError(f"下载图片失败: SSL证书校验失败") from exc
        else:
            raise RemoteDownloadError(f"下载图片失败: {str(exc)}") from exc
    except (InvalidImageError, FileTooLargeError, UnsafeRemoteAddressError):
        raise
    except Exception as exc:
        logger.error("下载图片异常", exc_info=True)
        raise RemoteDownloadError(f"下载图片失败: {str(exc)}") from exc


@app.route("/upload", methods=["POST"])
@limiter.limit("10 per minute")
def upload():
    """上传 Base64 编码的图片"""
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "请求体必须是 JSON"}), 400

        base64_data = data.get("base64", "")
        if not base64_data:
            return jsonify({"error": "base64 参数不能为空"}), 400

        # 估算解码后大小
        estimated_size = estimate_base64_decoded_size(base64_data)
        if estimated_size > MAX_FILE_SIZE:
            return (
                jsonify(
                    {
                        "error": f"文件过大，不能超过 {format_size(MAX_FILE_SIZE)}，估算大小: {format_size(estimated_size)}"
                    }
                ),
                400,
            )

        try:
            image_data = base64.b64decode(base64_data)
        except (TypeError, binascii.Error) as exc:
            logger.warning("Base64 解码失败: %s", str(exc))
            return jsonify({"error": "Base64 格式不合法"}), 400

        metadata = get_image_metadata(image_data)
        filename, size = save_uploaded_image(image_data, metadata.extension)
        return jsonify(build_uploaded_file_payload(filename, size)), 200

    except InvalidImageError as exc:
        return jsonify({"error": str(exc)}), 400
    except FileTooLargeError as exc:
        return jsonify({"error": str(exc)}), 413
    except Exception as exc:
        logger.error("上传图片异常", exc_info=True)
        return jsonify({"error": "上传失败"}), 500


@app.route("/upload-file", methods=["POST"])
@limiter.limit("10 per minute")
def upload_file():
    """上传图片文件"""
    try:
        if "file" not in request.files:
            return jsonify({"error": "缺少 file 参数"}), 400

        file = request.files["file"]
        if not file or not file.filename:
            return jsonify({"error": "文件不能为空"}), 400

        image_data, metadata = read_uploaded_image(file, validate_extension=True)
        filename, size = save_uploaded_image(image_data, metadata.extension)
        return jsonify(build_uploaded_file_payload(filename, size, original_name=file.filename)), 200

    except InvalidImageError as exc:
        return jsonify({"error": str(exc)}), 400
    except FileTooLargeError as exc:
        return jsonify({"error": str(exc)}), 413
    except Exception as exc:
        logger.error("上传文件异常", exc_info=True)
        return jsonify({"error": "上传失败"}), 500


@app.route("/upload-directory", methods=["POST"])
@limiter.limit("5 per minute")
def upload_directory():
    """批量上传图片文件"""
    try:
        if "files" not in request.files:
            return jsonify({"error": "缺少 files 参数"}), 400

        files = request.files.getlist("files")
        if not files:
            return jsonify({"error": "文件列表不能为空"}), 400

        uploaded_files = []
        failed_files = []

        for file in files:
            if not file or not file.filename:
                continue

            try:
                image_data, metadata = read_uploaded_image(file, validate_extension=True)
                filename, size = save_uploaded_image(image_data, metadata.extension)
                uploaded_files.append(
                    build_uploaded_file_payload(filename, size, original_name=file.filename)
                )
            except (InvalidImageError, FileTooLargeError) as exc:
                failed_files.append({"original_name": file.filename, "error": str(exc)})
            except Exception as exc:
                logger.error("处理文件 %s 异常", file.filename, exc_info=True)
                failed_files.append({"original_name": file.filename, "error": "处理失败"})

        if not uploaded_files and failed_files:
            return (
                jsonify(
                    {
                        "count": 0,
                        "failed_count": len(failed_files),
                        "files": [],
                        "failed": failed_files,
                    }
                ),
                400,
            )

        return jsonify(
            {
                "count": len(uploaded_files),
                "failed_count": len(failed_files),
                "files": uploaded_files,
                "failed": failed_files,
            }
        ), 200

    except Exception as exc:
        logger.error("批量上传异常", exc_info=True)
        return jsonify({"error": "批量上传失败"}), 500


@app.route("/file-to-base64", methods=["POST"])
@limiter.limit("10 per minute")
def file_to_base64():
    """将上传的文件转换为 Base64"""
    try:
        if "file" not in request.files:
            return jsonify({"error": "缺少 file 参数"}), 400

        file = request.files["file"]
        if not file or not file.filename:
            return jsonify({"error": "文件不能为空"}), 400

        image_data, metadata = read_uploaded_image(file, validate_extension=False)
        base64_data = base64.b64encode(image_data).decode("ascii")

        return jsonify(
            {
                "base64": base64_data,
                "mime_type": metadata.mime_type,
                "size": len(image_data),
            }
        ), 200

    except InvalidImageError as exc:
        return jsonify({"error": str(exc)}), 400
    except FileTooLargeError as exc:
        return jsonify({"error": str(exc)}), 413
    except Exception as exc:
        logger.error("文件转 Base64 异常", exc_info=True)
        return jsonify({"error": "转换失败"}), 500


@app.route("/url-to-base64", methods=["POST"])
@limiter.limit("10 per minute")
def url_to_base64():
    """从 URL 下载图片并转换为 Base64"""
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "请求体必须是 JSON"}), 400

        url = data.get("url")
        if not url:
            return jsonify({"error": "url 参数不能为空"}), 400

        if not isinstance(url, str):
            return jsonify({"error": "url参数必须是字符串"}), 400

        image_data, metadata = download_image(url)
        base64_data = base64.b64encode(image_data).decode("ascii")

        return jsonify(
            {
                "base64": base64_data,
                "mime_type": metadata.mime_type,
                "size": len(image_data),
            }
        ), 200

    except UnsafeRemoteAddressError as exc:
        return jsonify({"error": str(exc)}), 400
    except InvalidRemoteUrlError as exc:
        return jsonify({"error": str(exc)}), 400
    except InvalidImageError as exc:
        return jsonify({"error": str(exc)}), 400
    except FileTooLargeError as exc:
        return jsonify({"error": str(exc)}), 413
    except RemoteDownloadError as exc:
        return jsonify({"error": str(exc)}), 502
    except Exception as exc:
        logger.error("URL 转 Base64 异常", exc_info=True)
        return jsonify({"error": "转换失败"}), 500


@app.route("/get-image/<filename>", methods=["GET"])
@limiter.exempt
def get_image(filename: str):
    """获取图片"""
    try:
        # 防止路径遍历攻击
        if ".." in filename or "/" in filename or "\\" in filename:
            return jsonify({"error": "非法的文件名"}), 400

        filepath = os.path.join(UPLOAD_FOLDER, filename)
        if not os.path.exists(filepath):
            return jsonify({"error": "文件不存在"}), 404

        return send_from_directory(UPLOAD_FOLDER, filename)
    except Exception as exc:
        logger.error("获取图片异常", exc_info=True)
        return jsonify({"error": "获取失败"}), 500


@app.route("/thumbnail/<filename>", methods=["GET"])
@limiter.exempt
def get_thumbnail(filename: str):
    """获取缩略图"""
    try:
        # 防止路径遍历攻击
        if ".." in filename or "/" in filename or "\\" in filename:
            return jsonify({"error": "非法的文件名"}), 400

        filepath = os.path.join(UPLOAD_FOLDER, filename)
        if not os.path.exists(filepath):
            return jsonify({"error": "文件不存在"}), 404

        thumbnail_path = os.path.join(THUMBNAIL_FOLDER, filename)
        if not os.path.exists(thumbnail_path):
            # 如果缩略图不存在，实时生成
            generate_thumbnail(filepath, filename)

        return send_from_directory(THUMBNAIL_FOLDER, filename)
    except Exception as exc:
        logger.error("获取缩略图异常", exc_info=True)
        return jsonify({"error": "获取失败"}), 500


def get_image_info(filename: str) -> dict | None:
    """获取单个图片信息（用于并发处理）"""
    try:
        filepath = os.path.join(UPLOAD_FOLDER, filename)
        if not os.path.isfile(filepath):
            return None

        stat = os.stat(filepath)
        width, height = 0, 0
        try:
            with Image.open(filepath) as img:
                width, height = img.size
        except (UnidentifiedImageError, OSError, IOError) as e:
            logger.warning("无法读取图片尺寸 %s: %s", filename, str(e))

        return {
            "filename": filename,
            "url": f"/get-image/{filename}",
            "size": stat.st_size,
            "width": width,
            "height": height,
            "created_time": stat.st_ctime,
        }
    except Exception as exc:
        logger.error("获取图片信息异常 %s", filename, exc_info=True)
        return None


@app.route("/images", methods=["GET"])
@require_gallery_auth
def list_images():
    """获取图片列表（支持分页，使用缓存）"""
    try:
        import time
        # 获取分页参数
        page = request.args.get("page", 1, type=int)
        per_page = request.args.get("per_page", 20, type=int)
        
        # 限制每页数量
        per_page = min(per_page, 100)
        
        # 检查缓存是否有效（60秒 TTL）
        current_time = time.time()
        if cache["images"] is None or (current_time - cache["timestamp"]) > CACHE_TTL:
            files = os.listdir(UPLOAD_FOLDER)
            # 使用线程池并发处理图片信息
            with ThreadPoolExecutor(max_workers=THREAD_POOL_SIZE) as pool:
                results = pool.map(get_image_info, files)
                cache["images"] = [img for img in results if img is not None]
            cache["timestamp"] = current_time
        
        # 使用缓存数据
        all_images = cache["images"]
        # 按创建时间倒序排列
        all_images.sort(key=lambda x: x["created_time"], reverse=True)
        
        # 分页
        total = len(all_images)
        start = (page - 1) * per_page
        end = start + per_page
        images = all_images[start:end]
        
        return jsonify({
            "images": images,
            "total": total,
            "page": page,
            "per_page": per_page,
            "total_pages": (total + per_page - 1) // per_page
        }), 200

    except Exception as exc:
        logger.error("获取图片列表异常", exc_info=True)
        return jsonify({"error": "获取列表失败"}), 500


@app.route("/gallery-auth", methods=["POST"])
@limiter.limit("5 per minute")
def gallery_auth():
    """图库认证"""
    try:
        if not GALLERY_PASSWORD:
            return jsonify({"error": "图库未设置密码"}), 400

        data = request.get_json()
        if not data:
            return jsonify({"error": "请求体必须是 JSON"}), 400

        password = data.get("password", "")

        # 优化 2: 使用 hmac.compare_digest 防止时序攻击
        if hmac.compare_digest(password, GALLERY_PASSWORD):
            session["gallery_authenticated"] = True
            logger.info("用户已认证")
            return jsonify({"success": True}), 200
        else:
            logger.warning("认证失败：密码错误")
            return jsonify({"error": "密码错误"}), 401

    except Exception as exc:
        logger.error("认证异常", exc_info=True)
        return jsonify({"error": "认证失败"}), 500


@app.route("/gallery-logout", methods=["POST"])
def gallery_logout():
    """图库登出"""
    try:
        session.pop("gallery_authenticated", None)
        logger.info("用户已登出")
        return jsonify({"success": True}), 200
    except Exception as exc:
        logger.error("登出异常", exc_info=True)
        return jsonify({"error": "登出失败"}), 500


@app.route("/delete", methods=["POST"])
@require_gallery_auth
@limiter.limit("20 per minute")
def delete_image():
    """删除单个图片"""
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "请求体必须是 JSON"}), 400

        filename = data.get("filename", "")
        if not filename:
            return jsonify({"error": "filename 参数不能为空"}), 400

        # 防止路径遍历攻击
        if ".." in filename or "/" in filename or "\\" in filename:
            return jsonify({"error": "非法的文件名"}), 400

        filepath = os.path.join(UPLOAD_FOLDER, filename)
        if not os.path.exists(filepath):
            return jsonify({"error": "文件不存在"}), 404

        os.remove(filepath)
        # 删除缩略图
        thumbnail_path = os.path.join(THUMBNAIL_FOLDER, filename)
        if os.path.exists(thumbnail_path):
            os.remove(thumbnail_path)
        # 清除缓存
        cache["images"] = None
        logger.info("图片已删除: %s", filename)
        return jsonify({"success": True}), 200

    except Exception as exc:
        logger.error("删除图片异常", exc_info=True)
        return jsonify({"error": "删除失败"}), 500


@app.route("/delete-multiple", methods=["POST"])
@require_gallery_auth
@limiter.limit("10 per minute")
def delete_multiple():
    """批量删除图片"""
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "请求体必须是 JSON"}), 400

        filenames = data.get("filenames", [])
        if not filenames or not isinstance(filenames, list):
            return jsonify({"error": "filenames 参数必须是非空列表"}), 400

        deleted = []
        failed = []

        for filename in filenames:
            try:
                # 防止路径遍历攻击
                if ".." in filename or "/" in filename or "\\" in filename:
                    failed.append({"filename": filename, "error": "非法的文件名"})
                    continue

                filepath = os.path.join(UPLOAD_FOLDER, filename)
                if not os.path.exists(filepath):
                    failed.append({"filename": filename, "error": "文件不存在"})
                    continue

                os.remove(filepath)
                # 删除缩略图
                thumbnail_path = os.path.join(THUMBNAIL_FOLDER, filename)
                if os.path.exists(thumbnail_path):
                    os.remove(thumbnail_path)
                deleted.append(filename)
                logger.info("图片已删除: %s", filename)

            except Exception as exc:
                logger.error("删除图片 %s 异常", filename, exc_info=True)
                failed.append({"filename": filename, "error": "删除失败"})
        
        # 清除缓存
        cache["images"] = None

        return jsonify(
            {
                "deleted_count": len(deleted),
                "failed_count": len(failed),
                "deleted": deleted,
                "failed": failed,
            }
        ), 200

    except Exception as exc:
        logger.error("批量删除异常", exc_info=True)
        return jsonify({"error": "批量删除失败"}), 500


@app.route("/gallery-list", methods=["GET"])
@require_gallery_auth
def gallery_list():
    """获取图库列表（HTML 页面）"""
    html = """
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>图库</title>
        <style>
            * { margin: 0; padding: 0; box-sizing: border-box; }
            body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #f5f5f5; }
            .container { max-width: 1200px; margin: 0 auto; padding: 20px; }
            .header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 30px; }
            .header h1 { font-size: 28px; color: #333; }
            .logout-btn { padding: 10px 20px; background: #ff6b6b; color: white; border: none; border-radius: 4px; cursor: pointer; }
            .logout-btn:hover { background: #ff5252; }
            .gallery { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 20px; }
            .image-card { background: white; border-radius: 8px; overflow: hidden; box-shadow: 0 2px 8px rgba(0,0,0,0.1); transition: transform 0.2s; }
            .image-card:hover { transform: translateY(-4px); box-shadow: 0 4px 12px rgba(0,0,0,0.15); }
            .image-wrapper { position: relative; width: 100%; padding-bottom: 100%; overflow: hidden; }
            .image-wrapper img { position: absolute; top: 0; left: 0; width: 100%; height: 100%; object-fit: cover; }
            .image-info { padding: 12px; }
            .image-name { font-size: 12px; color: #666; word-break: break-all; margin-bottom: 8px; }
            .image-size { font-size: 12px; color: #999; margin-bottom: 8px; }
            .delete-btn { width: 100%; padding: 8px; background: #ff6b6b; color: white; border: none; border-radius: 4px; cursor: pointer; font-size: 12px; }
            .delete-btn:hover { background: #ff5252; }
            .empty { text-align: center; padding: 60px 20px; color: #999; }
            .select-all { margin-bottom: 20px; }
            .select-all input { margin-right: 10px; }
            .batch-delete { margin-bottom: 20px; }
            .batch-delete button { padding: 10px 20px; background: #ff6b6b; color: white; border: none; border-radius: 4px; cursor: pointer; }
            .batch-delete button:hover { background: #ff5252; }
            .batch-delete button:disabled { background: #ccc; cursor: not-allowed; }
            .pagination { display: flex; justify-content: center; align-items: center; gap: 10px; margin-top: 30px; }
            .pagination button, .pagination span { padding: 8px 12px; border: 1px solid #ddd; border-radius: 4px; background: white; cursor: pointer; }
            .pagination button:hover { background: #f0f0f0; }
            .pagination button:disabled { background: #f5f5f5; cursor: not-allowed; color: #999; }
            .pagination .current { background: #667eea; color: white; border-color: #667eea; }
            .page-info { text-align: center; color: #666; margin-top: 20px; }
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1>图库</h1>
                <button class="logout-btn" onclick="logout()">登出</button>
            </div>

            <div class="select-all">
                <input type="checkbox" id="selectAll" onchange="toggleSelectAll()">
                <label for="selectAll">全选</label>
            </div>

            <div class="batch-delete">
                <button onclick="batchDelete()" id="batchDeleteBtn" disabled>批量删除</button>
            </div>

            <div class="gallery" id="gallery"></div>
            <div class="empty" id="empty" style="display: none;">暂无图片</div>
            <div class="page-info" id="pageInfo"></div>
            <div class="pagination" id="pagination"></div>
        </div>

        <script>
            let currentPage = 1;
            let perPage = 20;
            let totalPages = 0;
            let totalImages = 0;

            async function loadImages(page = 1) {
                try {
                    const response = await fetch(`/images?page=${page}&per_page=${perPage}`);
                    const data = await response.json();

                    if (!response.ok) {
                        alert('加载失败: ' + data.error);
                        return;
                    }

                    currentPage = data.page;
                    totalPages = data.total_pages;
                    totalImages = data.total;

                    renderGallery(data.images);
                    renderPagination();
                    updatePageInfo();
                } catch (e) {
                    alert('加载失败: ' + e.message);
                }
            }

            function renderGallery(images) {
                const gallery = document.getElementById('gallery');
                const empty = document.getElementById('empty');

                if (images.length === 0) {
                    gallery.innerHTML = '';
                    empty.style.display = 'block';
                    return;
                }

                empty.style.display = 'none';
                gallery.innerHTML = images.map(img => `
                    <div class="image-card">
                        <div class="image-wrapper">
                            <img src="${img.url}" alt="${img.filename}">
                        </div>
                        <div class="image-info">
                            <div class="image-name">${img.filename}</div>
                            <div class="image-size">${(img.size / 1024).toFixed(2)} KB</div>
                            <input type="checkbox" class="image-checkbox" value="${img.filename}">
                            <button class="delete-btn" onclick="deleteImage('${img.filename}')">删除</button>
                        </div>
                    </div>
                `).join('');
            }

            function renderPagination() {
                const pagination = document.getElementById('pagination');
                let html = '';

                if (currentPage > 1) {
                    html += `<button onclick="loadImages(1)">首页</button>`;
                    html += `<button onclick="loadImages(${currentPage - 1})">上一页</button>`;
                }

                const start = Math.max(1, currentPage - 2);
                const end = Math.min(totalPages, currentPage + 2);

                if (start > 1) html += '<span>...</span>';

                for (let i = start; i <= end; i++) {
                    if (i === currentPage) {
                        html += `<span class="current">${i}</span>`;
                    } else {
                        html += `<button onclick="loadImages(${i})">${i}</button>`;
                    }
                }

                if (end < totalPages) html += '<span>...</span>';

                if (currentPage < totalPages) {
                    html += `<button onclick="loadImages(${currentPage + 1})">下一页</button>`;
                    html += `<button onclick="loadImages(${totalPages})">末页</button>`;
                }

                pagination.innerHTML = html;
            }

            function updatePageInfo() {
                const pageInfo = document.getElementById('pageInfo');
                if (totalImages === 0) {
                    pageInfo.textContent = '';
                } else {
                    pageInfo.textContent = `共 ${totalImages} 张图片，第 ${currentPage} / ${totalPages} 页`;
                }
            }

            function deleteImage(filename) {
                if (!confirm('确定要删除此图片吗？')) return;

                fetch('/delete', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ filename })
                })
                .then(r => r.json())
                .then(data => {
                    if (data.success) {
                        loadImages(currentPage);
                    } else {
                        alert('删除失败: ' + data.error);
                    }
                })
                .catch(e => alert('删除失败: ' + e));
            }

            function toggleSelectAll() {
                const checked = document.getElementById('selectAll').checked;
                document.querySelectorAll('.image-checkbox').forEach(cb => cb.checked = checked);
                updateBatchDeleteBtn();
            }

            function updateBatchDeleteBtn() {
                const checked = document.querySelectorAll('.image-checkbox:checked').length > 0;
                document.getElementById('batchDeleteBtn').disabled = !checked;
            }

            function batchDelete() {
                const filenames = Array.from(document.querySelectorAll('.image-checkbox:checked')).map(cb => cb.value);
                if (filenames.length === 0) return;
                if (!confirm(`确定要删除 ${filenames.length} 张图片吗？`)) return;

                fetch('/delete-multiple', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ filenames })
                })
                .then(r => r.json())
                .then(data => {
                    if (data.deleted_count > 0) {
                        loadImages(currentPage);
                    } else {
                        alert('删除失败');
                    }
                })
                .catch(e => alert('删除失败: ' + e));
            }

            function logout() {
                fetch('/gallery-logout', { method: 'POST' })
                .then(() => location.href = '/tuku')
                .catch(e => alert('登出失败: ' + e));
            }

            loadImages(1);
        </script>
    </body>
    </html>
    """
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/tuku", methods=["GET"])
def tuku():
    """图库登录页面"""
    if check_gallery_auth():
        return """
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>图库</title>
            <script>
                window.location.href = '/gallery-list';
            </script>
        </head>
        <body>
            <p>正在跳转到图库...</p>
        </body>
        </html>
        """, 200, {"Content-Type": "text/html; charset=utf-8"}

    return """
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>图库登录</title>
        <style>
            * { margin: 0; padding: 0; box-sizing: border-box; }
            body {
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                min-height: 100vh;
                display: flex;
                justify-content: center;
                align-items: center;
            }
            .login-container {
                background: white;
                padding: 40px;
                border-radius: 8px;
                box-shadow: 0 10px 25px rgba(0,0,0,0.2);
                width: 100%;
                max-width: 400px;
            }
            .login-container h1 {
                text-align: center;
                margin-bottom: 30px;
                color: #333;
                font-size: 24px;
            }
            .form-group {
                margin-bottom: 20px;
            }
            .form-group label {
                display: block;
                margin-bottom: 8px;
                color: #555;
                font-weight: 500;
            }
            .form-group input {
                width: 100%;
                padding: 12px;
                border: 1px solid #ddd;
                border-radius: 4px;
                font-size: 14px;
                transition: border-color 0.3s;
            }
            .form-group input:focus {
                outline: none;
                border-color: #667eea;
                box-shadow: 0 0 0 3px rgba(102, 126, 234, 0.1);
            }
            .login-btn {
                width: 100%;
                padding: 12px;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                color: white;
                border: none;
                border-radius: 4px;
                font-size: 16px;
                font-weight: 600;
                cursor: pointer;
                transition: transform 0.2s;
            }
            .login-btn:hover {
                transform: translateY(-2px);
            }
            .login-btn:active {
                transform: translateY(0);
            }
            .error-message {
                color: #ff6b6b;
                font-size: 14px;
                margin-top: 10px;
                display: none;
            }
            .loading {
                display: none;
                text-align: center;
                color: #667eea;
            }
        </style>
    </head>
    <body>
        <div class="login-container">
            <h1>图库登录</h1>
            <form onsubmit="handleLogin(event)">
                <div class="form-group">
                    <label for="password">密码</label>
                    <input type="password" id="password" placeholder="请输入密码" required>
                </div>
                <button type="submit" class="login-btn">登录</button>
                <div class="error-message" id="errorMessage"></div>
                <div class="loading" id="loading">登录中...</div>
            </form>
        </div>

        <script>
            async function handleLogin(event) {
                event.preventDefault();
                const password = document.getElementById('password').value;
                const errorMessage = document.getElementById('errorMessage');
                const loading = document.getElementById('loading');
                const btn = event.target.querySelector('button');

                errorMessage.style.display = 'none';
                loading.style.display = 'block';
                btn.disabled = true;

                try {
                    const response = await fetch('/gallery-auth', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ password })
                    });

                    const data = await response.json();

                    if (response.ok) {
                        window.location.href = '/gallery-list';
                    } else {
                        errorMessage.textContent = data.error || '登录失败';
                        errorMessage.style.display = 'block';
                    }
                } catch (error) {
                    errorMessage.textContent = '网络错误: ' + error.message;
                    errorMessage.style.display = 'block';
                } finally {
                    loading.style.display = 'none';
                    btn.disabled = false;
                }
            }

            document.getElementById('password').focus();
        </script>
    </body>
    </html>
    """, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/", methods=["GET"])
def index():
    """首页"""
    return jsonify({
        "name": "URLapi",
        "version": "2.0",
        "description": "图片处理和管理 API",
        "endpoints": {
            "upload": {
                "method": "POST",
                "path": "/upload",
                "description": "上传 Base64 编码的图片",
                "params": {"base64": "Base64 编码的图片数据"}
            },
            "upload_file": {
                "method": "POST",
                "path": "/upload-file",
                "description": "上传图片文件",
                "params": {"file": "图片文件"}
            },
            "upload_directory": {
                "method": "POST",
                "path": "/upload-directory",
                "description": "批量上传图片文件",
                "params": {"files": "图片文件列表"}
            },
            "file_to_base64": {
                "method": "POST",
                "path": "/file-to-base64",
                "description": "将上传的文件转换为 Base64",
                "params": {"file": "图片文件"}
            },
            "url_to_base64": {
                "method": "POST",
                "path": "/url-to-base64",
                "description": "从 URL 下载图片并转换为 Base64",
                "params": {"url": "图片 URL"}
            },
            "get_image": {
                "method": "GET",
                "path": "/get-image/<filename>",
                "description": "获取图片"
            },
            "list_images": {
                "method": "GET",
                "path": "/images",
                "description": "获取图片列表（需要认证）"
            },
            "gallery": {
                "method": "GET",
                "path": "/tuku",
                "description": "图库页面（需要认证）"
            },
            "gallery_auth": {
                "method": "POST",
                "path": "/gallery-auth",
                "description": "图库认证",
                "params": {"password": "图库密码"}
            },
            "gallery_logout": {
                "method": "POST",
                "path": "/gallery-logout",
                "description": "图库登出"
            },
            "delete": {
                "method": "POST",
                "path": "/delete",
                "description": "删除单个图片（需要认证）",
                "params": {"filename": "文件名"}
            },
            "delete_multiple": {
                "method": "POST",
                "path": "/delete-multiple",
                "description": "批量删除图片（需要认证）",
                "params": {"filenames": "文件名列表"}
            }
        }
    }), 200


@app.errorhandler(RequestEntityTooLarge)
def handle_request_entity_too_large(error):
    """处理请求体过大错误"""
    logger.warning("请求体过大: %s", str(error))
    return jsonify({"error": "请求体过大，不能超过 140MB"}), 413


@app.errorhandler(404)
def handle_not_found(error):
    """处理 404 错误"""
    return jsonify({"error": "端点不存在"}), 404


@app.errorhandler(500)
def handle_internal_error(error):
    """处理 500 错误"""
    logger.error("内部服务器错误", exc_info=True)
    return jsonify({"error": "内部服务器错误"}), 500


if __name__ == "__main__":
    logger.info("启动 URLapi 服务器")
    app.run(host="0.0.0.0", port=5000, debug=False)
