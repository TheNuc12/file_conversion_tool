# Rotary

One tool for converting files from one format to another: images, audio, video, documents, spreadsheets, slide decks, structured data and archives. You can use it from the command line, or open the web interface, drop in a file and turn the dial to the format you want.

```
$ rotary holiday.heic -t jpg
  ok holiday.heic -> holiday.jpg   (Pillow, 0.2s)

$ rotary notes.md -t pdf
  ok notes.md -> notes.pdf   (Pandoc, LibreOffice, 1.4s)

$ rotary ui
Rotary is running at http://127.0.0.1:8765/  (Ctrl+C to stop)
```

When there's no direct converter between two formats, Rotary chains converters together. For example, Markdown → PDF is Pandoc (Markdown → DOCX) followed by LibreOffice (DOCX → PDF). Run `rotary route md pdf` to see the route it picks.

## The desktop app (Windows)

1. Download **[Rotary-Setup.exe](https://github.com/TheNuc12/file_conversion_tool/releases/latest/download/Rotary-Setup.exe)**.
2. Double-click it and press **Install**. It installs just for you, so you don't need an administrator password.
3. Click the **Rotary** icon on your desktop.

Rotary opens in its own window. When you're done, close the window and Rotary shuts itself down. Clicking the icon while it's already open just brings up another window.

FFmpeg and Pandoc are built in, so images, audio, video, PDFs, data files, archives and Markdown/HTML documents all work straight away. **Word, Excel and PowerPoint files are converted with Microsoft Office** (for example Word → PDF), which most work PCs already have. Rotary runs Office invisibly in the background with macros turned off, and never touches Office windows you have open. If a PC has no Office, Rotary uses [LibreOffice](https://www.libreoffice.org/download/) instead, if that's installed.

### Where to download

* **The installer:** https://github.com/TheNuc12/file_conversion_tool/releases/latest/download/Rotary-Setup.exe always downloads the newest version. While the repository is private, sign in to GitHub first.
* **Without installing:** [Rotary-portable.zip](https://github.com/TheNuc12/file_conversion_tool/releases/latest/download/Rotary-portable.zip) is the same app. Unzip it and run `Rotary.exe`.
* **Release notes and older versions:** the [Releases page](https://github.com/TheNuc12/file_conversion_tool/releases).

To publish a new version, bump `version` in `pyproject.toml` and `rotary/__init__.py`. Then either push a matching tag (`git tag v0.5.0 && git push origin v0.5.0`), or open **Actions → Windows app → Run workflow** and tick **Publish a release**. The workflow builds, tests and publishes it.

### Using it on a work PC

* **Your files never leave the computer.** Rotary runs entirely on your PC and needs no internet connection.
* Windows may say **"Windows protected your PC"** the first time, because the app isn't code-signed. Click **More info → Run anyway**. Some companies block unsigned apps altogether, in which case IT has to approve it. Two things help them:
  * the source code in this repository
  * the built-in check: run `Rotary.exe --selftest report.txt` and send them the report
* If installers are blocked but apps aren't, use **Rotary-portable**: unzip it anywhere you like and double-click `Rotary.exe`. To get a desktop icon, right-click `Rotary.exe` → *Send to → Desktop (create shortcut)*.
* If something goes wrong, there's a log at `%LOCALAPPDATA%\Rotary\rotary.log`.

To build the app yourself on Windows, run `packaging\windows\build.ps1`. GitHub Actions also builds and tests it on every push (`.github/workflows/windows-app.yml`).

## Electronic signatures (Rotary Sign)

**Sign a PDF yourself.** Switch the desktop app to **Sign a PDF** and open your PDF.

1. Pick a field (Signature, Initials, Date, Name, Text or Checkbox) and click where it goes on the page.
2. Draw or type your signature once. If you like, Rotary remembers it on that PC.
3. Press **Save signed PDF**. The signed copy goes to your Downloads folder.

Nothing leaves the computer.

**Send documents to other people to sign.** This is the Rotary Sign server, which your company runs, either as a Docker container or as plain Python on a server.

* **Senders** (your staff) log in, upload a PDF, add signers, drag fields onto the pages and send.
* **Signers** get an email link. They agree to sign electronically and sign in their browser, with no account needed. Signers can go in order or all at once.
* **When everyone has signed**, the server stamps the document, adds a certificate of completion with the full audit trail, and seals it with a digital signature so any later change is detectable. Then it emails the finished PDF to everyone.
* **A public "Check a document" page** confirms that a file is authentic and unchanged.

```bash
pip install ".[server]"
rotary-sign run            # then open http://127.0.0.1:8080 and enter the setup code it prints
```

[docs/signing-server.md](docs/signing-server.md) covers setting it up for real (HTTPS, email, the seal certificate, backups), plus what it does and doesn't cover legally.

## Install from source

You need Python 3.11 or newer.

```bash
git clone https://github.com/TheNuc12/file_conversion_tool.git
cd file_conversion_tool
pip install .            # or: pip install -e ".[heic,test]" for HEIC photos + the test suite
```

That's enough for images, PDFs, structured data and archives. Audio, video and documents use free external programs. Install the ones you need:

| For | Install | macOS | Windows | Debian / Ubuntu |
| --- | --- | --- | --- | --- |
| Audio & video | FFmpeg | `brew install ffmpeg` | `winget install Gyan.FFmpeg` | `sudo apt install ffmpeg` |
| Markdown, HTML, EPUB, LaTeX… | Pandoc | `brew install pandoc` | `winget install JohnMacFarlane.Pandoc` | `sudo apt install pandoc` |
| Word, Excel, PowerPoint (if there's no Microsoft Office) | LibreOffice | `brew install --cask libreoffice` | `winget install TheDocumentFoundation.LibreOffice` | `sudo apt install libreoffice` |
| iPhone HEIC photos | pillow-heif | `pip install pillow-heif` | same | same |

`rotary backends` shows which of these Rotary found. `rotary-app` opens the same app window as the desktop icon.

## The web interface

```bash
rotary ui                 # opens http://127.0.0.1:8765 in your browser
rotary ui --port 9000 --no-browser
```

1. Drop one or more files onto the page, or click to pick them.
2. The dial fills with every format those files can become. To choose one, drag the knob, scroll over it, click a label, or focus the dial and use the arrow keys. The chips above the dial narrow it to one kind (Image, Document…).
3. Press **Convert** (or <kbd>Enter</kbd>). A single file downloads straight away; for several, each gets a Download button, plus **Download all**.

Quality, page and DPI controls appear when the chosen conversion uses them. The server only listens on your own computer by default. Files are converted in a temporary folder that is deleted afterwards.

## The command line

```bash
rotary photo.png -t webp                    # writes photo.webp next to the original
rotary photo.png -o ~/Desktop/small.jpg -q 70
rotary report.docx -t pdf -d exports/       # put results in another folder
rotary ./scans -t pdf                       # every file in a folder that can become PDF
rotary ./music -r -t mp3                    # ...including sub-folders
rotary "*.csv" -t xlsx                      # globs work on Windows too
rotary slides.pptx -t png --page 3 --dpi 200
rotary data.txt --from json -t yaml         # file has the wrong extension
rotary clip.mov -t mp4 --dry-run            # show what would happen
```

`rotary convert ...` is the long form of all of the above. Other commands:

| Command | What it does |
| --- | --- |
| `rotary formats` | every format Rotary knows, grouped by kind |
| `rotary formats photo.heic` | everything that file (or format) can become |
| `rotary route docx png` | the steps and backends a conversion would use |
| `rotary backends` | which backends are installed |
| `rotary ui` | start the web interface |

Rotary never overwrites an existing file unless you pass `--overwrite`. It exits with a non-zero status if any file fails, so it's safe to use in scripts.

## What converts to what

| Kind | Formats | Backend |
| --- | --- | --- |
| Images | PNG, JPG, WebP, GIF (animated too), BMP, TIFF, ICO, ICNS, AVIF, JPEG 2000, TGA, PPM, PCX, QOI, HEIC; SVG input; any image → PDF | Pillow; LibreOffice for SVG |
| Audio | MP3, WAV, FLAC, OGG, Opus, AAC, M4A, WMA, AIFF, AC3 | FFmpeg |
| Video | MP4, MKV, WebM, MOV, AVI, FLV, WMV, MPEG, OGV, 3GP, TS; video → audio, → GIF, → thumbnail PNG/JPG; animated GIF → video | FFmpeg |
| Documents | Markdown, HTML, DOCX, ODT, RTF, reStructuredText, LaTeX, EPUB, Org, AsciiDoc, MediaWiki, Jupyter, plain text | Pandoc |
| Office | DOC/DOCX/ODT/RTF/TXT/HTML → PDF and each other; XLS/XLSX/ODS → PDF, CSV, HTML and each other; PPT/PPTX/ODP → PDF and each other | Microsoft Office (Windows), else LibreOffice |
| PDF → Word | PDF → DOCX/ODT, as positioned text boxes | LibreOffice |
| PDF | PDF → any image format (pick a page), PDF → text | pdfium |
| Data | JSON, YAML, TOML, XML, CSV, TSV, XLSX: any to any | built in |
| Archives | ZIP, TAR, TAR.GZ, TAR.BZ2, TAR.XZ: any to any | built in |

Combining these gives you conversions like DOCX → PNG, CSV → PDF, JSON → XLSX → ODS and PPTX → JPG.

Some notes on what to expect:

* **Data**: nested JSON/YAML is flattened into dotted column names (`address.city`) when it goes into a table. Cell values that look like numbers only become numbers if nothing is lost, so `007` stays `007`. An Excel workbook with several sheets becomes an object keyed by sheet name, and a table format (CSV/TSV) keeps only the first sheet.
* **PDF → Word** puts the text in positioned text boxes. It looks right in Word but isn't free-flowing text. **PDF → TXT** extracts the plain text.
* **Video → MP4** uses H.264 and AAC, and **→ WebM** uses VP9 and Opus. Subtitle tracks are dropped.
* Rotary refuses chains that make no sense. A PNG won't be turned into an MP3 by way of an animated GIF.

## How it works

```
rotary/
  formats.py          the format catalogue: ids, labels, extensions
  engine.py           builds a graph of every available conversion and finds the cheapest route
  converters/         one module per backend; each declares edges (src -> dst, cost) and converts
  cli.py              the command line
  app.py              the desktop launcher: app window, single instance, quits when closed
  selftest.py         the --selftest check
  web/server.py       standard-library HTTP server for the UI
  web/static/         the single-page rotary-dial interface (no external assets)
  sign/pdf.py         signing core: stamping fields, certificate of completion, digital seal
  sign/server.py      the Rotary Sign server (Flask): envelopes, signing links, completion
  sign/store.py       its SQLite storage and hash-chained audit trail
  sign/static/        page viewer, field editor and signature pad, shared by the app and server
packaging/
  make_icon.py        draws rotary.ico / rotary.png
  windows/            PyInstaller recipe, Inno Setup installer, build and download scripts
```

Each converter lists the conversions it can do as *edges*, each with a cost. Slow or lossy backends cost more, so a direct Pillow conversion beats a LibreOffice round trip. The engine runs Dijkstra's algorithm over these edges, with two extra rules:

* *Terminal* edges (image → PDF, video → GIF) can only be the last step of a route.
* *Initial* edges (animated GIF → video) can only be the first step.

Routes are capped at four steps. Each step runs in a temporary folder, and the final file is moved into place only once every step has succeeded.

### Adding a backend

```python
from rotary.converters import Converter, Edge, Options, default_converters
from rotary.converters.base import run
from rotary.engine import Engine

class InkscapeConverter(Converter):
    name = "inkscape"
    label = "Inkscape"

    def edges(self):
        return [Edge("svg", "png", self, cost=1)]

    def convert(self, src, dst, src_fmt, dst_fmt, options: Options):
        run(["inkscape", str(src), "--export-filename", str(dst)], what="Inkscape")

engine = Engine([InkscapeConverter(), *default_converters()])
engine.convert("logo.svg", "png")
```

## Tests

```bash
pip install -e ".[test,server]"
pytest                 # tests needing FFmpeg / Pandoc / LibreOffice skip if those aren't installed
pytest -m "not slow"   # skip the LibreOffice and browser tests
```

The browser test uses Playwright (`playwright install chromium`). You can also point `ROTARY_TEST_CHROMIUM` at an existing Chromium binary.
