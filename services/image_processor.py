import io
import logging
from pathlib import Path

from PIL import Image
from pdf2image import convert_from_bytes

logger = logging.getLogger(__name__)

MAX_IMAGE_HEIGHT = 12000
MAX_IMAGE_WIDTH = 2048
JPEG_QUALITY = 85
# Лимит для base64 в JSON (nginx часто режет на ~1 MB body)
MAX_LLM_IMAGE_BYTES = 700_000
PDF_DPI = 150


def pdf_to_image(pdf_bytes: bytes) -> bytes:
    """Конвертирует все страницы PDF в одно вертикально склеенное JPEG-изображение."""
    pages = convert_from_bytes(pdf_bytes, dpi=PDF_DPI)
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


def fit_for_llm(image_bytes: bytes, max_bytes: int = MAX_LLM_IMAGE_BYTES) -> bytes:
    """Сжимает JPEG, чтобы base64-пayload влез в лимит прокси."""
    if len(image_bytes) <= max_bytes:
        return image_bytes

    img = Image.open(io.BytesIO(image_bytes))
    if img.mode != "RGB":
        img = img.convert("RGB")

    original = len(image_bytes)
    scale = 1.0
    quality = JPEG_QUALITY
    best = image_bytes

    for _ in range(24):
        w, h = img.size
        resized = img
        if scale < 1.0:
            resized = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
        candidate = _image_to_jpeg_bytes(resized, quality=quality)
        best = candidate
        if len(candidate) <= max_bytes:
            logger.info(
                "Изображение сжато: %d → %d байт (scale=%.2f, q=%d)",
                original,
                len(candidate),
                scale,
                quality,
            )
            return candidate
        if quality > 45:
            quality -= 10
        else:
            scale *= 0.85
            quality = JPEG_QUALITY

    logger.warning(
        "Изображение сжато до минимума: %d → %d байт (цель %d)",
        original,
        len(best),
        max_bytes,
    )
    return best


def _image_to_jpeg_bytes(img: Image.Image, *, quality: int = JPEG_QUALITY) -> bytes:
    buffer = io.BytesIO()
    img.save(buffer, format="JPEG", quality=quality, optimize=True)
    return buffer.getvalue()
