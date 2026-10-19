OpenSeek Desktop {VERSION} for Windows (x64)
=============================================

OpenSeek Desktop is a control panel for an OpenSeek server that is ALREADY installed and running on this PC.
It does not install, update or start the server for you.

What it shows and does
  Dashboard, poster Library, live Activity, Logs, Sharing (invitations, devices, guest contributions) and Settings.
  It reads the server's data folder (read-only) and talks to the server's local admin interface (127.0.0.1) with the
  admin secret stored in the server's data folder (admin.token). Nothing is sent anywhere else, except title and poster
  lookups on a public metadata service (Cinemeta) for the Library.

Getting started
  1. Install and start an OpenSeek server on this PC (https://github.com/obadevyh/openseek, docs/INSTALL.md).
  2. Double-click OpenSeek.exe. No Python and no PowerShell are needed.
  3. If the Dashboard says "Server offline" or "data not found": open Settings and set the server's data folder
     (the default is %LOCALAPPDATA%\OpenSeek\data) and, if you changed them, the ports.

Try it without a server
  Start from a terminal:  OpenSeek.exe --demo
  This opens the app on built-in synthetic sample data (open-licensed film titles, generated posters). It is offline,
  never touches a real server and writes nothing to your data folders.

Good to know
  * This is version 0.1.0, an initial experimental release. It is not code-signed, so Windows SmartScreen may warn
    "Windows protected your PC": choose "More info" and then "Run anyway" if you downloaded it from the official release page,
    and compare the file's SHA-256 with the checksum on that page.
  * The server must be a native Windows install on the same PC. A server in Docker keeps its admin interface inside the
    container, which this app cannot reach.
  * "Start server" / "Stop server" need the server's backend folder (Settings). They are optional.
  * It must match your server's version: servers from the 0.1.0 security fixes onwards require the admin secret.

Licence
  OpenSeek is free software under the GNU Affero General Public License v3.0 or later (see LICENSE).
  Corresponding source for this build: https://github.com/obadevyh/openseek (the release tag shown on the download page).
  Third-party components and their licences are in THIRD-PARTY-NOTICES.txt and the licenses folder. The Qt libraries (LGPL-3.0)
  are ordinary DLLs inside the _internal folder and can be replaced.
