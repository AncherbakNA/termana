/* Termana's Plink frontend bridge, not an SSH/cryptography implementation.
 * Included only by unix/plink.c and windows/plink.c in the pinned source build.
 * Binary length-bounded private control frames are never terminal output.
 * No PuTTY storage, console prompts, password arguments or secret files.
 */
#ifndef _WIN32
#include <sys/socket.h>
#include <sys/time.h>
#include <arpa/inet.h>
#include <unistd.h>
#define TM_SOCKET int
#define TM_INVALID (-1)
#else
#define TM_SOCKET SOCKET
#define TM_INVALID INVALID_SOCKET
#endif

#define TM_LIMIT 32768
static TM_SOCKET tm_socket = TM_INVALID;
static bool tm_host_approved = false;
static int tm_password_attempts = 0;
static void tm_die(void);
#ifdef _WIN32
static char tm_timer_context;
static void tm_echoedit(Seat *seat, bool echo, bool edit)
{
    /* Remote PTY owns echo, line editing and signals. Keep Ctrl-C and VT keys
     * as input bytes, not a local Windows console control event or editor. */
    if (!SetConsoleMode(GetStdHandle(STD_INPUT_HANDLE), ENABLE_VIRTUAL_TERMINAL_INPUT))
        tm_die();
}
#endif

static void tm_die(void)
{
    fputs("Termana SSH1 control channel failed.\n", stderr);
    exit(255);
}

static void tm_io(void *buffer, size_t length, bool writing)
{
    char *p = buffer;
    while (length) {
        int n = writing ? send(tm_socket, p, (int)length, 0)
                        : recv(tm_socket, p, (int)length, 0);
        if (n <= 0) tm_die();
        p += n;
        length -= n;
    }
}

static void tm_send(unsigned char kind, const void *data, size_t length)
{
    unsigned char header[5];
    if (length > TM_LIMIT) tm_die();
    header[0] = kind;
    header[1] = length >> 24; header[2] = length >> 16;
    header[3] = length >> 8; header[4] = length;
    tm_io(header, 5, true);
    if (length) tm_io((void *)data, length, true);
}

static strbuf *tm_answer(void)
{
    unsigned char header[5];
    tm_io(header, 5, false);
    size_t length = ((size_t)header[1]<<24) | ((size_t)header[2]<<16) |
                    ((size_t)header[3]<<8) | header[4];
    if (header[0] != 'A' || length > TM_LIMIT) tm_die();
    strbuf *answer = strbuf_new_nm();
    if (length) tm_io(strbuf_append(answer, length), length, false);
    return answer;
}

static void tm_expect_ok(void)
{
    strbuf *answer = tm_answer();
    bool ok = answer->len == 2 && !memcmp(answer->s, "OK", 2);
    strbuf_free(answer);
    if (!ok) tm_die();
}

static void tm_init(void)
{
    const char *port = getenv("TERMANA_SSH1_CONTROL_PORT");
    const char *token = getenv("TERMANA_SSH1_CONTROL_TOKEN");
    if (!port || !token || strlen(token) != 64 || strspn(token, "0123456789abcdef") != 64)
        tm_die();
    char *end;
    unsigned long number = strtoul(port, &end, 10);
    if (*end || number < 1 || number > 65535) tm_die();
    tm_socket = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (tm_socket == TM_INVALID) tm_die();
#ifdef _WIN32
    DWORD timeout = 65000;
    if (setsockopt(tm_socket, SOL_SOCKET, SO_RCVTIMEO, (char *)&timeout, sizeof(timeout)) ||
        setsockopt(tm_socket, SOL_SOCKET, SO_SNDTIMEO, (char *)&timeout, sizeof(timeout))) tm_die();
    unsigned long pid = GetCurrentProcessId();
#else
    struct timeval timeout = {65, 0};
    if (setsockopt(tm_socket, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout)) ||
        setsockopt(tm_socket, SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof(timeout))) tm_die();
#ifdef SO_NOSIGPIPE
    int one = 1;
    setsockopt(tm_socket, SOL_SOCKET, SO_NOSIGPIPE, &one, sizeof(one));
#endif
    unsigned long pid = getpid();
#endif
    struct sockaddr_in addr;
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    addr.sin_port = htons((unsigned short)number);
    if (connect(tm_socket, (struct sockaddr *)&addr, sizeof(addr))) tm_die();
    char *hello = dupprintf("1:%lu:%s", pid, token);
    tm_send('H', hello, strlen(hello));
    smemclr(hello, strlen(hello)); sfree(hello);
    tm_expect_ok();
    /* No inherited authentication method/config or protocol fallback. */
    if (conf_get_int(conf, CONF_protocol) != PROT_SSH ||
        conf_get_int(conf, CONF_sshprot) != 0) tm_die();
    conf_set_bool(conf, CONF_try_tis_auth, false);
    conf_set_bool(conf, CONF_tryagent, false);
