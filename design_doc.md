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
| 104    | Plasma Themes                | auto-install        |
| 112    | Plasma Color Schemes         | auto-install        |
| 132    | Full Icon Themes             | auto-install        |
| 717    | Plasma 6 Window Decorations (Aurorae) | auto-install |
| 462    | Konsole Color Schemes        | auto-install        |
| 299    | Wallpapers KDE Plasma         | auto-install        |
| (cursor themes — typeid not yet confirmed, likely grouped under or near Icon Themes; verify before assuming) | | auto-install |
| (fonts — typeid not yet confirmed; none linked from this sample theme) | | auto-install |
| 101    | SDDM Login Themes            | download only — v2 install |
| 135    | GTK3/4 Themes                | download only — v2 install |
| 123    | Kvantum                      | download only — v2 install (see note below) |

(Reminder: "download only" still means fully fetched and extracted
into the archive/cache folder — see section 3 point 3. The bucket only
controls whether we additionally copy it into a live XDG path.)

Note on Kvantum: Robin has Kvantum installed, so the only reason it's
not in the auto-install bucket is that the install location
(`~/.config/Kvantum/`) hasn't been worked out/tested yet, not a
"may not apply to this system" concern like with GTK. Good v2
candidate to tackle early once v1 is stable, since it's otherwise
identical in risk profile to the rest of the auto-install set.

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
  - "Magna-SDDM-6" downloaded but not installed (v2 feature) -> files at <cache>/Magna-SDDM-6/extracted/
  - "Magna-Dark-GTK" downloaded but not installed (v2 feature) -> files at <cache>/Magna-Dark-GTK/extracted/
  - "Magna-Dark-Kvantum" downloaded but not installed (v2 feature) -> files at <cache>/Magna-Dark-Kvantum/extracted/
```

## 7. GUI shape (sketch, not final)

Simple single-window Tkinter app, three-pane flow rather than
multi-window wizard:

- URL input + "Fetch" button at top.
- Middle: scrollable list of discovered content (primary + all
  description links), each row showing name/type/checkbox. All items
  are downloaded regardless of checkbox state; the checkbox controls
  install only. Auto-install-bucket items pre-checked; download-only
  (v2) items shown unchecked/disabled with a note that they'll be
  fetched but not installed, plus a "view on store" link.
- "Download & Install Selected" button.
- Bottom: log/status pane showing download/extract progress per item,
  ending in the summary list described above.

**Install locations are fixed, not user-chosen.** Every content type
always installs to its correct `~/.local/share/...` subpath (per the
table in section 4), respecting `$XDG_DATA_HOME` if set. This isn't
configurable — there's no benefit to letting the user pick a custom
install location, since System Settings only looks in the standard
XDG paths.

**The one user-chosen folder is a "downloads archive" location** — a
plain folder picker, used purely as a local cache/record of everything
fetched, organized per-theme:

```
<user-chosen-base>/
  Magna-Dark-Global-6/
    raw/
      Magna-Dark-Global-6.tar.gz          (original downloaded archive)
    extracted/
      ...                                  (unpacked tree, pre-install)
    manifest.json                          (what was found, what was
                                             installed, where, display
                                             names, timestamps, md5s)
  Magna-Violet-Dark-ColorScheme/
    raw/...
    extracted/...
    manifest.json
```

This gives a clean audit trail and means a theme can be reinstalled or
inspected later without re-fetching, without that folder being
involved in the actual install process at all (install always reads
from `extracted/` and copies to the fixed XDG path; the archive folder
is bookkeeping, not a working directory Plasma cares about).

**Real bug found via end-to-end usage testing: ImageTk.PhotoImage must
be constructed on the main thread.** While testing thumbnails against
a live theme, one out of ~20 real items (Magna-Blur-Dark-Konsole)
consistently failed to show its thumbnail even though a standalone
diagnostic confirmed the OCS API had the preview URL, the URL returned
valid PNG data, and PIL could decode it fine -- ruling out every data-
layer explanation. The actual cause: `_load_thumbnail_async`'s worker
thread was constructing the `ImageTk.PhotoImage` itself before handing
it to the main thread via `root.after()`. Tk is not thread-safe, and
creating a `PhotoImage` off the main thread is undefined behavior --
it can silently fail, "usually" happen to work, or corrupt depending
on timing, which explains why it was flaky/item-dependent rather than
consistently broken. Fixed by restructuring so the worker thread only
does the network fetch and PIL decode (`Image.open()` /
`.thumbnail()` / `.load()` to force eager decoding), and defers the
actual `ImageTk.PhotoImage()` construction to `_apply_thumbnail`,
which runs via `root.after(0, ...)` and is therefore guaranteed to
execute on the main thread. General lesson for any future Tkinter work
in this codebase: ANY direct Tk/PhotoImage object construction must
happen on the main thread, even if it looks like "just creating an
object" rather than an obvious widget mutation -- background threads
should only ever produce plain Python data (bytes, PIL Images, etc)
and hand that across via `root.after()`, never Tk objects themselves.

**v2 candidate, discovered from real usage feedback:** the current
flow shows a small thumbnail per item but only after the selection
list has rendered (loaded async, one network request per item). A
better flow would let the user click an item BEFORE confirming
download/install and see a popup with the full preview gallery
(`previewpic1..N`, all of which `ocs_client.ContentEntry` already
captures, not just the first one we currently fetch) so they can make
an informed choice -- especially useful for picking between several
similar-sounding options (e.g. multiple color schemes or Konsole
schemes whose names alone don't convey the visual difference). This is
a real, separate interaction (a details/preview popup), not a small
tweak to the current screen, so it's being tracked as a v2 item rather
than folded into the v1 GUI.

## 8. Open questions before implementation

0. ~~Base folder vs fixed install paths~~ — resolved: install paths
   are always the fixed XDG ones; the user only picks where the
   downloads/extracted-archive cache lives.
1. ~~Confirm typeid values~~ — mostly resolved (see table in section
   3). Remaining gaps: cursor theme typeid and font typeid weren't
   present in our test sample and still need confirming against a
   theme that links to those specifically. Also re-confirm the one
   failed lookup (color scheme typeid 112 is already confirmed via a
   sibling ID, so this is low priority).
2. Confirm description HTML is "clean enough" across a wider sample —
   Robin's spot-check of 10-20 pages is a good sign, but worth
   stress-testing the parser against a few with unusual formatting
   before assuming it's universal.
3. Decide on archive format handling — we've seen `.tar.gz` so far;
   need to confirm whether `.zip` and `.7z` also show up in practice
   (the OCS `downloadtags`/mimetype field should tell us per-file, so
   this is a "support multiple, detect from response" problem, not a
   design blocker).
