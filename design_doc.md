# KDE Store Theme Installer — Design Doc (v1)

## 1. Goal

Given a KDE Store / Pling-network URL for a theme (a "Global Theme" in
practice), automatically:

1. Fetch the content's metadata via the OCS API.
2. Download the primary archive.
3. Parse the description for links to companion content (icon packs,
   color schemes, Aurorae window decorations, fonts, wallpapers, etc.)
   and offer/auto-install those too.
4. Extract everything into the correct Plasma 6 user-data directories.
5. Report back to the user: the display name(s) as they'll appear in
   System Settings, and where to go select each one.

v2 (explicitly deferred): SDDM login themes, GTK themes — both involve
different install mechanisms (system paths, sudo, or a separate
theming subsystem) and don't fit the "drop files, multi-install
safely" model the rest of this tool relies on.

SDDM specifically: files are downloaded to the cache folder like
everything else, but a separate helper script
(`scripts/install_sddm_theme.py`) handles the sudo steps -- copying
to `/usr/share/sddm/themes/`, writing a drop-in config at
`/etc/sddm.conf.d/kde-theme-installer.conf`, and printing the exact
revert command. This keeps the main tool free of sudo while still
giving the user a guided path to installing SDDM themes. The summary
screen shows the exact command to run after a theme install.

IMPORTANT CAVEAT discovered during testing: Plasma Login Manager
(`plasmalogin`), which Nobara KDE ships by default instead of SDDM,
does NOT support arbitrary QML themes -- it is fixed to its own
Breeze-based login screen regardless of any SDDM configuration.
SDDM themes from the KDE Store therefore have no effect on systems
using plasmalogin. The install_sddm_theme.py script detects the
active display manager via `systemctl is-active` and warns the user
clearly before doing anything if SDDM is not the active DM. SDDM
themes are still downloaded to the cache (they may be useful to users
on other distros that do use SDDM -- Arch, openSUSE, etc.) but the
summary screen and GUI description both note that installing requires
SDDM to be active.

## 2. Data source: the OCS API

