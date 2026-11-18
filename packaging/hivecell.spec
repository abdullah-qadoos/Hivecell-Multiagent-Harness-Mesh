# PyInstaller build for the Windows release.
#   uv run pyinstaller packaging/hivecell.spec --noconfirm --distpath dist --workpath build
# Produces dist/Hivecell/ with two programs sharing one set of files:
#   Hivecell.exe  the desktop app (no console window)
#   axon.exe      the Axonmesh MCP server and CLI (console: MCP clients talk to it over stdin/stdout)
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).parent
ICON = str(ROOT / "hivecell" / "app" / "icon.ico")
DATAS = [
    (str(ROOT / "hivecell" / "app" / "ui.html"), "hivecell/app"),
    (ICON, "hivecell/app"),
    (str(ROOT / "agents"), "agents"),  # agent templates
]
# mcp.cli is the SDK's own optional CLI (needs extras we don't use); importing it to scan it exits
HIDDEN = (collect_submodules("hivecell") + collect_submodules("axonmesh")
          + collect_submodules("mcp", filter=lambda name: not name.startswith("mcp.cli")))
EXCLUDE = ["tkinter", "pytest", "IPython", "matplotlib", "numpy"]

app = Analysis([str(ROOT / "packaging" / "app_main.py")], pathex=[str(ROOT)], datas=DATAS,
               hiddenimports=HIDDEN, excludes=EXCLUDE)
axon = Analysis([str(ROOT / "packaging" / "axon_main.py")], pathex=[str(ROOT)], datas=DATAS,
                hiddenimports=HIDDEN, excludes=EXCLUDE)

app_exe = EXE(PYZ(app.pure), app.scripts, [], exclude_binaries=True, name="Hivecell", icon=ICON,
              console=False, upx=False)
axon_exe = EXE(PYZ(axon.pure), axon.scripts, [], exclude_binaries=True, name="axon", icon=ICON,
               console=True, upx=False)

COLLECT(app_exe, app.binaries, app.datas, axon_exe, axon.binaries, axon.datas, name="Hivecell", upx=False)
