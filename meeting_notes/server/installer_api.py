"""Client installers, manifests and packages, installed by ``create_app``."""

from __future__ import annotations

import hashlib
import logging

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response

from .. import __version__
from . import auth, client_packages, compat, mac_installer as mac_installer_mod, settings as settings_mod, web

# Same logger name as app.py, so log records and test captures are unchanged by the split.
logger = logging.getLogger("meeting_notes.server.app")


# Update-only installer scripts: same content as the browser installers but always
# rendered with the address the requesting client used (never ``server_address``).
UPDATE_INSTALLER_PS1_PATH = "/install/update/client-agent.ps1"

UPDATE_INSTALLER_MAC_PATH = "/install/update/mac.sh"


def install_installers(app: FastAPI, *, store) -> None:
    """Client install guide, installer/uninstaller scripts, update installers, manifests and client packages."""

    @app.get("/install", response_class=HTMLResponse)
    async def client_install_guide(
        request: Request, _auth: None = Depends(auth.require_web_token)
    ):
        current = settings_mod.load_settings(store.root)
        address = current.server_address or str(request.base_url).rstrip("/")
        return web.render_install_page(
            address,
            token_configured=auth.token_is_configured(),
            appearance=current.appearance,
            macos_package_available=(store.root / "client" / mac_installer_mod.PACKAGE_NAME).is_file(),
        )

    @app.get("/install/client-agent.ps1")
    async def client_installer(request: Request):
        """Public bootstrap used by the one-line PowerShell installer.

        It contains only the saved LAN address and never embeds the server
        token.  Requiring a browser cookie here would make ``irm ... | iex``
        download an empty login redirect instead of the installer.
        """
        current = settings_mod.load_settings(store.root)
        address = current.server_address or str(request.base_url).rstrip("/")
        script = web.render_client_installer(address)
        return Response(
            script,
            media_type="text/plain; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="Install-MeetingNotes.ps1"'},
        )

    def _serve_update_installer(request: Request, kind: str, script: str, media_type: str, disposition: str):
        logger.info(
            "serving %s update installer for address %s (client %s)",
            kind,
            str(request.base_url).rstrip("/"),
            request.headers.get("x-meeting-notes-client") or "unknown",
        )
        return Response(script, media_type=media_type, headers={"Content-Disposition": disposition})

    @app.get(UPDATE_INSTALLER_PS1_PATH)
    async def client_update_installer(request: Request):
        """The installer the in-app updater runs, rendered for the address the
        client used to reach us (see ``client_manifest``)."""
        return _serve_update_installer(
            request,
            "Windows",
            web.render_client_installer(str(request.base_url).rstrip("/")),
            "text/plain; charset=utf-8",
            'attachment; filename="Install-MeetingNotes.ps1"',
        )

    @app.get(UPDATE_INSTALLER_MAC_PATH)
    async def mac_update_installer(request: Request):
        return _serve_update_installer(
            request,
            "macOS",
            web.render_mac_installer(str(request.base_url).rstrip("/")),
            "text/x-shellscript; charset=utf-8",
            'inline; filename="mac.sh"',
        )

    @app.get("/install/uninstall-client.ps1")
    async def client_uninstaller():
        return Response(
            web.render_client_uninstaller(),
            media_type="text/plain; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="Uninstall-MeetingNotes.ps1"'},
        )

    @app.get("/install/client-manifest.json")
    async def client_manifest(request: Request):
        """Public metadata used before a recorder has a server token."""
        package = store.root / "client" / "MeetingNotes-Windows.zip"
        if not package.is_file():
            raise HTTPException(status_code=404, detail="Windows client package is not available")
        digest = hashlib.sha256()
        with package.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        address = str(request.base_url).rstrip("/")
        # The installer script that updates run is rendered with the address THIS
        # client used (not the saved server_address): a recorder that reached us by
        # IP because it cannot resolve the LAN name must download from, and keep
        # configured to, that same address.
        installer = web.render_client_installer(address).encode("utf-8")
        return {
            "url": address + "/install/MeetingNotes-Windows.zip",
            "sha256": digest.hexdigest(),
            "size": package.stat().st_size,
            "version": __version__,
            "min_client_version": compat.min_client_version(),
            "installer": {
                "url": address + UPDATE_INSTALLER_PS1_PATH,
                "sha256": hashlib.sha256(installer).hexdigest(),
                "size": len(installer),
            },
        }

    @app.get("/install/mac.sh")
    async def mac_installer_script(request: Request):
        """Public bootstrap for ``curl -fsSL .../install/mac.sh | bash``.

        Like the PowerShell installer it holds only the LAN address, never the
        server token, and must be reachable without a browser cookie.
        """
        current = settings_mod.load_settings(store.root)
        address = current.server_address or str(request.base_url).rstrip("/")
        return Response(
            web.render_mac_installer(address),
            media_type="text/x-shellscript; charset=utf-8",
            headers={"Content-Disposition": 'inline; filename="mac.sh"'},
        )

    @app.get(mac_installer_mod.UNINSTALLER_PATH)
    async def mac_uninstaller_script():
        """Public like ``mac.sh``: ``curl -fsSL .../install/uninstall-mac.sh | bash``. No address, no token."""
        return Response(
            web.render_mac_uninstaller(),
            media_type="text/x-shellscript; charset=utf-8",
            headers={"Content-Disposition": 'inline; filename="uninstall-mac.sh"'},
        )

    @app.get("/install/client-manifest-macos.json")
    async def client_manifest_macos(request: Request):
        """macOS counterpart of ``client-manifest.json`` (the mac package and the version inside it)."""
        package = store.root / "client" / mac_installer_mod.PACKAGE_NAME
        if not package.is_file():
            raise HTTPException(status_code=404, detail="macOS client package is not available")
        digest = hashlib.sha256()
        with package.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        address = str(request.base_url).rstrip("/")
        installer = web.render_mac_installer(address).encode("utf-8")
        return {
            "url": address + mac_installer_mod.PACKAGE_PATH,
            "sha256": digest.hexdigest(),
            "size": package.stat().st_size,
            # The version of the app actually in the zip: a release can ship without a new Mac build.
            "version": client_packages.published_version("macos", store.root),
            "min_client_version": compat.min_client_version(),
            "installer": {
                "url": address + UPDATE_INSTALLER_MAC_PATH,
                "sha256": hashlib.sha256(installer).hexdigest(),
                "size": len(installer),
            },
        }

    @app.get("/install/MeetingNotes-macOS.zip")
    async def client_package_macos():
        package = store.root / "client" / mac_installer_mod.PACKAGE_NAME
        if not package.is_file():
            raise HTTPException(status_code=404, detail="macOS client package is not available")
        return FileResponse(package, media_type="application/zip", filename=package.name)

    @app.get("/install/MeetingNotes-Windows.zip")
    async def client_package():
        package = store.root / "client" / "MeetingNotes-Windows.zip"
        if not package.is_file():
            raise HTTPException(status_code=404, detail="Windows client package is not available")
        return FileResponse(package, media_type="application/zip", filename=package.name)
