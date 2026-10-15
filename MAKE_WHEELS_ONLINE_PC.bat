@echo off
cd /d "%~dp0"
echo Run this ONCE on any computer that has internet. It downloads all packages into the "wheels" folder.
echo Then copy the whole project folder (with "wheels") to the offline computers.
py -3 -m pip download -r requirements.txt -d wheels
pause
