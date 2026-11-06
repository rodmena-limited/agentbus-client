audit_python() {
    if [ -n "${PYTHON:-}" ]; then
        printf '%s\n' "$PYTHON"
        return
    fi
    common=$(git -C "$1" rev-parse --git-common-dir 2>/dev/null)
    for venv in "$1/.venv" "${common%/.git}/.venv"; do
        if [ -x "$venv/bin/python" ]; then
            printf '%s\n' "$venv/bin/python"
            return
        fi
    done
    printf 'python3\n'
}
