/* Experimental Darwin spawn compatibility, never a containment mechanism.
 * The caller MUST separately deny native spawn/setsid/setpgid in Seatbelt.
 * Public POSIX file actions are recorded without inspecting opaque libc types.
 * Unsupported attributes/actions fail; there is no native-spawn fallback.
 */
#define _DARWIN_C_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <pthread.h>
#include <signal.h>
#include <spawn.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>

#define MAX_ACTIONS 128
#define MAX_PATHS 64
#define MAX_FDS 65536
enum kind { CLOSE, DUP, OPEN, CHDIR, FCHDIR, INHERIT };
struct action { enum kind kind; int fd, other, flags; mode_t mode; char path[PATH_MAX]; };
struct record {
    const posix_spawn_file_actions_t *key;
    size_t count;
    struct action actions[MAX_ACTIONS];
    struct record *next;
};
static pthread_mutex_t records_lock = PTHREAD_MUTEX_INITIALIZER;
static struct record *records;
static void before_fork(void) { pthread_mutex_lock(&records_lock); }
static void after_fork(void) { pthread_mutex_unlock(&records_lock); }
__attribute__((constructor)) static void fork_hooks(void) {
    if (pthread_atfork(before_fork,after_fork,after_fork)) _exit(126);
}

static struct record *find(const posix_spawn_file_actions_t *key) {
    for (struct record *r = records; r; r = r->next) if (r->key == key) return r;
    return NULL;
}
static int dk_init(posix_spawn_file_actions_t *key) {
    struct record *r = calloc(1, sizeof(*r));
    if (!r) return ENOMEM;
    int error = posix_spawn_file_actions_init(key);
    if (error) { free(r); return error; }
    r->key = key;
    pthread_mutex_lock(&records_lock);
    r->next = records; records = r;
    pthread_mutex_unlock(&records_lock);
    return 0;
}
static int dk_destroy(posix_spawn_file_actions_t *key) {
    pthread_mutex_lock(&records_lock);
    struct record **p = &records;
    while (*p && (*p)->key != key) p = &(*p)->next;
    if (!*p) { pthread_mutex_unlock(&records_lock); return ENOTSUP; }
    struct record *r = *p; *p = r->next;
    pthread_mutex_unlock(&records_lock);
    free(r);
    return posix_spawn_file_actions_destroy(key);
}
static int add(posix_spawn_file_actions_t *key, enum kind kind, int fd, int other,
               const char *path, int flags, mode_t mode) {
    if ((kind != CHDIR && fd < 0) || (kind == DUP && other < 0)) return EBADF;
    if (fd >= MAX_FDS || other >= MAX_FDS) return ENOTSUP;
    if (path && strlen(path) >= PATH_MAX) return ENAMETOOLONG;
    pthread_mutex_lock(&records_lock);
    struct record *r = find(key);
    int error = 0;
    if (!r || r->count == MAX_ACTIONS) error = ENOTSUP;
    else {
        struct action *a = &r->actions[r->count++];
        *a = (struct action){ .kind=kind, .fd=fd, .other=other, .flags=flags, .mode=mode };
        if (path) strcpy(a->path, path);
    }
    pthread_mutex_unlock(&records_lock);
    return error;
}
/* The adapter owns the action interpretation. Genuine empty libc objects are
 * retained for correct init/destroy, but action internals are never accessed. */
static int dk_close(posix_spawn_file_actions_t *k, int fd) { return add(k,CLOSE,fd,0,NULL,0,0); }
static int dk_dup(posix_spawn_file_actions_t *k, int fd, int to) { return add(k,DUP,fd,to,NULL,0,0); }
static int dk_open(posix_spawn_file_actions_t *k, int fd, const char *p, int f, mode_t m) { return add(k,OPEN,fd,0,p,f,m); }
static int dk_chdir(posix_spawn_file_actions_t *k, const char *p) { return add(k,CHDIR,0,0,p,0,0); }
static int dk_fchdir(posix_spawn_file_actions_t *k, int fd) { return add(k,FCHDIR,fd,0,NULL,0,0); }
static int dk_inherit(posix_spawn_file_actions_t *k, int fd) { return add(k,INHERIT,fd,0,NULL,0,0); }

