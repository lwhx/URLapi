from flask import Flask, request, jsonify, send_from_directory, url_for
import base64
import urllib.request
import urllib.error
import ssl
import os
import mimetypes
import uuid

app = Flask(__name__)

UPLOAD_FOLDER = "/app/uploads"
ALLOWED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


def get_mime_type(url_or_path: str) -> str:
    """根据文件路径或URL获取MIME类型"""
    ext = os.path.splitext(url_or_path)[1].lower()
    mime_type = mimetypes.guess_type(f"file{ext}")[0]
    if mime_type:
        return mime_type
    return "image/jpeg"


def download_image(url: str) -> bytes:
    """从URL下载图片"""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(url, context=ctx) as response:
        return response.read()


@app.route("/url-to-base64", methods=["POST"])
def url_to_base64():
    """网络图片URL转Base64"""
    data = request.get_json()
    if not data or "url" not in data:
        return jsonify({"error": "缺少url参数"}), 400
    
    try:
        image_data = download_image(data["url"])
        b64_data = base64.b64encode(image_data).decode("utf-8")
        return jsonify({
            "base64": b64_data,
            "mime_type": get_mime_type(data["url"])
        })
    except urllib.error.URLError as e:
        return jsonify({"error": f"下载图片失败: {str(e.reason)}"}), 400
    except Exception as e:
        return jsonify({"error": f"转换失败: {str(e)}"}), 500


@app.route("/file-to-base64", methods=["POST"])
def file_to_base64():
    """上传文件转Base64（multipart/form-data）"""
    if "file" not in request.files:
        return jsonify({"error": "缺少file参数"}), 400
    
    file = request.files["file"]
    if file.filename == "":
        return jsonify({"error": "文件名为空"}), 400
    
    try:
        image_data = file.read()
        b64_data = base64.b64encode(image_data).decode("utf-8")
        mime_type = file.content_type or get_mime_type(file.filename)
        return jsonify({
            "base64": b64_data,
            "mime_type": mime_type
        })
    except Exception as e:
        return jsonify({"error": f"转换失败: {str(e)}"}), 500


@app.route("/upload", methods=["POST"])
def upload():
    """上传Base64图片数据，返回访问URL"""
    data = request.get_json()
    if not data or "base64" not in data:
        return jsonify({"error": "缺少base64参数"}), 400
    
    base64_data = data["base64"]
    mime_type = data.get("mime_type", "image/jpeg")
    
    # 从MIME类型获取扩展名
    ext_map = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/gif": ".gif",
        "image/webp": ".webp"
    }
    ext = ext_map.get(mime_type, ".jpg")
    
    try:
        # 解码Base64
        image_data = base64.b64decode(base64_data)
        
        # 生成唯一文件名
        filename = f"{uuid.uuid4().hex}{ext}"
        filepath = os.path.join(UPLOAD_FOLDER, filename)
        
        # 保存文件
        with open(filepath, "wb") as f:
            f.write(image_data)
        
        # 返回访问URL
        image_url = url_for("get_image", filename=filename, _external=True)
        
        return jsonify({
            "url": image_url,
            "filename": filename,
            "size": len(image_data)
        })
    except Exception as e:
        return jsonify({"error": f"上传失败: {str(e)}"}), 500


@app.route("/uploads/<filename>", methods=["GET"])
def get_image(filename):
    """访问上传的图片"""
    return send_from_directory(UPLOAD_FOLDER, filename)


@app.route("/health", methods=["GET"])
def health():
    """健康检查"""
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    app.run(host="0.0.0.0", port=5000, debug=False)