KDE Store content runs on the OCS (Open Collaboration Services)
network. The store's own React frontend is NOT scrapeable — it's a
JS-rendered SPA sitting behind Cloudflare, and direct requests to it
get redirected to an OAuth login wall. We bypass the website entirely
and talk to the underlying API instead, which is public, anonymous,
and unauthenticated for read access (this is the same mechanism
Plasma's own "Get New ..." dialogs use internally via KNewStuff).

**Provider discovery:**
`https://download.kde.org/ocs/providers.xml` lists the current
provider. As of this writing it resolves to:

```
https://api.kde-look.org/ocs/v1/
```

This should be fetched at runtime (not hardcoded) since the provider
location is exactly the kind of thing that can change — that's the
whole point of the providers.xml indirection.

**Content lookup:**
```
GET {provider_base}content/data/{content_id}
```
Returns XML:
```xml
<ocs>
  <meta><status>ok</status>...</meta>
  <data>
    <content details="full">
      <id>2134200</id>
      <name>Magna-Dark-Global-6</name>
      <typeid>722</typeid>
      <typename>Global Themes (Plasma 6)</typename>
      <description><![CDATA[ ...HTML with <a href> links... ]]></description>
      <downloadlink1>https://files06.pling.com/.../Magna-Dark-Global-6.tar.gz</downloadlink1>
      <downloadname1>Magna-Dark-Global-6.tar.gz</downloadname1>
      <downloadsize1>1216</downloadsize1>
      <downloadmd5sum1>e4ba493bcc9e323451b72c0c8ded4bb2</downloadmd5sum1>
      <downloadtags1>data##mimetype=application/gzip</downloadtags1>
      <!-- downloadlink2, downloadname2, ... if multiple official files -->
    </content>
  </data>
</ocs>
```

Key fields we use: `id`, `name`, `typeid`/`typename`, `description`,
and the numbered `downloadlink*`/`downloadname*`/`downloadmd5sum*`
series (an entry can have more than one official download — handle
1..N, not just 1).

**Extracting a content ID from a URL.** Both `store.kde.org/p/<id>`
and `pling.com/p/<id>` (and `www.pling.com/p/<id>/`) follow the same
shape: the numeric path segment after `/p/`. A single regex/URL-parse
handles all of them, since they're all OCS-network sites — meaning
this same code path is the "later: opendesktop.org" support already,
with no extra work.

## 3. Description parsing — finding companion content

Robin's observation from manually reviewing 10-20 store pages: authors
consistently write the description as a list of labeled links, e.g.

```html
Icons <b>Magna-Dark-Icons</b>: <a href="https://www.pling.com/p/2102240/">Here</a>
Dark Plasma Color Scheme <b>Magna-Violet-Dark-ColorScheme</b>: <a href="...">Here</a>
```

**Approach:** parse the description HTML (BeautifulSoup), and for
every `<a href>` pointing at a `/p/<id>` URL on a known OCS-network
domain, capture the surrounding text (the text node(s) immediately
before the link, within the same block/line) as a label. This gives us
a list of `(label_text, content_id)` pairs.

We do NOT try to perfectly classify each one from the label text alone
up front. Instead:

1. Extract all `(label, content_id)` pairs from the description.
2. For each one, do a real `content/data` lookup — the API response's
   own `typeid`/`typename` field tells us definitively what kind of
   content it is (color scheme, icon theme, Aurorae, font, wallpaper,
   SDDM theme, GTK theme, Kvantum, etc.). This is more reliable than
   text parsing alone, since it comes from the store's own
   categorization.
3. **Download and extract every discovered item, regardless of
   bucket.** Fetching into the archive/cache folder (section 7) always
   happens — there's no reason to skip downloading something just
   because we're not installing it yet. The user-chosen archive folder
   ends up being a complete local copy of everything the theme
   references, useful on its own even before v2 adds install support
   for the rest.
4. Bucket each discovered item by its real type, and only this step
   differs by bucket. Items in the v1-safe buckets (color scheme, icon
   theme, cursor theme, global theme, Aurorae, font, wallpaper) get
   copied into their live XDG install path automatically. Items typed
   as SDDM, GTK, or Kvantum are downloaded/extracted like everything
   else, but NOT copied into a live location — the summary screen
   notes they're "downloaded but not installed (v2)" along with where
   the extracted files already sit, so the user can install them by
   hand now or use v2 functionality once it exists.

This sidesteps fragile text-pattern matching (no need to regex-detect
the word "Icons" vs "Cursor" vs "Color Scheme") and instead trusts the
store's own type metadata, which is exactly what determines install
behavior anyway.

**Known KDE Store type IDs**, confirmed empirically against real
content from the Magna description set:

| typeid | typename                    | install bucket     |
|--------|------------------------------|---------------------|
| 722    | Global Themes (Plasma 6)     | auto-install        |
| 716    | Plasma 6 Splashscreens       | auto-install        |
| 104    | Plasma Themes                | auto-install        |
| 112    | Plasma Color Schemes         | auto-install (flat-file) |
| 132    | Full Icon Themes             | auto-install        |
| 107    | Cursors                      | auto-install        |
| 717    | Plasma 6 Window Decorations (Aurorae) | auto-install |
| 462    | Konsole Color Schemes        | auto-install (flat-file) |
| 299    | Wallpapers KDE Plasma        | auto-install        |
| 123    | Kvantum                      | auto-install (to ~/.config/Kvantum/) |
| 101    | SDDM Login Themes            | download only -- use scripts/install_sddm_theme.py |
| 135    | GTK3/4 Themes                | download only -- copy manually to ~/.local/share/themes/ |
| 121    | Global Themes (Plasma 5)     | incompatible -- not downloaded |
| 114    | Plasma Window Decorations (Plasma 5) | incompatible -- not downloaded |
| 488    | Plasma Splashscreens (Plasma 5) | incompatible -- not downloaded |
| (fonts) | not yet confirmed; no themes found that link to fonts as companions | TBD |

All typeids above confirmed empirically against real KDE Store content.
"flat-file" means the actual config file is copied directly into the
XDG directory with no wrapping subfolder (see implementation note in
section 5).

(Reminder: "download only" still means fully fetched and extracted
into the archive/cache folder — see section 3 point 3. The bucket only
controls whether we additionally copy it into a live XDG path.)

One lookup (`2102231`, a second color scheme) failed with an SSL
hostname-mismatch error against `api.kde-look.org` — transient/
server-side, not a code issue (the sibling ID 2102230 succeeded and
already confirms typeid 112). Worth being defensive about this class
of failure in the real client: retry once, then skip-and-report rather
than crash the whole batch if one companion item's lookup fails.

## 4. Install paths (Plasma 6, user-level, no sudo)

All XDG_DATA_HOME-relative, i.e. under `~/.local/share/` unless noted:

| Content type            | Install path                              |
|--------------------------|--------------------------------------------|
| Global Theme (look-and-feel) | `~/.local/share/plasma/look-and-feel/` |
| Plasma Style / Desktop Theme | `~/.local/share/plasma/desktoptheme/`  |
| Aurorae window decoration    | `~/.local/share/aurorae/themes/`       |
| Color scheme                 | `~/.local/share/color-schemes/`        |
| Icon theme                   | `~/.local/share/icons/` (or `~/.icons/`) |
| Cursor theme                  | `~/.local/share/icons/` (cursors are packaged like icon themes) |
| Fonts                         | `~/.local/share/fonts/`               |
| Wallpapers                    | `~/.local/share/wallpapers/`          |
| Konsole color scheme          | `~/.local/share/konsole/`             |

Each archive's internal structure usually already matches "one
top-level folder named after the theme" — extraction is mostly "unpack
into the right parent directory," not reshuffling files. We'll need to
sanity-check structure post-extraction (e.g. confirm a `metadata.json`
or `metadata.desktop` exists where expected) rather than assume.

**Display name extraction:** after extraction, read `metadata.json`
(Plasma 6 / KF6 standard) or fall back to legacy `metadata.desktop`,
and pull the `Name` field — this is the literal string that'll appear
in System Settings, which is exactly what we want to report back. For
content types without their own metadata file (e.g. fonts), we report
the family name read from the font file itself, or fall back to the
declared `downloadname`/store `name` field.

## 5. Known risks / non-goals (read before automating too eagerly)

The ArchWiki has a blunt warning worth taking seriously: global themes
can contain arbitrary scripts, and the warning explicitly notes that
loss of user data has occurred from malicious or buggy ones. This tool
automates *fetching and placing files a user already chose to
install* — it does not vet, sandbox, or audit theme contents. That
risk exists identically whether installed by hand or by this tool; the
tool doesn't add new risk, but it also doesn't reduce it, and that's
worth being upfront about, especially since the eventual UI will make
installing a dozen items as easy as installing one.

**Implementation note: archive extraction and absolute symlinks.**
Real-world icon theme archives commonly contain symlinks with absolute
paths left over from the original packager's filesystem (e.g. a
"mimetypes/22/application-x-vdi-disk.svg" symlinked to
"/home/packager/icons/.../foo.svg"). Python's default tar extraction
filter (PEP 706, the secure default since 3.12) correctly refuses to
follow these as written, since an absolute symlink target is
indistinguishable from a real attack at face value. We handle this in
fetch_and_extract.py by: (1) checking whether the target, once
resolved, happens to land inside our own destination directory --
rare but possible; (2) failing that, searching the archive's own
member list for a file whose trailing path matches the symlink
target's trailing path, and if exactly one match is found, treating
it as an internal alias and rewriting the link as relative; (3) if
neither resolves, skipping just that one symlink (recorded in
FetchResult.skipped_entries for the summary/manifest) rather than
aborting the whole archive's extraction. Genuine path traversal
attempts (`../`-style entries trying to write outside the destination)
are still hard-rejected via FetchError regardless of this handling.
This was discovered and fixed against a real download (Magna-Dark-
Icons, a 6MB/20k-file icon pack) during testing, not anticipated in
the original design -- worth keeping in mind that other companion
content (especially other icon packs and cursor themes, which use the
same symlink-heavy aliasing convention) will exercise this same path.