struct launch {
    size_t count, paths;
    struct action actions[MAX_ACTIONS];
    char candidates[MAX_PATHS][PATH_MAX];
    short flags;
    sigset_t mask;
    struct sigaction signals[NSIG];
    unsigned char keep[MAX_FDS];
    int fd_limit, max_action_fd;
};
static int prepare(struct launch *l, const char *file, int search,
                   const posix_spawn_file_actions_t *actions, const posix_spawnattr_t *attrs) {
    int error;
    if (attrs && (error = posix_spawnattr_getflags(attrs, &l->flags))) return error;
    if (l->flags & (POSIX_SPAWN_SETSID | POSIX_SPAWN_SETPGROUP)) return EPERM;
    short supported = POSIX_SPAWN_SETSIGDEF | POSIX_SPAWN_SETSIGMASK | POSIX_SPAWN_CLOEXEC_DEFAULT;
    if (l->flags & ~supported) return ENOTSUP;
    sigset_t defaults;
    sigemptyset(&defaults);
    if ((l->flags & POSIX_SPAWN_SETSIGDEF) &&
        (error = posix_spawnattr_getsigdefault(attrs, &defaults))) return error;
    if (pthread_sigmask(SIG_SETMASK, NULL, &l->mask)) return EINVAL;
    if ((l->flags & POSIX_SPAWN_SETSIGMASK) &&
        (error = posix_spawnattr_getsigmask(attrs, &l->mask))) return error;
    for (int s=1; s<NSIG; s++) {
        if (s == SIGKILL || s == SIGSTOP) continue;
        if (sigaction(s, NULL, &l->signals[s])) return errno;
        if (l->signals[s].sa_handler != SIG_IGN || sigismember(&defaults,s)) {
            l->signals[s] = (struct sigaction){ .sa_handler=SIG_DFL };
            sigemptyset(&l->signals[s].sa_mask);
        }
    }
    long limit = sysconf(_SC_OPEN_MAX);
    if (limit < 3 || limit > MAX_FDS) return ENOTSUP;
    l->fd_limit = (int)limit;
    l->max_action_fd = 2;
    l->keep[0] = l->keep[1] = l->keep[2] = 1;
    if (actions) {
        pthread_mutex_lock(&records_lock);
        struct record *r = find(actions);
        if (!r) { pthread_mutex_unlock(&records_lock); return ENOTSUP; }
        l->count = r->count;
        memcpy(l->actions, r->actions, r->count * sizeof(struct action));
        pthread_mutex_unlock(&records_lock);
    }
    for (size_t i=0; i<l->count; i++) {
        struct action *a = &l->actions[i];
        if (a->fd > l->max_action_fd) l->max_action_fd = a->fd;
        if (a->other > l->max_action_fd) l->max_action_fd = a->other;
        if (a->kind == CLOSE) l->keep[a->fd] = 0;
        if (a->kind == OPEN || a->kind == INHERIT) l->keep[a->fd] = 1;
        if (a->kind == DUP) l->keep[a->other] = 1;
    }
    if (!search || strchr(file,'/')) {
        if (strlen(file) >= PATH_MAX) return ENAMETOOLONG;
        strcpy(l->candidates[l->paths++], file);
    } else {
        const char *path = getenv("PATH");
        if (!path) path = "/usr/bin:/bin";
        do {
            const char *end = strchr(path, ':');
            size_t n = end ? (size_t)(end-path) : strlen(path);
            if (l->paths == MAX_PATHS || n + strlen(file) + 2 > PATH_MAX) return ENAMETOOLONG;
            char *out = l->candidates[l->paths++];
            memcpy(out,path,n);
            if (n) out[n++] = '/';
            strcpy(out+n,file);
            if (!end) break;
            path = end+1;
        } while (1);
    }
    return 0;
}
static void child_error(int fd, int error) {
    const char *p = (const char *)&error;
    size_t remaining = sizeof(error);
    while (remaining) {
        ssize_t n = write(fd,p,remaining);
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) break;
        p += n; remaining -= (size_t)n;
    }
    _exit(127);
}
static void execute(struct launch *l, int error_fd, char *const argv[], char *const envp[]) {
    /* Only async-signal-safe operations after fork; no allocation, locks or
     * getenv/PATH expansion in the child of a multithreaded runtime. */
    for (int s=1; s<NSIG; s++) if (s != SIGKILL && s != SIGSTOP)
        if (sigaction(s,&l->signals[s],NULL)) child_error(error_fd,errno);
    for (size_t i=0; i<l->count; i++) {
        struct action *a = &l->actions[i]; int result=0;
        switch (a->kind) {
        case CLOSE: if (close(a->fd) && errno != EBADF) child_error(error_fd,errno); break;
        case DUP:
            result = dup2(a->fd,a->other);
            if (result >= 0) result = fcntl(a->other,F_SETFD,0);
            break;
        case OPEN:
            result = open(a->path,a->flags,a->mode);
            if (result >= 0 && result != a->fd) {
                int opened = result; result = dup2(opened,a->fd); close(opened);
            }
            break;
        case CHDIR: result = chdir(a->path); break;
        case FCHDIR: result = fchdir(a->fd); break;
        case INHERIT: result = fcntl(a->fd,F_SETFD,0); break;
        }
        if (result < 0) child_error(error_fd,errno);
    }
    if (l->flags & POSIX_SPAWN_CLOEXEC_DEFAULT)
        for (int fd=3; fd<l->fd_limit; fd++) if (fd != error_fd && !l->keep[fd]) close(fd);
    if (sigprocmask(SIG_SETMASK,&l->mask,NULL)) child_error(error_fd,errno);
    int error = ENOENT;
    for (size_t i=0; i<l->paths; i++) {
        execve(l->candidates[i],argv,envp);
        if (errno == EACCES) { error = EACCES; continue; }
        if (errno != ENOENT && errno != ENOTDIR) child_error(error_fd,errno);
    }
    child_error(error_fd,error);
}
static int spawn(pid_t *pid, const char *file, const posix_spawn_file_actions_t *actions,
                 const posix_spawnattr_t *attrs, char *const argv[], char *const envp[], int search) {
    if (!pid || !file || !*file || !argv || !envp) return EINVAL;
    struct launch *l = calloc(1,sizeof(*l));
    if (!l) return ENOMEM;
    int error = prepare(l,file,search,actions,attrs);
    if (error) { free(l); return error; }
    int pipes[2];
    if (pipe(pipes)) { error=errno; free(l); return error; }
    if (fcntl(pipes[0],F_SETFD,FD_CLOEXEC) || fcntl(pipes[1],F_SETFD,FD_CLOEXEC)) {
        error=errno; close(pipes[0]); close(pipes[1]); free(l); return error;
    }
    int writer = fcntl(pipes[1],F_DUPFD_CLOEXEC,l->max_action_fd+1);
    close(pipes[1]);
    if (writer < 0) { error=errno; close(pipes[0]); free(l); return error; }
    sigset_t all, saved; sigfillset(&all);
    error = pthread_sigmask(SIG_SETMASK,&all,&saved);
    if (error) { close(writer); close(pipes[0]); free(l); return error; }
    pid_t child = fork(); error = errno;
    if (!child) { close(pipes[0]); execute(l,writer,argv,envp); _exit(127); }
    pthread_sigmask(SIG_SETMASK,&saved,NULL);
    close(writer); free(l);
    if (child < 0) { close(pipes[0]); return error; }
    error = 0; size_t received=0;
    while (received < sizeof(error)) {
        ssize_t n = read(pipes[0],(char *)&error+received,sizeof(error)-received);
        if (n < 0 && errno == EINTR) continue;
        if (n < 0) { error=errno; break; }
        if (!n) break;
        received += (size_t)n;
    }
    close(pipes[0]);
    if (received && received != sizeof(error)) error=EIO;
    if (error) { while (waitpid(child,NULL,0) < 0 && errno == EINTR) {} return error; }
    *pid = child;
    return 0;
}
static int dk_spawn(pid_t *p,const char *f,const posix_spawn_file_actions_t *a,
                    const posix_spawnattr_t *s,char *const v[],char *const e[]) { return spawn(p,f,a,s,v,e,0); }
