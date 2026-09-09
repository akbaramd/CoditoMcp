from __future__ import annotations

import base64
from datetime import timedelta

import pytest
from asgiref.sync import async_to_sync
from codito_protocol import MessageKind, TunnelBindings, TunnelEnvelope, compute_action_digest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from django.utils import timezone

from codito_relay.core.device_crypto import verify_device_signature, websocket_proof_message
from codito_relay.core.models import AuditEvent, Device, Operation, Project
from codito_relay.core.websocket import (
    DeviceConnection,
    _new_message_id,
    _record_device_message,
    _recover_unsent_operations,
    _synchronize_projects,
    _terminal_ack,
)


def b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def test_relay_message_id_is_opaque_when_random_suffix_begins_with_symbol(monkeypatch) -> None:
    monkeypatch.setattr(
        "codito_relay.core.websocket.secrets.token_urlsafe", lambda _size: "_unsafe-prefix"
    )
    message_id = _new_message_id()
    assert message_id == "relay__unsafe-prefix"
    assert message_id[0].isalnum()


def test_device_proof_accepts_its_key_and_rejects_tampering() -> None:
    private_key = ec.generate_private_key(ec.SECP256R1())
    public = private_key.public_key().public_numbers()
    jwk = {
        "kty": "EC",
        "crv": "P-256",
        "x": b64(public.x.to_bytes(32, "big")),
        "y": b64(public.y.to_bytes(32, "big")),
    }
    message = websocket_proof_message("ticket", "challenge")
    signature = b64(private_key.sign(message, ec.ECDSA(hashes.SHA256())))
    assert verify_device_signature(jwk, message, signature)
    assert not verify_device_signature(jwk, message + b"!", signature)


@pytest.mark.django_db(transaction=True)
def test_project_sync_cannot_take_another_accounts_project(device, other_user) -> None:  # type: ignore[no-untyped-def]
    other_device = Device.objects.create(
        account=other_user,
        name="Other device",
        key_thumbprint="c" * 64,
        public_key_jwk={},
    )
    project = Project.objects.create(
        id="project_0123456789abcdef",
        account=other_user,
        device=other_device,
        title="Victim",
        root_fingerprint="1" * 64,
    )
    connection = DeviceConnection(
        device_id=str(device.pk), account_wire_id=f"account_{device.account_id:016x}", epoch=1
    )
    with pytest.raises(ValueError, match="substitution"):
        _synchronize_projects(
            connection,
            {
                "projects": [
                    {
                        "project_id": project.pk,
                        "title": "Taken",
                        "root_fingerprint": "2" * 64,
                        "mode": "isolated",
                    }
                ]
            },
        )
    project.refresh_from_db()
    assert project.account_id == other_user.pk
    assert project.title == "Victim"


@pytest.mark.django_db(transaction=True)
def test_terminal_ack_is_built_only_after_durable_result(device, link) -> None:  # type: ignore[no-untyped-def]
    device.connection_epoch = 4
    device.save(update_fields=["connection_epoch"])
    project = Project.objects.create(
        account=device.account,
        device=device,
        title="Project",
        root_fingerprint="3" * 64,
    )
    payload = {
        "tool_name": "project_read",
        "input": {"operation": "read_file", "project_id": project.pk, "path": "README.md"},
    }
    digest = compute_action_digest(payload)
    operation = Operation.objects.create(
        account=device.account,
        oauth_grant_id="grant_0123456789abcdef",
        device=device,
        device_link=link,
        project=project,
        kind=Operation.Kind.READ,
        connection_epoch=3,
        action_digest=digest,
        request_digest="4" * 64,
        deadline_at=timezone.now() + timedelta(minutes=1),
    )
    connection = DeviceConnection(
        device_id=str(device.pk), account_wire_id=f"account_{device.account_id:016x}", epoch=4
    )
    result_envelope = TunnelEnvelope(
        kind=MessageKind.OPERATION_RESULT,
        message_id="message_0123456789abcdef",
        correlation_id=str(operation.correlation_id),
        sequence=2,
        connection_epoch=4,
        bindings=TunnelBindings(
            account_id=connection.account_wire_id,
            device_id=str(device.pk),
            link_id=str(link.link_id),
            grant_id=operation.oauth_grant_id,
            project_id=project.pk,
        ),
        action_digest=digest,
        payload={"ok": False, "text": "Denied", "error": {"code": "approval_denied"}},
    )
    persisted = _record_device_message(connection, result_envelope)
    operation.refresh_from_db()
    assert operation.status == Operation.Status.FAILED
    assert operation.result == result_envelope.payload
    assert operation.connection_epoch == 4
    acknowledgement = _terminal_ack(connection, persisted, result_envelope)
    assert acknowledgement.kind is MessageKind.TERMINAL_ACK
    assert acknowledgement.action_digest == digest
    assert acknowledgement.correlation_id == str(operation.correlation_id)


