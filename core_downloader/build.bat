@echo off
REM Compila core_downloader con MinGW (g++) enlazando contra libcurl.
REM
REM Variables opcionales (definirlas antes de ejecutar para sobrescribir):
REM   CURL_DIR  carpeta que contiene include\curl\curl.h y bin\libcurl.dll
REM   CXX       compilador (por defecto g++)
REM
REM Ejemplo:  set CURL_DIR=C:\curl-8.x-win64-mingw && build.bat

setlocal
cd /d "%~dp0"

if "%CXX%"=="" set CXX=g++
if "%CURL_DIR%"=="" set CURL_DIR=%USERPROFILE%\.conda\envs\pysegundo\Library

if not exist "%CURL_DIR%\include\curl\curl.h" (
    echo [ERROR] No se encontro curl.h en "%CURL_DIR%\include\curl"
    echo         Define CURL_DIR apuntando a tu instalacion de libcurl.
    exit /b 1
)

REM MinGW puede enlazar directamente contra la DLL; si existe libcurl.dll.a se usa -lcurl.
set CURL_LINK="%CURL_DIR%\bin\libcurl.dll"
if exist "%CURL_DIR%\lib\libcurl.dll.a" set CURL_LINK=-L"%CURL_DIR%\lib" -lcurl

if not exist bin mkdir bin

echo Compilando con %CXX% ...
%CXX% -std=c++11 -O2 -Wall -Wextra -I"%CURL_DIR%\include" main.cpp %CURL_LINK% -o bin\core_downloader.exe
if errorlevel 1 (
    echo [ERROR] La compilacion fallo.
    exit /b 1
)

REM Copia la DLL junto al ejecutable para que funcione fuera del entorno conda.
if exist "%CURL_DIR%\bin\libcurl.dll" copy /y "%CURL_DIR%\bin\libcurl.dll" bin\ >nul

echo [OK] bin\core_downloader.exe
endlocal
