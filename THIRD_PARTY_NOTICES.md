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

- Archive: <https://www.gyan.dev/ffmpeg/builds/packages/ffmpeg-8.1.2-essentials_build.zip>
- Archive SHA-256: `db580001caa24ac104c8cb856cd113a87b0a443f7bdf47d8c12b1d740584a2ec`
- Corresponding source: <https://ffmpeg.org/releases/ffmpeg-8.1.2.tar.xz>
- Source archive SHA-256: `464beb5e7bf0c311e68b45ae2f04e9cc2af88851abb4082231742a74d97b524c`
- License and notices: copied from the verified archive into `LICENSES/FFmpeg/`

The release builder only accepts these v1.0.0 FFmpeg provenance values and
records them in `THIRD_PARTY_PROVENANCE.txt`. `SHA256SUMS.txt` records the final
binary and DLL hashes. Do not substitute a different FFmpeg build.