@pytest.mark.django_db(transaction=True)
def test_reconnect_delivers_operation_published_before_socket_send(device, link) -> None:  # type: ignore[no-untyped-def]
    device.connection_epoch = 8
    device.status = Device.Status.ONLINE
    device.save(update_fields=["connection_epoch", "status"])
    project = Project.objects.create(
        account=device.account,
        device=device,
        title="Recovered",
        root_fingerprint="7" * 64,
    )
    request_payload = {
        "tool_name": "project_read",
        "input": {"operation": "read_file", "project_id": project.pk, "path": "README.md"},
    }
    operation = Operation.objects.create(
        account=device.account,
        oauth_grant_id="grant_0123456789abcdef",
        device=device,
        device_link=link,
        project=project,
        kind=Operation.Kind.READ,
        status=Operation.Status.DISPATCHED,
        connection_epoch=7,
        action_digest=compute_action_digest(request_payload),
        request_digest="6" * 64,
        request_payload=request_payload,
        deadline_at=timezone.now() + timedelta(minutes=1),
    )
    connection = DeviceConnection(
        device_id=str(device.pk), account_wire_id=f"account_{device.account_id:016x}", epoch=8
    )

    class Socket:
        def __init__(self) -> None:
            self.sent: list[dict[str, object]] = []

        async def send_json(self, value: dict[str, object]) -> None:
            self.sent.append(value)

    socket = Socket()
    async_to_sync(_recover_unsent_operations)(socket, object(), connection)
    operation.refresh_from_db()
    assert len(socket.sent) == 1
    recovered = TunnelEnvelope.model_validate(socket.sent[0])
    assert recovered.connection_epoch == 8
    assert recovered.correlation_id == str(operation.correlation_id)
    assert operation.sent_at is not None
    assert operation.sent_epoch == 8


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("kind", [Operation.Kind.READ, Operation.Kind.PATCH])
@pytest.mark.parametrize(
    "nonterminal_status",
    [Operation.Status.DISPATCHED, Operation.Status.RECEIVED, Operation.Status.RUNNING],
)
def test_reconnect_replays_sent_safe_operation(  # type: ignore[no-untyped-def]
    device, link, kind: str, nonterminal_status: str
) -> None:
    device.connection_epoch = 12
    device.status = Device.Status.ONLINE
    device.save(update_fields=["connection_epoch", "status"])
    project = Project.objects.create(
        account=device.account,
        device=device,
        title="Replay safe",
        root_fingerprint="8" * 64,
    )
    if kind == Operation.Kind.READ:
        request_input = {
            "operation": "read_file",
            "project_id": project.pk,
            "path": "README.md",
        }
    else:
        request_input = {
            "project_id": project.pk,
            "patch": "*** Begin Patch\n*** Add File: x.txt\n+safe\n*** End Patch\n",
            "base_hashes": {"x.txt": None},
            "idempotency_key": "idem_replay_0123456789",
            "dry_run": False,
        }
    request_payload = {"tool_name": kind, "input": request_input}
    operation = Operation.objects.create(
        account=device.account,
        oauth_grant_id="grant_0123456789abcdef",
        device=device,
        device_link=link,
        project=project,
        kind=kind,
        status=nonterminal_status,
        connection_epoch=11,
        action_digest=compute_action_digest(request_payload),
        request_digest="9" * 64,
        request_payload=request_payload,
        deadline_at=timezone.now() + timedelta(minutes=1),
        delivery_attempted_at=timezone.now(),
        sent_at=timezone.now(),
        sent_epoch=11,
    )
    connection = DeviceConnection(
        device_id=str(device.pk), account_wire_id=f"account_{device.account_id:016x}", epoch=12
    )

    class Socket:
        def __init__(self) -> None:
            self.sent: list[dict[str, object]] = []

        async def send_json(self, value: dict[str, object]) -> None:
            self.sent.append(value)

    socket = Socket()
    async_to_sync(_recover_unsent_operations)(socket, object(), connection)
    operation.refresh_from_db()
    assert len(socket.sent) == 1
    replay = TunnelEnvelope.model_validate(socket.sent[0])
    assert replay.message_id == str(operation.message_id)
    assert replay.connection_epoch == 12
    assert operation.sent_epoch == 12


