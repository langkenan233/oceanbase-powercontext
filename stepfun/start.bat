@echo off
setlocal
cd /d "%~dp0"
if not exist node_modules (
  echo Installing dependencies...
  call npm install --legacy-peer-deps
  if errorlevel 1 exit /b 1
)
node server\index.mjs
