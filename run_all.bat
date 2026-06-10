@echo off
REM ===========================================================================
REM  run_all.bat - polymirror full pipeline (setup -> ingest -> backtest -> plots)
REM
REM  Runs every phase end to end:
REM    1. create .venv if missing
REM    2. install pinned dependencies
REM    3. self-test config.py
REM    4. Phase 3 - ingest the resolved-market universe -> data/cache/*.parquet
REM    5. Phase 4-5 - run the sensitivity grid -> results/*.csv, summary.md
REM    6. render plots -> results/*.png
REM
REM  Usage:  run_all.bat
REM ===========================================================================
setlocal
cd /d "%~dp0"

set "PY=.venv\Scripts\python.exe"

echo.
echo [1/6] Ensuring virtual environment...
if not exist "%PY%" (
    python -m venv .venv || goto :fail
) else (
    echo       .venv already exists - skipping.
)

echo.
echo [2/6] Installing dependencies...
"%PY%" -m pip install --upgrade pip || goto :fail
"%PY%" -m pip install -r requirements.txt || goto :fail

echo.
echo [3/6] Self-testing config...
"%PY%" config.py || goto :fail

echo.
echo [4/6] Phase 3 - ingesting market universe (this can take a while)...
"%PY%" -m polymirror.ingest || goto :fail

echo.
echo [5/6] Phase 4-5 - running backtest grid...
"%PY%" run.py || goto :fail

echo.
echo [6/6] Rendering plots...
"%PY%" plots.py || goto :fail

echo.
echo ===========================================================================
echo  DONE. Artifacts written to: results\
echo ===========================================================================
endlocal
exit /b 0

:fail
echo.
echo *** FAILED (exit code %errorlevel%). See output above. ***
endlocal
exit /b 1