@pytest.mark.django_db(transaction=True)
def test_reconnect_never_replays_uncertain_shell(device, link) -> None:  # type: ignore[no-untyped-def]
    device.connection_epoch = 15
    device.status = Device.Status.ONLINE
    device.save(update_fields=["connection_epoch", "status"])
    project = Project.objects.create(
        account=device.account,
        device=device,
        title="Shell uncertain",
        root_fingerprint="a" * 64,
    )
    request_payload = {
        "tool_name": "project_shell",
        "input": {
            "action": "start",
            "project_id": project.pk,
            "purpose": "Run tests",
            "idempotency_key": "shell_replay_0123456789",
            "command": {"kind": "exec", "executable": "python", "arguments": ["-V"]},
        },
    }
    operation = Operation.objects.create(
        account=device.account,
        oauth_grant_id="grant_0123456789abcdef",
        device=device,
        device_link=link,
        project=project,
        kind=Operation.Kind.SHELL,
        status=Operation.Status.RUNNING,
        connection_epoch=14,
        action_digest=compute_action_digest(request_payload),
        request_digest="b" * 64,
        request_payload=request_payload,
        deadline_at=timezone.now() + timedelta(minutes=1),
        delivery_attempted_at=timezone.now(),
        sent_at=timezone.now(),
        sent_epoch=14,
    )
    connection = DeviceConnection(
        device_id=str(device.pk), account_wire_id=f"account_{device.account_id:016x}", epoch=15
    )

    class Socket:
        sent: list[dict[str, object]] = []

        async def send_json(self, value: dict[str, object]) -> None:
            self.sent.append(value)

    class Redis:
        def __init__(self) -> None:
            self.results: list[dict[str, str]] = []

        async def xadd(self, _stream: str, value: dict[str, str], **_kwargs: object) -> None:
            self.results.append(value)

        async def expire(self, _stream: str, _seconds: int) -> None:
            return None

    socket = Socket()
    redis = Redis()
    async_to_sync(_recover_unsent_operations)(socket, redis, connection)
    operation.refresh_from_db()
    assert socket.sent == []
    assert operation.status == Operation.Status.OUTCOME_UNKNOWN
    assert operation.error_code == "outcome_unknown"
    assert operation.result is None
    assert len(redis.results) == 1


