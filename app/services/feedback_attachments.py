from __future__ import annotations

import os
import re
import uuid
import inspect
from pathlib import Path

from app.models import FeedbackAnexo, FeedbackOperacional
from app.utils import utc_now


MAX_FEEDBACK_FILES = int(os.getenv("FEEDBACK_MAX_FILES", "3"))
MAX_FEEDBACK_FILE_BYTES = int(os.getenv("FEEDBACK_MAX_FILE_MB", "5")) * 1024 * 1024
ALLOWED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".pdf"}
ALLOWED_CONTENT_TYPES = {
    "image/png",
    "image/jpeg",
    "image/webp",
    "application/pdf",
}


def feedback_upload_root() -> Path:
    root = Path(os.getenv("FEEDBACK_UPLOAD_DIR", "uploads/feedback"))
    if not root.is_absolute():
        root = Path.cwd() / root
    root.mkdir(parents=True, exist_ok=True)
    return root


def _safe_filename(filename: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", (filename or "anexo").strip())
    cleaned = cleaned.strip("._") or "anexo"
    return cleaned[:180]


def _extension(filename: str) -> str:
    return Path(filename or "").suffix.lower()


def _validar_upload(upload, filename: str) -> None:
    extension = _extension(filename)
    content_type = (getattr(upload, "content_type", "") or "").lower()
    if extension not in ALLOWED_EXTENSIONS:
        raise ValueError("Anexo invalido. Envie apenas PNG, JPG, WEBP ou PDF.")
    if content_type and content_type not in ALLOWED_CONTENT_TYPES and content_type != "application/octet-stream":
        raise ValueError("Tipo de anexo nao permitido.")


def _safe_storage_path(relative_path: str) -> Path:
    root = feedback_upload_root().resolve()
    full_path = (root / relative_path).resolve()
    if root != full_path and root not in full_path.parents:
        raise ValueError("Caminho de anexo invalido.")
    return full_path


async def salvar_anexos_feedback(feedback: FeedbackOperacional, uploads) -> list[FeedbackAnexo]:
    valid_uploads = [
        upload for upload in list(uploads or [])
        if getattr(upload, "filename", None)
    ]
    if not valid_uploads:
        return []
    if len(valid_uploads) > MAX_FEEDBACK_FILES:
        raise ValueError(f"Envie no maximo {MAX_FEEDBACK_FILES} anexo(s) por feedback.")

    root = feedback_upload_root()
    feedback_dir = root / str(feedback.id)
    feedback_dir.mkdir(parents=True, exist_ok=True)
    saved_paths: list[Path] = []
    anexos: list[FeedbackAnexo] = []

    try:
        try:
            for upload in valid_uploads:
                original_name = _safe_filename(upload.filename)
                _validar_upload(upload, original_name)
                extension = _extension(original_name)
                content = await upload.read(MAX_FEEDBACK_FILE_BYTES + 1)
                if len(content) > MAX_FEEDBACK_FILE_BYTES:
                    raise ValueError("Anexo muito grande. Limite de 5 MB por arquivo.")
                if not content:
                    continue

                stored_name = f"{uuid.uuid4().hex}{extension}"
                relative_path = f"{feedback.id}/{stored_name}"
                final_path = _safe_storage_path(relative_path)
                temp_path = final_path.with_suffix(final_path.suffix + ".tmp")
                temp_path.write_bytes(content)
                os.replace(temp_path, final_path)
                saved_paths.append(final_path)
                anexos.append(
                    FeedbackAnexo(
                        feedback_id=feedback.id,
                        nome_original=original_name,
                        nome_armazenado=stored_name,
                        caminho_relativo=relative_path,
                        content_type=(getattr(upload, "content_type", "") or "application/octet-stream")[:120],
                        tamanho_bytes=len(content),
                        criado_em=utc_now(),
                    )
                )
        except Exception:
            for path in saved_paths:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise
    finally:
        for upload in valid_uploads:
            close = getattr(upload, "close", None)
            if close:
                result = close()
                if inspect.isawaitable(result):
                    await result

    return anexos


def caminho_anexo(anexo: FeedbackAnexo) -> Path:
    return _safe_storage_path(anexo.caminho_relativo)


def excluir_arquivo_anexo(anexo: FeedbackAnexo) -> None:
    try:
        caminho_anexo(anexo).unlink(missing_ok=True)
    except OSError:
        pass


def excluir_arquivos_feedback(feedback: FeedbackOperacional) -> None:
    for anexo in list(feedback.anexos or []):
        excluir_arquivo_anexo(anexo)
    try:
        feedback_dir = feedback_upload_root() / str(feedback.id)
        feedback_dir.rmdir()
    except OSError:
        pass
