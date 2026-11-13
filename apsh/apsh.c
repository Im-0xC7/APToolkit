/* apsh.c - freestanding ARMv5 Linux shell broker (no libc) for the AccessPort v3 cobb USB
 * gadget. Bridges /dev/ap_usb <-> a `busybox sh -i` so the host gets a shell over bulk USB.
 * Build:
 *   arm-linux-gnueabihf-gcc -nostdlib -static -ffreestanding -march=armv5te -marm \
 *                           -mfloat-abi=soft -Os apsh.c -o apsh
 */
#define SYS_exit      1
#define SYS_fork      2
#define SYS_read      3
#define SYS_write     4
#define SYS_open      5
#define SYS_close     6
#define SYS_execve   11
#define SYS_chdir    12
#define SYS_chroot   61
#define SYS_kill     37
#define SYS_dup2     63
#define SYS_pipe     42
#define SYS_wait4   114
#define SYS_nanosleep 162
#define SIGKILL       9
#define O_RDWR        2
#define RESET       0x12   /* host->device: "kill the current shell, give me a fresh one" */

static long sys(long nr, long a, long b, long c) {
  register long r7 __asm__("r7") = nr; register long r0 __asm__("r0") = a;
  register long r1 __asm__("r1") = b;  register long r2 __asm__("r2") = c;
  __asm__ volatile("svc 0" : "+r"(r0) : "r"(r7), "r"(r1), "r"(r2) : "memory");
  return r0;
}

/* wait4(pid, status): block until `pid` exits, reap it, store the raw wait status (NULL ok).
 * r3 (rusage) MUST be zeroed explicitly -- the 3-arg sys() would leave garbage and the kernel
 * may EFAULT and fail to reap. */
static long reap(long pid, long *status) {
  register long r7 __asm__("r7") = SYS_wait4; register long r0 __asm__("r0") = pid;
  register long r1 __asm__("r1") = (long)status; register long r2 __asm__("r2") = 0;
  register long r3 __asm__("r3") = 0;
  __asm__ volatile("svc 0" : "+r"(r0) : "r"(r7), "r"(r1), "r"(r2), "r"(r3) : "memory");
  return r0;
}

/* sleep sec+nsec (explicit fields: no division -> no __aeabi_idivmod under -nostdlib) */
static void snooze(long sec, long nsec) {
  long ts[2]; ts[0] = sec; ts[1] = nsec;
  sys(SYS_nanosleep, (long)ts, 0, 0);
}

/* block (spin on the non-blocking OUT read) until the host sends a RESET byte, discarding any
 * other bytes. Used before spawning a shell so none is pre-spawned ahead of a client. */
static void wait_for_reset(long usbfd) {
  char buf[1024];
  for (;;) {
    long n = sys(SYS_read, usbfd, (long)buf, sizeof buf);
    if (n > 0) { long i; for (i = 0; i < n; i++) if ((unsigned char)buf[i] == RESET) return; }
    else snooze(0, 5000000L);
  }
}

