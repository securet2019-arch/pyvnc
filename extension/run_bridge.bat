@echo off
rem One-click websockify bridge for pyvnc Web Viewer.
rem Extension default settings point at ws://localhost:6080/websockify.
rem Needs: pip install websockify
title websockify bridge - VNC 192.168.1.176:5900
python -m websockify 6080 192.168.1.176:5900
pause
