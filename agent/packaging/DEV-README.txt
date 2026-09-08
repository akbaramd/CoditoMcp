Codito Windows Agent - unsigned developer package
=================================================

This package is for Windows 11 x64 testing. The executables and archive are
unsigned, so Windows may display SmartScreen or trust warnings. Do not use this
artifact as a production release.

Layout
------

cli\codito-agent.exe
    Interactive setup, login, enrollment, and project-management commands.

daemon\codito-agent-daemon.exe
    Per-user background agent. It discovers broker\Codito.Broker.exe from this
    package when broker_path is not explicitly configured.

tray\codito-agent-tray.exe
    PySide6 tray UI and local approval dialogs.

broker\Codito.Broker.exe
    Self-contained .NET 8 win-x64 security broker.

SHA256SUMS
    Sorted SHA-256 manifest for every payload file except the manifest itself.

Quick test
----------

1. Verify SHA256SUMS before running any executable.
2. Run cli\codito-agent.exe init.
3. Run cli\codito-agent.exe login and complete sign-in in the system browser.
4. Register a project with cli\codito-agent.exe register-project.
5. Start daemon\codito-agent-daemon.exe, then tray\codito-agent-tray.exe.

The MSI normally owns sign-in startup. For a ZIP-only test, launch daemon and
tray manually. Do not use `codito-agent startup enable` from this ZIP.

User state is kept under %LOCALAPPDATA%\Codito and is not part of this package.
Removing the package does not revoke the remote device or erase local user data.
