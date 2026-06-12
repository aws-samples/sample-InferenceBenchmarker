"""Find file descriptor limits — system-wide and per-process."""

import os
import resource


def find_file_descriptors_limit():
    """Find file descriptor limits at system-wide and per-process levels.

    System-wide:
        current: /proc/sys/fs/file-max — total fds allowed across all processes
        hard:    /proc/sys/fs/nr_open  — kernel's absolute cap (max any process fd limit can be set to, even with sudo)

    Per-process:
        soft (current): currently enforced limit for this process — raises EMFILE if exceeded
        hard (no sudo): ceiling for soft — process can raise soft up to this without sudo
        hard (w sudo):  same as system-wide hard (nr_open) — the absolute ceiling reachable with sudo

    Returns:
        dict with all limits
    """
    # ── System-wide ───────────────────────────────────────────────────────────
    with open('/proc/sys/fs/file-max') as f:
        system_current = int(f.read().strip())

    with open('/proc/sys/fs/nr_open') as f:
        system_hard = int(f.read().strip())

    # ── Per-process ───────────────────────────────────────────────────────────
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    # RLIM_INFINITY means unlimited
    per_process_soft = soft if soft != resource.RLIM_INFINITY else float('inf')
    per_process_hard = hard if hard != resource.RLIM_INFINITY else float('inf')

    # Currently open fds in this process
    current_open = len(os.listdir(f'/proc/{os.getpid()}/fd'))

    result = {
        'system_wide': {
            'current':     system_current,
            'hard_w_sudo': system_hard,
        },
        'per_process': {
            'currently_open': current_open,
            'soft_current':   per_process_soft,
            'hard_no_sudo':   per_process_hard,
            'hard_w_sudo':    system_hard,
        },
    }

    # ── Print ─────────────────────────────────────────────────────────────────
    print("=" * 80)
    print("FILE DESCRIPTOR LIMITS")
    label_w = 27  # width of label column
    print("=" * 80)
    print()
    print("System-wide:")
    print(f"   {'Current set limit:':<{label_w}} {system_current:,}")
    print(f"   {'Hard limit (sudo):':<{label_w}} {system_hard:,}")
    print()
    print("Per-process:")
    print(f"   {'Current set limit:':<{label_w}} {per_process_soft:,}" if per_process_soft != float('inf') else f"   {'Current set limit:':<{label_w}} unlimited")
    print(f"   {'Hard Limit (no sudo):':<{label_w}} {per_process_hard:,}" if per_process_hard != float('inf') else f"   {'Hard Limit (no sudo):':<{label_w}} unlimited")
    # print()
    # print("Results:")
    # print(f"   Soft can be raised to hard without sudo")
    # print(f"   Hard can be raised to kernel cap ({system_hard:,}) with sudo")
    # print(f"   System-wide current can be raised with sudo (up to kernel cap)")
    print("=" * 80)

    return result
