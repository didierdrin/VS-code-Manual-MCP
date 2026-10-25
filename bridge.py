import atexit, pathlib, re, shutil, socket, subprocess, sys, threading
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

import os, ctypes, winreg

INSTALL_DIR = pathlib.Path(os.environ["LOCALAPPDATA"]) / "Programs" / "bridge"


def _user_path():
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ) as k:
        try:
            return winreg.QueryValueEx(k, "Path")
        except FileNotFoundError:
            return "", winreg.REG_EXPAND_SZ


def _set_user_path(value, typ):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, "Path", 0, typ, value)
    # tell Windows the environment changed
    ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x1A, 0, "Environment", 0x2, 5000, None)


def install():
    if not getattr(sys, "frozen", False):
        sys.exit("Run --install from the built bridge.exe, not from python.")
    INSTALL_DIR.mkdir(parents=True, exist_ok=True)
    target = INSTALL_DIR / "bridge.exe"
    if pathlib.Path(sys.executable).resolve() != target.resolve():
        shutil.copy2(sys.executable, target)
    value, typ = _user_path()
    parts = [p for p in value.split(";") if p]
    if str(INSTALL_DIR).lower() not in [p.lower().rstrip("\\") for p in parts]:
        parts.append(str(INSTALL_DIR))
        _set_user_path(";".join(parts), typ)
    print(f"Installed to {INSTALL_DIR}")
    print("Close and reopen VS Code and your terminals, then type: bridge")


def uninstall():
    value, typ = _user_path()
    parts = [p for p in value.split(";") if p and p.lower().rstrip("\\") != str(INSTALL_DIR).lower()]
    _set_user_path(";".join(parts), typ)
    print(f"Removed from PATH. You can delete {INSTALL_DIR} manually.")


_args = [a for a in sys.argv[1:] if not a.startswith("--")]
ROOT = pathlib.Path(_args[0] if _args else ".").resolve()


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_tunnel(port):
    exe = shutil.which("cloudflared")
    if not exe:
        sys.exit("cloudflared not found on PATH")
    proc = subprocess.Popen(
        [exe, "tunnel", "--url", f"http://localhost:{port}"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
    )
    atexit.register(proc.terminate)
    pattern = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
    url = None
    for line in proc.stdout:
        m = pattern.search(line)
        if m and m.group(0) != "https://api.trycloudflare.com":
            url = m.group(0)
            break
    if not url:
        sys.exit("Could not find the tunnel address in cloudflared output")
    threading.Thread(target=lambda: [None for _ in proc.stdout], daemon=True).start()
    return url


def main():
    if "--install" in sys.argv:
        return install()
    if "--uninstall" in sys.argv:
        return uninstall()

    if ROOT == pathlib.Path.home() or ROOT.parent == ROOT:
        sys.exit(f"Refusing to share {ROOT}. Open a specific project folder.")
    port = free_port()
    url = start_tunnel(port)
    host = url.removeprefix("https://")

    mcp = FastMCP(
        "vscode-bridge", host="127.0.0.1", port=port,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=["127.0.0.1:*", "localhost:*", host, f"{host}:*"],
        ),
    )

    def safe(path: str) -> pathlib.Path:
        p = (ROOT / path).resolve()
        if ROOT not in p.parents and p != ROOT:
            raise ValueError("Path outside project")
        return p

    @mcp.tool()
    def list_files(path: str = ".") -> str:
        return "\n".join(
            str(f.relative_to(ROOT)) for f in safe(path).rglob("*")
            if f.is_file() and not {".git", ".venv", "node_modules"} & set(f.parts)
        )[:5000]

    @mcp.tool()
    def read_file(path: str) -> str:
        return safe(path).read_text()

    @mcp.tool()
    def write_file(path: str, content: str) -> str:
        p = safe(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return "saved"

    print("\n" + "=" * 60)
    print(f"Project folder: {ROOT}")
    print(f"Connector address for Claude: {url}/mcp")
    print("=" * 60 + "\n")
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
