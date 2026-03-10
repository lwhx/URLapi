FROM python:3.11-alpine

RUN apk add --no-cache \
    libjpeg-turbo \
    libwebp \
    openjpeg \
    tiff \
    zlib \
 && apk add --no-cache --virtual .build-deps \
    gcc \
    musl-dev \
    libjpeg-turbo-dev \
    libwebp-dev \
    openjpeg-dev \
    tiff-dev \
    zlib-dev

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --break-system-packages -r requirements.txt \
 && apk del .build-deps

COPY app.py .

RUN mkdir -p /app/uploads

EXPOSE 5000

CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "--timeout", "120", "app:app"]