**Implementation note: not every download is an archive.** Discovered
while testing color scheme installation: some content types (color
schemes in particular) are served as a single bare `.colors` file with
no wrapping archive at all -- `_detect_archive_format` correctly
identifies these as "unknown" (not a zip or tar), and
`fetch_and_extract.py` now handles this by copying the file directly
into the cache's `extracted/` folder rather than trying and failing to
extract it. `installer.py`'s `_find_content_root` and the metadata
readers already handled this shape correctly (a loose file directly in
`extracted_dir` rather than a wrapping folder), since that logic was
written generically rather than assuming a wrapper always exists --
but worth flagging that the "always an archive" assumption baked into
early fetch_and_extract.py was wrong and is a useful pattern to expect
again from other small, single-file content types (fonts in particular
seem likely to hit this too, once we test against a real one).

**Real bug found via end-to-end usage testing: flat-file types need
flat installs, not folder copies.** While testing the GUI against a
live theme (Amy-Light-Global-6), the Konsole color scheme didn't show
up in Konsole's scheme picker after a successful "installed"
confirmation in the summary screen. Root cause: `install_content` had
one universal copy strategy (preserve whatever folder structure
`extracted/` contained, including any wrapping folder), which is
correct for KPackage-style content (Global Themes, Plasma styles, icon
themes, Aurorae, splashscreens) but wrong for Plasma color schemes
(typeid 112) and Konsole color schemes (typeid 462) -- both of these
are read by consumers (System Settings' color picker, Konsole's
Appearance tab) that only scan the immediate XDG directory for
matching files and never recurse into subfolders. When the source
archive happened to extract with a wrapping folder (as Amy-Light's did
-- `Amy-Light-Konsole/Amy-Light-Konsole.colorscheme`), the installed
file ended up one level too deep to ever be found, despite the install
"succeeding" with no error. Fixed by splitting install logic into two
paths: `FLAT_FILE_TYPEIDS` (112, 462) now locate the real config
file(s) anywhere under `extracted/` via recursive glob and copy them
directly into the XDG directory with no wrapper, regardless of how the
source archive was structured; everything else keeps the original
folder-copy behavior. Also added a proper Konsole `.colorscheme` name
reader (`[General] Description=`, confirmed against Konsole's own
source rather than guessed) since the earlier `.colors`-only reader
didn't recognize the Konsole format and was silently falling back to
the generic store name. Worth treating this as a signal that other
typeids we haven't deeply tested yet (cursor themes, fonts, once their
typeids are confirmed) should be checked against their real consumer's
actual file-discovery behavior before assuming the folder-copy
default is correct for them too.

