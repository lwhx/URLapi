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
    
    参数:
        url_or_path: 文件路径或URL
        
    返回:
        MIME类型字符串
    """
    # 尝试从URL获取
    ext = os.path.splitext(url_or_path)[1].lower()
    mime_type = mimetypes.guess_type(f"file{ext}")[0]
    if mime_type:
        return mime_type
    
    # 默认返回jpeg
    return "image/jpeg"


def download_image(url: str) -> bytes:
    """
    从URL下载图片
    
    参数:
        url: 图片URL
        
    返回:
        图片二进制数据
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
    
    请求体:
        {"url": "图片URL"}
        
    返回:
        {"base64": "Base64编码字符串", "mime_type": "image/jpeg"}
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
    将本地图片文件转换为Base64编码
    
    请求体:
        {"path": "文件路径"}
        
    返回:
        {"base64": "Base64编码字符串", "mime_type": "image/jpeg"}
    """
    data = request.get_json()
    if not data or "path" not in data:
        return jsonify({"error": "缺少path参数"}), 400
    
    file_path = data["path"]
    
    # 安全检查：防止路径遍历攻击
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
    # 确保上传目录存在
    os.makedirs("/app/uploads", exist_ok=True)
    
    app.run(host="0.0.0.0", port=5000, debug=False)
