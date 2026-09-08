"""Never encourage resubmission when shell start may have crossed the tunnel."""

import pytest
from asgiref.sync import sync_to_async
from django.utils import timezone
from test_dispatch import principal_for

from codito_relay.core.dispatch import ToolDispatchError, dispatch_tool
from codito_relay.core.models import Device, Operation, Project


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("action", ["start", "poll", "cancel"])
@pytest.mark.parametrize("failure", ["timeout", "connection", "malformed", "offline"])
async def test_shell_start_uncertainty_is_terminal_but_poll_cancel_remain_normal(
    device, link, action, failure
):
    device.status = Device.Status.ONLINE
    device.connection_epoch = 3
    device.last_seen_at = timezone.now()
    await sync_to_async(device.save)()
    project = await sync_to_async(Project.objects.create)(
        account=device.account, device=device, title="Synthetic", root_fingerprint="a" * 64
    )
    arguments = {"action": action, "project_id": project.pk}
    if action == "start":
        arguments.update(
            {
                "purpose": "No live process",
                "idempotency_key": "start_abcdefghijkl",
                "command": {
                    "kind": "script",
                    "shell": "powershell",
                    "script": "Write-Output synthetic",
                },
            }
        )
    else:
        arguments["job_id"] = "job_abcdefghijkl"

    class UncertainTransport:
        calls = 0

        async def publish_and_wait(self, **kwargs):
            self.calls += 1
            if failure in {"timeout", "offline"}:
                raise ToolDispatchError(
                    "operation_timeout" if failure == "timeout" else "device_offline",
                    "Synthetic error",
                    retryable=True,
                )
            if failure == "malformed":
                return {"ok": True, "result": {"invalid": True}}
            raise ConnectionError("Synthetic transport interruption")

    transport = UncertainTransport()
    principal = principal_for(device, link)
    with pytest.raises(ToolDispatchError) as caught:
        await dispatch_tool(principal, "project_shell", arguments, transport=transport)
    row = await sync_to_async(Operation.objects.get)(project=project)
    if action == "start" and failure != "offline":
        assert caught.value.code == "outcome_unknown"
        assert caught.value.retryable is False
        assert row.status == Operation.Status.OUTCOME_UNKNOWN
        assert caught.value.details["operation_id"] == str(row.pk)
        with pytest.raises(ToolDispatchError) as repeated:
            await dispatch_tool(principal, "project_shell", arguments, transport=transport)
        assert repeated.value.code == "outcome_unknown" and not repeated.value.retryable
        assert transport.calls == 1
    else:
        expected = {
            "timeout": "operation_timeout",
            "connection": "relay_unavailable",
            "malformed": "protocol_error",
            "offline": "device_offline",
        }[failure]
        assert caught.value.code == expected
        assert caught.value.retryable is (failure != "malformed")
        assert row.status == Operation.Status.FAILED
