// core_downloader: descarga un único fragmento (rango de bytes) de una URL.
//
// Uso:
//   core_downloader <url> <inicio> <fin> <archivo_temporal> [--append]
//
// Ejemplo:
//   core_downloader https://example.com/file.zip 0 1048575 file.zip.part0
//
// Con --append los bytes se añaden al final del archivo existente en lugar de
// sobrescribirlo: así el orquestador reanuda un fragmento a medias pidiendo
// solo el rango que falta.
//
// Códigos de salida:
//   0 = OK, 1 = argumentos inválidos, 2 = error de archivo,
//   3 = error de red/libcurl,
//   4 = el servidor no respetó el rango (no devolvió 206 o Content-Range distinto al pedido),
//   5 = los bytes recibidos no coinciden con el tamaño del rango
//
// La respuesta se valida antes de escribir el primer byte y nunca se escriben
// más bytes que los del rango, así que todo lo que llega al disco es un prefijo
// correcto del rango:
//   - Con 4 o 5 se descarta lo escrito en esta ejecución (se borra el archivo,
//     o con --append se trunca a su tamaño inicial).
//   - Con 3 (red caída, proceso interrumpido...) lo escrito se conserva para
//     poder reanudar.
//
// Progreso: por stdout se emiten líneas "PROGRESS <bytes_escritos> <bytes_esperados>"
// (relativas al rango pedido) cada vez que el avance sube al menos un 1 %, y al
// terminar una línea "OK ...".

#include <curl/curl.h>

#include <io.h>

#include <cctype>
#include <cerrno>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>

namespace {

// Content-Range de la respuesta final (tras seguir redirecciones).
struct RangeInfo {
    bool present;
    long long start;
    long long end;
};

struct WriteContext {
    FILE* file;
    CURL* curl;
    const RangeInfo* range;
    long long start;
    long long end;
    long long written;
    bool checked;        // la respuesta ya se validó
    int error;           // código de salida si la validación falló (4 o 5), si no 0
    std::string message;
};

struct ProgressContext {
    const WriteContext* write;
    int last_percent;
};

// Validación 1: el servidor debe devolver 206 con un Content-Range igual al
// pedido. Con file:// o ftp:// no hay código HTTP (0) ni cabeceras.
bool check_response(WriteContext* ctx) {
    ctx->checked = true;
    long http_code = 0;
    curl_easy_getinfo(ctx->curl, CURLINFO_RESPONSE_CODE, &http_code);
    if (http_code == 0) return true;

    if (http_code != 206) {
        ctx->message = "el servidor respondió " + std::to_string(http_code) +
                       " en lugar de 206; no soporta rangos.";
    } else if (!ctx->range->present) {
        ctx->message = "respuesta 206 sin cabecera Content-Range válida.";
    } else if (ctx->range->start != ctx->start || ctx->range->end != ctx->end) {
        ctx->message = "se pidió el rango " + std::to_string(ctx->start) + "-" +
                       std::to_string(ctx->end) + " pero el servidor devolvió " +
                       std::to_string(ctx->range->start) + "-" + std::to_string(ctx->range->end) + ".";
    } else {
        return true;
    }
    ctx->error = 4;
    return false;
}

void report_progress(long long written, long long expected) {
    std::printf("PROGRESS %s %s\n", std::to_string(written).c_str(),
                std::to_string(expected).c_str());
    std::fflush(stdout);  // stdout es un pipe (búfer completo) cuando lo lee el orquestador
}

// Se usan los bytes ya escritos en disco en lugar de dlnow: dlnow se reinicia
// en cada redirección y no refleja lo que de verdad contiene el fragmento.
int xferinfo_callback(void* userp, curl_off_t, curl_off_t, curl_off_t, curl_off_t) {
    ProgressContext* ctx = static_cast<ProgressContext*>(userp);
    long long expected = ctx->write->end - ctx->write->start + 1;
    long long written = ctx->write->written;
    int percent = static_cast<int>(written * 100 / expected);
    if (percent > ctx->last_percent) {
        ctx->last_percent = percent;
        report_progress(written, expected);
    }
    return 0;  // distinto de 0 abortaría la transferencia
}

// Devolver un valor distinto de los bytes recibidos aborta la transferencia
// (CURLE_WRITE_ERROR); se usa para no escribir nunca datos no válidos.
size_t write_callback(char* data, size_t size, size_t nmemb, void* userp) {
    WriteContext* ctx = static_cast<WriteContext*>(userp);
    size_t bytes = size * nmemb;

    if (!ctx->checked && !check_response(ctx)) return 0;

    // Validación 2 (por exceso): el servidor envía más de lo que mide el rango.
    long long expected = ctx->end - ctx->start + 1;
    if (ctx->written + static_cast<long long>(bytes) > expected) {
        ctx->error = 5;
        ctx->message = "el servidor envía más bytes de los " + std::to_string(expected) +
                       " que tiene el rango.";
        return 0;
    }

    size_t out = std::fwrite(data, 1, bytes, ctx->file);
    ctx->written += static_cast<long long>(out);
    return out;
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
                 "Uso: %s <url> <inicio> <fin> <archivo_temporal> [--append]\n"
                 "  inicio y fin son offsets en bytes, inclusivos (fin >= inicio).\n"
                 "  --append añade al final del archivo en lugar de sobrescribirlo.\n",
                 prog);
}

}  // namespace

