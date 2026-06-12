#!/usr/bin/env python3
"""
PET Installer for LoongArch64
================================
Installs or updates Python Environment Tools (PET) in VSCode / Code-OSS extensions.

Target: LoongArch64 (LA64) platform only.
Python 3.6+, zero external dependencies.
Interactive Q&A UX; accepts only --dry-run as a CLI flag.

Strategy:
  - Always fetches the latest GitHub Release (no local/remote version comparison
    because Cargo.toml version ≠ release tag).
  - Skips extensions whose version is older than MIN_PYTHON_EXT_VERSION
    (Python extension started bundling PET only after a certain version).

Exit codes:
  0 - Already up-to-date, no changes made
  1 - Updated successfully
  2 - Network error
  3 - Platform mismatch (not running on loongarch64)
  4 - Permission denied
  5 - User cancelled
"""

import sys
import os
import platform
import subprocess
import json
import shutil
import hashlib
import tempfile
import glob
import time
import re
from pathlib import Path
from typing import List, Optional, Tuple, Dict

# ── Constants ──────────────────────────────────────────────────────────
GITHUB_REPO = "wubzbz/python-environment-tools-la64"
GITHUB_API_RELEASES = f"https://api.github.com/repos/{GITHUB_REPO}/releases"
GITHUB_RELEASE_PAGE = f"https://github.com/{GITHUB_REPO}/releases"
ASSET_NAME = "pet-loongarch64-unknown-linux-musl"
ASSET_CHECKSUM = f"{ASSET_NAME}.sha256"
PET_RELATIVE_PATH = "python-env-tools/bin/pet"

# i18n files are fetched from the pet-deploy-tools repo main branch
I18N_BASE_URL = (
    "https://raw.githubusercontent.com/wubzbz/pet-deploy-tools/main/resource/i18n"
)

# i18n files to manage per extension: source_filename → dest_relative_path
I18N_FILES = {
    "package.nls.zh-cn.json": "package.nls.zh-cn.json",
    "bundle.l10n.zh-cn.json": os.path.join("l10n", "bundle.l10n.zh-cn.json"),
}

# Minimum extension version that requires PET.
# Extensions older than this will be skipped.
# Format: (major, minor, patch)  —  e.g. (2026, 4, 0)
# TODO: confirm the exact version with upstream.
MIN_PYTHON_EXT_VERSION = (2025, 1, 0)  # PLACEHOLDER — update after confirmation

# Extension directory candidates (ordered by priority)
EXTENSION_SEARCH_PATHS = [
    "~/.code-oss/extensions",
    "~/.vscode-oss/extensions",
    "~/.vscode/extensions",
    "~/.vscode-server/extensions",
    "~/.vscode-server/cli/servers/*/server/extensions",
]

# Extension IDs to match
EXTENSION_IDS = [
    "ms-python.python",
    "ms-python.vscode-python-envs",
]

# ── Exit codes ─────────────────────────────────────────────────────────
EXIT_UP_TO_DATE = 0
EXIT_UPDATED = 1
EXIT_NETWORK_ERROR = 2
EXIT_PLATFORM_MISMATCH = 3
EXIT_PERMISSION_DENIED = 4
EXIT_CANCELLED = 5


# ══════════════════════════════════════════════════════════════════════════
#  Utility functions
# ══════════════════════════════════════════════════════════════════════════

def is_dry_run() -> bool:
    """Check if --dry-run flag was passed."""
    return "--dry-run" in sys.argv[1:]


def eprint(*args, **kwargs):
    """Print to stderr."""
    print(*args, file=sys.stderr, **kwargs)


