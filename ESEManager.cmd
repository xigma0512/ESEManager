@echo off
setlocal
chcp 65001 >nul
pushd "%~dp0" || exit /b 1

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Project virtual environment not found: .venv\Scripts\python.exe
    pause
    popd
    exit /b 1
)

".venv\Scripts\python.exe" "src\ese_qt.py" %*
set "ESE_EXIT_CODE=%ERRORLEVEL%"
if not "%ESE_EXIT_CODE%"=="0" (
    echo [ERROR] ESEManager exited with code %ESE_EXIT_CODE%.
    pause
)

popd
exit /b %ESE_EXIT_CODE%
