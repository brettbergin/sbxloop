@echo off
rem sbxloop launcher — written by `sbxloop init`; edits here are overwritten.
rem
rem The Windows counterpart of sbxloop.launcher.sh: binds this command to the
rem home it lives in and runs the venv's sbxloop. There are no secrets in this
rem file — sbxloop reads config\secrets.env itself, and nothing is exported to
rem other processes.
rem
rem A native Windows host cannot boot sandboxes (Docker Sandboxes ships no
rem native Windows `sbx`), so there is no sbx wrapper beside this one and the
rem commands that need one refuse by name. The read-only commands — doctor,
rem config, logs — answer, so the refusal can be diagnosed from the host.
setlocal
set "SBXLOOP_HOME=%~dp0.."
set "PATH=%~dp0;%PATH%"
set "TMPDIR=%SBXLOOP_HOME%\tmp"
set "TEMP=%SBXLOOP_HOME%\tmp"
set "TMP=%SBXLOOP_HOME%\tmp"
set "PIP_CACHE_DIR=%SBXLOOP_HOME%\cache\pip"
set "UV_CACHE_DIR=%SBXLOOP_HOME%\cache\uv"
set "UV_PYTHON_INSTALL_DIR=%SBXLOOP_HOME%\python"
set "COPILOT_CLI_EXTRACT_DIR=%SBXLOOP_HOME%\cache\copilot-sdk"
if not exist "%SBXLOOP_HOME%\venv\Scripts\sbxloop.exe" (
  echo sbxloop is not installed in %SBXLOOP_HOME% ^(expected venv\Scripts\sbxloop.exe^); run: sbxloop init 1>&2
  exit /b 127
)
"%SBXLOOP_HOME%\venv\Scripts\sbxloop.exe" %*
exit /b %ERRORLEVEL%
