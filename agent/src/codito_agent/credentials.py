from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import os
import secrets
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from .errors import AgentError


class _DataBlob(ctypes.Structure):
    _fields_ = [("size", ctypes.c_uint32), ("data", ctypes.POINTER(ctypes.c_ubyte))]


class DataProtector(Protocol):
    def protect(self, value: bytes) -> bytes: ...

    def unprotect(self, value: bytes) -> bytes: ...


class DpapiProtector:
    """Current-user DPAPI with UI disabled and application entropy."""

    _entropy = b"Codito device-key fallback v1"

    @staticmethod
    def _blob(value: bytes) -> tuple[_DataBlob, ctypes.Array[ctypes.c_ubyte]]:
        buffer = (ctypes.c_ubyte * len(value)).from_buffer_copy(value)
        return _DataBlob(len(value), buffer), buffer

    def protect(self, value: bytes) -> bytes:
        if os.name != "nt":
            raise AgentError("credential_unavailable", "DPAPI is available only on Windows")
        value_blob, value_buffer = self._blob(value)
        entropy_blob, entropy_buffer = self._blob(self._entropy)
        output = _DataBlob()
        crypt32 = ctypes.windll.crypt32
        crypt32.CryptProtectData.argtypes = [
            ctypes.POINTER(_DataBlob),
            ctypes.c_wchar_p,
            ctypes.POINTER(_DataBlob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(_DataBlob),
        ]
        crypt32.CryptProtectData.restype = ctypes.c_int
        if not crypt32.CryptProtectData(
            ctypes.byref(value_blob),
            "Codito device key",
            ctypes.byref(entropy_blob),
            None,
            None,
            0x1,
            ctypes.byref(output),
        ):
            raise AgentError("credential_unavailable", "DPAPI could not protect the device key")
        try:
            return ctypes.string_at(output.data, output.size)
        finally:
            ctypes.windll.kernel32.LocalFree(output.data)
            del value_buffer, entropy_buffer

    def unprotect(self, value: bytes) -> bytes:
        if os.name != "nt":
            raise AgentError("credential_unavailable", "DPAPI is available only on Windows")
        value_blob, value_buffer = self._blob(value)
        entropy_blob, entropy_buffer = self._blob(self._entropy)
        output = _DataBlob()
        crypt32 = ctypes.windll.crypt32
        if not crypt32.CryptUnprotectData(
            ctypes.byref(value_blob),
            None,
            ctypes.byref(entropy_blob),
            None,
            None,
            0x1,
            ctypes.byref(output),
        ):
            raise AgentError("credential_unavailable", "DPAPI could not unlock the device key")
        try:
            return ctypes.string_at(output.data, output.size)
        finally:
            ctypes.windll.kernel32.LocalFree(output.data)
            del value_buffer, entropy_buffer


class NativeDeviceKey(Protocol):
    """Interface implemented by the future broker CNG/TPM verb."""

    def create(self, key_name: str) -> tuple[str, bytes]: ...

    def sign(self, key_name: str, data: bytes) -> bytes: ...


@dataclass(frozen=True, slots=True)
class DeviceIdentity:
    device_id: str
    key_id: str
    algorithm: str
    public_key_pem: str
    public_key_jwk: dict[str, str]
    key_thumbprint: str
    hardware_backed: bool

    def public_dict(self) -> dict[str, object]:
        return {
            "device_id": self.device_id,
            "key_id": self.key_id,
            "algorithm": self.algorithm,
            "public_key_pem": self.public_key_pem,
            "public_key_jwk": self.public_key_jwk,
            "key_thumbprint": self.key_thumbprint,
            "hardware_backed": self.hardware_backed,
        }


class DeviceCredentialStore:
    def __init__(
        self,
        directory: Path,
        *,
        protector: DataProtector | None = None,
        native_key: NativeDeviceKey | None = None,
    ) -> None:
        self.directory = directory
        self.protector = protector or DpapiProtector()
        self.native_key = native_key
        self.metadata_path = directory / "device.json"
        self.fallback_path = directory / "device-key.dpapi"

    def enroll(self) -> DeviceIdentity:
        if self.metadata_path.exists():
            raise AgentError("already_enrolled", "This agent already has a device identity")
        self.directory.mkdir(parents=True, exist_ok=True)
        device_id = f"local_{secrets.token_urlsafe(24)}"
        key_id = f"codito-{uuid.uuid4()}"
        identity: DeviceIdentity
        if self.native_key is not None:
            try:
                provider, public_key = self.native_key.create(key_id)
                public_pem = public_key.decode("ascii")
                parsed = serialization.load_pem_public_key(public_key)
                if not isinstance(parsed, ec.EllipticCurvePublicKey):
                    raise TypeError("CNG key is not an EC public key")
                jwk, thumbprint = self._jwk(parsed)
                identity = DeviceIdentity(
                    device_id,
                    key_id,
                    "ES256",
                    public_pem,
                    jwk,
                    thumbprint,
                    provider.lower().startswith("tpm"),
                )
                self._write_metadata(identity, backend="cng")
                return identity
            except Exception as native_error:
                # A CNG/TPM failure is allowed to select the explicitly documented
                # DPAPI software-key fallback, never plaintext storage.
                _fallback_reason = type(native_error).__name__
                del _fallback_reason

        private_key = ec.generate_private_key(ec.SECP256R1())
        private_raw = private_key.private_bytes(
            serialization.Encoding.DER,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        protected = self.protector.protect(private_raw)
        self._atomic_write(self.fallback_path, protected)
        fallback_public_pem = private_key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        jwk, thumbprint = self._jwk(private_key.public_key())
        identity = DeviceIdentity(
            device_id,
            key_id,
            "ES256",
            fallback_public_pem.decode(),
            jwk,
            thumbprint,
            False,
        )
        self._write_metadata(identity, backend="dpapi")
        return identity

    def load(self) -> DeviceIdentity:
        try:
            value = json.loads(self.metadata_path.read_text(encoding="utf-8"))
            return DeviceIdentity(
                value["device_id"],
                value["key_id"],
                value["algorithm"],
                value["public_key_pem"],
                dict(value["public_key_jwk"]),
                value["key_thumbprint"],
                bool(value["hardware_backed"]),
            )
        except (OSError, KeyError, json.JSONDecodeError, TypeError) as exc:
            raise AgentError("not_enrolled", "Device credential metadata is unavailable") from exc

    def sign(self, data: bytes) -> bytes:
        metadata = json.loads(self.metadata_path.read_text(encoding="utf-8"))
        if metadata.get("backend") == "cng":
            if self.native_key is None:
                raise AgentError("credential_unavailable", "CNG signing broker is unavailable")
            return self.native_key.sign(str(metadata["key_id"]), data)
        try:
            protected = self.fallback_path.read_bytes()
            private_raw = self.protector.unprotect(protected)
            private_key = serialization.load_der_private_key(private_raw, password=None)
            if not isinstance(private_key, ec.EllipticCurvePrivateKey):
                raise TypeError("unexpected key type")
            return private_key.sign(data, ec.ECDSA(hashes.SHA256()))
        except (OSError, TypeError, ValueError) as exc:
            raise AgentError("credential_unavailable", "Device signing key is unavailable") from exc

    def proof(self, ticket: str, nonce: str) -> dict[str, str]:
        identity = self.load()
        payload = f"codito-ws-v1\n{identity.device_id}\n{ticket}\n{nonce}".encode()
        signature = base64.urlsafe_b64encode(self.sign(payload)).decode().rstrip("=")
        return {
            "device_id": identity.device_id,
            "key_id": identity.key_id,
            "nonce": nonce,
            "signature": signature,
            "algorithm": identity.algorithm,
        }

    def websocket_signature(self, ticket: str, challenge: str) -> str:
        payload = b"codito-ws-proof-v1\x00" + ticket.encode() + b"\x00" + challenge.encode()
        return base64.urlsafe_b64encode(self.sign(payload)).decode().rstrip("=")

    def revoke_local(self) -> None:
        self.metadata_path.unlink(missing_ok=True)
        self.fallback_path.unlink(missing_ok=True)

    def bind_remote_device(self, device_id: str) -> DeviceIdentity:
        """Replace the provisional local id with the relay-issued opaque device id."""

        identity = self.load()
        bound = DeviceIdentity(
            device_id,
            identity.key_id,
            identity.algorithm,
            identity.public_key_pem,
            identity.public_key_jwk,
            identity.key_thumbprint,
            identity.hardware_backed,
        )
        metadata = json.loads(self.metadata_path.read_text(encoding="utf-8"))
        self._write_metadata(bound, backend=str(metadata["backend"]))
        return bound

    def _write_metadata(self, identity: DeviceIdentity, *, backend: str) -> None:
        value = {**identity.public_dict(), "backend": backend, "version": 1}
        self._atomic_write(
            self.metadata_path,
            json.dumps(value, separators=(",", ":"), sort_keys=True).encode(),
        )

    @staticmethod
    def _jwk(public_key: ec.EllipticCurvePublicKey) -> tuple[dict[str, str], str]:
        numbers = public_key.public_numbers()

        def encode(value: int) -> str:
            return base64.urlsafe_b64encode(value.to_bytes(32, "big")).decode().rstrip("=")

        jwk = {"kty": "EC", "crv": "P-256", "x": encode(numbers.x), "y": encode(numbers.y)}
        thumbprint_input = json.dumps(jwk, separators=(",", ":"), sort_keys=True).encode()
        thumbprint = hashlib.sha256(thumbprint_input).hexdigest()
        return jwk, thumbprint

    @staticmethod
    def _atomic_write(path: Path, value: bytes) -> None:
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as stream:
                stream.write(value)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
