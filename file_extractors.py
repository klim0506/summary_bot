import io
import logging
import zipfile
from typing import List, Tuple, Optional

logger = logging.getLogger("file_extractors")


class ZipPackSession:
    """
    Простая сессия для сборки ZIP.
    Лимиты по количеству файлов и общему размеру.
    """

    def __init__(self, max_files: int = 50, max_total_size: int = 50 * 1024 * 1024):
        self.max_files = max_files
        self.max_total_size = max_total_size
        self.files: List[Tuple[str, bytes]] = []
        self.total_size = 0

    @property
    def count(self) -> int:
        return len(self.files)

    def add_file(self, name: str, content: bytes) -> bool:
        if self.count + 1 > self.max_files:
            return False
        if self.total_size + len(content) > self.max_total_size:
            return False
        self.files.append((name, content))
        self.total_size += len(content)
        return True

    def build_zip(self) -> Tuple[Optional[bytes], dict]:
        if not self.files:
            return None, {"added": 0, "skipped": 0, "total": 0}
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for name, data in self.files:
                zf.writestr(name, data)
        return buf.getvalue(), {
            "added": len(self.files),
            "skipped": 0,
            "total": len(self.files),
        }


def extract_files_from_zip(
    file_content: bytes,
    max_files: int = 50,
    max_uncompressed: int = 50 * 1024 * 1024,
) -> Optional[List[Tuple[str, bytes]]]:
    """
    Извлекает файлы из ZIP с лимитами.
    Возвращает список (name, bytes).
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(file_content))
    except Exception as e:
        logger.warning("Ошибка открытия ZIP: %s", e)
        return None

    names = zf.namelist()[:max_files]
    extracted = []
    total_size = 0

    for name in names:
        try:
            with zf.open(name) as f:
                data = f.read()
                total_size += len(data)
                if total_size > max_uncompressed:
                    logger.warning("Превышен лимит распаковки ZIP")
                    break
                extracted.append((name, data))
        except Exception as e:
            logger.warning("Ошибка чтения %s из ZIP: %s", name, e)
            continue

    return extracted if extracted else None
