from __future__ import annotations

import json
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

from codito_agent.credentials import DeviceCredentialStore


class ReversibleProtector:
    def protect(self, value: bytes) -> bytes:
        return b"test:" + value[::-1]

    def unprotect(self, value: bytes) -> bytes:
        assert value.startswith(b"test:")
        return value[5:][::-1]


def test_software_device_key_is_protected_and_signs(tmp_path: Path) -> None:
    store = DeviceCredentialStore(tmp_path, protector=ReversibleProtector())
    identity = store.enroll()
    assert not identity.hardware_backed
    assert identity.public_key_jwk["kty"] == "EC"
    assert len(identity.key_thumbprint) == 64
    assert not (tmp_path / "device-key.dpapi").read_bytes().startswith(b"0")
    signature = store.sign(b"payload")
    from cryptography.hazmat.primitives import serialization

    public = serialization.load_pem_public_key(identity.public_key_pem.encode())
    assert isinstance(public, ec.EllipticCurvePublicKey)
    public.verify(signature, b"payload", ec.ECDSA(hashes.SHA256()))
    assert json.loads((tmp_path / "device.json").read_text())["backend"] == "dpapi"