void _start(void) {
  /* Open the cobb gadget char dev ONCE and never close it (a close runs cobb_gadget_release ->
   * set_config(0x20), disabling the bulk endpoints). apsh.sh mknod's it at /tmp/apdev/ap_usb on a
   * tmpfs; retry until the host has SET_CONFIGURATION'd the gadget (open returns <0 until then). */
  long usbfd;
  do { usbfd = sys(SYS_open, (long)"/tmp/apdev/ap_usb", O_RDWR, 0);
       if (usbfd < 0) snooze(0, 100000000L); }
  while (usbfd < 0);

  long need_reset = 1;  /* wait for the client's connect-RESET before the FIRST shell, so nothing
                         * is pre-spawned; cleared after a RESET-kill to respawn immediately. */
  for (;;) {                               /* one fresh shell session per iteration */
    if (need_reset) wait_for_reset(usbfd);
    long in[2], out[2];                    /* in: broker->sh stdin;  out: sh stdout->broker */
    sys(SYS_pipe, (long)in, 0, 0);
    sys(SYS_pipe, (long)out, 0, 0);

    long sh = sys(SYS_fork, 0, 0, 0);
    if (sh == 0) {
      /* shell child: wire pipes to std fds, drop every other fd, exec busybox sh -i */
      sys(SYS_dup2, in[0], 0, 0);
      sys(SYS_dup2, out[1], 1, 0);
      sys(SYS_dup2, out[1], 2, 0);
      sys(SYS_close, in[0], 0, 0);  sys(SYS_close, in[1], 0, 0);
      sys(SYS_close, out[0], 0, 0); sys(SYS_close, out[1], 0, 0);
      sys(SYS_close, usbfd, 0, 0);
      /* argv[0]="busybox" + "sh" selects the sh applet; -i forces interactive mode so ash prints
       * PS1 ("> ", to stderr -> out) after each command => a real prompt synced to completion.
       * (It prints one harmless "can't access tty; job control turned off" line at startup.) */
      char *argv[] = { "busybox", "sh", "-i", 0 };
      char *envp[] = { "PATH=/bin:/sbin:/usr/bin:/usr/sbin:/ap-app/bin", "LD_LIBRARY_PATH=/ap-app/lib:/usr/local/lib:/lib", "HOME=/", "PS1=> ", "PS2=> ", 0 };
      sys(SYS_execve, (long)"/bin/busybox", (long)argv, (long)envp);
      sys(SYS_exit, 127, 0, 0);
    }

    /* pump A: host->shell. Spin on the non-blocking OUT read; forward to the shell stdin. A
     * RESET byte from a (re)connecting client means "recycle": kill THIS shell and exit, so the
     * supervisor tears the session down and loops to a fresh one. (sh's pid is in scope because
     * pump A is forked after the shell.) */
    long pa = sys(SYS_fork, 0, 0, 0);
    if (pa == 0) {
      sys(SYS_close, out[0], 0, 0); sys(SYS_close, out[1], 0, 0); sys(SYS_close, in[0], 0, 0);
      char buf[1024];
      for (;;) {
        long n = sys(SYS_read, usbfd, (long)buf, sizeof buf);
        if (n > 0) {
          long i, reset = 0;
          for (i = 0; i < n; i++) if ((unsigned char)buf[i] == RESET) { reset = 1; break; }
          if (reset) { sys(SYS_kill, sh, SIGKILL, 0); sys(SYS_exit, 0, 0, 0); }
          sys(SYS_write, in[1], (long)buf, n);   /* may SIGPIPE if sh already gone -> pump dies, fine */
        } else {
          snooze(0, 5000000L);                   /* n==0: no host data yet -> poll */
        }
      }
    }

    /* pump B: shell->host. Blocking reads from the shell; blocking writes to the host (IN ep).
     * Exits on EOF (read==0): once the shell is the only out[1] writer and it exits. */
    long pb = sys(SYS_fork, 0, 0, 0);
    if (pb == 0) {
      sys(SYS_close, in[0], 0, 0); sys(SYS_close, in[1], 0, 0); sys(SYS_close, out[1], 0, 0);
      char buf[1024];
      for (;;) {
        long n = sys(SYS_read, out[0], (long)buf, sizeof buf);
        if (n > 0) sys(SYS_write, usbfd, (long)buf, n);
        else if (n == 0) sys(SYS_exit, 0, 0, 0);
      }
    }

    /* supervisor: close ALL pipe fds so the shell is the SOLE reader of in[] and SOLE writer of
     * out[] (otherwise pump B never sees EOF). Keep only usbfd. */
    sys(SYS_close, in[0], 0, 0);  sys(SYS_close, in[1], 0, 0);
    sys(SYS_close, out[0], 0, 0); sys(SYS_close, out[1], 0, 0);

    long st = 0;
    reap(sh, &st);                          /* block until the shell exits (normally or killed) */
    sys(SYS_kill, pa, SIGKILL, 0); reap(pa, 0);  /* stop + reap the input pump */
    reap(pb, 0);                            /* let the output pump flush, hit EOF, and exit */
    if ((st & 0x7f) == 0) {                 /* NORMAL exit (user typed `exit`): low 7 bits == 0 */
      sys(SYS_write, usbfd, (long)"\r\n\x04", 3);   /* EOT: client disconnects */
      sys(SYS_exit, 0, 0, 0);               /* quit the broker so apsh.sh resumes usbmgr */
    } else {                                /* killed by SIGNAL == a RESET: client is attached */
      need_reset = 0;                       /* spawn its fresh shell immediately */
    }
  }
}
