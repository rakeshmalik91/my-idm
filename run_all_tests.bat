@echo off
echo Running all tests for My-IDM...
python -m pytest tests/ -v --tb=short
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo Tests FAILED!
    exit /b 1
) else (
    echo.
    echo All tests PASSED!
    exit /b 0
)