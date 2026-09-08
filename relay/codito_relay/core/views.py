from __future__ import annotations

import json
import secrets
from typing import cast

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, login
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, transaction
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods, require_POST
from oauth2_provider.models import AccessToken

from codito_relay import __version__

from .forms import AdminResetPasswordForm, InvitationRegistrationForm
from .models import (
    AuditEvent,
    Device,
    DeviceConnectionTicket,
    DeviceLink,
    Invitation,
    OAuthGrantBinding,
    PasswordResetLink,
    hash_secret,
)

User = get_user_model()


def _desktop_user(request: HttpRequest):  # type: ignore[no-untyped-def]
    authorization = request.headers.get("Authorization", "")
    scheme, separator, raw = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer" or not raw:
        return None
    checksum = __import__("hashlib").sha256(raw.encode()).hexdigest()
    token = (
        AccessToken.objects.select_related("user", "application")
        .filter(token_checksum=checksum, expires__gt=timezone.now())
        .first()
    )
    if (
        token is None
        or token.user is None
        or token.application is None
        or token.application.client_id != settings.DESKTOP_OAUTH_CLIENT_ID
        or token.application.name != "Codito Windows Agent"
        or token.application.client_type != token.application.CLIENT_PUBLIC
        or "device:manage" not in token.scope.split()
        or token.resource != [settings.DESKTOP_RESOURCE]
    ):
        return None
    return token.user


def _desktop_unauthorized() -> JsonResponse:
    return JsonResponse(
        {"error": "invalid_token"},
        status=401,
        headers={"WWW-Authenticate": 'Bearer scope="device:manage"'},
    )


@login_required
def dashboard(request: HttpRequest) -> HttpResponse:
    devices = Device.objects.filter(account_id=request.user.pk).prefetch_related(
        "links", "projects"
    )
    audit_events = AuditEvent.objects.filter(account_id=request.user.pk).order_by("-occurred_at")[
        :50
    ]
    return render(
        request,
        "core/dashboard.html",
        {"devices": devices, "audit_events": audit_events, "relay_version": __version__},
    )


@transaction.atomic
def register_with_invitation(request: HttpRequest, token: str) -> HttpResponse:
    invitation = (
        Invitation.objects.select_for_update().filter(token_hash=hash_secret(token)).first()
    )
    if invitation is None or not invitation.matches(token) or not invitation.usable:
        raise Http404("Invitation is invalid, expired, used, or revoked")
    form = InvitationRegistrationForm(
        request.POST or None,
        invitation=invitation,
        initial={"email": invitation.email},
    )
    if request.method == "POST" and form.is_valid():
        user = form.save()
        invitation.used_at = timezone.now()
        invitation.used_by = user
        invitation.save(update_fields=["used_at", "used_by"])
        AuditEvent.objects.create(
            account=user,
            actor_type="user",
            actor_id=str(user.pk),
            event_type="account.invitation_accepted",
            metadata={"invitation_id": str(invitation.pk)},
        )
        login(request, user)
        return redirect("dashboard")
    return render(request, "registration/invited_signup.html", {"form": form})


@transaction.atomic
def reset_password_with_link(request: HttpRequest, token: str) -> HttpResponse:
    reset = (
        PasswordResetLink.objects.select_for_update()
        .select_related("user")
        .filter(token_hash=hash_secret(token))
        .first()
    )
    if (
        reset is None
        or not secrets.compare_digest(reset.token_hash, hash_secret(token))
        or not reset.usable
    ):
        raise Http404("Reset link is invalid, expired, or used")
    form = AdminResetPasswordForm(reset.user, request.POST or None)
    if request.method == "POST" and form.is_valid():
        form.save()
        reset.used_at = timezone.now()
        reset.save(update_fields=["used_at"])
        AuditEvent.objects.create(
            account=reset.user,
            actor_type="user",
            actor_id=str(reset.user_id),
            event_type="account.password_reset",
            metadata={"reset_id": str(reset.pk)},
        )
        messages.success(request, "Password changed. Sign in with the new password.")
        return redirect("login")
    return render(request, "registration/reset_with_link.html", {"form": form})


@login_required
@require_POST
def rotate_device_link(request: HttpRequest, device_id: str) -> HttpResponse:
    account_id = cast(int, request.user.pk)
    with transaction.atomic():
        device = get_object_or_404(
            Device.objects.select_for_update(),
            pk=device_id,
            account_id=account_id,
            revoked_at__isnull=True,
        )
        DeviceLink.objects.filter(device=device, revoked_at__isnull=True).update(
            revoked_at=timezone.now()
        )
        link = DeviceLink.objects.create(account_id=account_id, device=device)
        AuditEvent.objects.create(
            account_id=account_id,
            actor_type="user",
            actor_id=str(request.user.pk),
            event_type="device_link.rotated",
            device=device,
            metadata={"link_id": str(link.link_id)},
        )
    messages.success(request, "A new MCP link was created; prior links were revoked.")
    return redirect("dashboard")


