@echo off
setlocal

:: Navigate to the directory of this script so it can be run from anywhere
cd /d "%~dp0"

:: Activate virtual environment if present
if exist ".venv\Scripts\activate.bat" (
    call .venv\Scripts\activate.bat
) else (
    if exist "venv\Scripts\activate.bat" call venv\Scripts\activate.bat
)

:: ---------------------------------------------------------------------------
:: Tier selection.
::
::   run_all_tests.bat          full suite (default, unchanged behaviour)
::   run_all_tests.bat basic    fast tier, safe to leave running in the background
::   run_all_tests.bat ui       only the UI / tray / clipboard tests
::
:: The basic tier excludes everything marked `ui` (see _INTERACTIVE_MODULES in
:: tests/conftest.py), so it opens no window, drives no tray icon and never reads or
:: writes the real system clipboard. UI regressions are invisible to it by
:: construction - run the full suite before committing.
:: ---------------------------------------------------------------------------

set "TIER=%~1"
if not defined TIER set "TIER=full"

if /i "%TIER%"=="basic" goto basic
if /i "%TIER%"=="ui" goto uitier
if /i "%TIER%"=="full" goto full
goto usage

:basic
echo Running BASIC SANITY tests for My-IDM (no window, no tray, no clipboard)...
python -m pytest tests/ -m "not ui" -q --tb=short
set "RC=%ERRORLEVEL%"
goto done

:uitier
echo Running UI tests for My-IDM (windows, system tray, clipboard)...
python -m pytest tests/ -m ui -v --tb=short
set "RC=%ERRORLEVEL%"
goto done

:full
echo Running all tests for My-IDM...
python -m pytest tests/ -v --tb=short
set "RC=%ERRORLEVEL%"
goto done

:usage
echo Usage: run_all_tests.bat [basic^|ui^|full]
echo.
echo   basic   Fast tier. ~52s, 1489 tests. No window, no tray, no real clipboard,
echo           so it is safe to leave running while you work.
echo   ui      Only the UI, tray and clipboard tests. ~932 tests.
echo   full    Everything. Default when no argument is given. ~3.4 min, 2421 tests.
echo.
echo The script switches to its own directory first, so it works from anywhere.
echo See .agents\workflows\testing.md for what each tier covers.
endlocal & exit /b 2

:done
if not "%RC%"=="0" (
    echo.
    echo Tests FAILED!
    endlocal & exit /b 1
)

echo.
echo All tests PASSED!
endlocal & exit /b 0