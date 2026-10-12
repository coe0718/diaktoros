/* Small consumers of the public spawn API; compiled only on disposable Macs. */
#define _DARWIN_C_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <pthread.h>
#include <signal.h>
#include <spawn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>
extern char **environ;
static const char *program, *work;
static int waited(pid_t pid) {
    int status;
    return waitpid(pid,&status,0) == pid && WIFEXITED(status) && WEXITSTATUS(status)==0;
}
static int worker(int keep) {
    char cwd[PATH_MAX];
    if (!getcwd(cwd,sizeof(cwd)) || strcmp(cwd,work)) return 40;
    if (!getenv("FIXTURE_ENV") || strcmp(getenv("FIXTURE_ENV"),"ok")) return 41;
    for (int fd=3; fd<128; fd++)
        if (fd != keep && fcntl(fd,F_GETFD) != -1) return 42;
    if (keep && fcntl(keep,F_GETFD) < 0) return 43;
    sigset_t mask; pthread_sigmask(SIG_SETMASK,NULL,&mask);
    if (!sigismember(&mask,SIGUSR1)) return 44;
    struct sigaction action;
    if (sigaction(SIGUSR2,NULL,&action) || action.sa_handler != SIG_DFL) return 45;
    printf("WORKER_OK group=%d\n",getpgrp());
    return 0;
}
static int actions(int by_fd) {
    posix_spawn_file_actions_t a;
    posix_spawnattr_t attrs;
    if (posix_spawn_file_actions_init(&a) || posix_spawnattr_init(&attrs)) return 46;
    int pipes[2]; if (pipe(pipes)) return 47;
    int inherited = open("/dev/null",O_RDONLY), dir = open(work,O_RDONLY);
    if (inherited < 0 || dir < 0) return 48;
    fcntl(inherited,F_SETFD,FD_CLOEXEC);
    char keep[24]; snprintf(keep,sizeof(keep),"%d",inherited);
    int error = 0;
    if (by_fd) error |= posix_spawn_file_actions_addfchdir_np(&a,dir);
    else error |= posix_spawn_file_actions_addchdir_np(&a,work);
    error |= posix_spawn_file_actions_addopen(&a,STDIN_FILENO,"/dev/null",O_RDONLY,0);
    error |= posix_spawn_file_actions_adddup2(&a,pipes[1],STDOUT_FILENO);
    error |= posix_spawn_file_actions_addclose(&a,pipes[0]);
    error |= posix_spawn_file_actions_addclose(&a,pipes[1]);
    error |= posix_spawn_file_actions_addinherit_np(&a,inherited);
    sigset_t mask, defaults; sigemptyset(&mask); sigemptyset(&defaults);
    sigaddset(&mask,SIGUSR1); sigaddset(&defaults,SIGUSR2);
    signal(SIGUSR2,SIG_IGN);
    error |= posix_spawnattr_setflags(&attrs,POSIX_SPAWN_CLOEXEC_DEFAULT |
                                    POSIX_SPAWN_SETSIGMASK | POSIX_SPAWN_SETSIGDEF);
    error |= posix_spawnattr_setsigmask(&attrs,&mask);
    error |= posix_spawnattr_setsigdefault(&attrs,&defaults);
    char *args[] = {(char *)program,"worker",(char *)work,keep,NULL};
    pid_t child;
    if (!error) error = posix_spawn(&child,program,&a,&attrs,args,environ);
    posix_spawn_file_actions_destroy(&a); posix_spawnattr_destroy(&attrs);
    close(pipes[1]); close(inherited); close(dir);
    if (error) { close(pipes[0]); fprintf(stderr,"spawn errno=%d\n",error); return 49; }
    char output[256]={0}; ssize_t n=read(pipes[0],output,sizeof(output)-1); close(pipes[0]);
    if (!waited(child) || n<=0) return 50;
    char expected[128]; snprintf(expected,sizeof(expected),"WORKER_OK group=%d\n",getpgrp());
    if (strcmp(output,expected)) return 51;
    puts("ACTIONS_OK"); return 0;
}
static void *threaded(void *unused) {
    (void)unused;
    for (int i=0; i<8; i++) {
        pid_t child; char *args[]={"true",NULL};
        int error = posix_spawnp(&child,"true",NULL,NULL,args,environ);
        if (error || !waited(child)) return (void *)1;
    }
    return NULL;
}
int main(int argc,char **argv) {
    if (argc<3) return 52;
    program=argv[0]; work=argv[2];
    if (!strcmp(argv[1],"worker")) return worker(atoi(argv[3]));
    if (!strcmp(argv[1],"actions")) return actions(0);
    if (!strcmp(argv[1],"fchdir")) return actions(1);
    if (!strcmp(argv[1],"threads")) {
        pthread_t threads[4];
        for (int i=0;i<4;i++) if (pthread_create(&threads[i],NULL,threaded,NULL)) return 53;
        for (int i=0;i<4;i++) { void *result; if (pthread_join(threads[i],&result) || result) return 54; }
        puts("THREADS_OK"); return 0;
    }
    if (!strcmp(argv[1],"errors")) {
        pid_t child=-1; char *args[]={"missing",NULL};
        if (posix_spawn(&child,"/no-such-dk-program",NULL,NULL,args,environ)!=ENOENT || child!=-1) return 55;
        posix_spawnattr_t attrs; posix_spawnattr_init(&attrs);
        posix_spawnattr_setflags(&attrs,POSIX_SPAWN_RESETIDS);
        if (posix_spawn(&child,"/usr/bin/true",NULL,&attrs,args,environ)!=ENOTSUP) return 56;
        posix_spawnattr_destroy(&attrs);
        if (waitpid(-1,NULL,WNOHANG)!=-1 || errno!=ECHILD) return 57;
        puts("ERRORS_OK"); return 0;
    }
    return 58;
}
