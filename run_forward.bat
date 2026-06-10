@echo off
REM Wrapper invoked by the Windows Scheduled Task "polymirror-forward" every ~15 min.
REM Runs ONE poll cycle of the live forward mirror collector and appends stdout/stderr
REM to data\forward\schtask.log. Paths are absolute so the task's working dir is irrelevant.
set ROOT=C:\Users\jepst\Downloads\github\polymirror
if not exist "%ROOT%\data\forward" mkdir "%ROOT%\data\forward"
"%ROOT%\.venv\Scripts\python.exe" "%ROOT%\forward_collect.py" >> "%ROOT%\data\forward\schtask.log" 2>&1
