from django.urls import path

from . import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("invite/<str:token>/", views.register_with_invitation, name="invited-signup"),
    path("reset/<str:token>/", views.reset_password_with_link, name="reset-with-link"),
    path(
        "devices/<uuid:device_id>/links/rotate/",
        views.rotate_device_link,
        name="rotate-device-link",
    ),
    path("devices/<uuid:device_id>/revoke/", views.revoke_device, name="revoke-device"),
    path("api/devices/enroll/", views.enroll_device, name="enroll-device"),
    path("api/devices/", views.list_desktop_devices, name="list-desktop-devices"),
    path(
        "api/devices/<uuid:device_id>/tickets/",
        views.issue_device_ticket,
        name="issue-device-ticket",
    ),
]
