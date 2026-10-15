@echo off
cd /d "%~dp0"
if not exist backups mkdir backups
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set stamp=%%i
copy /Y madrasa.db "backups\madrasa_manual_%stamp%.db"
echo Backup saved in the backups folder.
pause
