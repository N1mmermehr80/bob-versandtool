@echo off
rem Baut BOB-Sendcloud-Versand.exe lokal (benötigt: python -m pip install pyinstaller pypdf pypdfium2==5.14.0)
rem Für Releases nicht nötig: GitHub baut die exe automatisch beim Versions-Tag (siehe README).
cd /d "%~dp0"
python -m PyInstaller --noconfirm --clean --onefile --windowed --hidden-import pypdf --collect-all pypdfium2 --collect-all pypdfium2_raw --name "BOB-Sendcloud-Versand" versand_tool.py
echo.
echo Fertig: dist\BOB-Sendcloud-Versand.exe
pause
