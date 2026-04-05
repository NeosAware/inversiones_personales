import os
from io import BytesIO

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.files.base import ContentFile, File
from django.core.files.storage import FileSystemStorage
from django.urls import reverse


ENCRYPTION_MARKER = b"IPH-ENC-1\x00"


class EncryptedFileSystemStorage(FileSystemStorage):
    @property
    def encryption_key(self) -> str | None:
        key = (getattr(settings, "APP_MEDIA_ENCRYPTION_KEY", "") or "").strip()
        return key or None

    @property
    def is_encryption_enabled(self) -> bool:
        return bool(self.encryption_key)

    def _get_fernet(self) -> Fernet | None:
        if not self.encryption_key:
            return None
        try:
            return Fernet(self.encryption_key.encode("ascii"))
        except Exception as exc:
            raise ImproperlyConfigured(
                "APP_MEDIA_ENCRYPTION_KEY debe ser una clave Fernet valida."
            ) from exc

    def _read_all_bytes(self, content) -> bytes:
        if hasattr(content, "seek"):
            content.seek(0)

        if hasattr(content, "chunks"):
            payload = b"".join(chunk for chunk in content.chunks())
        else:
            payload = content.read()

        if isinstance(payload, str):
            payload = payload.encode("utf-8")

        if hasattr(content, "seek"):
            content.seek(0)

        return payload

    def _encrypt_bytes(self, payload: bytes) -> bytes:
        if not self.is_encryption_enabled:
            return payload
        return ENCRYPTION_MARKER + self._get_fernet().encrypt(payload)

    def _decrypt_bytes(self, payload: bytes) -> bytes:
        if not payload.startswith(ENCRYPTION_MARKER):
            return payload

        fernet = self._get_fernet()
        if fernet is None:
            raise ImproperlyConfigured(
                "APP_MEDIA_ENCRYPTION_KEY es obligatoria para abrir documentos cifrados."
            )

        try:
            return fernet.decrypt(payload[len(ENCRYPTION_MARKER) :])
        except InvalidToken as exc:
            raise ImproperlyConfigured(
                "APP_MEDIA_ENCRYPTION_KEY no puede descifrar los documentos existentes."
            ) from exc

    def _open(self, name, mode="rb"):
        if not any(flag in mode for flag in ("r", "+", "a")):
            return super()._open(name, mode)

        encrypted_file = super()._open(name, "rb")
        try:
            payload = encrypted_file.read()
        finally:
            encrypted_file.close()

        decrypted = self._decrypt_bytes(payload)
        in_memory = File(BytesIO(decrypted), name=name)
        in_memory.size = len(decrypted)
        return in_memory

    def _save(self, name, content):
        if not self.is_encryption_enabled:
            return super()._save(name, content)

        encrypted_content = ContentFile(self._encrypt_bytes(self._read_all_bytes(content)))
        encrypted_content.name = getattr(content, "name", os.path.basename(name))
        return super()._save(name, encrypted_content)

    def size(self, name):
        if not self.is_encryption_enabled:
            return super().size(name)

        encrypted_file = super()._open(name, "rb")
        try:
            payload = encrypted_file.read()
        finally:
            encrypted_file.close()
        return len(self._decrypt_bytes(payload))

    def url(self, name):
        if self.is_encryption_enabled:
            return reverse("secure_media_download", kwargs={"path": name})
        return super().url(name)
