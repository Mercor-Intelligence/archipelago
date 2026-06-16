/*
 * decrypt_fs.c - LD_PRELOAD library: transparent at-rest decryption for a SQLite DB
 *
 * PROOF OF CONCEPT for "Path B": instead of trying to BLOCK the agent from the
 * DB file (an access-control wall that fails under a privilege-flattening
 * sandbox), we ENCRYPT the DB on disk and load THIS shim into the trusted app
 * process. The shim holds the key and decrypts page bytes on the fly as the
 * app's SQLite engine reads them. The agent's `bash` is a separate process that
 * never loads this shim and has no key, so a raw `cat` of the file yields
 * ciphertext.
 *
 * This is the sibling of code_execution_server/sandbox_fs.c: same LD_PRELOAD
 * interposition technique (dlsym(RTLD_NEXT, ...)), opposite job — that one
 * DENIES paths for the untrusted model; this one DECRYPTS for the trusted app.
 *
 * Scheme: AES-256-CTR over the whole file, with the 128-bit counter derived
 * from the byte offset (counter = offset / 16, big-endian, starting at 0 for
 * offset 0). Because the counter is a pure function of the offset, any aligned
 * region can be decrypted independently — which is exactly what SQLite needs
 * for random-access page reads, with no need to hold the whole DB in memory.
 *
 * Compile (Linux):
 *   gcc -shared -fPIC -O2 -o decrypt_fs.so decrypt_fs.c -ldl -lpthread -lcrypto
 * Usage (loaded into the APP, not the agent):
 *   DB_ENC_KEY=<64 hex chars>  DB_ENC_PATH=/.apps_data/<svc>/data.db \
 *     LD_PRELOAD=/path/decrypt_fs.so  <app or sqlite3 ...>
 *
 * PoC KEY HANDLING: the key is read from DB_ENC_KEY for demonstration only.
 * In production the key MUST come from somewhere the agent cannot read (e.g.
 * injected into the app process at launch by the delivery layer, never written
 * to a file or an env var visible to the agent's shell). See README.md.
 */

#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

#include <openssl/evp.h>

/* ------------------------------------------------------------------ config */

#define AES_KEY_BYTES 32  /* AES-256 */
#define MAX_TRACKED_FDS 1024

static unsigned char g_key[AES_KEY_BYTES];
static int g_have_key = 0;
static char g_target_path[PATH_MAX];   /* canonical path of the encrypted DB */
static int g_have_target = 0;
static int g_debug = 0;

static pthread_once_t g_init_once = PTHREAD_ONCE_INIT;

/* Set of fds currently open against the encrypted DB. Guarded by g_lock. */
static int g_enc_fds[MAX_TRACKED_FDS];
static int g_enc_fds_count = 0;
static pthread_mutex_t g_lock = PTHREAD_MUTEX_INITIALIZER;