#ifdef _WIN32
    CONSOLE_SCREEN_BUFFER_INFO info;
    if (!GetConsoleScreenBufferInfo(GetStdHandle(STD_OUTPUT_HANDLE), &info)) tm_die();
    DWORD outmode;
    if (!GetConsoleMode(GetStdHandle(STD_OUTPUT_HANDLE), &outmode) ||
        !SetConsoleMode(GetStdHandle(STD_OUTPUT_HANDLE), outmode |
            ENABLE_PROCESSED_OUTPUT | ENABLE_VIRTUAL_TERMINAL_PROCESSING | DISABLE_NEWLINE_AUTO_RETURN) ||
        !SetConsoleCP(CP_UTF8) || !SetConsoleOutputCP(CP_UTF8)) tm_die();
    tm_echoedit(NULL, false, false);
    conf_set_int(conf, CONF_width, info.srWindow.Right-info.srWindow.Left+1);
    conf_set_int(conf, CONF_height, info.srWindow.Bottom-info.srWindow.Top+1);
#endif
}

static SeatPromptResult tm_host_key(
    Seat *seat, const char *host, int port, const char *keytype,
    char *keystr, SeatDialogText *text, HelpCtx helpctx,
    void (*callback)(void *ctx, SeatPromptResult result), void *ctx)
{
    if (tm_socket == TM_INVALID || tm_host_approved || strcmp(keytype, "rsa"))
        return SPR_USER_ABORT;
    const char *fingerprint = NULL;
    for (size_t i=0; i<text->nitems; i++)
        if (text->items[i].type == SDT_DISPLAY) fingerprint = text->items[i].text;
    if (!fingerprint) return SPR_USER_ABORT;
    strbuf *message = strbuf_new();
    put_asciz(message, host);
    put_fmt(message, "%d", port); put_byte(message, 0);
    put_asciz(message, keytype); put_asciz(message, keystr);
    put_dataz(message, fingerprint);
    tm_send('K', message->s, message->len);
    strbuf_free(message);
    strbuf *answer = tm_answer();
    tm_host_approved = answer->len == 2 && !memcmp(answer->s, "OK", 2);
    strbuf_free(answer);
    return tm_host_approved ? SPR_OK : SPR_USER_ABORT;
}

static SeatPromptResult tm_userpass(Seat *seat, prompts_t *p)
{
    if (!tm_host_approved || p->n_prompts != 1 || p->prompts[0]->echo || p->from_server)
        return SPR_USER_ABORT;
    bool password = p->to_server && p->name && !strcmp(p->name, "SSH password");
    bool passphrase = !p->to_server && p->name && !strcmp(p->name, "SSH key passphrase");
    if ((!password && !passphrase) || ++tm_password_attempts > 6) return SPR_USER_ABORT;
    tm_send('P', password ? "password" : "passphrase", password ? 8 : 10);
    strbuf *answer = tm_answer();
    if (!answer->len || answer->len > 16384 || memchr(answer->s, 0, answer->len)) {
        strbuf_free(answer); return SPR_USER_ABORT;
    }
    prompt_set_result(p->prompts[0], answer->s);
    strbuf_free(answer);
    return SPR_OK;
}

static SeatPromptResult tm_weak(
    Seat *seat, SeatDialogText *text,
    void (*callback)(void *ctx, SeatPromptResult result), void *ctx)
{
    /* Whole-protocol insecure consent is required by the parent every launch. */
    return tm_host_approved ? SPR_OK : SPR_USER_ABORT;
}

#ifdef _WIN32
static void tm_resize(void *ctx, unsigned long now)
{
    static int columns = 0, rows = 0;
    CONSOLE_SCREEN_BUFFER_INFO info;
    if (backend && GetConsoleScreenBufferInfo(GetStdHandle(STD_OUTPUT_HANDLE), &info)) {
        int c = info.srWindow.Right-info.srWindow.Left+1;
        int r = info.srWindow.Bottom-info.srWindow.Top+1;
        if (columns != c || rows != r) {
            backend_size(backend, c, r);
            columns = c; rows = r;
        }
    }
    schedule_timer(100, tm_resize, &tm_timer_context);
}
#endif

static void tm_ready(Seat *seat)
{
    if (!tm_host_approved) tm_die();
    tm_send('R', "", 0); tm_expect_ok();
#ifdef _WIN32
    schedule_timer(1, tm_resize, &tm_timer_context);
#endif
}
