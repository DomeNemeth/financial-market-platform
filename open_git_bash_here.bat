@echo off
setlocal
set "TARGET_DIR=%~dp0"
set "GIT_BASH=\"C:\Program Files\Git\git-bash.exe\""
if exist %GIT_BASH% (
    %GIT_BASH% --cd="%TARGET_DIR%"
) else if exist "C:\Program Files (x86)\Git\git-bash.exe" (
    "C:\Program Files (x86)\Git\git-bash.exe" --cd="%TARGET_DIR%"
) else (
    echo Git Bash not found in default locations.
    echo Install Git for Windows or update this script with the correct path to git-bash.exe.
    pause
)
