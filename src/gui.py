"""
gui.py

Tkinter GUI for the KDE Store theme installer, built on top of
pipeline.py. Per design doc section 7:

  - URL input + Fetch
  - A selection screen showing primary + all discovered companions,
    each with a thumbnail/name/type/checkbox. Auto-install items
    pre-checked; download-only (v2) items shown but unchecked/
    disabled with a note + store link.
  - A user-chosen "downloads root" folder (downloads_root) -- this is
    the ONLY user-configurable path; install locations are always the
    fixed XDG ones. Each theme install gets its own named subfolder
    underneath downloads_root (theme_cache_dir, derived from the
    primary entry's real name once known), so every companion item's
    raw/extracted files for one theme land together in one place
    instead of dozens of sibling folders accumulating flat in
    downloads_root across multiple theme installs over time.
  - Download & Install button, kicking off the full pipeline run on a
    background thread (so the UI doesn't freeze across ~20+ API calls
    and several MB of downloads) with live progress feedback.
  - A final summary screen.

This module intentionally does very little "thinking" of its own --
almost all real logic (fetching, bucketing, installing, error
handling) lives in pipeline.py and the modules it orchestrates. The
GUI's job is to collect input, kick off run_pipeline() correctly, and
render PipelineResult / progress callbacks as they arrive.
"""

from __future__ import annotations

import queue
import threading
import webbrowser
from pathlib import Path
from tkinter import (
    Tk, Toplevel, Frame, Label, Button, Entry, Checkbutton, IntVar, StringVar,
    Canvas, Scrollbar, Text, filedialog, messagebox, ttk, END, BOTH, X, Y,
    LEFT, RIGHT, TOP, BOTTOM, W, E, N, S, NW, DISABLED, NORMAL, WORD,
)
from typing import Optional

try:
    from PIL import Image, ImageTk
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

import ocs_client
import companion_finder
import pipeline


APP_TITLE = "KDE Store Theme Installer"
WINDOW_MIN_SIZE = (760, 560)


class SelectableItem:
    """Bridges a resolved companion_finder/pipeline-stage item to a
    Tkinter IntVar checkbox state for the selection screen, before
    we've actually run the download/install pipeline. Built from the
    discovery step (companion_finder.resolve_companions), not from a
    PipelineResult -- that comes later, after the user confirms."""

    def __init__(self, entry: ocs_client.ContentEntry, label: str, bucket: str):
        self.entry = entry
        self.label = label
        self.bucket = bucket
        is_auto = bucket == companion_finder.BUCKET_AUTO_INSTALL
        self.is_incompatible = bucket == companion_finder.BUCKET_INCOMPATIBLE
        self.install_var = IntVar(value=1 if is_auto else 0)
        self.is_editable = is_auto


