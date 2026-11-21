@echo off
setlocal
title Message - Server Panel
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0ServerPanel.ps1"
if errorlevel 1 pause
