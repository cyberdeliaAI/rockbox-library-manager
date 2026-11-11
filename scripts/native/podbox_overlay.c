/* SPDX-License-Identifier: GPL-2.0-only
 * Rockbox Library Manager's read-only player / writable local-library overlay.
 * Inserted after the original (renamed) sim_get_os_path in filesystem-sim.c.
 * The upstream normalizer resolves volume prefixes and .. before this mapping.
 * No source/player path may be opened for writing, removed or renamed.
 */
static size_t rlm_root_len(void)
{
    size_t n = strlen(sim_root_dir);
    while (n > 1 && sim_root_dir[n - 1] == '/') n--;
    return n;
}

static bool rlm_can_write(const char *path)
{
    const char *stage = getenv("RLM_PODBOX_STAGE");
    if (stage != NULL && *stage != '\0')
    {
        size_t root = rlm_root_len();
        if (strncmp(stage, sim_root_dir, root) == 0 &&
            (stage[root] == '/' || stage[root] == '\0'))
        {
            errno = EACCES;
            return false;
        }
        size_t n = strlen(stage);
        if (strncmp(path, stage, n) == 0 &&
            strncmp(path + n, "/.rockbox/library", 17) == 0 &&
            (path[n + 17] == '/' || path[n + 17] == '\0'))
            return true;
    }
    errno = EACCES;
    return false;
}

int sim_get_os_path(char *buffer, const char *path, size_t bufsize)
{
    /* The upstream normalizer appends its own slash after sim_root_dir.
     * Windows drive roots arrive as F:/; avoid F:// bypassing the mapping. */
    static char normalized_root[SIM_TMPBUF_MAX_PATH];
    if (sim_root_dir != normalized_root)
    {
        size_t root_length = rlm_root_len();
        if (root_length >= sizeof(normalized_root))
        {
            errno = ENAMETOOLONG;
            return -1;
        }
        memcpy(normalized_root, sim_root_dir, root_length);
        normalized_root[root_length] = '\0';
        /* One player per process. Static storage remains valid if soundscan
         * longjmps out of a decoder fault; never leave a stack pointer here. */
        sim_root_dir = normalized_root;
    }
    int rc = rlm_original_os_path(buffer, path, bufsize);
    if (rc > 1) /* Only the selected player's primary volume is supported. */
    {
        errno = ENXIO;
        return -1;
    }
    const char *stage = getenv("RLM_PODBOX_STAGE");
    if (rc >= 0 && stage != NULL && *stage != '\0')
    {
        size_t n = rlm_root_len();
        if (strncmp(buffer, sim_root_dir, n) == 0 &&
            strncmp(buffer + n, "/.rockbox/library", 17) == 0 &&
            (buffer[n + 17] == '/' || buffer[n + 17] == '\0'))
        {
            char mapped[SIM_TMPBUF_MAX_PATH];
            int len = snprintf(mapped, sizeof(mapped), "%s%s", stage, buffer + n);
            if (len < 0 || (size_t)len >= sizeof(mapped) || (size_t)len >= bufsize)
            {
                errno = ENAMETOOLONG;
                return -1;
            }
            memcpy(buffer, mapped, len + 1);
        }
    }
    return rc;
}
