// core_downloader: descarga un único fragmento (rango de bytes) de una URL.
//
// Uso:
//   core_downloader <url> <inicio> <fin> <archivo_temporal>
//
// Ejemplo:
//   core_downloader https://example.com/file.zip 0 1048575 file.zip.part0
//
// Códigos de salida:
//   0 = OK, 1 = argumentos inválidos, 2 = error de archivo,
//   3 = error de red/libcurl,
//   4 = el servidor no respetó el rango (no devolvió 206 o Content-Range distinto al pedido),
//   5 = los bytes recibidos no coinciden con el tamaño del rango
//
// Si la validación falla (4 o 5) se borra el archivo de salida para que el
// orquestador nunca una un fragmento corrupto.

#include <curl/curl.h>

#include <cctype>
#include <cerrno>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>

namespace {

struct WriteContext {
    FILE* file;
    curl_off_t written;
};

// Content-Range de la respuesta final (tras seguir redirecciones).
struct RangeInfo {
    bool present;
    long long start;
    long long end;
};

size_t write_callback(char* data, size_t size, size_t nmemb, void* userp) {
    WriteContext* ctx = static_cast<WriteContext*>(userp);
    size_t bytes = size * nmemb;
    size_t out = std::fwrite(data, 1, bytes, ctx->file);
    ctx->written += static_cast<curl_off_t>(out);
    return out;  // si out != bytes, libcurl aborta con CURLE_WRITE_ERROR
}

bool starts_with_nocase(const char* text, size_t len, const char* prefix) {
    size_t n = std::strlen(prefix);
    if (len < n) return false;
    for (size_t i = 0; i < n; ++i) {
        if (std::tolower(static_cast<unsigned char>(text[i])) != prefix[i]) return false;
    }
    return true;
}

// Parsea "bytes <inicio>-<fin>/<total|*>". Se usa strtoll en lugar de
// sscanf("%lld") porque el msvcrt de MinGW antiguo no soporta %lld.
bool parse_content_range(const std::string& value, long long& start, long long& end) {
    size_t pos = value.find_first_not_of(" \t");
    if (pos == std::string::npos || value.compare(pos, 6, "bytes ") != 0) return false;
    const char* p = value.c_str() + pos + 6;
    char* stop = nullptr;
    start = std::strtoll(p, &stop, 10);
    if (stop == p || *stop != '-') return false;
    p = stop + 1;
    end = std::strtoll(p, &stop, 10);
    return stop != p && *stop == '/';
}

size_t header_callback(char* data, size_t size, size_t nitems, void* userp) {
    RangeInfo* info = static_cast<RangeInfo*>(userp);
    size_t len = size * nitems;

    // Cada respuesta (incluidas las 3xx de una redirección) empieza con la línea
    // de estado: se reinicia para quedarnos solo con la cabecera de la final.
    if (starts_with_nocase(data, len, "http/")) {
        info->present = false;
    } else if (starts_with_nocase(data, len, "content-range:")) {
        std::string value(data + 14, len - 14);
        info->present = parse_content_range(value, info->start, info->end);
    }
    return len;
}

bool parse_offset(const char* text, long long& value) {
    errno = 0;
    char* end = nullptr;
    value = std::strtoll(text, &end, 10);
    return errno == 0 && end != text && *end == '\0' && value >= 0;
}

void print_usage(const char* prog) {
    std::fprintf(stderr,
                 "Uso: %s <url> <inicio> <fin> <archivo_temporal>\n"
                 "  inicio y fin son offsets en bytes, inclusivos (fin >= inicio).\n",
                 prog);
}

}  // namespace

