@echo off
setlocal

cd /d "%~dp0" || (
    echo Could not open the Excel AI Agent folder. 1>&2
    exit /b 1
)

set "AGENT_PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%AGENT_PYTHON%" (
    echo Project Python was not found. Run py -3.12 -m venv .venv and install requirements.txt. 1>&2
    exit /b 1
)

"%AGENT_PYTHON%" -m scripts.check_startup
if not "%errorlevel%"=="0" exit /b %errorlevel%

"%AGENT_PYTHON%" -m streamlit run app.py --server.address 127.0.0.1 --server.showEmailPrompt false --browser.gatherUsageStats false
set "AGENT_EXIT=%errorlevel%"
if not "%AGENT_EXIT%"=="0" echo Streamlit could not start or stopped with an error. See details above. 1>&2
exit /b %AGENT_EXIT%
