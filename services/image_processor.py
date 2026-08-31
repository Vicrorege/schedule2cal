import io
import logging
from pathlib import Path

from PIL import Image
from pdf2image import convert_from_bytes

logger = logging.getLogger(__name__)

MAX_IMAGE_HEIGHT = 16000
MAX_IMAGE_WIDTH = 4096
JPEG_QUALITY = 85


def pdf_to_image(pdf_bytes: bytes) -> bytes:
    """Конвертирует все страницы PDF в одно вертикально склеенное JPEG-изображение."""
    pages = convert_from_bytes(pdf_bytes, dpi=200)
    if not pages:
        raise ValueError("PDF не содержит страниц")

    total_height = sum(page.height for page in pages)
    max_width = max(page.width for page in pages)

    if total_height > MAX_IMAGE_HEIGHT:
        scale = MAX_IMAGE_HEIGHT / total_height
        pages = [
            page.resize((int(page.width * scale), int(page.height * scale)), Image.LANCZOS)
            for page in pages
        ]
        total_height = sum(page.height for page in pages)
        max_width = max(page.width for page in pages)

    combined = Image.new("RGB", (max_width, total_height), "white")
    y_offset = 0
    for page in pages:
        combined.paste(page, (0, y_offset))
        y_offset += page.height

    return _image_to_jpeg_bytes(combined)


def prepare_image(image_bytes: bytes, mime_type: str | None = None) -> bytes:
    """Подготавливает изображение: нормализует формат и уменьшает при необходимости."""
    img = Image.open(io.BytesIO(image_bytes))

    if img.mode != "RGB":
        img = img.convert("RGB")

    if img.width > MAX_IMAGE_WIDTH or img.height > MAX_IMAGE_HEIGHT:
        img.thumbnail((MAX_IMAGE_WIDTH, MAX_IMAGE_HEIGHT), Image.LANCZOS)

    return _image_to_jpeg_bytes(img)


def process_upload(file_bytes: bytes, filename: str) -> bytes:
    """Обрабатывает загруженный файл (PDF или изображение) и возвращает JPEG bytes."""
    ext = Path(filename).suffix.lower()

    if ext == ".pdf":
        return pdf_to_image(file_bytes)

    if ext in (".png", ".jpg", ".jpeg", ".webp", ".bmp"):
        return prepare_image(file_bytes)

    raise ValueError(f"Неподдерживаемый формат файла: {ext}")


def _image_to_jpeg_bytes(img: Image.Image) -> bytes:
    buffer = io.BytesIO()
    img.save(buffer, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    return buffer.getvalue()
