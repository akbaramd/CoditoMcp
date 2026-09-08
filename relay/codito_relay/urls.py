from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path, re_path
from oauth2_provider.urls import base_urlpatterns

from codito_relay.core import views

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/login/", auth_views.LoginView.as_view(), name="login"),
    path("accounts/logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("o/", include((base_urlpatterns, "oauth2_provider"), namespace="oauth2_provider")),
    path(".well-known/oauth-authorization-server", views.oauth_authorization_server_metadata),
    path(".well-known/openid-configuration", views.oidc_discovery),
    path(".well-known/jwks.json", views.jwks),
    re_path(
        r"^\.well-known/oauth-protected-resource/mcp/d/(?P<link_id>[0-9a-fA-F-]{32,36})$",
        views.protected_resource_metadata,
        name="protected-resource-metadata",
    ),
    path("oidc/userinfo", views.userinfo),
    path("", include("codito_relay.core.urls")),
]
