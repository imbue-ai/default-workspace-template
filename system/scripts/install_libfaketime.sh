#!/bin/bash
# Build libfaketime from a pinned upstream commit into /usr/local/lib/faketime,
# and put a `tmux` first on PATH that runs the real one without it.
#
# Nothing in the workspace loads the library on its own. It is there for
# whatever creates the workspace to start supervisord's programs on a shifted
# clock through system/clock.env (see system/supervisord.conf), which an
# evaluation harness does to let days pass between two steps of a task.
#
# Upstream master rather than Debian's package: 0.9.10 makes Claude Code exit
# with a sem_open error when the process that started it is faked too.
# FAKE_STAT is compiled out so a file's times read the same through libc's
# stat as through node's statx, which libfaketime does not intercept; the grep
# fails the install if the Makefile stops naming the flag where the sed looks.
#
# The minds evals box image builds the same library for its fake services
# (mngr's apps/minds_evals/imbue/minds_evals/templates/environment/Dockerfile);
# move the commit and the FAKE_STAT strip in both places together.
#
# tmux links jemalloc, whose initialisation deadlocks under libfaketime, in the
# client as well as the server, and FAKETIME_SKIP_CMDS takes effect too late to
# help. So the `tmux` in /usr/local/bin (ahead of /usr/bin on PATH) drops a
# libfaketime preload before it execs the real one, and a faked program that
# drives tmux (mngr, the terminal) keeps working.
#
# Idempotent and version-gated like install_earlyoom.sh.
set -euo pipefail

LIBFAKETIME_COMMIT="9da91d97cc2b5bc08bcf420a34c9788d5946786c"
LIBFAKETIME_SHA256="39bb693d31821c6c387c1d215d0698084615f7a5fc71f5d65b4e75e7faf7e1f5"
INSTALL_PATH="/usr/local/lib/faketime/libfaketime.so.1"
VERSION_STAMP="/usr/local/lib/faketime/.version"
TMUX_SHIM_PATH="/usr/local/bin/tmux"
REAL_TMUX_PATH="/usr/bin/tmux"

install_tmux_shim() {
    cat > "${TMUX_SHIM_PATH}.new" <<SHIM
#!/bin/sh
case "\${LD_PRELOAD:-}" in
    *libfaketime*) unset LD_PRELOAD ;;
esac
exec ${REAL_TMUX_PATH} "\$@"
SHIM
    chmod 0755 "${TMUX_SHIM_PATH}.new"
    mv -f "${TMUX_SHIM_PATH}.new" "$TMUX_SHIM_PATH"
}

install_tmux_shim

if [ -f "$INSTALL_PATH" ] && [ "$(cat "$VERSION_STAMP" 2>/dev/null || true)" = "$LIBFAKETIME_COMMIT" ]; then
    exit 0
fi

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
curl -fsSL --retry 3 --retry-delay 2 --retry-all-errors \
    -o "${tmp}/libfaketime.tar.gz" \
    "https://github.com/wolfcw/libfaketime/archive/${LIBFAKETIME_COMMIT}.tar.gz"
echo "${LIBFAKETIME_SHA256}  ${tmp}/libfaketime.tar.gz" | sha256sum -c - >/dev/null
tar -xzf "${tmp}/libfaketime.tar.gz" -C "$tmp"
src="${tmp}/libfaketime-${LIBFAKETIME_COMMIT}/src"
grep -q ' -DFAKE_STAT ' "${src}/Makefile"
sed -i 's/ -DFAKE_STAT / /' "${src}/Makefile"
make -C "$src" libfaketime.so.1 >/dev/null

# Install to a sibling, then rename: a running faked process keeps its inode.
install -D -m 0644 "${src}/libfaketime.so.1" "${INSTALL_PATH}.new"
mv -f "${INSTALL_PATH}.new" "$INSTALL_PATH"
printf '%s\n' "$LIBFAKETIME_COMMIT" > "$VERSION_STAMP"
