# K5 delivery requirements

Builds belong in this shared workspace and must be linked directly in the chat.
Do not upload installers, compiled binaries, build bundles or build artifacts to
GitHub Releases, GitHub Actions artifacts, or another public hosting service.
Do not enable a publishing workflow. Source control and local test execution do
not authorize build publication.

Keep build inputs locally, preserve their licenses and verify their pinned
hashes. Product installation and beta testing must use the native Windows GUI;
do not require an operator to run PowerShell, CMD, Docker or WSL.

Call a new installer qualified only after its own native Windows installation,
recording, playback, analytics, administration and clock checks have passed.
Prior installer evidence stays tied to the installer that produced it.
