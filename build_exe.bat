@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "APP_NAME=WebFlowManager"
set "PYTHON=python"
set "INCLUDE_LOCAL_DATA=0"
set "INSTALL_DEPENDENCIES=0"
set "CHECK_ONLY=0"

if exist "env\Scripts\python.exe" set "PYTHON=env\Scripts\python.exe"

:parse_args
if "%~1"=="" goto :args_done
if /I "%~1"=="--with-data" (
  set "INCLUDE_LOCAL_DATA=1"
) else if /I "%~1"=="--setup" (
  set "INSTALL_DEPENDENCIES=1"
) else if /I "%~1"=="--check" (
  set "CHECK_ONLY=1"
) else (
  echo Unknown option: %~1
  echo Usage: build_exe.bat [--setup] [--with-data] [--check]
  exit /b 1
)
shift
goto :parse_args

:args_done
if "%CHECK_ONLY%"=="1" goto :check

echo [1/6] Checking Python...
"%PYTHON%" --version
if errorlevel 1 goto :error

if "%INSTALL_DEPENDENCIES%"=="1" (
  echo [2/6] Installing build dependencies...
  "%PYTHON%" -m pip install --upgrade pyinstaller
  if errorlevel 1 goto :error
  "%PYTHON%" -m pip install -r requirements.txt
  if errorlevel 1 goto :error
) else (
  echo [2/6] Checking installed build dependencies...
  "%PYTHON%" -c "import PyInstaller, playwright, openpyxl"
  if errorlevel 1 goto :missing_dependencies
)

echo [3/6] Cleaning old build output...
if exist "build" rmdir /s /q "build"
if exist "dist\%APP_NAME%" rmdir /s /q "dist\%APP_NAME%"
if exist "%APP_NAME%.spec" del /q "%APP_NAME%.spec"

echo [4/6] Building %APP_NAME%.exe...
"%PYTHON%" -m PyInstaller --noconfirm --clean --windowed --onedir --contents-directory "_internal" --name "%APP_NAME%" --icon "assets\app.ico" --add-data "locales;locales" --add-data "assets;assets" --collect-all playwright "main.py"
if errorlevel 1 goto :error

echo [5/6] Preparing writable folders...
if not exist "dist\%APP_NAME%\data" mkdir "dist\%APP_NAME%\data"
if not exist "dist\%APP_NAME%\log" mkdir "dist\%APP_NAME%\log"
if not exist "dist\%APP_NAME%\artifacts" mkdir "dist\%APP_NAME%\artifacts"
for /d %%D in (*) do if exist "%%D\index.html" if exist "%%D\style.css" xcopy "%%D" "dist\%APP_NAME%\%%~nxD\" /e /i /y >nul

if "%INCLUDE_LOCAL_DATA%"=="1" (
  echo Copying local database and login states...
  if exist "data\flows.db" copy /y "data\flows.db" "dist\%APP_NAME%\data\flows.db" >nul
  if exist "data\browser_state.json" copy /y "data\browser_state.json" "dist\%APP_NAME%\data\browser_state.json" >nul
  if exist "data\browser_states" xcopy "data\browser_states" "dist\%APP_NAME%\data\browser_states\" /e /i /y >nul
)

echo [6/6] Removing temporary build files...
for /l %%R in (1,1,5) do (
  if exist "build" rmdir /s /q "%CD%\build"
  if exist "%APP_NAME%.spec" del /f /q "%CD%\%APP_NAME%.spec"
  if not exist "build" if not exist "%APP_NAME%.spec" goto :cleanup_done
  echo Cleanup retry %%R/5...
  timeout /t 1 /nobreak >nul
)

echo.
echo Cleanup failed:
if exist "build" echo   %CD%\build
if exist "%APP_NAME%.spec" echo   %CD%\%APP_NAME%.spec
echo Close programs using these paths, then run the build again.
exit /b 1

:cleanup_done
echo.
echo Build completed:
echo   %CD%\dist\%APP_NAME%\%APP_NAME%.exe
echo.
if "%INCLUDE_LOCAL_DATA%"=="0" (
  echo Local database and login states were NOT included.
  echo To include them, run: build_exe.bat --with-data
) else (
  echo WARNING: This build contains local data and login states.
  echo Do not distribute it to untrusted people.
)
if "%INSTALL_DEPENDENCIES%"=="0" echo To install or update dependencies, run: build_exe.bat --setup
exit /b 0

:check
echo Build script check:
echo   Project: %CD%
echo   App: %APP_NAME%
echo   Python: %PYTHON%
"%PYTHON%" --version
if errorlevel 1 goto :error
if not exist "main.py" goto :missing_input
if not exist "locales" goto :missing_input
if not exist "assets\app.ico" goto :missing_input
for /d %%D in (*) do if exist "%%D\index.html" if exist "%%D\style.css" echo   Manual: %%D
echo Build script check passed.
exit /b 0

:missing_input
echo Required build input is missing.
exit /b 1

:missing_dependencies
echo.
echo Required build dependencies are not installed in:
echo   %PYTHON%
echo Run the following command once, then build again:
echo   build_exe.bat --setup
exit /b 1

:error
echo.
echo Build failed. See the error output above.
exit /b 1
