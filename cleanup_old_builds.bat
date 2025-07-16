@echo off
echo ================================
echo    Cleaning Old Build Files     
echo ================================
echo.

if not exist "Compiled version" (
    echo No compiled versions found.
    pause
    exit /b 0
)

echo Current compiled versions:
dir "Compiled version" /b

echo.
set /p keep_latest="Keep the latest version? (Y/n): "

if /i "%keep_latest%"=="n" (
    echo Removing ALL compiled versions...
    rmdir /s /q "Compiled version"
    mkdir "Compiled version"
    echo All compiled versions removed.
) else (
    echo Keeping latest version, removing others...
    
    :: Get the newest folder (first in reverse date order)
    set newest_folder=
    for /f "delims=" %%i in ('dir "Compiled version\compiled_*" /b /ad /o-d 2^>nul') do (
        if not defined newest_folder set newest_folder=%%i
    )
    
    if defined newest_folder (
        echo Keeping: %newest_folder%
        
        :: Remove all except the newest
        for /f "delims=" %%i in ('dir "Compiled version\compiled_*" /b /ad 2^>nul') do (
            if not "%%i"=="%newest_folder%" (
                echo Removing: %%i
                rmdir /s /q "Compiled version\%%i"
            )
        )
        echo Cleanup complete!
    ) else (
        echo No compiled folders found.
    )
)

echo.
echo Current disk usage:
dir "Compiled version" /s

echo.
pause 