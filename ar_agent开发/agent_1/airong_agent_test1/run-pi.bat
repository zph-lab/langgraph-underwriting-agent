@echo off
setlocal

rem Start pi with this folder as the working directory.
rem pi loads project extensions from <cwd>\.pi\extensions (see pi-main loader.ts),
rem and pi-test.bat never changes the directory itself.
cd /d "%~dp0"

call "%~dp0..\pi-main\pi-test.bat" %*
exit /b %ERRORLEVEL%
