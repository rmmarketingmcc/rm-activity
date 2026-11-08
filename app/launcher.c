// RM Activity tracker launcher. launchd starts this small program inside RM Activity.app, so macOS ties the
// permissions (Accessibility for window titles, Automation for Chrome tabs) to "RM Activity" and never to
// Python as a whole. It runs /usr/bin/python3 -I <app>/Contents/Resources/activity_tracker.py run with
// RM_ACTIVITY_HOME = ~/Library/Application Support/RM Activity, and passes stop signals on.
#include <errno.h>
#include <libgen.h>
#include <limits.h>
#include <mach-o/dyld.h>
#include <signal.h>
#include <spawn.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/wait.h>
extern char **environ;
static pid_t child = 0;
static void pass_on(int sig) { if (child > 0) kill(child, sig); }
int main(int argc, char **argv) {
    char exe[PATH_MAX], real[PATH_MAX], script[PATH_MAX], home[PATH_MAX];
    uint32_t size = sizeof(exe);
    if (_NSGetExecutablePath(exe, &size) != 0 || !realpath(exe, real)) return 1;
    snprintf(script, sizeof(script), "%s/../Resources/activity_tracker.py", dirname(real));
    const char *h = getenv("HOME");
    if (!h) return 1;
    snprintf(home, sizeof(home), "%s/Library/Application Support/RM Activity", h);
    setenv("RM_ACTIVITY_HOME", home, 1);
    char *args[] = {"/usr/bin/python3", "-I", script, argc > 1 ? argv[1] : "run", 0};
    signal(SIGTERM, pass_on); signal(SIGINT, pass_on); signal(SIGHUP, pass_on);
    if (posix_spawn(&child, "/usr/bin/python3", 0, 0, args, environ) != 0) return 1;
    int status = 0;
    while (waitpid(child, &status, 0) < 0) if (errno != EINTR) return 1;
    return WIFEXITED(status) ? WEXITSTATUS(status) : 1;
}
