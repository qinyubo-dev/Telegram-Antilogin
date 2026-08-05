# Copyright (c) 2026 秦屿泊 (@qinyubo)
# SPDX-License-Identifier: MIT

import os
import tempfile
import zipfile
from dataclasses import dataclass
from typing import List, Optional

from localization import t


SESSION_UPLOAD_MAX_BYTES = 40 * 1024
ZIP_UPLOAD_MAX_BYTES = 200 * 1024
ZIP_MAX_SESSION_FILES = 25


class ZipSessionUploadError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class SessionImportResult:
    success: bool
    label: str
    phone: Optional[str] = None
    reason: str = ""


def find_zip_session_entries(zip_path: str) -> List[zipfile.ZipInfo]:
    """Return session entries in archive order without extracting archive paths."""
    try:
        with zipfile.ZipFile(zip_path, "r") as archive:
            entries = [
                info
                for info in archive.infolist()
                if not info.is_dir() and info.filename.lower().endswith(".session")
            ]
    except (zipfile.BadZipFile, OSError) as error:
        raise ZipSessionUploadError("invalid_zip", "ZIP 压缩包已损坏或格式无效") from error

    if not entries:
        raise ZipSessionUploadError("no_session", "ZIP 中未找到 .session 文件")
    if len(entries) > ZIP_MAX_SESSION_FILES:
        raise ZipSessionUploadError(
            "too_many_sessions",
            f"ZIP 中最多允许 {ZIP_MAX_SESSION_FILES} 个 Session 文件",
        )
    return entries


def extract_zip_session_entry(zip_path: str, entry: zipfile.ZipInfo) -> str:
    """Stream one archive entry to a random temp file with a hard byte limit."""
    if entry.flag_bits & 0x1:
        raise ZipSessionUploadError("encrypted", "加密的 Session 文件不受支持")
    if entry.file_size > SESSION_UPLOAD_MAX_BYTES:
        raise ZipSessionUploadError("session_too_large", "Session 文件解压后超过 40 KB")

    fd, temp_path = tempfile.mkstemp(suffix=".session")
    os.close(fd)
    written = 0
    completed = False
    try:
        with zipfile.ZipFile(zip_path, "r") as archive:
            with archive.open(entry, "r") as source, open(temp_path, "wb") as target:
                while True:
                    chunk = source.read(8192)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > SESSION_UPLOAD_MAX_BYTES:
                        raise ZipSessionUploadError(
                            "session_too_large", "Session 文件解压后超过 40 KB"
                        )
                    target.write(chunk)
        completed = True
        return temp_path
    except ZipSessionUploadError:
        raise
    except (zipfile.BadZipFile, RuntimeError, OSError) as error:
        raise ZipSessionUploadError("extract_failed", "Session 文件解压失败") from error
    finally:
        if os.path.exists(temp_path) and not completed:
            os.unlink(temp_path)


def safe_archive_label(
    name: str, max_length: int = 80, language: str = "zh"
) -> str:
    label = (name or t(language, "account.upload.unnamed")).replace(
        "\r", " "
    ).replace("\n", " ")
    return label if len(label) <= max_length else label[: max_length - 1] + "…"


def upload_size_limit(file_name: str) -> Optional[int]:
    lower_name = (file_name or "").lower()
    if lower_name.endswith(".session"):
        return SESSION_UPLOAD_MAX_BYTES
    if lower_name.endswith(".zip"):
        return ZIP_UPLOAD_MAX_BYTES
    return None


def is_upload_size_allowed(file_name: str, size: int) -> bool:
    limit = upload_size_limit(file_name)
    return limit is not None and 0 <= int(size) <= limit


def render_zip_upload_error(error: ZipSessionUploadError, language: str = "zh") -> str:
    keys = {
        "invalid_zip": "account.upload.zip_invalid",
        "no_session": "account.upload.zip_empty",
        "too_many_sessions": "account.upload.zip_too_many",
        "encrypted": "account.upload.zip_encrypted",
        "session_too_large": "account.upload.zip_session_too_large",
        "extract_failed": "account.upload.zip_extract_failed",
    }
    key = keys.get(error.code)
    if not key:
        return str(error)
    values = {"count": ZIP_MAX_SESSION_FILES} if error.code == "too_many_sessions" else {}
    return t(language, key, **values)


def render_zip_import_summary(
    results: List[SessionImportResult], language: str = "zh"
) -> str:
    success_results = [result for result in results if result.success]
    quota_results = [result for result in results if result.reason == 'quota_full']
    failure_results = [result for result in results if not result.success and result.reason != 'quota_full']
    lines = [t(language, "account.upload.zip_done")]

    if success_results:
        lines.extend([
            "",
            t(language, "account.upload.zip_success", count=len(success_results)),
        ])
        lines.extend(
            f"• {result.phone or t(language, 'account.upload.unknown_phone')}"
            for result in success_results
        )

    if failure_results:
        lines.extend([
            "",
            t(language, "account.upload.zip_failed", count=len(failure_results)),
        ])
        lines.extend(f"• {result.label}" for result in failure_results)

    if quota_results:
        lines.extend([
            "",
            t(language, "account.upload.zip_quota", count=len(quota_results)),
        ])
        lines.extend(f"• {result.label}" for result in quota_results)

    return "\n".join(lines)