## 6. High-level flow

```
User pastes store URL
        |
        v
Extract content ID -> GET content/data/{id}
        |
        v
Show: name, typename, screenshot(s), description (cleaned)
        |
        v
Parse description -> find /p/<id> links -> resolve each via content/data
        |
        v
Bucket by typeid: [auto-install list] vs [download-only / v2 list]
        |
        v
User confirms (shows full list before doing anything)
        |
        v
Download EVERY discovered item's archive into the cache folder
(primary + all companions, both buckets — verify md5sum if provided)
        |
        v
Extract every item into the cache folder's extracted/ subdir
        |
        v
Copy auto-install-bucket items from extracted/ into their correct
~/.local/share/... path. Download-only items stay in the cache folder,
not copied anywhere live.
        |
        v
Read metadata from each extracted item -> collect display names
        |
        v
Summary screen:
  - "Magna-Dark-Global-6" installed -> System Settings > Appearance > Global Theme
  - "Magna-Violet-Dark-ColorScheme" installed -> ... > Colors
  - ... etc
  - "Magna-SDDM-6" downloaded but not installed -> files at <cache>/Magna-SDDM-6/extracted/
                       run: python3 scripts/install_sddm_theme.py <cache>/Magna-SDDM-6/extracted/Magna-SDDM-6
  - "Magna-Dark-GTK" downloaded but not installed -> copy manually to ~/.local/share/themes/
  - "Magna-Dark-Kvantum" installed -> ~/.config/Kvantum/Magna-Dark-Kvantum/
```

## 7. GUI shape (as built)

Single-window Tkinter app, four-screen flow:

**Screen 1: URL entry**
- Theme URL text field (paste any store.kde.org / pling.com /
  opendesktop.org /p/<id> URL).
