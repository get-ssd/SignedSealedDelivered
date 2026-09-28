@echo off
if not exist "%~dp0runs" mkdir "%~dp0runs"
type nul > "%~dp0runs\go"
echo Demo step released.