def prompt(msg: str) -> str:
    """Prompt user and return stripped answer."""
    try:
        return input(msg + " ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(EXIT_CANCELLED)


def prompt_yes_no(msg: str, default: bool = True) -> bool:
    """Prompt for y/n answer."""
    hint = "[Y/n]" if default else "[y/N]"
    ans = prompt(f"{msg} {hint}").lower()
    if not ans:
        return default
    return ans in ("y", "yes")


def run_command(cmd: List[str], timeout: int = 30) -> Tuple[int, str, str]:
    """Run a command and return (returncode, stdout, stderr)."""
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            universal_newlines=True,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "Command timed out"
    except FileNotFoundError:
        return -1, "", f"Command not found: {cmd[0]}"
    except Exception as e:
        return -1, "", str(e)


# ══════════════════════════════════════════════════════════════════════════
#  Extension version parsing
# ══════════════════════════════════════════════════════════════════════════

def parse_extension_version(dir_name: str) -> Optional[Tuple[int, int, int]]:
    """
    Extract version from extension directory name.

    Examples:
      ms-python.python-2026.4.0-universal  → (2026, 4, 0)
      ms-python.python-2024.14.1           → (2024, 14, 1)
      ms-python.vscode-python-envs-1.2.3   → (1, 2, 3)
    """
    # Match the last X.Y.Z pattern in the name
    m = re.search(r"(\d+)\.(\d+)\.(\d+)(?:[^.]|$)", dir_name)
    if not m:
        return None
    try:
        return (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def is_extension_too_old(dir_name: str) -> bool:
    """Check if an extension version is below the minimum threshold."""
    ver = parse_extension_version(dir_name)
    if ver is None:
        return False  # can't parse → don't skip
    return ver < MIN_PYTHON_EXT_VERSION


# ══════════════════════════════════════════════════════════════════════════
#  Platform detection
# ══════════════════════════════════════════════════════════════════════════

def check_platform() -> bool:
    """Verify we are running on LoongArch64."""
    machine = platform.machine()
    if machine == "loongarch64":
        return True

    # Double-check with uname -m
    try:
        rc, stdout, _ = run_command(["uname", "-m"])
        if rc == 0 and stdout.strip() == "loongarch64":
            return True
    except Exception:
        pass

    return False


# ══════════════════════════════════════════════════════════════════════════
#  Extension directory discovery
# ══════════════════════════════════════════════════════════════════════════

def find_extension_dirs() -> List[Path]:
    """Search for VSCode / Code-OSS extension directories."""
    found = []
    for pattern in EXTENSION_SEARCH_PATHS:
        expanded = os.path.expanduser(pattern)
        for p in sorted(glob.glob(expanded), reverse=True):
            path = Path(p).resolve()
            if path.is_dir() and path not in found:
                found.append(path)
    return found


def find_eligible_extensions(ext_dirs: List[Path]) -> Dict[str, List[Tuple[Path, Optional[str]]]]:
    """
    Scan extension directories for eligible extensions.

    Returns dict keyed by extension ID (e.g. 'ms-python.python'),
    values are lists of (extension_dir, pet_version_or_None).

    Version threshold (MIN_PYTHON_EXT_VERSION) applies only to ms-python.python;
    ms-python.vscode-python-envs is not filtered by version because it uses
    a different versioning scheme (semver vs calendar).
    """
    result: Dict[str, List[Tuple[Path, Optional[str]]]] = {}

    for ext_dir in ext_dirs:
        for ext_id in EXTENSION_IDS:
            pattern = f"{ext_id}-*"
            matches = sorted(ext_dir.glob(pattern), reverse=True)
            if not matches:
                eprint(f"  ⚠ 未找到 {ext_id}（匹配模式: {ext_dir}/{pattern}）")
            for match in matches:
                # Version threshold only for ms-python.python
                if ext_id == "ms-python.python" and is_extension_too_old(match.name):
                    eprint(f"  ⚠ 跳过旧版本: {match.name} (低于 {_format_version(MIN_PYTHON_EXT_VERSION)})")
                    continue

                pet_path = match / PET_RELATIVE_PATH
                pet_ver = None
                if pet_path.is_file():
                    rc, stdout, _ = run_command([str(pet_path), "--version"], timeout=10)
                    if rc == 0:
                        pet_ver = stdout.strip()

                result.setdefault(ext_id, []).append((match, pet_ver))

    return result


# ══════════════════════════════════════════════════════════════════════════
#  GitHub API
# ══════════════════════════════════════════════════════════════════════════

def http_get(url: str, timeout: int = 30) -> Tuple[int, Optional[str]]:
    """HTTP GET with urllib. Returns (status, body)."""
    from urllib.request import Request, urlopen
    from urllib.error import URLError, HTTPError

    req = Request(url, headers={
        "User-Agent": "pet-installer/1.0",
        "Accept": "application/vnd.github+json",
    })
    try:
        resp = urlopen(req, timeout=timeout)
        return resp.status, resp.read().decode("utf-8")
    except HTTPError as e:
        return e.code, None
    except URLError:
        return -1, None
    except Exception:
        return -1, None


def fetch_latest_release(retries: int = 3) -> Optional[dict]:
    """Fetch latest GitHub Release with retries and incremental timeouts."""
    url = f"{GITHUB_API_RELEASES}/latest"
    for attempt in range(1, retries + 1):
        timeout = 10 * attempt
        eprint(f"  尝试获取最新版本... (第 {attempt}/{retries} 次)")
        status, body = http_get(url, timeout=timeout)
        if status == 200 and body:
            try:
                return json.loads(body)
            except json.JSONDecodeError:
                eprint("  ✗ 解析响应失败")
        else:
            eprint(f"  ✗ HTTP {status}")
        if attempt < retries:
            time.sleep(2)
    return None


def find_asset(release: dict, name: str) -> Optional[dict]:
    """Find an asset by name in a release."""
    for asset in release.get("assets", []):
        if asset.get("name") == name:
            return asset
    return None


def download_file(url: str, dest: Path, timeout: int = 60) -> bool:
    """Download a file from URL to dest path."""
    from urllib.request import Request, urlopen
    from urllib.error import URLError

    req = Request(url, headers={
        "User-Agent": "pet-installer/1.0",
        "Accept": "application/octet-stream",
    })
    try:
        resp = urlopen(req, timeout=timeout)
        with open(dest, "wb") as f:
            shutil.copyfileobj(resp, f)
        return True
    except URLError as e:
        eprint(f"  ✗ 下载失败: {e}")
        return False
    except Exception as e:
        eprint(f"  ✗ 下载失败: {e}")
        return False


# ══════════════════════════════════════════════════════════════════════════
#  Checksum verification
# ══════════════════════════════════════════════════════════════════════════

def verify_checksum(file_path: Path, expected_sum: str) -> bool:
    """Verify SHA256 checksum of a file."""
    sha = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha.update(chunk)
    actual = sha.hexdigest()
    return actual == expected_sum


def parse_sha256sum(content: str) -> Optional[str]:
    """Parse sha256sum output: '<hash>  <filename>' -> hash."""
    parts = content.strip().split()
    if len(parts) >= 1 and len(parts[0]) == 64:
        return parts[0]
    return None


# ══════════════════════════════════════════════════════════════════════════
#  Installation
# ══════════════════════════════════════════════════════════════════════════

def get_extension_id(ext_dir_name: str) -> Optional[str]:
    """Extract extension ID from directory name.

    e.g. 'ms-python.python-2026.4.0-universal' → 'ms-python.python'
    """
    for ext_id in EXTENSION_IDS:
        if ext_dir_name.startswith(ext_id + "-") or ext_dir_name == ext_id:
            return ext_id
    return None


def download_i18n_file(ext_id: str, filename: str, dest_dir: Path,
                       timeout: int = 30, retries: int = 2) -> Optional[Path]:
    """Download a single i18n file and save to dest_dir. Returns path or None."""
    url = f"{I18N_BASE_URL}/{ext_id}/{filename}"
    dest = dest_dir / filename
    for attempt in range(1, retries + 2):  # total = retries + 1
        status, body = http_get(url, timeout=timeout)
        if status == 200 and body:
            try:
                dest.write_text(body, encoding="utf-8")
                return dest
            except OSError as e:
                eprint(f"    ✗ 保存 i18n 文件失败: {e}")
                return None
        if status == 404:
            eprint(f"    ✗ i18n 文件不存在 (404): {ext_id}/{filename}")
            return None
        if attempt <= retries:
            time.sleep(1)
    eprint(f"    ✗ 下载 i18n 失败 (HTTP {status}): {ext_id}/{filename}")
    return None


def install_i18n(ext_dir: Path, i18n_files: Dict[str, Optional[Path]]) -> bool:
    """Copy downloaded i18n files into the extension directory.

    i18n_files: {dest_relative_path: temp_file_path_or_None}
    Returns True if at least one file was installed.
    """
    installed = False
    for dest_rel, src in i18n_files.items():
        dest = ext_dir / dest_rel
        if src is None:
            # Already present on disk — nothing to do
            continue
        if not src.is_file():
            eprint(f"    ✗ i18n 下载失败: {os.path.basename(dest_rel)}")
            continue
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(src), str(dest))
            eprint(f"    ✓ i18n: {dest_rel}")
            installed = True
        except (PermissionError, OSError) as e:
            eprint(f"    ✗ i18n 复制失败 ({dest_rel}): {e}")

    return installed


def install_pet(ext_dir: Path, binary_path: Path) -> bool:
    """
    Replace pet in the given extension directory.
    - Back up old pet → pet.bak
    - Copy new binary in place
    - chmod +x
    """
    pet_path = ext_dir / PET_RELATIVE_PATH
    backup_path = pet_path.with_suffix(pet_path.suffix + ".bak")

    try:
        if pet_path.exists():
            shutil.copy2(pet_path, backup_path)
            eprint(f"    ✓ 已备份旧版本至 {backup_path.name}")

        pet_path.parent.mkdir(parents=True, exist_ok=True)

        # Atomic-ish: write to temp then rename
        tmp_path = pet_path.with_suffix(pet_path.suffix + ".tmp")
        shutil.copy2(binary_path, tmp_path)
        os.chmod(tmp_path, 0o755)
        os.replace(tmp_path, pet_path)

        return True
    except PermissionError:
        eprint(f"    ✗ 权限不足，无法写入 {pet_path}")
        return False
    except Exception as e:
        eprint(f"    ✗ 安装失败: {e}")
        return False


# ══════════════════════════════════════════════════════════════════════════
#  Main flow
# ══════════════════════════════════════════════════════════════════════════

def print_header():
    """Print script header."""
    print("╔══════════════════════════════════════════╗")
    print("║   PET Installer for LoongArch64 (LA64)   ║")
    print("╚══════════════════════════════════════════╝")
    if is_dry_run():
        print(">>> DRY-RUN 模式：仅检查，不执行任何修改 <<<")
    print()


def step_platform_check():
    """Step 1: Platform check."""
    print("▸ 检测平台...")
    if not check_platform():
        eprint("✗ 此脚本仅支持 LoongArch64 (LA64) 平台。")
        eprint(f"  当前平台: {platform.machine()}")
        sys.exit(EXIT_PLATFORM_MISMATCH)
    print("  ✓ LoongArch64 平台已确认")


def step_find_extensions() -> Tuple[List[Path], Dict[str, List[Tuple[Path, Optional[str]]]]]:
    """
    Step 2: Find extension directories and eligible extensions.
    Returns (ext_dirs, eligible_extensions).
    """
    print()
    print("▸ 搜索 VSCode / Code-OSS 扩展目录...")

    ext_dirs = find_extension_dirs()
    if ext_dirs:
        print(f"  找到 {len(ext_dirs)} 个扩展目录:")
        for d in ext_dirs:
            print(f"    - {d}")
    else:
        print("  ✗ 未自动发现任何扩展目录。")
        manual = prompt("  请手动输入扩展目录路径（或按回车跳过）:")
        if manual:
            p = Path(os.path.expanduser(manual)).resolve()
            if p.is_dir():
                ext_dirs = [p]
            else:
                eprint(f"  ✗ 路径不存在: {p}")
                sys.exit(EXIT_CANCELLED)
        else:
            eprint("  已取消。")
            sys.exit(EXIT_CANCELLED)

    eligible = find_eligible_extensions(ext_dirs)
    return ext_dirs, eligible


def check_i18n_status(ext_dir: Path, ext_id: str) -> Dict[str, bool]:
    """Check which i18n files exist in the extension directory.

    Returns {filename: exists}.
    """
    status = {}
    for dest_rel in I18N_FILES.values():
        dest = ext_dir / dest_rel
        status[dest_rel] = dest.is_file()
    return status


def step_show_local_status(eligible: Dict[str, List[Tuple[Path, Optional[str]]]]):
    """Step 3: Show local extension/PET/i18n status."""
    print()
    print("▸ 符合条件的扩展及 PET / i18n 状态:")

    total = sum(len(v) for v in eligible.values())
    if total == 0:
        print(f"  ✗ 未找到版本 ≥ {_format_version(MIN_PYTHON_EXT_VERSION)} 的扩展。")
        print(f"    将提供手动安装选项。")
        return

    for ext_id, entries in eligible.items():
        print(f"  [{ext_id}]")
        for ext_dir, pet_ver in entries:
            pet_status = pet_ver if pet_ver else "✗ 未安装"
            i18n = check_i18n_status(ext_dir, ext_id)
            i18n_parts = []
            for dest_rel, exists in i18n.items():
                mark = "✓" if exists else "✗"
                i18n_parts.append(f"{mark} {os.path.basename(dest_rel)}")
            i18n_str = "  ".join(i18n_parts)
            print(f"    - {ext_dir.name}")
            print(f"      PET:  {pet_status}")
            print(f"      i18n: {i18n_str}")


def step_fetch_remote() -> dict:
    """Step 4: Fetch remote release info. Exits on failure."""
    print()
    print("▸ 获取最新 Release 信息...")
    release = fetch_latest_release()
    if release is None:
        eprint("✗ 无法连接到 GitHub，请检查网络。")
        eprint(f"  手动下载页面: {GITHUB_RELEASE_PAGE}")
        sys.exit(EXIT_NETWORK_ERROR)
    tag = release.get("tag_name", "未知")
    name = release.get("name", "未知")
    print(f"  ✓ 最新 Release: {name} ({tag})")
    return release


def step_download(release: dict) -> Optional[Path]:
    """Step 5: Download and verify binary. Returns path to temp file."""
    print()
    print("▸ 下载 PET 二进制...")

    asset = find_asset(release, ASSET_NAME)
    if not asset:
        eprint(f"✗ 未在 Release 中找到资产: {ASSET_NAME}")
        eprint("  可用资产:")
        for a in release.get("assets", []):
            eprint(f"    - {a.get('name')}")
        return None

    checksum_asset = find_asset(release, ASSET_CHECKSUM)

    # Download checksum
    expected_sum = None
    if checksum_asset:
        print(f"  下载校验和文件...")
        status, body = http_get(checksum_asset["browser_download_url"])
        if status == 200 and body:
            expected_sum = parse_sha256sum(body)
            if expected_sum:
                print(f"  ✓ SHA256: {expected_sum}")
            else:
                eprint("  ⚠ 无法解析校验和文件")
        else:
            eprint(f"  ⚠ 校验和文件下载失败 (HTTP {status})")
    else:
        eprint(f"  ⚠ 未找到校验和文件: {ASSET_CHECKSUM}")
        if not prompt_yes_no("是否跳过校验和验证?", default=False):
            return None

    # Download binary
    tmp_dir = Path(tempfile.mkdtemp(prefix="pet-install-"))
    tmp_file = tmp_dir / "pet"
    download_url = asset["browser_download_url"]
    size_kb = (asset.get("size", 0) or 0) / 1024
    print(f"  下载 {ASSET_NAME} ({size_kb:.0f} KB)...")

    if not download_file(download_url, tmp_file):
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return None

    print("  ✓ 下载完成")

    # Verify checksum
    if expected_sum:
        print("  验证校验和...")
        if verify_checksum(tmp_file, expected_sum):
            print("  ✓ 校验和匹配")
        else:
            eprint("  ✗ 校验和不匹配！文件可能损坏。")
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return None

    return tmp_file


def step_select_targets(eligible: Dict[str, List[Tuple[Path, Optional[str]]]],
                        ext_dirs: List[Path]) -> List[Path]:
    """
    Step 6: Let user choose which extension directories to install into.
    Returns list of extension directories.
    """
    print()
    print("▸ 选择安装目标:")

    all_targets: List[Tuple[Path, str, Optional[str]]] = []
    # (ext_dir, ext_id, pet_version_or_None)

    for ext_id, entries in eligible.items():
        for ext_dir, pet_ver in entries:
            all_targets.append((ext_dir, ext_id, pet_ver))

    if all_targets:
        print(f"  找到 {len(all_targets)} 个符合条件的扩展:")
        for i, (ext_dir, ext_id, pet_ver) in enumerate(all_targets, 1):
            pet_str = pet_ver if pet_ver else "(未安装)"
            print(f"    [{i}] {ext_id}: {ext_dir.name}  PET: {pet_str}")

        print(f"    [A] 全部安装")
        print(f"    [0] 取消")
        choice = prompt("  请选择:").upper()

        if choice == "0" or not choice:
            return []
        if choice == "A":
            return [d for d, _, _ in all_targets]

        try:
            idx = int(choice)
            if 1 <= idx <= len(all_targets):
                return [all_targets[idx - 1][0]]
        except ValueError:
            pass

        eprint("  无效选择，已取消。")
        return []

    else:
        # No eligible extensions found — manual target
        print("  未找到符合条件的扩展。请手动指定扩展目录。")
        if ext_dirs:
            for i, d in enumerate(ext_dirs, 1):
                print(f"    [{i}] {d}")
            print(f"    [0] 取消")
            choice = prompt("  请选择基础目录:")
            try:
                idx = int(choice)
                if idx == 0:
                    return []
                base_dir = ext_dirs[idx - 1]
            except (ValueError, IndexError):
                return []
        else:
            manual = prompt("  请输入扩展目录路径:")
            if not manual:
                return []
            base_dir = Path(os.path.expanduser(manual)).resolve()
            if not base_dir.is_dir():
                eprint(f"  ✗ 路径不存在: {base_dir}")
                return []

        # Look for extension subdirectories in the chosen base
        sub_choices: List[Path] = []
        for ext_id in EXTENSION_IDS:
            for match in sorted(base_dir.glob(f"{ext_id}-*"), reverse=True):
                if ext_id == "ms-python.python" and is_extension_too_old(match.name):
                    continue
                sub_choices.append(match)

        if not sub_choices:
            eprint(f"  ✗ 该目录中无符合条件的扩展。")
            return []

        print(f"  在 {base_dir} 中找到:")
        for i, m in enumerate(sub_choices, 1):
            print(f"    [{i}] {m.name}")
        print(f"    [A] 全部")
        print(f"    [0] 取消")
        choice = prompt("  请选择:").upper()

        if choice == "0" or not choice:
            return []
        if choice == "A":
            return sub_choices

        try:
            idx = int(choice)
            if 1 <= idx <= len(sub_choices):
                return [sub_choices[idx - 1]]
        except ValueError:
            pass

        return []


def step_install(targets: List[Path], binary_path: Path) -> Tuple[bool, bool]:
    """Step 7: Download i18n + install PET binary and i18n files.

    Returns (changed, failed).
    """
    print()
    print("▸ 安装 PET 及 i18n 文件...")

    if is_dry_run():
        print("  (DRY-RUN: 跳过实际安装)")
        for t in targets:
            print(f"  {t.name}:")
            print(f"    PET  → {t / PET_RELATIVE_PATH}")
            ext_id = get_extension_id(t.name)
            if ext_id:
                for dest_rel in I18N_FILES.values():
                    print(f"    i18n → {t / dest_rel}")
            else:
                print(f"    ⚠ 无法识别扩展 ID，跳过 i18n")
        return False, False

    if not binary_path or not binary_path.is_file():
        eprint("✗ 无可用的二进制文件。")
        return False, True

    changed = False
    failed = False

    # Figure out which i18n files are missing for each target
    # {ext_id: {dest_rel: needs_download}}
    i18n_needed: Dict[str, Dict[str, bool]] = {}
    for ext_dir in targets:
        ext_id = get_extension_id(ext_dir.name)
        if not ext_id:
            continue
        if ext_id not in i18n_needed:
            # Check which files are missing across all instances of this ext_id
            i18n_needed[ext_id] = {}
            status = check_i18n_status(ext_dir, ext_id)
            for dest_rel, exists in status.items():
                i18n_needed[ext_id][dest_rel] = not exists

    # Download only missing i18n files
    i18n_cache: Dict[str, Dict[str, Optional[Path]]] = {}
    for ext_id, needed in i18n_needed.items():
        missing_count = sum(1 for v in needed.values() if v)
        if missing_count == 0:
            continue  # nothing missing for this extension
        print(f"  下载 {ext_id} 缺失的 i18n 文件 ({missing_count} 个)...")
        tmp = Path(tempfile.mkdtemp(prefix=f"pet-i18n-{ext_id}-"))
        i18n_cache[ext_id] = {}
        for src_name, dest_rel in I18N_FILES.items():
            if needed.get(dest_rel):
                path = download_i18n_file(ext_id, src_name, tmp)
                i18n_cache[ext_id][dest_rel] = path
                if path is None:
                    failed = True
            else:
                i18n_cache[ext_id][dest_rel] = None  # already present, skip

    # Install PET + i18n for each target
    for ext_dir in targets:
        ext_id = get_extension_id(ext_dir.name)
        print(f"  {ext_dir.name}:")

        # Install PET binary
        pet_dest = ext_dir / PET_RELATIVE_PATH
        print(f"    PET  → {pet_dest}")
        if install_pet(ext_dir, binary_path):
            changed = True
        else:
            eprint(f"    ✗ PET 安装失败")
            failed = True

        # Install i18n files
        if ext_id and ext_id in i18n_cache:
            if install_i18n(ext_dir, i18n_cache[ext_id]):
                changed = True
        elif ext_id and ext_id in i18n_needed:
            # All files already present — nothing to do
            print(f"    i18n: ✓ 文件齐全")
        elif ext_id:
            eprint(f"    ⚠ i18n 数据未缓存，跳过")
        else:
            eprint(f"    ⚠ 无法识别扩展 ID，跳过 i18n")

    # Clean up i18n temp dirs
    for cache in i18n_cache.values():
        for p in cache.values():
            if p is not None and p.parent.name.startswith("pet-i18n-"):
                shutil.rmtree(p.parent, ignore_errors=True)
                break  # only need to remove once per ext_id

    return changed, failed


def _format_version(ver: Tuple[int, int, int]) -> str:
    return ".".join(str(x) for x in ver)


# ══════════════════════════════════════════════════════════════════════════
#  Entry point
# ══════════════════════════════════════════════════════════════════════════

def main():
    print_header()

    # 1. Platform check
    step_platform_check()

    # 2. Find extensions
    ext_dirs, eligible = step_find_extensions()

    # 3. Show local status
    step_show_local_status(eligible)

    # 4. Fetch remote release
    release = step_fetch_remote()

    # 5. Download
    if not prompt_yes_no("是否下载最新版本?", default=True):
        print("已取消。")
        sys.exit(EXIT_CANCELLED)

    binary_path = step_download(release)
    if binary_path is None:
        eprint("✗ 下载失败。")
        sys.exit(EXIT_NETWORK_ERROR)

    # 6. Select targets
    targets = step_select_targets(eligible, ext_dirs)
    if not targets:
        print("未选择安装目标，已取消。")
        shutil.rmtree(binary_path.parent, ignore_errors=True)
        sys.exit(EXIT_CANCELLED)

    # 7. Install
    changed, failed = step_install(targets, binary_path)

    # Clean up temp files
    if binary_path.parent.name.startswith("pet-install-"):
        shutil.rmtree(binary_path.parent, ignore_errors=True)

    # Report
    print()
    if changed:
        print("✓ 安装完成。")
        sys.exit(EXIT_UPDATED)
    elif failed:
        print("✗ 安装未完全成功，请检查上方错误信息。")
        sys.exit(EXIT_UPDATED)
    else:
        print("✓ 未执行任何更改。")
        sys.exit(EXIT_UP_TO_DATE)


if __name__ == "__main__":
    main()
