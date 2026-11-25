@echo off
setlocal
set "agent_shell=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\native\powershell\pwsh.exe"
if not exist "%agent_shell%" set "agent_shell=pwsh.exe"
"%agent_shell%" -NoProfile -File "%~dp0start-local-tui.ps1" %*
exit /b %errorlevel%
