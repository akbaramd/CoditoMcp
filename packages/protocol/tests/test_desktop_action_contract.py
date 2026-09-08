import pytest
from codito_protocol.desktop_action import DeviceDesktopInput, DeviceDesktopResult
from pydantic import ValidationError


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/path?q=one%20two#section",
        "http://localhost:8094/",
        "https://192.168.200.39:8094/",
        "http://[::1]:8094/",
    ],
)
def test_browser_accepts_only_valid_http_urls(url):
    request = DeviceDesktopInput(url=url, purpose="Review the development server")
    assert request.url == url
    result = DeviceDesktopResult(url=request.url)
    assert result.status == "submitted"


@pytest.mark.parametrize(
    "url",
    [
        "file:///C:/Windows/System32/cmd.exe",
        "javascript:alert(1)",
        "data:text/html,hello",
        "codito-approval://decision?token=anything",
        "ms-settings:network",
        "https:example.com",
        "//example.com",
        "https:///example.com",
        "https://",
        "https://user:password@example.com",
        "https://@example.com",
        "https://example.com:invalid/",
        "https://example.com:99999/",
        "https://[not-ipv6]/",
        "https://example.com\\@evil.example/",
        "https://example.com/\nnext",
        "https://example.com/%0anext",
        "https://example.com/%00next",
        "https://example.com/\u202eevil",
        "https://example.com/%E2%80%AEevil",
        " https://example.com/",
        "https://example.com/some path",
        "https://example.com/%zz",
        "https://example.com/%",
        "https://%65xample.com/",
    ],
)
def test_browser_rejects_handlers_credentials_and_ambiguous_urls(url):
    with pytest.raises(ValidationError):
        DeviceDesktopInput(url=url, purpose="Should reject")


@pytest.mark.parametrize(
    "extra", [{"approved": True}, {"trusted": True}, {"action": "click"}, {"purpose": ""}]
)
def test_desktop_input_has_no_remote_approval_or_arbitrary_gui_actions(extra):
    with pytest.raises(ValidationError):
        DeviceDesktopInput.model_validate(
            {"url": "https://example.com/", "purpose": "Test", **extra}
        )


def test_normalized_url_is_explicit_and_result_does_not_claim_page_loaded():
    request = DeviceDesktopInput(url="HTTPS://EXAMPLE.COM", purpose="Test")
    assert request.url == "https://example.com/"
    with pytest.raises(ValidationError):
        DeviceDesktopResult(url=request.url, status="loaded")


def test_browser_choice_is_a_fixed_enum_not_an_executable_or_cli():
    assert (
        DeviceDesktopInput(browser="firefox", url="https://example.com", purpose="Test").browser
        == "firefox"
    )
    for browser in ("cmd", "C:\\Windows\\cmd.exe", "firefox -profile secret", "edge"):
        with pytest.raises(ValidationError):
            DeviceDesktopInput(browser=browser, url="https://example.com", purpose="Test")
