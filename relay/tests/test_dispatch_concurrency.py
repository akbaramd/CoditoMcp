from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from django.db import close_old_connections, connection
from django.test import override_settings
from test_dispatch import principal_for

from codito_relay.core.dispatch import ToolDispatchError, _create_operation
from codito_relay.core.models import Operation, Project


@pytest.mark.integration
@pytest.mark.django_db(transaction=True)
@override_settings(DEVICE_QUEUE_LIMIT=1)
def test_postgresql_device_admission_cap_is_atomic(device, link) -> None:  # type: ignore[no-untyped-def]
    if connection.vendor != "postgresql":
        pytest.skip("row-lock admission regression requires PostgreSQL")
    project = Project.objects.create(
        account=device.account,
        device=device,
        title="Concurrent admission",
        root_fingerprint="a" * 64,
    )
    principal = principal_for(device, link)
    barrier = Barrier(2, timeout=5)

    def admit(index: int):  # type: ignore[no-untyped-def]
        close_old_connections()
        try:
            barrier.wait()
            return _create_operation(
                principal=principal,
                device=device,
                project=project,
                tool_name="project_read",
                arguments={"project_id": str(project.pk), "request": index},
                action_digest=str(index) * 64,
                timeout_seconds=45,
            )
        except ToolDispatchError as exc:
            return exc.code
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(admit, range(2)))

    assert sum(isinstance(result, tuple) for result in results) == 1
    assert results.count("device_queue_full") == 1
    assert Operation.objects.filter(device=device).count() == 1