class App:
    def __init__(self, root: Tk):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.minsize(*WINDOW_MIN_SIZE)

        self.downloads_root: Optional[Path] = None
        # The user-chosen top-level downloads folder (e.g.
        # ~/kde-theme-downloads). Each theme install gets its own
        # named subfolder underneath this -- see theme_cache_dir below
        # -- so installing multiple themes over time doesn't dump
        # dozens of sibling companion folders flat into one directory
        # with no grouping by which theme they belonged to.
        self.theme_cache_dir: Optional[Path] = None
        # The actual folder passed to fetch_and_extract() for this
        # run: downloads_root / <safe theme name>. Set once the
        # primary entry's real name is known (after fetch succeeds).
        self.primary_url: str = ""
        self.primary_entry: Optional[ocs_client.ContentEntry] = None
        self.provider_base: Optional[str] = None
        self.selectable_items: list[SelectableItem] = []
        self.progress_queue: "queue.Queue[tuple[str, str]]" = queue.Queue()
        self.pipeline_result: Optional[pipeline.PipelineResult] = None
        self._poll_after_id: Optional[str] = None
        # Tracks the pending root.after() id for _poll_progress_queue,
        # so _clear_screen can cancel it outright when navigating away
        # from the progress screen, instead of relying solely on the
        # widget-existence check inside the poller itself.

        self.container = Frame(root)
        self.container.pack(fill=BOTH, expand=True)

        self.current_frame: Optional[Frame] = None
        self._show_url_entry_screen()

    # ---- screen management -------------------------------------------------

    def _clear_screen(self):
        self._unbind_mousewheel()
        self._cancel_pending_poll()
        if self.current_frame is not None:
            self.current_frame.destroy()
            self.current_frame = None

    def _cancel_pending_poll(self):
        if self._poll_after_id is not None:
            try:
                self.root.after_cancel(self._poll_after_id)
            except Exception:
                pass
            self._poll_after_id = None

    def _bind_mousewheel(self, canvas: Canvas):
        """
        Wire up mousewheel scrolling for a Canvas. Tkinter doesn't do
        this automatically -- dragging the scrollbar works out of the
        box, but the mousewheel needs explicit event bindings, and the
        event name/delta semantics differ by platform:
          - Windows/macOS: <MouseWheel>, event.delta is +/-120 per notch
          - Linux (X11): <Button-4> (up) / <Button-5> (down), no delta
        We bind globally (bind_all) while this screen is visible so
        the wheel works no matter which child widget has focus, not
        just when the mouse is directly over the canvas itself --
        otherwise scrolling would only work if the cursor happened to
        be exactly over empty canvas space, not over a row's text.
        Unbound in _clear_screen so it doesn't leak onto other screens.
        """
        def on_mousewheel_windows_mac(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        def on_mousewheel_linux_up(event):
            canvas.yview_scroll(-1, "units")

        def on_mousewheel_linux_down(event):
            canvas.yview_scroll(1, "units")

        self.root.bind_all("<MouseWheel>", on_mousewheel_windows_mac)
        self.root.bind_all("<Button-4>", on_mousewheel_linux_up)
        self.root.bind_all("<Button-5>", on_mousewheel_linux_down)

    def _unbind_mousewheel(self):
        self.root.unbind_all("<MouseWheel>")
        self.root.unbind_all("<Button-4>")
        self.root.unbind_all("<Button-5>")

    # ---- Screen 1: URL entry ------------------------------------------------

    def _show_url_entry_screen(self):
        self._clear_screen()
        frame = Frame(self.container, padx=20, pady=20)
        frame.pack(fill=BOTH, expand=True)
        self.current_frame = frame

        Label(
            frame, text=APP_TITLE, font=("", 16, "bold")
        ).pack(anchor=W, pady=(0, 4))
        Label(
            frame,
            text=(
                "Paste a KDE Store / Pling theme URL below (e.g. "
                "https://store.kde.org/p/2134200). We'll fetch its "
                "details and look for companion content (icons, color "
                "schemes, etc.) referenced in its description."
            ),
            wraplength=680, justify=LEFT,
        ).pack(anchor=W, pady=(0, 16))

        url_frame = Frame(frame)
        url_frame.pack(fill=X, pady=(0, 8))
        Label(url_frame, text="Theme URL:").pack(side=LEFT, padx=(0, 8))
        self.url_entry = Entry(url_frame, width=60)
        self.url_entry.pack(side=LEFT, fill=X, expand=True)
        self.url_entry.focus_set()

        cache_frame = Frame(frame)
        cache_frame.pack(fill=X, pady=(8, 16))
        Label(cache_frame, text="Downloads folder:").pack(side=LEFT, padx=(0, 8))
        if not hasattr(self, "cache_path_var"):
            # Only created once, on first launch -- this screen is
            # rebuilt from scratch on every visit (including after
            # "Install Another Theme"), and recreating the StringVar
            # here every time would silently reset the user's chosen
            # downloads folder back to the hardcoded default after
            # every single theme install, which defeats the point of
            # remembering it.
            self.cache_path_var = StringVar(value=str(Path.home() / "kde-theme-downloads"))
        Entry(
            cache_frame, textvariable=self.cache_path_var, width=44
        ).pack(side=LEFT, fill=X, expand=True, padx=(0, 8))
        Button(
            cache_frame, text="Browse...", command=self._browse_cache_folder
        ).pack(side=LEFT)

        self.status_label = Label(frame, text="", fg="#a00")
        self.status_label.pack(anchor=W, pady=(0, 8))

        self.fetch_button = Button(
            frame, text="Fetch", command=self._on_fetch_clicked, width=14
        )
        self.fetch_button.pack(anchor=W)

    def _browse_cache_folder(self):
        chosen = filedialog.askdirectory(
            initialdir=self.cache_path_var.get() or str(Path.home())
        )
        if chosen:
            self.cache_path_var.set(chosen)

    def _on_fetch_clicked(self):
        url = self.url_entry.get().strip()
        if not url:
            self.status_label.config(text="Please enter a theme URL.")
            return

        if ocs_client.extract_content_id(url) is None:
            self.status_label.config(
                text="That doesn't look like a valid KDE Store / Pling content URL "
                     "(expected something with /p/<number> in it)."
            )
            return

        cache_path_str = self.cache_path_var.get().strip()
        if not cache_path_str:
            self.status_label.config(text="Please choose a downloads folder.")
            return

        self.primary_url = url
        self.downloads_root = Path(cache_path_str).expanduser()
        self.status_label.config(text="Fetching...", fg="#444")
        self.fetch_button.config(state=DISABLED)
        self.root.update_idletasks()

        # Fetching the primary entry + resolving companions involves
        # ~20 network calls -- run it off the UI thread so the window
        # stays responsive, then hand back to the main thread to build
        # the selection screen.
        threading.Thread(target=self._fetch_worker, args=(url,), daemon=True).start()

    def _fetch_worker(self, url: str):
        try:
            provider_base = ocs_client.get_provider_base_url()
            primary_entry = ocs_client.get_content_from_url(url, provider_base=provider_base)
            companions, failures = companion_finder.find_and_resolve_companions(
                primary_entry.description_html, provider_base=provider_base
            )
        except ocs_client.OcsError as exc:
            self.root.after(0, self._on_fetch_failed, str(exc))
            return
        except Exception as exc:  # noqa: BLE001 -- background thread,
            # see _run_pipeline_worker's comment for why this is
            # deliberately broad rather than letting an unexpected
            # error silently kill the thread.
            self.root.after(0, self._on_fetch_failed, f"unexpected error: {exc}")
            return

        self.root.after(0, self._on_fetch_succeeded, provider_base, primary_entry, companions, failures)

    def _on_fetch_failed(self, error_message: str):
        self.status_label.config(
            text=f"Couldn't fetch that theme: {error_message}", fg="#a00"
        )
        self.fetch_button.config(state=NORMAL)

    def _on_fetch_succeeded(
        self,
        provider_base: str,
        primary_entry: ocs_client.ContentEntry,
        companions: list[companion_finder.ResolvedCompanion],
        failures: list[companion_finder.FailedCompanion],
    ):
        self.provider_base = provider_base
        self.primary_entry = primary_entry

        # Every companion in this run shares one folder, named after
        # the primary theme, nested under the user's chosen downloads
        # root -- e.g. ~/kde-theme-downloads/Amy-Light-Global-6/ holds
        # all of Amy's companions' raw/extracted subfolders together,
        # rather than ~20 companion folders sitting flat alongside
        # whatever the next theme install adds later.
        from fetch_and_extract import safe_dirname
        self.theme_cache_dir = self.downloads_root / safe_dirname(primary_entry.name)

        primary_bucket = companion_finder.bucket_for_typeid(primary_entry.typeid)
        self.selectable_items = [
            SelectableItem(primary_entry, primary_entry.name, primary_bucket)
        ]
        for resolved in companions:
            self.selectable_items.append(
                SelectableItem(resolved.entry, resolved.link.label, resolved.bucket)
            )

        self._companion_lookup_failures = failures
        self._show_selection_screen()

    # ---- Screen 2: selection -------------------------------------------------

    def _show_selection_screen(self):
        self._clear_screen()
        frame = Frame(self.container, padx=16, pady=16)
        frame.pack(fill=BOTH, expand=True)
        self.current_frame = frame

        header = Frame(frame)
        header.pack(fill=X, pady=(0, 8))
        Label(
            header, text=f"Found {len(self.selectable_items)} item(s) for "
                         f"{self.primary_entry.name!r}",
            font=("", 13, "bold"),
        ).pack(anchor=W)
        Label(
            header,
            text=(
                "Everything below will be downloaded. Checked items will "
                "also be installed to the correct system location. "
                "Items KDE Plasma can't auto-install yet (SDDM, GTK, "
                "Kvantum) are downloaded only -- you can install those "
                "by hand later from the downloads folder."
            ),
            wraplength=700, justify=LEFT, fg="#555",
        ).pack(anchor=W, pady=(4, 0))

        if self._companion_lookup_failures:
            warn_text = (
                f"Note: {len(self._companion_lookup_failures)} link(s) in the "
                f"description couldn't be looked up and will be skipped."
            )
            Label(header, text=warn_text, fg="#a60").pack(anchor=W, pady=(4, 0))

        # Scrollable list area
        list_outer = Frame(frame)
        list_outer.pack(fill=BOTH, expand=True, pady=(12, 12))

        canvas = Canvas(list_outer, highlightthickness=0)
        scrollbar = Scrollbar(list_outer, orient="vertical", command=canvas.yview)
        scrollable_frame = Frame(canvas)

        scrollable_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        canvas.create_window((0, 0), window=scrollable_frame, anchor=NW)
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side=LEFT, fill=BOTH, expand=True)
        scrollbar.pack(side=RIGHT, fill=Y)

        self._bind_mousewheel(canvas)

        for item in self.selectable_items:
            self._build_item_row(scrollable_frame, item)

        # Footer buttons
        footer = Frame(frame)
        footer.pack(fill=X)
        Button(
            footer, text="< Back", command=self._show_url_entry_screen
        ).pack(side=LEFT)
        Button(
            footer, text="Download && Install Selected",
            command=self._on_confirm_clicked,
        ).pack(side=RIGHT)

    def _build_item_row(self, parent: Frame, item: SelectableItem):
        row = Frame(parent, pady=6, relief="groove", borderwidth=1)
        row.pack(fill=X, padx=2, pady=3)

        def open_preview(e=None):
            if item.entry.preview_image_urls:
                self._open_preview_popup(item)
        row.bind("<Button-1>", open_preview)
        row.configure(cursor="hand2" if item.entry.preview_image_urls else "")

        text_frame = Frame(row, padx=10)
        text_frame.pack(side=LEFT, fill=X, expand=True)
        text_frame.bind("<Button-1>", open_preview)

        name_label = Label(
            text_frame, text=item.label, font=("", 10, "bold"), anchor=W
        )
        name_label.pack(anchor=W)
        name_label.bind("<Button-1>", open_preview)

        type_label = Label(
            text_frame, text=item.entry.typename, fg="#666", anchor=W
        )
        type_label.pack(anchor=W)
        type_label.bind("<Button-1>", open_preview)

        if item.entry.preview_image_urls:
            hint = Label(text_frame, text="Click to preview", fg="#888",
                         font=("", 8), anchor=W)
            hint.pack(anchor=W)
            hint.bind("<Button-1>", open_preview)

        if item.is_incompatible:
            warn = Label(
                text_frame,
                text="⚠ Incompatible with Plasma 6 -- will not be downloaded or installed.",
                fg="#a00", anchor=W,
            )
            warn.pack(anchor=W)
            warn.bind("<Button-1>", open_preview)
        elif not item.is_editable:
            dl_label = Label(
                text_frame,
                text="Download only -- automatic install not yet supported for this type.",
                fg="#a60", anchor=W,
            )
            dl_label.pack(anchor=W)
            dl_label.bind("<Button-1>", open_preview)
            store_url = item.entry.homepage or f"https://store.kde.org/p/{item.entry.content_id}"
            link = Label(text_frame, text="View on store", fg="#06c", cursor="hand2")
            link.pack(anchor=W)
            link.bind("<Button-1>", lambda e, u=store_url: webbrowser.open(u))

        checkbox_frame = Frame(row)
        checkbox_frame.pack(side=RIGHT, padx=10)
        cb = Checkbutton(
            checkbox_frame, variable=item.install_var,
            state=NORMAL if item.is_editable else DISABLED,
        )
        cb.pack()

    # ---- Preview popup --------------------------------------------------------

    def _open_preview_popup(self, item: SelectableItem):
        """
        Open a Toplevel window showing the full preview gallery for an
        item, plus its cleaned description text. Purely informational --
        no install action from here. Multiple popups can be open at
        once (one per item clicked), each is fully independent.
        """
        popup = Toplevel(self.root)
        popup.title(item.entry.name)
        popup.geometry("680x560")
        popup.minsize(600, 500)
        popup.update_idletasks()  # force the window to actually render
                                  # before grab_set() -- calling grab on
                                  # a not-yet-viewable window raises
                                  # TclError: grab failed: window not viewable
        popup.grab_set()

        # Header
        header = Frame(popup, padx=16, pady=12)
        header.pack(fill=X)
        Label(
            header, text=item.entry.name, font=("", 13, "bold"), anchor=W
        ).pack(anchor=W)
        Label(
            header, text=item.entry.typename, fg="#666", anchor=W
        ).pack(anchor=W)

        # Image gallery -- horizontally scrollable strip of previews
        gallery_outer = Frame(popup, padx=16)
        gallery_outer.pack(fill=X)

        gallery_canvas = Canvas(gallery_outer, height=220, highlightthickness=0)
        h_scrollbar = Scrollbar(
            gallery_outer, orient="horizontal", command=gallery_canvas.xview
        )
        gallery_frame = Frame(gallery_canvas)
        gallery_frame.bind(
            "<Configure>",
            lambda e: gallery_canvas.configure(
                scrollregion=gallery_canvas.bbox("all")
            ),
        )
        gallery_canvas.create_window((0, 0), window=gallery_frame, anchor=NW)
        gallery_canvas.configure(xscrollcommand=h_scrollbar.set)
        gallery_canvas.pack(fill=X)
        h_scrollbar.pack(fill=X)

        # Placeholder labels -- one per preview image URL -- images
        # load async and swap in as they arrive.
        PREVIEW_SIZE = (300, 200)
        popup._preview_images = []  # keep PhotoImage refs alive on the popup

        for i, url in enumerate(item.entry.preview_image_urls):
            placeholder = Label(
                gallery_frame, width=38, height=12, bg="#ddd",
                text=f"Loading {i+1}...", compound="center", fg="#999",
            )
            placeholder.pack(side=LEFT, padx=4, pady=4)
            self._load_preview_image_async(url, placeholder, popup, PREVIEW_SIZE)

        if not item.entry.preview_image_urls:
            Label(
                gallery_frame, text="No preview images available.",
                fg="#888", padx=8, pady=8,
            ).pack(side=LEFT)

        # Description (HTML-stripped)
        if item.entry.description_html:
            desc_frame = Frame(popup, padx=16)
            desc_frame.pack(fill=BOTH, expand=True, pady=(0, 8))
            Label(
                desc_frame, text="Description", font=("", 10, "bold"), anchor=W
            ).pack(anchor=W, pady=(8, 4))
            desc_text = Text(
                desc_frame, wrap=WORD, height=8,
                relief="flat", bg=popup.cget("bg"),
            )
            desc_text.pack(fill=BOTH, expand=True)
            cleaned = self._strip_html(item.entry.description_html)
            desc_text.insert(END, cleaned)
            desc_text.configure(state=DISABLED)

        # Footer
        footer = Frame(popup, padx=16, pady=12)
        footer.pack(fill=X)
        store_url = item.entry.homepage or f"https://store.kde.org/p/{item.entry.content_id}"
        Button(
            footer, text="View on Store",
            command=lambda u=store_url: webbrowser.open(u),
        ).pack(side=LEFT)
        Button(
            footer, text="Close", command=popup.destroy
        ).pack(side=RIGHT)

    def _load_preview_image_async(
        self,
        url: str,
        label_widget: Label,
        popup: Toplevel,
        size: tuple[int, int],
    ):
        """Load a single preview gallery image off-thread. Same
        threading-safety rules apply as for all Tkinter image loading:
        PIL decode in the worker, PhotoImage construction on the main
        thread (see _apply_preview_image)."""
        if not PIL_AVAILABLE:
            label_widget.configure(text="(PIL not installed)", fg="#a00")
            return

        def worker():
            try:
                import requests
                import io
                resp = requests.get(url, timeout=15)
                if resp.status_code != 200:
                    return
                image = Image.open(io.BytesIO(resp.content))
                image.thumbnail(size)
                image.load()
            except Exception:
                return
            self.root.after(
                0, self._apply_preview_image, image, label_widget, popup
            )

        threading.Thread(target=worker, daemon=True).start()

    def _apply_preview_image(
        self,
        pil_image,
        label_widget: Label,
        popup: Toplevel,
    ):
        """Apply a loaded preview image to its placeholder label.
        PhotoImage constructed here on the main thread -- Tk is not
        thread-safe and PhotoImage must never be created off the main
        thread, even if it looks like "just creating an object"."""
        if not self._widget_exists(popup) or not self._widget_exists(label_widget):
            return  # popup was closed before the image finished loading
        try:
            photo = ImageTk.PhotoImage(pil_image)
            popup._preview_images.append(photo)  # keep reference alive
            label_widget.configure(
                image=photo, text="", width=pil_image.width, height=pil_image.height
            )
        except Exception:
            pass

    @staticmethod
    def _strip_html(html: str) -> str:
        """Very lightweight HTML tag stripper for description display.
        We don't need a full parser here -- just remove tags and
        decode the most common HTML entities, then collapse whitespace
        into something readable."""
        import re
        # Replace block-level tags with newlines before stripping
        text = re.sub(r"<br\s*/?>", "\n", html, flags=re.IGNORECASE)
        text = re.sub(r"</(p|div|li|h[1-6])>", "\n", text, flags=re.IGNORECASE)
        # Strip remaining tags
        text = re.sub(r"<[^>]+>", "", text)
        # Decode common entities
        text = (text
                .replace("&amp;", "&")
                .replace("&lt;", "<")
                .replace("&gt;", ">")
                .replace("&quot;", '"')
                .replace("&#39;", "'")
                .replace("&nbsp;", " "))
        # Collapse runs of blank lines and trim
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    # ---- Screen 3: progress ---------------------------------------------------

    def _on_confirm_clicked(self):
        self._show_progress_screen()
        threading.Thread(target=self._run_pipeline_worker, daemon=True).start()
        self._poll_after_id = self.root.after(100, self._poll_progress_queue)

    def _show_progress_screen(self):
        self._clear_screen()
        frame = Frame(self.container, padx=16, pady=16)
        frame.pack(fill=BOTH, expand=True)
        self.current_frame = frame

        Label(
            frame, text="Downloading and installing...", font=("", 13, "bold")
        ).pack(anchor=W, pady=(0, 8))

        self.progress_bar = ttk.Progressbar(frame, mode="indeterminate")
        self.progress_bar.pack(fill=X, pady=(0, 8))
        self.progress_bar.start(12)

        self.log_text = Text(frame, height=20, wrap=WORD, state=DISABLED)
        self.log_text.pack(fill=BOTH, expand=True)

    def _run_pipeline_worker(self):
        try:
            self._run_pipeline_worker_inner()
        except Exception as exc:  # noqa: BLE001 -- deliberately broad: this
            # is the top of a background thread, and ANY unhandled
            # exception here would otherwise just silently kill the
            # thread, leaving the user staring at a progress bar
            # forever with no feedback. Specific, expected failures
            # (FetchError, InstallError, OcsError) are already caught
            # per-item inside _run_pipeline_worker_inner; this is only
            # a last-resort net for genuinely unexpected bugs.
            self.root.after(0, self._on_pipeline_error, str(exc))

    def _run_pipeline_worker_inner(self):
        # We already downloaded nothing yet in the selection step --
        # this is the real fetch+install run, using the user's actual
        # checkbox choices to decide what gets installed. The pipeline
        # itself re-discovers companions from the description (cheap,
        # already-cached lookups won't repeat real downloads thanks to
        # fetch_and_extract's overwrite=False default), but to honor
        # per-item checkbox choices we don't call run_pipeline() as a
        # single opaque call -- instead we replicate its steps here so
        # we can pass install=True/False per item based on what the
        # user actually checked.
        from fetch_and_extract import fetch_and_extract, write_manifest, FetchError
        from installer import install_content, InstallError

        def progress(stage: str, detail: str):
            self.progress_queue.put((stage, detail))

        outcomes: list[pipeline.ItemOutcome] = []

        for item in self.selectable_items:
            entry = item.entry
            should_install = bool(item.install_var.get()) and item.is_editable

            if item.is_incompatible:
                progress("skipping", f"{item.label} ({entry.typename}) -- incompatible with Plasma 6")
                outcomes.append(pipeline.ItemOutcome(
                    content_id=entry.content_id, name=entry.name,
                    typeid=entry.typeid, typename=entry.typename,
                    label=item.label, bucket=item.bucket, fetch_succeeded=False,
                ))
                continue

            progress("downloading", f"{item.label} ({entry.typename})")
            outcome = pipeline.ItemOutcome(
                content_id=entry.content_id, name=entry.name,
                typeid=entry.typeid, typename=entry.typename,
                label=item.label, bucket=item.bucket, fetch_succeeded=False,
            )
            try:
                fetch_result = fetch_and_extract(entry, self.theme_cache_dir)
            except FetchError as exc:
                outcome.fetch_error = str(exc)
                progress("downloading", f"FAILED: {item.label}: {exc}")
                outcomes.append(outcome)
                continue

            outcome.fetch_succeeded = True
            outcome.cache_dir = fetch_result.cache_dir
            outcome.thumbnail_file = fetch_result.thumbnail_file

            if should_install:
                progress("installing", item.label)
                outcome.install_attempted = True
                try:
                    install_result = install_content(entry, fetch_result)
                except InstallError as exc:
                    outcome.install_error = str(exc)
                    progress("installing", f"FAILED: {item.label}: {exc}")
                else:
                    outcome.install_succeeded = True
                    outcome.install_path = install_result.install_path
                    outcome.display_name = install_result.display_name
                    outcome.display_name_confirmed = install_result.display_name_confirmed

            write_manifest(
                fetch_result.cache_dir, entry, fetch_result, item.bucket,
                installed_to=str(outcome.install_path) if outcome.install_path else None,
                installed_display_name=outcome.display_name,
            )
            outcomes.append(outcome)

        progress("done", "")

        if not outcomes:
            # selectable_items should always have at least the primary
            # entry in it by the time this runs -- if it's somehow
            # empty, surface that clearly instead of crashing on
            # outcomes[0] below.
            self.root.after(0, self._on_pipeline_error, "No items were processed.")
            return

        primary_outcome = outcomes[0]
        companion_outcomes = outcomes[1:]
        self.pipeline_result = pipeline.PipelineResult(
            primary=primary_outcome,
            companions=companion_outcomes,
            companion_lookup_failures=self._companion_lookup_failures,
        )
        self.root.after(0, self._show_summary_screen)

    def _on_pipeline_error(self, message: str):
        messagebox.showerror(APP_TITLE, f"Something went wrong: {message}")
        self._show_url_entry_screen()

    def _poll_progress_queue(self):
        # Guard against the screen having already been torn down --
        # this can happen because the background pipeline thread sets
        # self.pipeline_result AND schedules _show_summary_screen (via
        # root.after(0, ...)) at nearly the same moment this poller's
        # own root.after(150, ...) callback may also be waking up. If
        # Tk processes the screen-switch callback first, log_text gets
        # destroyed by _clear_screen() before this poll runs, and
        # touching it then raises TclError: invalid command name.
        if not self._widget_exists(self.log_text):
            return

        try:
            while True:
                stage, detail = self.progress_queue.get_nowait()
                self._append_log(f"[{stage}] {detail}")
        except queue.Empty:
            pass

        if self.pipeline_result is None:
            self._poll_after_id = self.root.after(150, self._poll_progress_queue)
        else:
            self._poll_after_id = None

    @staticmethod
    def _widget_exists(widget) -> bool:
        try:
            return bool(widget.winfo_exists())
        except Exception:
            return False

    def _append_log(self, line: str):
        if not self._widget_exists(self.log_text):
            return
        self.log_text.configure(state=NORMAL)
        self.log_text.insert(END, line + "\n")
        self.log_text.see(END)
        self.log_text.configure(state=DISABLED)

    # ---- Screen 4: summary -----------------------------------------------------

    def _show_summary_screen(self):
        self.progress_bar.stop()
        self._clear_screen()
        result = self.pipeline_result
        if result is None:
            # Shouldn't be reachable -- _run_pipeline_worker always
            # sets this before scheduling this screen -- but fail
            # loudly rather than silently rendering a broken screen
            # if something upstream changes.
            messagebox.showerror(
                APP_TITLE,
                "Internal error: no pipeline result available to summarize."
            )
            self._show_url_entry_screen()
            return

        frame = Frame(self.container, padx=16, pady=16)
        frame.pack(fill=BOTH, expand=True)
        self.current_frame = frame

        Label(
            frame, text="Done", font=("", 14, "bold")
        ).pack(anchor=W, pady=(0, 8))

        summary_text = Text(frame, wrap=WORD, height=24)
        summary_text.pack(fill=BOTH, expand=True, pady=(0, 12))
        summary_text.insert(END, pipeline.format_summary(result))
        # Deliberately left in NORMAL state (not DISABLED) so the user
        # can select and copy text -- Tkinter's DISABLED state blocks
        # selection/copy entirely, not just editing, which made the
        # summary impossible to copy out. Being technically editable
        # is harmless here since this is a read-only display, not a
        # file being written to.

        footer = Frame(frame)
        footer.pack(fill=X)
        Button(
            footer, text="Install Another Theme",
            command=self._reset_and_restart,
        ).pack(side=LEFT)
        Button(
            footer, text="Copy Summary",
            command=lambda: self._copy_to_clipboard(pipeline.format_summary(result)),
        ).pack(side=LEFT, padx=(8, 0))
        Button(
            footer, text="Quit", command=self.root.quit
        ).pack(side=RIGHT)

    def _copy_to_clipboard(self, text: str):
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        # clipboard_append alone doesn't always "stick" after the app
        # loses focus on some Linux clipboard managers unless the
        # selection is owned past this call -- update() forces Tk to
        # actually flush it to the system clipboard now rather than
        # lazily.
        self.root.update()

    def _reset_and_restart(self):
        self.primary_entry = None
        self.theme_cache_dir = None
        # downloads_root deliberately NOT reset -- keeping the user's
        # last-chosen downloads folder pre-filled for the next theme
        # is more convenient than making them re-pick it every time;
        # _show_url_entry_screen already seeds the field from
        # Path.home() only on first launch, not on every reset.
        self.selectable_items = []
        self.pipeline_result = None
        self._companion_lookup_failures = []
        self.progress_queue = queue.Queue()
        self._show_url_entry_screen()


def main():
    if not PIL_AVAILABLE:
        print(
            "Note: Pillow (PIL) isn't installed -- preview images won't be "
            "shown in the item preview popup, but everything else will "
            "still work. Install it with: pip install Pillow --user"
        )
    root = Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