static int dk_spawnp(pid_t *p,const char *f,const posix_spawn_file_actions_t *a,
                     const posix_spawnattr_t *s,char *const v[],char *const e[]) { return spawn(p,f,a,s,v,e,1); }

#define INTERPOSE(replacement, original) \
    __attribute__((used)) static struct { const void *new_fn, *old_fn; } \
    pair_##original __attribute__((section("__DATA,__interpose"))) = \
    { (const void *)(uintptr_t)&replacement, (const void *)(uintptr_t)&original }
INTERPOSE(dk_spawn,posix_spawn);
INTERPOSE(dk_spawnp,posix_spawnp);
INTERPOSE(dk_init,posix_spawn_file_actions_init);
INTERPOSE(dk_destroy,posix_spawn_file_actions_destroy);
INTERPOSE(dk_close,posix_spawn_file_actions_addclose);
INTERPOSE(dk_dup,posix_spawn_file_actions_adddup2);
INTERPOSE(dk_open,posix_spawn_file_actions_addopen);
INTERPOSE(dk_chdir,posix_spawn_file_actions_addchdir_np);
INTERPOSE(dk_fchdir,posix_spawn_file_actions_addfchdir_np);
INTERPOSE(dk_inherit,posix_spawn_file_actions_addinherit_np);
