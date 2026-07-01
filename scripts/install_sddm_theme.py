#!/usr/bin/env python3
"""
install_sddm_theme.py

Helper script to install an SDDM login theme that was already
downloaded by the KDE Store Theme Installer.

Usage:
    python3 scripts/install_sddm_theme.py <path-to-extracted-theme-folder>

Example:
    python3 scripts/install_sddm_theme.py \
        ~/kde-theme-downloads/Magna-Dark-Global-6/Magna-SDDM-6/extracted/Magna-SDDM-6

Why is this a separate script and not part of the main installer?
  - SDDM themes must be installed to /usr/share/sddm/themes/ (a
    system path), which requires sudo. The main installer deliberately
    never touches system paths or requires elevated privileges -- a
    broken SDDM theme leaves you with a broken login screen, which is
    a much worse failure mode than a broken icon pack.
  - This script is intentionally separate so you can review it,
    understand what it does, and run it consciously rather than having
    it happen automatically as part of a batch install.

What this script does:
  1. Validates the theme folder looks like a real SDDM theme
     (contains a metadata.desktop or Main.qml file).
  2. Backs up your current SDDM config before touching anything.
  3. Copies the theme folder to /usr/share/sddm/themes/ with sudo.
  4. Writes a drop-in config file at /etc/sddm.conf.d/
     kde-theme-installer.conf setting [Theme] Current=<theme-name>.
     Uses a drop-in file rather than editing /etc/sddm.conf directly
     so it's easy to revert -- just delete that one file.
  5. Shows you exactly what was done and how to undo it.
  6. Does NOT restart SDDM or log you out -- that's your call after
     you've reviewed everything.

How to revert if something goes wrong:
  Boot to a TTY (Ctrl+Alt+F2), log in, then run:
    sudo rm /etc/sddm.conf.d/kde-theme-installer.conf
  That removes the theme selection; SDDM falls back to its default.
  The theme files themselves stay in /usr/share/sddm/themes/ but
  won't be active. You can remove them with:
    sudo rm -rf /usr/share/sddm/themes/<theme-name>
"""

import subprocess
import sys
from pathlib import Path


SDDM_THEMES_DIR = Path("/usr/share/sddm/themes")
SDDM_CONF_DIR = Path("/etc/sddm.conf.d")
CONF_FILE_NAME = "kde-theme-installer.conf"


def die(message: str):
    print(f"\nERROR: {message}", file=sys.stderr)
    sys.exit(1)


