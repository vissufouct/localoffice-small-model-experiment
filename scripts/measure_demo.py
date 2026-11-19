"""Measure an existing offline demo and its owned process tree; never downloads weights."""
import argparse
import hashlib
import importlib.metadata
import json
import platform
import subprocess
import sys
import time
from pathlib import Path
import psutil

# Provisional development regression limits, declared before the measured run.
# They apply to the small backend-switch fixture, not arbitrary documents.
LIMITS = {'elapsed_seconds': 180, 'tree_rss_bytes': 8 * 2**30,
          'swap_growth_bytes': 256 * 2**20, 'minimum_available_bytes': 1 * 2**30}


def command_version(args):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=5)
        return (result.stdout + result.stderr).strip()[:1000]
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {'unavailable': type(exc).__name__}


def pressure():
    try:
        value = subprocess.run(['sysctl', '-n', 'kern.memorystatus_vm_pressure_level'],
                               capture_output=True, text=True, timeout=2, check=True)
        return int(value.stdout.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('script', type=Path)
    parser.add_argument('arguments', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    script = args.script.resolve(strict=True)
    if script.parent != Path('scripts').resolve() or not script.name.startswith('demo_'):
        parser.error('Select an existing project scripts/demo_*.py file.')
    args.report.parent.mkdir(parents=True, exist_ok=True)
    packages = {}
    for name in ('mlx', 'mlx-lm', 'fastapi', 'uvicorn', 'psutil', 'pypdf', 'pypdfium2', 'pillow'):
        try: packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError: packages[name] = None
    versions = {'python': sys.version, 'packages': packages,
                'node': command_version(['node', '--version']),
                'cargo': command_version(['cargo', '--version']),
                'llama_server': command_version(['llama-server', '--version']),
                'tesseract': command_version(['tesseract', '--version'])}
    locks = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in
             map(Path, ('uv.lock', 'package-lock.json', 'apps/desktop/src-tauri/Cargo.lock'))}
    samples = []; owned = {}; started = time.monotonic(); next_pressure = 0; level = None
    baseline = psutil.swap_memory().used
    log_path = args.report.with_suffix('.log')
    with log_path.open('w') as log:
        process = subprocess.Popen([sys.executable, str(script), *args.arguments],
                                   stdout=log, stderr=log, start_new_session=True)
        root = psutil.Process(process.pid)
        timed_out = False
        try:
            while True:
                elapsed = time.monotonic() - started
                try:
                    for child in [root, *root.children(recursive=True)]:
                        owned[(child.pid, child.create_time())] = child
                except psutil.NoSuchProcess: pass
                rss = 0; live = 0
                for (pid, born), child in list(owned.items()):
                    try:
                        if child.create_time() == born and child.status() != psutil.STATUS_ZOMBIE:
                            rss += child.memory_info().rss; live += 1
                    except psutil.NoSuchProcess: pass
                if elapsed >= next_pressure:
                    level = pressure(); next_pressure = elapsed + 1
                vm = psutil.virtual_memory()
                samples.append({'seconds': round(elapsed, 3), 'tree_rss_bytes': rss,
                                'live_owned_processes': live, 'available_bytes': vm.available,
                                'system_total_minus_available_bytes': vm.total-vm.available,
                                'swap_used_bytes': psutil.swap_memory().used, 'pressure_level': level})
                if process.poll() is not None: break
                if elapsed > LIMITS['elapsed_seconds'] + 30:
                    timed_out = True; process.kill(); process.wait(timeout=5); break
                time.sleep(.1)
        finally:
            if process.poll() is None: process.kill(); process.wait(timeout=5)
    peak = max(s['tree_rss_bytes'] for s in samples)
    swap_growth = max(0, max(s['swap_used_bytes'] for s in samples)-baseline)
    minimum = min(s['available_bytes'] for s in samples)
    levels = sorted({s['pressure_level'] for s in samples if s['pressure_level'] is not None})
    elapsed = time.monotonic()-started
    checks = {'demo_exit_success': process.returncode == 0 and not timed_out,
              'latency': elapsed <= LIMITS['elapsed_seconds'],
              'process_tree_rss': peak <= LIMITS['tree_rss_bytes'],
              'swap_growth': swap_growth <= LIMITS['swap_growth_bytes'],
              'available_headroom': minimum >= LIMITS['minimum_available_bytes'],
              'pressure_observed_and_normal': bool(levels) and levels == [1]}
    result = {'status': 'passed' if all(checks.values()) else 'needs_review',
              'scope': 'Small offline demo development regression envelope; not physical 16 GB qualification',
              'script': str(script), 'arguments': args.arguments, 'versions': versions,
              'lock_sha256': locks, 'hardware': {'platform': platform.platform(),
              'chip': command_version(['sysctl', '-n', 'machdep.cpu.brand_string']),
              'physical_memory_bytes': psutil.virtual_memory().total},
              'limits': LIMITS, 'checks': checks, 'elapsed_seconds': elapsed,
              'peak_sampled_tree_rss_bytes': peak, 'peak_system_total_minus_available_bytes':
              max(s['system_total_minus_available_bytes'] for s in samples),
              'minimum_available_bytes': minimum, 'peak_swap_growth_bytes': swap_growth,
              'pressure_levels': levels, 'exit_code': process.returncode, 'log': str(log_path),
              'measurement_notes': ['100ms sampling can miss short peaks.',
              'Summed process RSS may double-count shared pages; it is not unique physical or GPU allocation.',
              'System availability/pressure/swap include unrelated workloads; no claim of attribution.',
              'macOS pressure raw levels: 1 normal, 2 warning, 4 critical; unknown values require review.'],
              'samples': samples}
    args.report.write_text(json.dumps(result, indent=2))
    print(json.dumps({k:v for k,v in result.items() if k not in ('samples','versions','lock_sha256')}, indent=2))
    return 0 if result['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
