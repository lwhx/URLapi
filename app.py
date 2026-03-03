from flask import Flask, request, jsonify
import base64
import urllib.request
import urllib.error
import ssl
import os
import mimetypes

app = Flask(__name__)


def get_mime_type(url_or_path: str) -> str:
    """
    根据文件路径或URL获取MIME类型
    """
    ext = os.path.splitext(url_or_path)[1].lower()
    mime_type = mimetypes.guess_type(f"file{ext}")[0]
    if mime_type:
        return mime_type
    return "image/jpeg"


def download_image(url: str) -> bytes:
    """
    从URL下载图片
    """
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    
    with urllib.request.urlopen(url, context=ctx) as response:
        return response.read()


@app.route("/url-to-base64", methods=["POST"])
def url_to_base64():
    """
    将网络图片URL转换为Base64编码
    
    请求体: {"url": "图片URL"}
    返回: {"base64": "Base64编码", "mime_type": "image/jpeg"}
    """
    data = request.get_json()
    if not data or "url" not in data:
        return jsonify({"error": "缺少url参数"}), 400
    
    image_url = data["url"]
    
    try:
        image_data = download_image(image_url)
        b64_data = base64.b64encode(image_data).decode("utf-8")
        mime_type = get_mime_type(image_url)
        
        return jsonify({
            "base64": b64_data,
            "mime_type": mime_type
        })
    except urllib.error.URLError as e:
        return jsonify({"error": f"下载图片失败: {str(e.reason)}"}), 400
    except Exception as e:
        return jsonify({"error": f"转换失败: {str(e)}"}), 500


@app.route("/file-to-base64", methods=["POST"])
def file_to_base64():
    """
    直接上传图片文件转换为Base64编码（multipart/form-data）
    
    请求: form-data, key="file", value=图片文件
    返回: {"base64": "Base64编码", "mime_type": "image/jpeg"}
    """
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


@app.route("/file-to-base64/json", methods=["POST"])
def file_to_base64_json():
    """
    将本地图片文件转换为Base64编码（JSON方式，需要先上传到uploads目录）
    
    请求体: {"path": "文件名"}
    返回: {"base64": "Base64编码", "mime_type": "image/jpeg"}
    """
    data = request.get_json()
    if not data or "path" not in data:
        return jsonify({"error": "缺少path参数"}), 400
    
    file_path = data["path"]
    
    if ".." in file_path or file_path.startswith("/"):
        return jsonify({"error": "无效的文件路径"}), 400
    
    full_path = os.path.join("/app/uploads", file_path)
    
    if not os.path.exists(full_path):
        return jsonify({"error": "文件不存在"}), 404
    
    try:
        with open(full_path, "rb") as f:
            image_data = f.read()
        
        b64_data = base64.b64encode(image_data).decode("utf-8")
        mime_type = get_mime_type(full_path)
        
        return jsonify({
            "base64": b64_data,
            "mime_type": mime_type
        })
    except Exception as e:
        return jsonify({"error": f"转换失败: {str(e)}"}), 500


@app.route("/health", methods=["GET"])
def health():
    """健康检查"""
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    os.makedirs("/app/uploads", exist_ok=True)
    app.run(host="0.0.0.0", port=5000, debug=False)
