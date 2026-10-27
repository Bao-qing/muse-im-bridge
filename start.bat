@echo off
rem ============================================================
rem muse-relay-lib 启动脚本（QQ 适配器 + Muse 库）
rem 用法：把 start.bat 放在项目根目录（main.py 旁边），双击运行。
rem 这个文件不含任何 ID 和密钥：
rem   配置读 config.json（没有就按环境变量来）；
rem   QQ AppSecret 启动后手动粘贴，只进内存，不落盘。
rem 关掉窗口即停止。
rem ============================================================
chcp 65001 >nul
set "PYTHONUTF8=1"
cd /d "%~dp0"
if not exist main.py (
  echo 找不到 main.py：请把 start.bat 放在项目根目录（main.py 旁边）再运行。
  pause
  exit /b 1
)
if exist .venv\Scripts\python.exe (
  set "PY=.venv\Scripts\python.exe"
) else (
  set "PY=python"
)
echo 正在启动 muse-relay-lib ...
echo 如果 QQ 适配器启用，启动后会提示粘贴 AppSecret，粘贴后按回车。
"%PY%" main.py
echo.
echo 主程序已退出，上面有报错的话按报错排查。
pause