@login_required
@require_POST
def revoke_device(request: HttpRequest, device_id: str) -> HttpResponse:
    account_id = cast(int, request.user.pk)
    with transaction.atomic():
        device = get_object_or_404(
            Device.objects.select_for_update(), pk=device_id, account_id=account_id
        )
        now = timezone.now()
        device.status = Device.Status.REVOKED
        device.revoked_at = now
        device.connection_epoch += 1
        device.save(update_fields=["status", "revoked_at", "connection_epoch"])
        DeviceLink.objects.filter(device=device, revoked_at__isnull=True).update(revoked_at=now)
        OAuthGrantBinding.objects.filter(
            device_link__device=device, revoked_at__isnull=True
        ).update(revoked_at=now)
        AuditEvent.objects.create(
            account_id=account_id,
            actor_type="user",
            actor_id=str(request.user.pk),
            event_type="device.revoked",
            device=device,
            metadata={},
        )
    messages.success(request, "Device, links, and grants were revoked.")
    return redirect("dashboard")


@csrf_exempt
@require_http_methods(["POST"])
def enroll_device(request: HttpRequest) -> JsonResponse:
    account = _desktop_user(request)
    if account is None:
        return _desktop_unauthorized()
    try:
        body = json.loads(request.body)
        name = str(body["name"]).strip()
        public_key_jwk = body["public_key_jwk"]
        thumbprint = str(body["key_thumbprint"])
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        return JsonResponse({"error": "invalid_request"}, status=400)
    if not name or len(name) > 120 or not isinstance(public_key_jwk, dict) or len(thumbprint) != 64:
        return JsonResponse({"error": "invalid_request"}, status=400)
    if Device.objects.filter(account=account, name=name, revoked_at__isnull=True).exists():
        return JsonResponse(
            {"error": "device_name_conflict", "message": "Choose a unique device name."},
            status=409,
        )
    if Device.objects.filter(account=account, key_thumbprint=thumbprint).exists():
        return JsonResponse(
            {
                "error": "device_key_conflict",
                "message": "A revoked device key cannot be reused; rotate the local key.",
            },
            status=409,
        )
    try:
        with transaction.atomic():
            device = Device.objects.create(
                account=account,
                name=name,
                public_key_jwk=public_key_jwk,
                key_thumbprint=thumbprint,
                status=Device.Status.OFFLINE,
            )
            link = DeviceLink.objects.create(account=account, device=device)
            AuditEvent.objects.create(
                account=account,
                actor_type="desktop_oauth",
                actor_id=str(account.pk),
                event_type="device.enrolled",
                device=device,
                metadata={"key_thumbprint": thumbprint},
            )
    except IntegrityError:
        return JsonResponse(
            {"error": "device_conflict", "message": "Device name or key is already registered."},
            status=409,
        )
    return JsonResponse(
        {"device_id": str(device.pk), "link_id": str(link.link_id), "mcp_url": link.resource},
        status=201,
    )


@csrf_exempt
@require_POST
def revoke_desktop_device(request: HttpRequest, device_id: str) -> JsonResponse:
    account = _desktop_user(request)
    if account is None:
        return _desktop_unauthorized()
    with transaction.atomic():
        device = get_object_or_404(
            Device.objects.select_for_update(),
            pk=device_id,
            account=account,
            revoked_at__isnull=True,
        )
        now = timezone.now()
        device.status = Device.Status.REVOKED
        device.revoked_at = now
        device.connection_epoch += 1
        device.save(update_fields=["status", "revoked_at", "connection_epoch"])
        DeviceLink.objects.filter(device=device, revoked_at__isnull=True).update(revoked_at=now)
        OAuthGrantBinding.objects.filter(
            device_link__device=device, revoked_at__isnull=True
        ).update(revoked_at=now)
        AuditEvent.objects.create(
            account=account,
            actor_type="desktop_oauth",
            actor_id=str(account.pk),
            event_type="device.revoked",
            device=device,
            metadata={"source": "windows_agent"},
        )
    return JsonResponse({"revoked": True, "device_id": str(device.pk)})


