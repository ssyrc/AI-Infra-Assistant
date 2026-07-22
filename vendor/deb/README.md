# vendor/deb - offline Debian packages

These packages let `mcp_servers/Dockerfile` install `openssh-client` without reaching an apt mirror.

Target runtime:
- Image family: `python:3.11-slim-bullseye`
- Debian suite: bullseye
- Architecture: linux/amd64
- OpenSSH package: `openssh-client_8.4p1-5+deb11u7_amd64.deb`

The Dockerfile verifies `SHA256SUMS` when present, then installs every `*.deb` in this directory with
`dpkg --unpack` followed by `dpkg --configure -a`, so missing dependencies fail immediately instead of
falling back to the network. Keep only packages intended for this one local install set in this directory.

The current files were downloaded from the official Debian bullseye and bullseye-security package indexes:
- `https://deb.debian.org/debian/dists/bullseye/main/binary-amd64/Packages.gz`
- `https://security.debian.org/debian-security/dists/bullseye-security/main/binary-amd64/Packages.gz`

Expected checksums are tracked in `SHA256SUMS`.

To refresh the set, use Debian bullseye amd64 package metadata and include `openssh-client` plus the runtime
libraries that are not reliably present in the slim Python base image.
