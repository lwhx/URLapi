FROM python:3.11-alpine

# 安装构建依赖
RUN apk add --no-cache --virtual .build-deps \
    gcc \
    musl-dev

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --break-system-packages -r requirements.txt

COPY app.py .

RUN mkdir -p /app/uploads

EXPOSE 5000

CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "--timeout", "120", "app:app"]