@csrf_exempt
@require_POST
def issue_device_ticket(request: HttpRequest, device_id: str) -> JsonResponse:
    account = _desktop_user(request)
    if account is None:
        return _desktop_unauthorized()
    with transaction.atomic():
        device = get_object_or_404(
            Device.objects.select_for_update(),
            pk=device_id,
            account=account,
            revoked_at__isnull=True,
        )
        link = (
            DeviceLink.objects.filter(device=device, account=account, revoked_at__isnull=True)
            .order_by("-created_at")
            .first()
        )
        if link is None:
            return JsonResponse({"error": "active_link_required"}, status=409)
        ticket, raw = DeviceConnectionTicket.issue(device=device)
    return JsonResponse(
        {
            "ticket": raw,
            "challenge": ticket.challenge,
            "expires_at": ticket.expires_at.isoformat(),
            "websocket_url": settings.PUBLIC_BASE_URL.replace("https://", "wss://") + "/ws/device",
            "link_id": str(link.link_id),
            "mcp_url": link.resource,
        }
    )


@csrf_exempt
@require_GET
def list_desktop_devices(request: HttpRequest) -> JsonResponse:
    account = _desktop_user(request)
    if account is None:
        return _desktop_unauthorized()
    devices = Device.objects.filter(account=account, revoked_at__isnull=True).prefetch_related(
        "links"
    )
    return JsonResponse(
        {
            "devices": [
                {
                    "device_id": str(device.pk),
                    "name": device.name,
                    "status": device.status,
                    "last_seen_at": device.last_seen_at.isoformat()
                    if device.last_seen_at
                    else None,
                    "link_id": next(
                        (
                            str(link.link_id)
                            for link in device.links.all()
                            if link.revoked_at is None
                        ),
                        None,
                    ),
                }
                for device in devices
            ]
        }
    )


@require_GET
def oauth_authorization_server_metadata(request: HttpRequest) -> JsonResponse:
    base = settings.PUBLIC_BASE_URL
    return JsonResponse(
        {
            "issuer": base,
            "authorization_endpoint": f"{base}/o/authorize/",
            "token_endpoint": f"{base}/o/token/",
            "revocation_endpoint": f"{base}/o/revoke_token/",
            "introspection_endpoint": f"{base}/o/introspect/",
            "jwks_uri": f"{base}/.well-known/jwks.json",
            "userinfo_endpoint": f"{base}/oidc/userinfo",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["private_key_jwt", "none"],
            "token_endpoint_auth_signing_alg_values_supported": ["RS256"],
            "scopes_supported": list(cast(dict[str, str], settings.OAUTH2_PROVIDER["SCOPES"])),
            "client_id_metadata_document_supported": True,
        }
    )


@require_GET
def oidc_discovery(request: HttpRequest) -> JsonResponse:
    data = json.loads(oauth_authorization_server_metadata(request).content)
    data.update(
        {
            "subject_types_supported": ["public"],
            "id_token_signing_alg_values_supported": ["RS256"],
            "claims_supported": ["sub", "email"],
        }
    )
    return JsonResponse(data)


@require_GET
def jwks(request: HttpRequest) -> JsonResponse:
    from jwcrypto import jwk  # type: ignore[import-untyped]

    private_key = cast(str, settings.OAUTH2_PROVIDER["OIDC_RSA_PRIVATE_KEY"])
    if not private_key:
        return JsonResponse({"keys": []})
    key = jwk.JWK.from_pem(private_key.encode())
    public = json.loads(key.export_public())
    public.update({"alg": "RS256", "use": "sig", "kid": key.thumbprint()})
    return JsonResponse({"keys": [public]}, headers={"Cache-Control": "public, max-age=3600"})


@require_GET
def protected_resource_metadata(request: HttpRequest, link_id: str) -> JsonResponse:
    try:
        link = DeviceLink.objects.select_related("device").get(
            link_id=link_id, revoked_at__isnull=True
        )
    except (DeviceLink.DoesNotExist, ValueError):
        raise Http404("Unknown or revoked device link") from None
    return JsonResponse(
        {
            "resource": link.resource,
            "authorization_servers": [settings.PUBLIC_BASE_URL],
            "bearer_methods_supported": ["header"],
            "scopes_supported": list(settings.MCP_TOOL_SCOPES),
            "resource_documentation": f"{settings.PUBLIC_BASE_URL}/",
        }
    )


@require_GET
def userinfo(request: HttpRequest) -> JsonResponse:
    authorization = request.headers.get("Authorization", "")
    if not authorization.startswith("Bearer "):
        return JsonResponse({"error": "invalid_token"}, status=401)
    raw = authorization.removeprefix("Bearer ").strip()
    token = (
        AccessToken.objects.select_related("user")
        .filter(
            token_checksum=__import__("hashlib").sha256(raw.encode()).hexdigest(),
            expires__gt=timezone.now(),
        )
        .first()
    )
    if token is None or token.user is None:
        return JsonResponse({"error": "invalid_token"}, status=401)
    if "openid" not in token.scope.split():
        return JsonResponse({"error": "insufficient_scope"}, status=403)
    return JsonResponse({"sub": str(token.user_id), "email": token.user.email})
