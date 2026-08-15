# Third-party tools

Espresso Compresso is Python standard-library code. Its Windows package invokes
separate media-tool executables and includes their notices under `LICENSES/`.

## HandBrakeCLI 1.11.2 (Windows x86_64)

- Official archive: <https://github.com/HandBrake/HandBrake/releases/download/1.11.2/HandBrakeCLI-1.11.2-win-x86_64.zip>
- Archive SHA-256: `80bfe8d5f5d11cc3ef76b834add3ed4e82dee6523ffeb435c283f88b1a21f09d`
- Corresponding source: <https://github.com/HandBrake/HandBrake/releases/download/1.11.2/HandBrake-1.11.2-source.tar.bz2>
- Source archive SHA-256: `12b046350f2422dc28783ff94229aff4ba5fe5e683431e057355d36163b2593a`
- License and notices: included from the official archive under `LICENSES/HandBrake/`.

## FFmpeg 8.1.2 essentials build (Windows x64)

The v1.0.0 builder refuses to proceed until all of these release-specific fields
are supplied and the archive checksum matches:

- Exact archive URL: **REQUIRED at build time** (`-FfmpegArchiveUrl`)
- Archive SHA-256: **REQUIRED at build time** (`-FfmpegArchiveSha256`)
- Corresponding source URL and source checksum: **REQUIRED at build time**
  (`-FfmpegSourceUrl`, `-FfmpegSourceSha256`) and record here before release
- License and notices: copied from the verified archive into `LICENSES/FFmpeg/`

The assembled ZIP also contains `THIRD_PARTY_PROVENANCE.txt` with the exact
archive/source URLs and checksums provided to the release builder.

Do not substitute a different FFmpeg build or infer its license. Record its
exact version/configuration and all final binary/DLL hashes in the release
`SHA256SUMS.txt` and this notice before publication.
