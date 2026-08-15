# Third-party tools

Espresso Compresso is Python standard-library code. It invokes external media
tools rather than bundling Python packages.

- HandBrakeCLI is required for encoding. A local Windows development copy may be
  placed under `tools/`; its upstream license and notices are retained under
  `tools/doc/`. The executable is intentionally excluded from normal Git history.
- FFmpeg and ffprobe are optional local tools. When distributing them, include the
  exact upstream build, its license, and its notices. Do not assume a particular
  FFmpeg license applies to every downloadable build.

Before making a release, record each bundled binary's source URL, version, SHA-256
checksum, and license in this file.
