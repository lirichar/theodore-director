@echo off
setlocal
if not defined COMFY_ROOT set "COMFY_ROOT=D:\path\to\ComfyUI"
if not defined WORKFLOW_DIR set "WORKFLOW_DIR=%~dp0workflows"
if not defined COMFY_URL set "COMFY_URL=http://127.0.0.1:8188"
if not defined DIRECTOR_PORT set "DIRECTOR_PORT=8190"
if not defined FFMPEG_PATH set "FFMPEG_PATH=ffmpeg"
python "%~dp0mobile_director\server.py"
pause