int main(int argc, char* argv[]) {
    const bool append = argc == 6 && std::strcmp(argv[5], "--append") == 0;
    if (argc != 5 && !append) {
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

    FILE* file = std::fopen(out_path, append ? "ab" : "wb");
    if (!file) {
        std::perror("Error abriendo archivo de salida");
        return 2;
    }
    // Tamaño previo, para poder deshacer lo escrito en esta ejecución.
    _fseeki64(file, 0, SEEK_END);
    const long long initial_size = _ftelli64(file);

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
    RangeInfo range_info = {false, 0, 0};
    WriteContext ctx = {file, curl, &range_info, start, end, 0, false, 0, std::string()};
    ProgressContext progress = {&ctx, -1};
    char errbuf[CURL_ERROR_SIZE] = {0};

    curl_easy_setopt(curl, CURLOPT_URL, url);
    curl_easy_setopt(curl, CURLOPT_RANGE, range.c_str());
    curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, write_callback);
    curl_easy_setopt(curl, CURLOPT_WRITEDATA, &ctx);
    curl_easy_setopt(curl, CURLOPT_HEADERFUNCTION, header_callback);
    curl_easy_setopt(curl, CURLOPT_HEADERDATA, &range_info);
    curl_easy_setopt(curl, CURLOPT_XFERINFOFUNCTION, xferinfo_callback);
    curl_easy_setopt(curl, CURLOPT_XFERINFODATA, &progress);
    curl_easy_setopt(curl, CURLOPT_NOPROGRESS, 0L);
    curl_easy_setopt(curl, CURLOPT_FOLLOWLOCATION, 1L);
    curl_easy_setopt(curl, CURLOPT_FAILONERROR, 1L);
    curl_easy_setopt(curl, CURLOPT_CONNECTTIMEOUT, 15L);
    curl_easy_setopt(curl, CURLOPT_USERAGENT, "idm-clon-core/0.1");
    curl_easy_setopt(curl, CURLOPT_ERRORBUFFER, errbuf);

    CURLcode res = curl_easy_perform(curl);

    int code = 0;
    std::string message;
    if (ctx.error != 0) {
        code = ctx.error;
        message = "Error: " + ctx.message;
    } else if (res != CURLE_OK) {
        code = 3;
        message = std::string("Error de descarga: ") + (errbuf[0] ? errbuf : curl_easy_strerror(res));
    } else if (!ctx.checked && !check_response(&ctx)) {
        code = 4;  // respuesta sin cuerpo: no pasó por write_callback
        message = "Error: " + ctx.message;
    } else if (ctx.written != expected) {
        code = 5;
        message = "Error: se recibieron " + std::to_string(ctx.written) + " bytes pero el rango " +
                  range + " tiene " + std::to_string(expected) + ".";
    }

    curl_easy_cleanup(curl);
    curl_global_cleanup();

    const bool discard = code == 4 || code == 5;
    if (discard && append) {
        std::fflush(file);
        _chsize_s(_fileno(file), initial_size);
    }
    std::fclose(file);
    if (discard && !append) std::remove(out_path);

    if (code != 0) {
        std::fprintf(stderr, "%s\n", message.c_str());
        return code;
    }

    std::printf("OK rango=%s bytes=%s archivo=%s\n", range.c_str(),
                std::to_string(ctx.written).c_str(), out_path);
    return 0;
}
