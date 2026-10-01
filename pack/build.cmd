@echo off
rem zcode-meter Windows 打包脚本(2026-09-30 实测定稿)
rem 前置:pip install pyinstaller(本机 6.22.3);watchdog 可选装(装了
rem 自动进包,不装运行时优雅降级 15s TTL)
rem 产物:dist/zcode-meter/(onedir,冷启动快,常驻托盘工具不要 onefile)

cd /d "%~dp0.."

python -m PyInstaller --noconfirm --clean --windowed ^
  --name zcode-meter ^
  --paths src ^
  --collect-all dxcam ^
  --collect-submodules comtypes ^
  --exclude-module tkinter ^
  --exclude-module PyQt5 ^
  --exclude-module PyQt6 ^
  --exclude-module IPython ^
  --exclude-module matplotlib ^
  pack/entry.py || exit /b 1

rem 体积瘦身(实测安全,删后必须复验):
rem  - opencv 只用图像处理,videoio 的 ffmpeg 30MB 纯死重
rem  - Qt 软件 OpenGL 回退 20MB:QPainter 走光栅,桌面 GL 由系统提供
del /q "dist\zcode-meter\_internal\cv2\opencv_videoio_ffmpeg*.dll" 2>NUL
del /q "dist\zcode-meter\_internal\PySide6\opengl32sw.dll" 2>NUL

echo build ok: dist\zcode-meter\zcode-meter.exe