- Downloads folder picker (the one user-configurable path -- everything
  installs to fixed XDG locations, but raw/extracted archives land here
  organized as `<downloads-root>/<theme-name>/<companion-name>/raw|extracted/`).
- Fetch button (disables itself on click to prevent double-submission,
  re-enables on failure). Fetch runs on a background thread so the UI
  stays responsive across ~20 OCS API calls.

**Screen 2: Selection list**
- Scrollable list of the primary theme + all discovered companions.
  Mousewheel scrolling wired up cross-platform (Linux Button-4/5,
  Windows/macOS MouseWheel).
- Each row shows: name (bold), type, a "Click to preview" hint if
  preview images are available. Clicking anywhere on the row opens
  the preview popup (see below).
- Auto-install-bucket items: checkbox pre-checked, cursor changes to
  hand pointer.
- Download-only items (SDDM, GTK): checkbox unchecked/disabled,
  orange warning text, "View on store" link.
- Incompatible items (Plasma 5 content): red warning text, no
  checkbox, not downloaded or installed at all.
- "< Back" and "Download && Install Selected" buttons.

**Preview popup (modal)**
- Opened by clicking any row that has preview images.
- Horizontally scrollable gallery showing all `previewpic1..N` images,
  loaded async (PIL decode off-thread, ImageTk.PhotoImage constructed
  on main thread -- see threading safety note below).
- Description text below the gallery, HTML-stripped and readable.
- "View on Store" link + "Close" button.
- Multiple popups can be open simultaneously (one per clicked item).

**Screen 3: Progress**
- Indeterminate progress bar.
- Scrolling log pane showing `[stage] detail` lines in real time via
  a `queue.Queue` polled every 150ms from the main thread.
- Download/install runs entirely on a background thread. Progress
  polling uses `root.after()` and guards against the race condition
  where the pipeline finishes and navigates to the summary screen
  while a poll is still pending (widget-existence check + explicit
  `after_cancel()` on screen transition).

**Screen 4: Summary**
- Read-only text widget (left in NORMAL state, not DISABLED, so text
  is selectable/copyable -- Tkinter's DISABLED state blocks copy).
- "Copy Summary" button (copies to system clipboard via
  `root.clipboard_append()`).
- "Install Another Theme" (resets state, returns to Screen 1,
  remembers the downloads folder choice).
- "Quit".

**Install locations are fixed, not user-chosen.** Every content type
always installs to its correct XDG subpath, respecting `$XDG_DATA_HOME`
and `$XDG_CONFIG_HOME` if set. The only user-chosen path is the
downloads-root folder where raw archives and extracted content are
cached, organized per theme run:

```
<downloads-root>/
  <Theme-Name>/                   one folder per theme install run
    <PrimaryTheme>/
      raw/                        original downloaded archive
      extracted/                  unpacked tree
      manifest.json               what was installed, where, md5s
    <CompanionName>/
      raw/...
      extracted/...
      manifest.json
```

**Threading safety note (Tkinter).** Any `ImageTk.PhotoImage`
construction must happen on the main thread -- creating one off a
worker thread is undefined behavior that silently fails in a
timing-dependent way. All background threads (fetch worker, pipeline
worker, preview image loaders) produce plain Python data (bytes, PIL
Images) and hand results to the main thread via `root.after(0, ...)`.
This was discovered as a real bug: one out of ~20 thumbnails
consistently failed to render despite the image data being fine,
diagnosed via a standalone diagnostic script.

## 8. Open questions before implementation

0. ~~Base folder vs fixed install paths~~ — resolved.
1. ~~Confirm typeid values~~ — resolved for all types except fonts
   (typeid unknown; no themes found that link to fonts as companions).
   Cursor themes confirmed as typeid 107, installing to
   `~/.local/share/icons/` same as icon themes. See table in section 3.
2. ~~Confirm description HTML is "clean enough"~~ — confirmed across
   many real themes during live testing. Parser handles the common
   `<br>`-separated list format correctly.
3. ~~Archive format handling~~ — resolved: `.tar.gz`, `.tar.xz`, and
   bare non-archive files (e.g. lone `.colors` files) all handled.
   `.7z` has not been seen in practice; would fall through to the
   "unknown format / copy as-is" path if encountered.
