# Espresso Compresso

Espresso Compresso is a small, local app for compressing a folder of recordings.
It processes one file at a time so the computer stays usable. It is deliberately
not a media library, editor, or cloud service.

## Use it

Windows: double-click `Start Espresso Compresso.bat`, or drag a folder onto it.

Linux: run `sh start-espresso-compresso.sh /path/to/recordings` (Tk and Python 3
must be installed).

Choose one of three modes:

- **Smaller** — reliable CPU H.265; used by default when Fast is unavailable.
- **Fast** — RTX/NVENC H.265; selected by default only when the installed
  HandBrakeCLI supports it.
- **Editing** — constant-frame-rate H.264 MP4 for editors.

Start with the three-file test. Results go to `_compressed` unless another output
folder is selected.

## Safety

- Originals are kept by default.
- A completed output is written to a unique temporary name, validated, then moved
  into place. Interrupted temporary files are retained for inspection.
- Restarting recognizes a valid existing output, but **never deletes an original
  because of it**.
- Deletion requires a newly encoded, smaller output in the current run, strict
  media validation, a source recheck, and a successful full video/audio decode by
  `ffmpeg`. Without `ffmpeg`, originals remain untouched.
- Stopping asks the active encode to end gracefully before using a forced stop.

Ordinary output names stay readable. A short stable source-path tag is added only
when two inputs would otherwise produce the same output name.

## Requirements

Python 3.10+ with Tk is required when running from source. The app itself has no
third-party Python packages.

HandBrakeCLI is required. `ffprobe` improves inspection speed and `ffmpeg` is
required only when permanently deleting originals. Put the tools on `PATH`, choose
them with the command-line options, or place the Windows HandBrakeCLI in `tools`.
The local development folder may contain that executable, but the large binary is
kept out of normal Git history. Check `THIRD_PARTY_NOTICES.md` before sharing it.

The command line remains available:

```powershell
python .\espresso_compresso_cli.py "D:\Recordings" --mode quality --limit 3
```

Use `python .\espresso_compresso_cli.py --help` for all options.

## Development

Run the isolated tests with:

```powershell
python -m unittest discover -s tests -v
```

The tests create only temporary directories and never use real recordings.