@pytest.mark.django_db(transaction=True)
def test_late_lifecycle_message_cannot_downgrade_terminal_operation(device, link) -> None:  # type: ignore[no-untyped-def]
    device.connection_epoch = 21
    device.save(update_fields=["connection_epoch"])
    project = Project.objects.create(
        account=device.account,
        device=device,
        title="Monotonic",
        root_fingerprint="c" * 64,
    )
    payload = {
        "tool_name": "project_read",
        "input": {"operation": "read_file", "project_id": project.pk, "path": "README.md"},
    }
    operation = Operation.objects.create(
        account=device.account,
        oauth_grant_id="grant_0123456789abcdef",
        device=device,
        device_link=link,
        project=project,
        kind=Operation.Kind.READ,
        status=Operation.Status.SUCCEEDED,
        connection_epoch=21,
        action_digest=compute_action_digest(payload),
        request_digest="d" * 64,
        request_payload=payload,
        deadline_at=timezone.now() + timedelta(minutes=1),
        result={"ok": True, "text": "done", "result": {}},
    )
    connection = DeviceConnection(
        device_id=str(device.pk), account_wire_id=f"account_{device.account_id:016x}", epoch=21
    )
    late = TunnelEnvelope(
        kind=MessageKind.OPERATION_RECEIVED,
        message_id="late_message_0123456789",
        correlation_id=str(operation.correlation_id),
        sequence=99,
        connection_epoch=21,
        bindings=TunnelBindings(
            account_id=connection.account_wire_id,
            device_id=str(device.pk),
            link_id=str(link.link_id),
            grant_id=operation.oauth_grant_id,
            project_id=project.pk,
        ),
        action_digest=operation.action_digest,
    )
    _record_device_message(connection, late)
    operation.refresh_from_db()
    assert operation.status == Operation.Status.SUCCEEDED
    assert operation.result == {"ok": True, "text": "done", "result": {}}


@pytest.mark.django_db(transaction=True)
def test_lower_sequence_result_cannot_terminalize_running_operation(device, link) -> None:  # type: ignore[no-untyped-def]
    device.connection_epoch = 22
    device.save(update_fields=["connection_epoch"])
    project = Project.objects.create(
        account=device.account,
        device=device,
        title="Ordered terminal",
        root_fingerprint="1" * 64,
    )
    request_payload = {
        "tool_name": "project_read",
        "input": {"operation": "read_file", "project_id": project.pk, "path": "README.md"},
    }
    operation = Operation.objects.create(
        account=device.account,
        oauth_grant_id="grant_0123456789abcdef",
        device=device,
        device_link=link,
        project=project,
        kind=Operation.Kind.READ,
        status=Operation.Status.RUNNING,
        connection_epoch=22,
        action_digest=compute_action_digest(request_payload),
        request_digest="1" * 64,
        request_payload=request_payload,
        deadline_at=timezone.now() + timedelta(minutes=1),
        last_device_sequence=10,
        last_device_sequence_epoch=22,
    )
    connection = DeviceConnection(
        device_id=str(device.pk), account_wire_id=f"account_{device.account_id:016x}", epoch=22
    )
    result_payload = {
        "ok": True,
        "text": "Read complete",
        "result": {
            "operation": "read_file",
            "project_id": project.pk,
            "path": "README.md",
            "numbered_text": "1: hello",
            "encoding": "utf-8",
            "newline": "none",
            "size": 5,
            "sha256": "0" * 64,
            "first_line": 1,
            "last_line": 1,
            "truncated": False,
        },
    }

    def terminal(sequence: int, message_id: str) -> TunnelEnvelope:
        return TunnelEnvelope(
            kind=MessageKind.OPERATION_RESULT,
            message_id=message_id,
            correlation_id=str(operation.correlation_id),
            sequence=sequence,
            connection_epoch=22,
            bindings=TunnelBindings(
                account_id=connection.account_wire_id,
                device_id=str(device.pk),
                link_id=str(link.link_id),
                grant_id=operation.oauth_grant_id,
                project_id=project.pk,
            ),
            action_digest=operation.action_digest,
            payload=result_payload,
        )

    with pytest.raises(ValueError, match="stale operation lifecycle sequence"):
        _record_device_message(connection, terminal(9, "lower_sequence_terminal_01"))
    operation.refresh_from_db()
    assert operation.status == Operation.Status.RUNNING
    assert operation.result is None
    assert operation.last_device_sequence == 10

    persisted = _record_device_message(connection, terminal(11, "ordered_terminal_000001"))
    operation.refresh_from_db()
    assert operation.status == Operation.Status.SUCCEEDED
    assert operation.last_device_sequence == 11
    # An exact duplicate at the same sequence remains ACK-able and immutable.
    assert (
        _record_device_message(connection, terminal(11, "ordered_terminal_duplicate")).pk
        == persisted.pk
    )


