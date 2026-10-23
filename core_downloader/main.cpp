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
//   3 = error de red/libcurl, 4 = el servidor no respetó el rango (no devolvió 206)

#include <curl/curl.h>

#include <cerrno>
#include <cstdio>
#include <cstdlib>
#include <string>

namespace {

struct WriteContext {
    FILE* file;
    curl_off_t written;
};

size_t write_callback(char* data, size_t size, size_t nmemb, void* userp) {
    WriteContext* ctx = static_cast<WriteContext*>(userp);
    size_t bytes = size * nmemb;
    size_t out = std::fwrite(data, 1, bytes, ctx->file);
    ctx->written += static_cast<curl_off_t>(out);
    return out;  // si out != bytes, libcurl aborta con CURLE_WRITE_ERROR
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
    char errbuf[CURL_ERROR_SIZE] = {0};

    curl_easy_setopt(curl, CURLOPT_URL, url);
    curl_easy_setopt(curl, CURLOPT_RANGE, range.c_str());
    curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, write_callback);
    curl_easy_setopt(curl, CURLOPT_WRITEDATA, &ctx);
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

    // 206 = Partial Content. Con file:// o ftp:// no hay código HTTP (0).
    if (http_code != 206 && http_code != 0) {
        std::fprintf(stderr,
                     "Error: el servidor respondió %ld en lugar de 206; no soporta rangos.\n",
                     http_code);
        return 4;
    }

    // std::to_string evita %lld, que el msvcrt de MinGW antiguo no soporta.
    const std::string written = std::to_string(static_cast<long long>(ctx.written));
    std::printf("OK rango=%s bytes=%s esperados=%s archivo=%s\n", range.c_str(),
                written.c_str(), std::to_string(expected).c_str(), out_path);
    return 0;
}
