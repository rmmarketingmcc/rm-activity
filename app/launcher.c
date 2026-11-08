// RM Activity tracker launcher. launchd starts this small program inside RM Activity.app, so macOS ties the
// permissions (Accessibility for window titles, Automation for Chrome tabs) to "RM Activity" and never to
// Python as a whole.
//
// It only runs the exact tracker it was built with: it reads Resources/activity_tracker.py, checks its SHA-256
// against SCRIPT_SHA256 (set by build.sh) and hands Python the checked text on stdin. A changed file is never
// run, so nothing else on the Mac can borrow the permissions given to RM Activity by editing the script, and
// there is no gap between the check and the run. Changing this program itself changes its code signature,
// and macOS then no longer applies the permissions.
//
// Runs: /usr/bin/python3 -I - <command>  with RM_ACTIVITY_HOME = ~/Library/Application Support/RM Activity.
#include <CommonCrypto/CommonDigest.h>
#include <errno.h>
#include <libgen.h>
#include <limits.h>
#include <mach-o/dyld.h>
#include <signal.h>
#include <spawn.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>

#ifndef SCRIPT_SHA256
#error "build with -DSCRIPT_SHA256=\"<hex>\" (build.sh does)"
#endif

extern char **environ;
static pid_t child = 0;
static void pass_on(int sig) { if (child > 0) kill(child, sig); }

static unsigned char *read_file(const char *path, size_t *len) {
    FILE *f = fopen(path, "rb");
    if (!f) return NULL;
    size_t cap = 1 << 17, n = 0;
    unsigned char *buf = malloc(cap);
    for (size_t got; buf && (got = fread(buf + n, 1, cap - n, f)) > 0;) {
        n += got;
        if (n == cap) {
            if (cap >= (8u << 20)) { free(buf); buf = NULL; break; }  // never a tracker this big
            unsigned char *more = realloc(buf, cap *= 2);
            if (!more) { free(buf); buf = NULL; break; }
            buf = more;
        }
    }
    fclose(f);
    *len = n;
    return buf;
}

int main(int argc, char **argv) {
    char exe[PATH_MAX], real[PATH_MAX], script[PATH_MAX], home[PATH_MAX], hex[CC_SHA256_DIGEST_LENGTH * 2 + 1];
    uint32_t size = sizeof(exe);
    if (_NSGetExecutablePath(exe, &size) != 0 || !realpath(exe, real)) return 1;
    snprintf(script, sizeof(script), "%s/../Resources/activity_tracker.py", dirname(real));

    size_t len = 0;
    unsigned char *code = read_file(script, &len), digest[CC_SHA256_DIGEST_LENGTH];
    if (!code) { fprintf(stderr, "RM Activity: cannot read %s\n", script); return 78; }
    CC_SHA256(code, (CC_LONG)len, digest);
    for (int i = 0; i < CC_SHA256_DIGEST_LENGTH; i++) snprintf(hex + i * 2, 3, "%02x", digest[i]);
    if (strcmp(hex, SCRIPT_SHA256) != 0) {
        fprintf(stderr, "RM Activity: the tracker file was changed after install, so it is not run. "
                        "Install RM Activity again to repair it.\n");
        return 78;
    }

    const char *h = getenv("HOME");
    if (!h) return 1;
    snprintf(home, sizeof(home), "%s/Library/Application Support/RM Activity", h);
    setenv("RM_ACTIVITY_HOME", home, 1);
    setenv("RM_ACTIVITY_SCRIPT", script, 1);

    int fds[2];
    if (pipe(fds) != 0) return 1;
    posix_spawn_file_actions_t actions;
    posix_spawn_file_actions_init(&actions);
    posix_spawn_file_actions_adddup2(&actions, fds[0], 0);
    posix_spawn_file_actions_addclose(&actions, fds[0]);
    posix_spawn_file_actions_addclose(&actions, fds[1]);
    char *args[] = {"/usr/bin/python3", "-I", "-", argc > 1 ? argv[1] : "run", 0};
    signal(SIGPIPE, SIG_IGN);
    signal(SIGTERM, pass_on); signal(SIGINT, pass_on); signal(SIGHUP, pass_on);
    if (posix_spawn(&child, "/usr/bin/python3", &actions, 0, args, environ) != 0) return 1;
    posix_spawn_file_actions_destroy(&actions);
    close(fds[0]);
    for (size_t off = 0; off < len;) {  // Python reads the whole program before it runs any of it
        ssize_t w = write(fds[1], code + off, len - off);
        if (w < 0) { if (errno == EINTR) continue; break; }
        off += (size_t)w;
    }
    close(fds[1]);
    free(code);

    int status = 0;
    while (waitpid(child, &status, 0) < 0) if (errno != EINTR) return 1;
    return WIFEXITED(status) ? WEXITSTATUS(status) : 1;
}
