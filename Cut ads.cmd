@echo off
rem Drag a raw video onto this file (or run: "Cut ads.cmd" "path\to\video.mov").
python "%~dp0ad_cutter.py" %*
pause
