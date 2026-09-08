from __future__ import annotations

import base64
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def websocket_proof_message(ticket: str, challenge: str) -> bytes:
    return b"codito-ws-proof-v1\x00" + ticket.encode() + b"\x00" + challenge.encode()


def verify_device_signature(
    public_jwk: dict[str, Any], message: bytes, signature_text: str
) -> bool:
    try:
        signature = _decode(signature_text)
        key_type = public_jwk.get("kty")
        if key_type == "EC" and public_jwk.get("crv") == "P-256":
            x = int.from_bytes(_decode(str(public_jwk["x"])), "big")
            y = int.from_bytes(_decode(str(public_jwk["y"])), "big")
            ec_public_key = ec.EllipticCurvePublicNumbers(x, y, ec.SECP256R1()).public_key()
            if len(signature) == 64:  # CNG commonly emits IEEE P1363 r||s.
                signature = encode_dss_signature(
                    int.from_bytes(signature[:32], "big"), int.from_bytes(signature[32:], "big")
                )
            ec_public_key.verify(signature, message, ec.ECDSA(hashes.SHA256()))
            return True
        if key_type == "RSA":
            modulus = int.from_bytes(_decode(str(public_jwk["n"])), "big")
            exponent = int.from_bytes(_decode(str(public_jwk["e"])), "big")
            rsa_public_key = rsa.RSAPublicNumbers(exponent, modulus).public_key()
            rsa_public_key.verify(signature, message, padding.PKCS1v15(), hashes.SHA256())
            return True
    except (KeyError, TypeError, ValueError, InvalidSignature):
        return False
    return False
