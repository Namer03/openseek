# Licence options

No licence has been chosen. Until a `LICENSE` file is added, the code is under default copyright: nobody but the copyright holder may use, copy or distribute it. This page is a neutral
comparison to help the maintainer decide; it is not legal advice. The development repository's `pyproject.toml` declares `GPL-3.0-or-later`, and the development repository carried GPL-3.0; that
metadata should be updated to match whatever is chosen.

## Comparison

| | MIT | Apache-2.0 | AGPL-3.0 | (GPL-3.0) |
|---|---|---|---|---|
| Type | permissive | permissive | strong copyleft incl. network use | strong copyleft |
| Use in closed-source products | yes | yes | only if the combined work is also AGPL | only if GPL |
| Patent grant | none explicit | explicit, with termination on patent suit | explicit | explicit |
| Must publish source of modified versions run as a network service | no | no | yes, to its users | no (the "SaaS loophole") |
| Attribution / notices | keep notice | keep notice, NOTICE file, state changes | keep notices, state changes | keep notices, state changes |
| Compatible with Apache-2.0 dependencies | yes | yes | yes (one way) | yes (GPL-3.0 only, one way) |
| Length / readability | very short | longer | long | long |
| Corporate / contributor friendliness | highest | high | lower; some organisations ban AGPL | medium |

## Considerations for OpenSeek

* It is a network service that people may host for others (guest listener, Contributor Mode). AGPL-3.0 is the licence designed for that case: someone who runs a modified copy for others
  must offer those users the modified source. This also discourages a closed, hosted fork. Cost: some potential contributors and downstream integrators avoid AGPL, and a client
  library or integration that links the server code would also need to be AGPL (clients that use only the HTTP API are not affected, since they are separate programs).
* MIT or Apache-2.0 maximise adoption and allow commercial forks. Apache-2.0 adds an explicit patent licence and is the better of the two permissive options for a project with cryptographic and protocol code.
* The maintainer wrote the backend entirely, so relicensing is not blocked by third-party contributions today. This changes as soon as outside contributions are accepted without a
  contributor agreement or a clear inbound licence; decide the licence before merging them.
* The desktop app (`desktop/`) links PySide6 (LGPL-3.0, plus commercial). Using it under the LGPL from any of the options above is possible if the LGPL conditions (notice, ability to replace the library when distributing a bundle such as a PyInstaller build) are met. Verify this for the bundled build.

## Recommendation

For a self-hosted network service written by a single maintainer, AGPL-3.0-or-later is the most consistent choice if the goal is to keep modified hosted versions open. If broad adoption and
the simplest contribution story matter more, choose Apache-2.0. MIT is acceptable but offers no patent grant. This is an opinion for the maintainer to weigh, not a requirement. Consider a
short contributor sign-off (DCO) whichever is chosen.

## Third-party licences to check before release

| Component | Licence (as generally published; verify against the pinned versions) |
|---|---|
| FastAPI | MIT |
| uvicorn | BSD-3-Clause |
| cryptography | Apache-2.0 or BSD-3-Clause |
| PyAV (`av`) | BSD-3-Clause |
| FFmpeg libraries bundled in the PyAV wheels | LGPL-2.1+; some wheel builds enable GPL components. Check the exact build of `av==18.1.0` for each platform before distributing a Docker image or a bundled desktop build, and satisfy the notice and source-offer obligations |
| Pillow | HPND (MIT-CMU style) |
| numpy | BSD-3-Clause (the wheels bundle other libraries with their own notices) |
| PySide6 (desktop) | LGPL-3.0 / GPL / commercial |
| PyInstaller (desktop build tool) | GPL-2.0 with a bootloader exception |

Transitive dependencies (Starlette, Pydantic, anyio and others) were not listed here; generate a full list with a tool such as `pip-licenses` and review it before the first release.
Distributing the Docker image or a PyInstaller bundle is redistribution of these components and brings their notice requirements with it.
