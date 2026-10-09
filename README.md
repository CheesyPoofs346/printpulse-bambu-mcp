# Bambu MCP

A local MCP server for working with Bambu Lab printers. It combines printer status and camera access, local model preparation and slicing, and guarded print workflows for use from MCP-compatible assistants.

This is a personal project and an evolving code sample. It is not a hosted service or a production print-farm platform.

## Requirements

- Windows with Python 3.12+ and `uv`
- Bambu Studio installed locally for slicing
- A Bambu Lab printer reachable from the same local network
- An MCP-compatible client

Some optional print-submission paths depend on components installed by official Bambu software. Those vendor binaries and certificates are not redistributed in this repository. Cloud submission is experimental and depends on the local installation and account state.

## Local setup

1. Clone this repository and install dependencies with `uv sync`.
2. Copy `printers.example.json` to `printers.json` and replace the placeholder with your own printer's serial and local address if needed. `printers.json` is ignored by Git.
3. Open the printer in Bambu Studio once so its local access code is available to the integration.
4. Configure your MCP client to run `uv --directory <path-to-repository> run python server.py`.

Keep printer serials, access codes, cloud credentials, local addresses, print files, and logs out of commits. Review the available tools and safety behavior before connecting a printer; print actions affect physical hardware.

## Project status

This repository is shared as a public source project. Features and hardware paths are under active development; compatibility and cloud-print behavior have not been independently verified for every setup.

There is no license file yet, so publication does not grant permission to reuse or redistribute the code.
