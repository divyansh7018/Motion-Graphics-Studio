"""Generate stub shared objects for system libraries this container lacks.

Symbols AND their version nodes are read from the Qt libraries themselves, so
the dynamic loader accepts the stubs exactly as it would the real libraries.
Runs until `import PySide6.QtWidgets` succeeds.
"""
import collections, glob, os, re, subprocess, sys

SITE = glob.glob(os.path.expanduser("~/.venv/lib/python*/site-packages"))[0]
QT = sorted(glob.glob(f"{SITE}/PySide6/Qt/lib/*.so*"))
PLUGINS = sorted(glob.glob(f"{SITE}/PySide6/Qt/plugins/**/*.so", recursive=True))
STUB_DIR = "/tmp/stublib"
PY = os.path.expanduser("~/.venv/bin/python")

# version name -> the file that provides it (from every library's version needs)
version_owner: dict[str, str] = {}
needed_by: dict[str, set[str]] = collections.defaultdict(set)   # soname -> libs that need it
for path in QT + PLUGINS:
    out = subprocess.run(["readelf", "-d", path], capture_output=True, text=True).stdout
    for line in out.splitlines():
        m = re.search(r"NEEDED\s+Shared library: \[(\S+)\]", line)
        if m:
            needed_by[m.group(1)].add(path)
    out = subprocess.run(["readelf", "-V", path], capture_output=True, text=True).stdout
    current = ""
    for line in out.splitlines():
        line = line.strip()
        m = re.search(r"File: (\S+)", line)
        if m:
            current = m.group(1)
            continue
        m = re.search(r"Name: (\S+)\s+Flags:", line)
        if m and current:
            version_owner.setdefault(m.group(1), current)

PREFIXES = (("xkb_", "libxkbcommon.so.0"), ("dbus_", "libdbus-1.so.3"),
            ("egl", "libEGL.so.1"), ("glX", "libGL.so.1"), ("gl", "libGL.so.1"),
            ("xcb_", "libxcb.so.1"), ("X", "libX11.so.6"))


def undefined_symbols(path: str) -> list[tuple[str, str]]:
    """[(symbol, version)] for every UND entry in *path*."""
    out = subprocess.run(["readelf", "--dyn-syms", "-W", path], capture_output=True, text=True).stdout
    found = []
    for line in out.splitlines():
        if " UND " not in line:
            continue
        tail = line.split(" UND ", 1)[1].strip()
        tail = re.sub(r"\s*\(\d+\)$", "", tail)
        symbol, _, version = tail.partition("@")
        symbol = symbol.strip()
        if symbol:
            found.append((symbol, version.lstrip("@")))
    return found


def build_stub(soname: str) -> int:
    symbols: dict[str, str] = {}
    # When no Qt library lists the soname explicitly (plugins dlopen it),
    # fall back to prefix matching across every Qt library.
    for path in (needed_by.get(soname) or (QT + PLUGINS)):
        for symbol, version in undefined_symbols(path):
            owner = version_owner.get(version) if version else ""
            if not owner:
                owner = next((lib for prefix, lib in PREFIXES if symbol.startswith(prefix)), "")
            if owner == soname:
                symbols.setdefault(symbol, version)
    if not symbols:
        return 0

    by_version: dict[str, list[str]] = collections.defaultdict(list)
    for symbol, version in sorted(symbols.items()):
        by_version[version or "STUB_BASE"].append(symbol)

    with open(f"{STUB_DIR}/stub_{soname}.c", "w") as handle:
        for symbol in sorted(symbols):
            handle.write(f"void {symbol}(void) {{}}\n")

    lines, names = [], sorted(by_version)
    for index, version in enumerate(names):
        body = "".join(f"    {name};\n" for name in by_version[version])
        tail = "} STUB_BASE;\n" if index < len(names) - 1 else "  local: *;\n};\n"
        lines.append(f"{version} {{\n  global:\n{body}{tail}")
    if len(names) > 1:
        lines.append("STUB_BASE {\n  local: *;\n};\n")
    with open(f"{STUB_DIR}/stub_{soname}.map", "w") as handle:
        handle.write("".join(lines))

    subprocess.run(["gcc", "-shared", "-fPIC", "-o", f"{STUB_DIR}/{soname}", f"{STUB_DIR}/stub_{soname}.c",
                    f"-Wl,-soname,{soname}", "-Wl,--version-script", f"{STUB_DIR}/stub_{soname}.map"], check=True)
    return len(symbols)


def missing_library() -> str:
    env = dict(os.environ, LD_LIBRARY_PATH=STUB_DIR, QT_QPA_PLATFORM="offscreen")
    proc = subprocess.run([PY, "-c", "import PySide6.QtWidgets, PySide6.QtGui; "
                                     "from PySide6.QtWidgets import QApplication; a = QApplication([])"],
                          capture_output=True, text=True, env=env)
    if proc.returncode == 0:
        return ""
    match = re.search(r"([\w.+-]+\.so[\w.]*): cannot open shared object file", proc.stderr)
    return match.group(1) if match else proc.stderr.strip().splitlines()[-1]


for attempt in range(40):
    soname = missing_library()
    if not soname:
        print(f"Qt imports and constructs a QApplication after {attempt} stub(s)")
        break
    count = build_stub(soname)
    print(f"stub {soname}: {count} symbols")
    if count == 0:
        print(f"!! no symbols resolved for {soname}")
        sys.exit(1)
else:
    sys.exit(1)