@pytest.mark.django_db(transaction=True)
def test_late_authoritative_result_reconciles_no_result_timeout_once(device, link) -> None:  # type: ignore[no-untyped-def]
    device.connection_epoch = 31
    device.save(update_fields=["connection_epoch"])
    project = Project.objects.create(
        account=device.account,
        device=device,
        title="Late terminal",
        root_fingerprint="e" * 64,
    )
    payload = {
        "tool_name": "project_read",
        "input": {"operation": "read_file", "project_id": project.pk, "path": "README.md"},
    }
    operation = Operation.objects.create(
        account=device.account,
        oauth_grant_id="grant_0123456789abcdef",
        device=device,
        device_link=link,
        project=project,
        kind=Operation.Kind.READ,
        status=Operation.Status.FAILED,
        connection_epoch=30,
        action_digest=compute_action_digest(payload),
        request_digest="f" * 64,
        request_payload=payload,
        deadline_at=timezone.now() - timedelta(seconds=1),
        error_code="operation_timeout",
        result=None,
    )
    connection = DeviceConnection(
        device_id=str(device.pk), account_wire_id=f"account_{device.account_id:016x}", epoch=31
    )
    result_payload = {
        "ok": True,
        "text": "Recovered from the device journal.",
        "result": {
            "operation": "read_file",
            "project_id": project.pk,
            "path": "README.md",
            "numbered_text": "1: recovered",
            "encoding": "utf-8",
            "newline": "none",
            "size": 9,
            "sha256": "0" * 64,
            "first_line": 1,
            "last_line": 1,
            "truncated": False,
        },
    }
    result = TunnelEnvelope(
        kind=MessageKind.OPERATION_RESULT,
        message_id="late_result_0123456789",
        correlation_id=str(operation.correlation_id),
        sequence=7,
        connection_epoch=31,
        bindings=TunnelBindings(
            account_id=connection.account_wire_id,
            device_id=str(device.pk),
            link_id=str(link.link_id),
            grant_id=operation.oauth_grant_id,
            project_id=project.pk,
        ),
        action_digest=operation.action_digest,
        payload=result_payload,
    )

    persisted = _record_device_message(connection, result)
    operation.refresh_from_db()
    assert operation.status == Operation.Status.SUCCEEDED
    assert operation.result == result_payload
    assert operation.error_code == ""
    assert operation.connection_epoch == 31
    event = AuditEvent.objects.get(
        operation=operation, event_type="operation.late_terminal_reconciled"
    )
    assert event.metadata == {
        "prior_status": Operation.Status.FAILED,
        "prior_error_code": "operation_timeout",
        "resolved_status": Operation.Status.SUCCEEDED,
    }
    acknowledgement = _terminal_ack(connection, persisted, result)
    assert acknowledgement.correlation_id == str(operation.correlation_id)

    substituted = result.model_copy(
        update={
            "message_id": "late_result_substitute_012345",
            "sequence": 8,
            "payload": {"ok": False, "text": "different", "error": {"code": "internal_error"}},
        }
    )
    with pytest.raises(ValueError, match="terminal result substitution"):
        _record_device_message(connection, substituted)
    operation.refresh_from_db()
    assert operation.result == result_payload