def run_sudo(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run a command with sudo, streaming output so the user can see
    what's happening."""
    cmd = ["sudo"] + list(args)
    print(f"  running: {' '.join(cmd)}")
    return subprocess.run(cmd, check=check)


def validate_sddm_theme(theme_path: Path) -> str:
    """
    Check that the given path looks like a real SDDM theme folder.
    Returns the theme name (the folder's own name) if valid.

    A valid SDDM theme must contain at least one of:
      - Main.qml  (the primary theme entry point)
      - metadata.desktop  (theme metadata, shown in System Settings)

    We check for these rather than just accepting any directory, since
    a user might accidentally point this script at the wrong folder.
    """
    if not theme_path.exists():
        die(f"Path does not exist: {theme_path}")
    if not theme_path.is_dir():
        die(f"Path is not a directory: {theme_path}")

    has_main_qml = (theme_path / "Main.qml").exists()
    has_metadata = (theme_path / "metadata.desktop").exists()

    if not has_main_qml and not has_metadata:
        die(
            f"{theme_path} doesn't look like a valid SDDM theme folder.\n"
            f"Expected to find Main.qml and/or metadata.desktop inside it.\n"
            f"Make sure you're pointing at the extracted theme folder itself,\n"
            f"not its parent directory. For example:\n"
            f"  .../extracted/Magna-SDDM-6/   <-- correct (contains Main.qml)\n"
            f"  .../extracted/                 <-- wrong (contains the folder)"
        )

    return theme_path.name


def read_display_name(theme_path: Path, theme_name: str) -> str:
    """Read the human-readable theme name from metadata.desktop if
    present, falling back to the folder name."""
    metadata = theme_path / "metadata.desktop"
    if not metadata.exists():
        return theme_name
    for line in metadata.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("Name="):
            value = line[5:].strip()
            if value:
                return value
    return theme_name


def backup_existing_config():
    """Back up any existing SDDM config files before we touch anything.
    These are tiny files -- cheap to back up, potentially very
    useful if something goes wrong."""
    backed_up = []

    sddm_conf = Path("/etc/sddm.conf")
    if sddm_conf.exists():
        backup = Path("/etc/sddm.conf.bak")
        run_sudo("cp", str(sddm_conf), str(backup))
        backed_up.append(f"  {sddm_conf} -> {backup}")

    our_conf = SDDM_CONF_DIR / CONF_FILE_NAME
    if our_conf.exists():
        backup = SDDM_CONF_DIR / f"{CONF_FILE_NAME}.bak"
        run_sudo("cp", str(our_conf), str(backup))
        backed_up.append(f"  {our_conf} -> {backup}")

    if backed_up:
        print("Backed up existing config:")
        for line in backed_up:
            print(line)
    else:
        print("No existing SDDM config to back up (clean slate).")


def install_theme(theme_path: Path, theme_name: str):
    """Copy the theme folder to /usr/share/sddm/themes/ with sudo."""
    dest = SDDM_THEMES_DIR / theme_name

    if dest.exists():
        print(f"\nTheme folder already exists at {dest}.")
        answer = input("Overwrite it? [y/N] ").strip().lower()
        if answer != "y":
            die("Aborted by user.")
        run_sudo("rm", "-rf", str(dest))

    run_sudo("cp", "-r", str(theme_path), str(dest))
    print(f"Theme copied to {dest}")


def write_config(theme_name: str):
    """Write a drop-in config file selecting this theme. Uses
    /etc/sddm.conf.d/ rather than editing /etc/sddm.conf directly --
    drop-in files are easier to revert (just delete the file) and
    don't risk corrupting an existing config."""
    conf_content = (
        "# Written by kde-theme-installer install_sddm_theme.py\n"
        "# Delete this file to revert to the default SDDM theme.\n"
        "[Theme]\n"
        f"Current={theme_name}\n"
    )

    # Write to a temp file in /tmp first, then sudo-move it into place
    # -- we can't write to /etc/sddm.conf.d/ directly without sudo,
    # and 'sudo echo > file' doesn't work because the redirect is
    # handled by the shell before sudo gets involved.
    tmp_file = Path("/tmp/kde-theme-installer-sddm.conf")
    tmp_file.write_text(conf_content, encoding="utf-8")

    run_sudo("mkdir", "-p", str(SDDM_CONF_DIR))
    run_sudo("cp", str(tmp_file), str(SDDM_CONF_DIR / CONF_FILE_NAME))
    tmp_file.unlink()

    print(f"Config written to {SDDM_CONF_DIR / CONF_FILE_NAME}")


def main():
    if len(sys.argv) != 2 or sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        sys.exit(0 if sys.argv[1:] == ["-h"] or sys.argv[1:] == ["--help"] else 1)

    theme_path = Path(sys.argv[1]).expanduser().resolve()

    print(f"SDDM theme install helper")
    print(f"Theme path: {theme_path}")
    print()

    theme_name = validate_sddm_theme(theme_path)
    display_name = read_display_name(theme_path, theme_name)

    print(f"Theme name: {theme_name}")
    print(f"Display name: {display_name}")
    print()
    print("This script will:")
    print(f"  1. Back up any existing SDDM config")
    print(f"  2. Copy theme to {SDDM_THEMES_DIR / theme_name}  (requires sudo)")
    print(f"  3. Write {SDDM_CONF_DIR / CONF_FILE_NAME}  (requires sudo)")
    print()
    print("It will NOT restart SDDM or log you out.")
    print()
    print("To revert if something goes wrong, boot to a TTY (Ctrl+Alt+F2)")
    print("and run:")
    print(f"  sudo rm {SDDM_CONF_DIR / CONF_FILE_NAME}")
    print()

    answer = input("Proceed? [y/N] ").strip().lower()
    if answer != "y":
        print("Aborted.")
        sys.exit(0)

    print()
    backup_existing_config()
    print()
    install_theme(theme_path, theme_name)
    print()
    write_config(theme_name)

    print()
    print("=" * 60)
    print("Done!")
    print(f"SDDM theme '{display_name}' is now configured.")
    print()
    print("To apply it: log out and back in, or reboot.")
    print()
    print("To revert:")
    print(f"  sudo rm {SDDM_CONF_DIR / CONF_FILE_NAME}")
    print("  (then log out/reboot)")
    print("=" * 60)


if __name__ == "__main__":
    main()
