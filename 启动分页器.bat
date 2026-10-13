@echo off
title PDF Book Splitter
python "%~dp0app.py" %*
if errorlevel 1 pause