#define DBG(fmt, ...)                                                     \
    do {                                                                  \
        if (g_debug) fprintf(stderr, "[decrypt_fs] " fmt "\n", ##__VA_ARGS__); \
    } while (0)

/* --------------------------------------------------------- real functions */

static int (*real_open)(const char *, int, ...) = NULL;
static int (*real_open64)(const char *, int, ...) = NULL;
static int (*real_openat)(int, const char *, int, ...) = NULL;
static int (*real_openat64)(int, const char *, int, ...) = NULL;
static ssize_t (*real_pread)(int, void *, size_t, off_t) = NULL;
static ssize_t (*real_pread64)(int, void *, size_t, off_t) = NULL;
static ssize_t (*real_read)(int, void *, size_t) = NULL;
static off_t (*real_lseek)(int, off_t, int) = NULL;
static int (*real_close)(int) = NULL;

static void resolve_real(void) {
    real_open = dlsym(RTLD_NEXT, "open");
    real_open64 = dlsym(RTLD_NEXT, "open64");
    real_openat = dlsym(RTLD_NEXT, "openat");
    real_openat64 = dlsym(RTLD_NEXT, "openat64");
    real_pread = dlsym(RTLD_NEXT, "pread");
    real_pread64 = dlsym(RTLD_NEXT, "pread64");
    real_read = dlsym(RTLD_NEXT, "read");
    real_lseek = dlsym(RTLD_NEXT, "lseek");
    real_close = dlsym(RTLD_NEXT, "close");
}

/* --------------------------------------------------------------- key/init */

static int hex_to_bytes(const char *hex, unsigned char *out, size_t out_len) {
    if (strlen(hex) != out_len * 2) return -1;
    for (size_t i = 0; i < out_len; i++) {
        unsigned int b;
        if (sscanf(hex + 2 * i, "%2x", &b) != 1) return -1;
        out[i] = (unsigned char)b;
    }
    return 0;
}

static void init_once(void) {
    resolve_real();

    const char *dbg = getenv("SANDBOX_DEBUG");
    if (!dbg) dbg = getenv("DB_ENC_DEBUG");
    if (dbg && dbg[0] == '1') g_debug = 1;

    const char *key_hex = getenv("DB_ENC_KEY");
    if (key_hex && hex_to_bytes(key_hex, g_key, AES_KEY_BYTES) == 0) {
        g_have_key = 1;
    } else if (key_hex) {
        DBG("DB_ENC_KEY present but not %d hex bytes; decryption disabled",
            AES_KEY_BYTES);
    }

    const char *path = getenv("DB_ENC_PATH");
    if (path && path[0]) {
        /* Store the configured path as-is and also its realpath if it exists,
         * so matching works whether the app opens it by the same string or a
         * resolved one. For the PoC we compare against the configured string
         * and the realpath. */
        char resolved[PATH_MAX];
        if (real_open && realpath(path, resolved)) {
            strncpy(g_target_path, resolved, sizeof(g_target_path) - 1);
        } else {
            strncpy(g_target_path, path, sizeof(g_target_path) - 1);
        }
        g_target_path[sizeof(g_target_path) - 1] = '\0';
        g_have_target = 1;
    }

    DBG("init: have_key=%d target=%s", g_have_key,
        g_have_target ? g_target_path : "(none)");
}

static void ensure_init(void) { pthread_once(&g_init_once, init_once); }

/* ----------------------------------------------------------- fd tracking */

static int path_is_target(const char *path) {
    if (!g_have_target || !path) return 0;
    if (strcmp(path, g_target_path) == 0) return 1;
    char resolved[PATH_MAX];
    if (realpath(path, resolved) && strcmp(resolved, g_target_path) == 0) return 1;
    return 0;
}

static void track_fd(int fd) {
    if (fd < 0) return;
    pthread_mutex_lock(&g_lock);
    if (g_enc_fds_count < MAX_TRACKED_FDS) {
        g_enc_fds[g_enc_fds_count++] = fd;
        DBG("tracking fd %d (encrypted DB)", fd);
    }
    pthread_mutex_unlock(&g_lock);
}

static void untrack_fd(int fd) {
    pthread_mutex_lock(&g_lock);
    for (int i = 0; i < g_enc_fds_count; i++) {
        if (g_enc_fds[i] == fd) {
            g_enc_fds[i] = g_enc_fds[--g_enc_fds_count];
            break;
        }
    }
    pthread_mutex_unlock(&g_lock);
}

static int fd_is_encrypted(int fd) {
    int found = 0;
    pthread_mutex_lock(&g_lock);
    for (int i = 0; i < g_enc_fds_count; i++) {
        if (g_enc_fds[i] == fd) { found = 1; break; }
    }
    pthread_mutex_unlock(&g_lock);
    return found;
}

/* --------------------------------------------------------------- crypto */

/* Build the 16-byte big-endian counter block for AES-CTR at the given
 * 16-byte block index. */
static void counter_block(uint64_t block_index, unsigned char iv[16]) {
    memset(iv, 0, 16);
    for (int i = 0; i < 8; i++) {
        iv[15 - i] = (unsigned char)(block_index & 0xff);
        block_index >>= 8;
    }
}

/* Decrypt (== XOR keystream) `count` bytes of `buf` that live at absolute file
 * `offset`. Works for any offset/length by generating keystream from the
 * containing 16-byte block. Returns 0 on success, -1 on crypto failure. */
static int decrypt_in_place(unsigned char *buf, size_t count, off_t offset) {
    if (count == 0) return 0;

    uint64_t skip = (uint64_t)offset % 16;
    uint64_t block_index = (uint64_t)offset / 16;
    size_t ks_len = skip + count;

    unsigned char iv[16];
    counter_block(block_index, iv);

    unsigned char *zeros = calloc(1, ks_len);
    unsigned char *keystream = malloc(ks_len);
    if (!zeros || !keystream) { free(zeros); free(keystream); return -1; }

    int ok = -1;
    EVP_CIPHER_CTX *ctx = EVP_CIPHER_CTX_new();
    if (ctx) {
        int outl = 0;
        /* Encrypting zeros under AES-CTR yields the raw keystream. */
        if (EVP_EncryptInit_ex(ctx, EVP_aes_256_ctr(), NULL, g_key, iv) == 1 &&
            EVP_EncryptUpdate(ctx, keystream, &outl, zeros, (int)ks_len) == 1) {
            for (size_t i = 0; i < count; i++) buf[i] ^= keystream[skip + i];
            ok = 0;
        }
        EVP_CIPHER_CTX_free(ctx);
    }
    free(zeros);
    free(keystream);
    return ok;
}

/* ---------------------------------------------------------- interposers */

/* After a successful open of the target path, start tracking the fd. */
static void maybe_track(int fd, const char *pathname) {
    if (fd >= 0 && g_have_key && path_is_target(pathname)) track_fd(fd);
}

int open(const char *pathname, int flags, ...) {
    ensure_init();
    mode_t mode = 0;
    if (flags & O_CREAT) {
        va_list ap; va_start(ap, flags); mode = va_arg(ap, int); va_end(ap);
    }
    int fd = real_open(pathname, flags, mode);
    maybe_track(fd, pathname);
    return fd;
}

/* glibc binaries built with large-file support (the default for the stock
 * sqlite3 CLI) call open64/openat64/pread64 rather than the bare names, so we
 * must interpose those too or the fd is never tracked and reads stay encrypted. */
int open64(const char *pathname, int flags, ...) {
    ensure_init();
    mode_t mode = 0;
    if (flags & O_CREAT) {
        va_list ap; va_start(ap, flags); mode = va_arg(ap, int); va_end(ap);
    }
    int fd = real_open64 ? real_open64(pathname, flags, mode)
                         : real_open(pathname, flags, mode);
    maybe_track(fd, pathname);
    return fd;
}

int openat(int dirfd, const char *pathname, int flags, ...) {
    ensure_init();
    mode_t mode = 0;
    if (flags & O_CREAT) {
        va_list ap; va_start(ap, flags); mode = va_arg(ap, int); va_end(ap);
    }
    int fd = real_openat(dirfd, pathname, flags, mode);
    /* For the PoC we resolve absolute / AT_FDCWD paths; openat with a dirfd and
     * a relative path is left to realpath() best-effort inside path_is_target. */
    maybe_track(fd, pathname);
    return fd;
}

int openat64(int dirfd, const char *pathname, int flags, ...) {
    ensure_init();
    mode_t mode = 0;
    if (flags & O_CREAT) {
        va_list ap; va_start(ap, flags); mode = va_arg(ap, int); va_end(ap);
    }
    int fd = real_openat64 ? real_openat64(dirfd, pathname, flags, mode)
                           : real_openat(dirfd, pathname, flags, mode);
    maybe_track(fd, pathname);
    return fd;
}

static ssize_t pread_decrypt(ssize_t n, int fd, void *buf, off_t offset) {
    if (n > 0 && fd_is_encrypted(fd)) {
        if (decrypt_in_place((unsigned char *)buf, (size_t)n, offset) != 0) {
            DBG("decrypt failed (pread fd=%d off=%lld)", fd, (long long)offset);
            errno = EIO;
            return -1;
        }
    }
    return n;
}

ssize_t pread(int fd, void *buf, size_t count, off_t offset) {
    ensure_init();
    return pread_decrypt(real_pread(fd, buf, count, offset), fd, buf, offset);
}

ssize_t pread64(int fd, void *buf, size_t count, off_t offset) {
    ensure_init();
    ssize_t n = real_pread64 ? real_pread64(fd, buf, count, offset)
                             : real_pread(fd, buf, count, offset);
    return pread_decrypt(n, fd, buf, offset);
}

/* SQLite's unix VFS uses pread for DB reads, but plain read() is intercepted
 * too so the shim is correct for tools (and code paths) that use it. We capture
 * the current offset BEFORE the read so we know where the bytes came from. */
ssize_t read(int fd, void *buf, size_t count) {
    ensure_init();
    if (fd_is_encrypted(fd) && real_lseek) {
        off_t offset = real_lseek(fd, 0, SEEK_CUR);
        ssize_t n = real_read(fd, buf, count);
        if (n > 0 && offset >= 0) {
            if (decrypt_in_place((unsigned char *)buf, (size_t)n, offset) != 0) {
                errno = EIO;
                return -1;
            }
        }
        return n;
    }
    return real_read(fd, buf, count);
}

int close(int fd) {
    ensure_init();
    if (fd >= 0) untrack_fd(fd);
    return real_close(fd);
}
