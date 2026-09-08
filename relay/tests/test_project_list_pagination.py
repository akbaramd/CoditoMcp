from dataclasses import replace

import pytest
from codito_protocol.read import ListProjectsResult
from test_dispatch import principal_for

from codito_relay.core.dispatch import ToolDispatchError, _list_projects
from codito_relay.core.models import Device, DeviceLink, Project


@pytest.mark.django_db
def test_project_list_bounds_cursor_bindings_duplicate_titles_and_offline(device, link):
    for index, mode in enumerate(
        [Project.Mode.NATIVE_TRUSTED, Project.Mode.FULL_ACCESS, Project.Mode.ISOLATED]
    ):
        Project.objects.create(
            account=device.account,
            device=device,
            title="Same",
            root_fingerprint=str(index),
            mode=mode,
        )
    principal = principal_for(device, link)
    first = _list_projects(principal, {"limit": 2})
    ListProjectsResult.model_validate(first)
    assert len(first["projects"]) == 2
    assert all(project["online"] is False for project in first["projects"])
    cursor = first["continuation"]["cursor"]
    second = _list_projects(principal, {"limit": 2, "cursor": cursor})
    assert len(second["projects"]) == 1
    assert second["continuation"] is None
    assert len({row["project_id"] for row in first["projects"] + second["projects"]}) == 3
    assert {row["mode"] for row in first["projects"] + second["projects"]} == {
        "native_trusted",
        "full_access",
        "isolated",
    }
    other_link = DeviceLink.objects.create(account=device.account, device=device)
    other_device = Device.objects.create(
        account=device.account, name="Other", public_key_jwk={}, key_thumbprint="b"
    )
    for changed in [
        replace(principal, link_id=other_link.link_id),
        replace(principal, device_id=other_device.pk),
        replace(principal, account_id=999),
    ]:
        with pytest.raises(ToolDispatchError) as failure:
            _list_projects(changed, {"limit": 2, "cursor": cursor})
        assert failure.value.code == "invalid_request"
    with pytest.raises(ToolDispatchError):
        _list_projects(principal, {"cursor": "tampered"})