int main(int argc, char* argv[]) {
    if (argc != 5) {
        print_usage(argv[0]);
        return 1;
    }

    const char* url = argv[1];
    long long start = 0, end = 0;
    if (!parse_offset(argv[2], start) || !parse_offset(argv[3], end) || end < start) {
        std::fprintf(stderr, "Error: rango inválido '%s'-'%s'\n", argv[2], argv[3]);
        print_usage(argv[0]);
        return 1;
    }
    const char* out_path = argv[4];
    const long long expected = end - start + 1;

    FILE* file = std::fopen(out_path, "wb");
    if (!file) {
        std::perror("Error abriendo archivo de salida");
        return 2;
    }

    if (curl_global_init(CURL_GLOBAL_DEFAULT) != CURLE_OK) {
        std::fprintf(stderr, "Error: no se pudo inicializar libcurl\n");
        std::fclose(file);
        return 3;
    }

    CURL* curl = curl_easy_init();
    if (!curl) {
        std::fprintf(stderr, "Error: curl_easy_init falló\n");
        std::fclose(file);
        curl_global_cleanup();
        return 3;
    }

    const std::string range = std::to_string(start) + "-" + std::to_string(end);
    WriteContext ctx = {file, 0};
    RangeInfo range_info = {false, 0, 0};
    char errbuf[CURL_ERROR_SIZE] = {0};

    curl_easy_setopt(curl, CURLOPT_URL, url);
    curl_easy_setopt(curl, CURLOPT_RANGE, range.c_str());
    curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, write_callback);
    curl_easy_setopt(curl, CURLOPT_WRITEDATA, &ctx);
    curl_easy_setopt(curl, CURLOPT_HEADERFUNCTION, header_callback);
    curl_easy_setopt(curl, CURLOPT_HEADERDATA, &range_info);
    curl_easy_setopt(curl, CURLOPT_FOLLOWLOCATION, 1L);
    curl_easy_setopt(curl, CURLOPT_FAILONERROR, 1L);
    curl_easy_setopt(curl, CURLOPT_CONNECTTIMEOUT, 15L);
    curl_easy_setopt(curl, CURLOPT_USERAGENT, "idm-clon-core/0.1");
    curl_easy_setopt(curl, CURLOPT_ERRORBUFFER, errbuf);

    CURLcode res = curl_easy_perform(curl);
    long http_code = 0;
    curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &http_code);

    curl_easy_cleanup(curl);
    curl_global_cleanup();
    std::fclose(file);

    if (res != CURLE_OK) {
        std::fprintf(stderr, "Error de descarga: %s\n", errbuf[0] ? errbuf : curl_easy_strerror(res));
        return 3;
    }

    // std::to_string evita %lld, que el msvcrt de MinGW antiguo no soporta.
    const std::string written = std::to_string(static_cast<long long>(ctx.written));

    // Validación 1: el servidor debe devolver 206 con un Content-Range igual al
    // pedido. Con file:// o ftp:// no hay código HTTP (0) ni cabeceras.
    if (http_code != 0) {
        if (http_code != 206) {
            std::fprintf(stderr,
                         "Error: el servidor respondió %ld en lugar de 206; no soporta rangos.\n",
                         http_code);
            std::remove(out_path);
            return 4;
        }
        if (!range_info.present) {
            std::fprintf(stderr, "Error: respuesta 206 sin cabecera Content-Range válida.\n");
            std::remove(out_path);
            return 4;
        }
        if (range_info.start != start || range_info.end != end) {
            std::fprintf(stderr, "Error: se pidió el rango %s pero el servidor devolvió %s-%s.\n",
                         range.c_str(), std::to_string(range_info.start).c_str(),
                         std::to_string(range_info.end).c_str());
            std::remove(out_path);
            return 4;
        }
    }

    // Validación 2: los bytes escritos deben coincidir con el tamaño del rango.
    if (ctx.written != expected) {
        std::fprintf(stderr, "Error: se recibieron %s bytes pero el rango %s tiene %s.\n",
                     written.c_str(), range.c_str(), std::to_string(expected).c_str());
        std::remove(out_path);
        return 5;
    }

    std::printf("OK rango=%s bytes=%s archivo=%s\n", range.c_str(), written.c_str(), out_path);
    return 0;
}
