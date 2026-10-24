#!/usr/bin/env bash
# Compila core_downloader con g++ y libcurl en Linux.
#
# Variables opcionales:
#   CXX       compilador (por defecto g++)
#   CURL_DIR  prefijo de una instalación de libcurl (include/ y lib/)

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

CXX="${CXX:-g++}"
mkdir -p bin

if [[ -n "${CURL_DIR:-}" ]]; then
    if [[ ! -f "$CURL_DIR/include/curl/curl.h" ]]; then
        echo "[ERROR] No se encontró curl.h en '$CURL_DIR/include/curl'." >&2
        exit 1
    fi
    CURL_CFLAGS=(-I"$CURL_DIR/include")
    if [[ -f "$CURL_DIR/lib/pkgconfig/libcurl.pc" ]]; then
        read -r -a CURL_LIBS <<< "$(PKG_CONFIG_PATH="$CURL_DIR/lib/pkgconfig${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}" pkg-config --libs libcurl)"
    elif [[ -f "$CURL_DIR/lib64/pkgconfig/libcurl.pc" ]]; then
        read -r -a CURL_LIBS <<< "$(PKG_CONFIG_PATH="$CURL_DIR/lib64/pkgconfig${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}" pkg-config --libs libcurl)"
    else
        CURL_LIBS=(-L"$CURL_DIR/lib" -lcurl)
    fi
else
    if ! command -v pkg-config >/dev/null 2>&1 || ! pkg-config --exists libcurl; then
        echo "[ERROR] No se encontró libcurl. Instala sus encabezados de desarrollo y pkg-config." >&2
        echo "        En Debian/Ubuntu: sudo apt install g++ libcurl4-openssl-dev pkg-config" >&2
        exit 1
    fi
    read -r -a CURL_CFLAGS <<< "$(pkg-config --cflags libcurl)"
    read -r -a CURL_LIBS <<< "$(pkg-config --libs libcurl)"
fi

echo "Compilando con $CXX ..."
"$CXX" -std=c++11 -O2 -Wall -Wextra "${CURL_CFLAGS[@]}" main.cpp "${CURL_LIBS[@]}" -o bin/core_downloader
echo "[OK] bin/core_downloader"
