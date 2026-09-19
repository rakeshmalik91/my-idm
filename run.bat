@echo off
setlocal

:: Navigate to the directory of this script
cd /d "%~dp0"

:: Activate virtual environment if present
if exist ".venv\Scripts\activate.bat" (
    call .venv\Scripts\activate.bat
) else if exist "venv\Scripts\activate.bat" (
    call venv\Scripts\activate.bat
)

:: Run My-IDM forwarding all CLI arguments
python -m my_idm.main %*

endlocal
