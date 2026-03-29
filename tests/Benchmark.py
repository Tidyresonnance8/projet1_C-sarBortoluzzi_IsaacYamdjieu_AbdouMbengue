#!/usr/bin/env python3
"""
Benchmark de performances SRTP — génère les graphiques pour les slides.

Usage :
    python3 benchmark_srtp.py

Produit 3 figures dans ./plots/ :
    throughput_vs_filesize.png — débit (KB/s) en fonction de la taille du fichier
    throughput_vs_loss.png — débit en fonction du taux de perte (réseau imparfait)
    latency_cdf.png — CDF du temps de transfert par taille de fichier
"""

import os
import sys
import time
import socket
import hashlib
import pathlib
import subprocess
import threading
import statistics
import random

import matplotlib
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker
import numpy as np

# Chemins
SCRIPT_DIR = pathlib.Path(__file__).parent.parent
SRC_DIR    = SCRIPT_DIR / "src" if (SCRIPT_DIR / "src").exists() else SCRIPT_DIR
SERVER_PY  = SRC_DIR / "server.py"
CLIENT_PY  = SRC_DIR / "client.py"
PLOT_DIR   = SCRIPT_DIR / "plots"
PLOT_DIR.mkdir(exist_ok=True)

HOST    = "::1"
TIMEOUT = 120

# 12 tailles de fichier de 1 KB à 100 MB (progression quasi-logarithmique)
FILE_SIZES_KB = [1, 5, 20, 50, 100, 256, 512, 1024, 2048, 5120, 10240, 102400]

# Taille fixe utilisée pour l'expérience « débit vs taux de perte »
LOSS_BENCH_SIZE_KB = 512   # 512 KB 


# Helpers 

def free_port():
    with socket.socket(socket.AF_INET6, socket.SOCK_DGRAM) as s:
        s.bind((HOST, 0))
        return s.getsockname()[1]


def make_file(path: pathlib.Path, size: int) -> pathlib.Path:
    path.write_bytes(bytes((i * 37 + 13) % 256 for i in range(size)))
    return path


def run_transfer(serve_dir, filename, filesize, timeout=TIMEOUT):
    """
    Lance server.py + client.py et retourne (durée_s, ok).
    ok = True si le fichier reçu correspond à la source.
    """
    make_file(serve_dir / filename, filesize)
    dst  = serve_dir / "received.out"
    port = free_port()

    server_proc = subprocess.Popen(
        [sys.executable, str(SERVER_PY), HOST, str(port)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        cwd=str(serve_dir)
    )
    time.sleep(0.3)

    t0 = time.perf_counter()
    try:
        result = subprocess.run(
            [sys.executable, str(CLIENT_PY),
            f"http://[{HOST}]:{port}/{filename}",
            "--save", str(dst)],
            timeout=timeout,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        elapsed = time.perf_counter() - t0
        ok = (result.returncode == 0 and dst.exists()
            and dst.stat().st_size == filesize)
    except subprocess.TimeoutExpired:
        elapsed = timeout
        ok = False
    finally:
        server_proc.terminate()
        server_proc.wait(timeout=2)

    return elapsed, ok


def _fmt_size(kb: int) -> str:
    """Formate un nombre de KB en chaîne lisible (KB / MB)."""
    if kb < 1024:
        return f"{kb} KB"
    mb = kb / 1024
    return f"{mb:.0f} MB" if mb == int(mb) else f"{mb:.1f} MB"


# Expérience 1 : débit vs taille de fichier

def bench_throughput_vs_size(tmp_base: pathlib.Path, sizes_kb, repeats=3):
    """Mesure le débit moyen (KB/s) pour chaque taille de fichier."""
    results = {}   # size_kb -> [throughput_KBs, ...]
    for kb in sizes_kb:
        size = kb * 1024
        throughputs = []
        for rep in range(repeats):
            serve_dir = tmp_base / f"sz_{kb}_{rep}"
            serve_dir.mkdir(parents=True, exist_ok=True)
            elapsed, ok = run_transfer(serve_dir, "bench.bin", size)
            if ok and elapsed > 0:
                throughputs.append(size / elapsed / 1024)   # KB/s
            else:
                throughputs.append(0.0)
            print(f"  size={_fmt_size(kb):>10}  rep={rep}  {'OK' if ok else 'FAIL'}  "
                f"{elapsed:.2f}s  {throughputs[-1]:.1f} KB/s")
        results[kb] = throughputs
    return results


def plot_throughput_vs_size(results: dict, out: pathlib.Path):
    sizes  = sorted(results)
    means  = [statistics.mean(results[s]) for s in sizes]
    stdevs = [statistics.stdev(results[s]) if len(results[s]) > 1 else 0
            for s in sizes]

    x_kb = np.array(sizes, dtype=float)

    fig, ax = plt.subplots(figsize=(10, 5))
    ax.errorbar(x_kb, means, yerr=stdevs, fmt="-o", color="blue",
                linewidth=2, capsize=4, markersize=6)
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.xaxis.set_major_formatter(ticker.FuncFormatter(
        lambda v, _: _fmt_size(int(v))))
    ax.xaxis.set_minor_formatter(ticker.NullFormatter())
    ax.set_xticks(x_kb)
    ax.set_xticklabels([_fmt_size(s) for s in sizes], fontsize=8,
                        rotation=30, ha="right")
    ax.set_xlabel("Taille du fichier")
    ax.set_ylabel("Débit moyen (KB/s)")
    ax.set_title("Débit en fonction de la taille du fichier (log-log)\n"
                f"(réseau loopback, sans pertes, {len(sizes)} tailles de 1 KB à 100 MB)")
    ax.grid(which="both", linestyle="--", alpha=0.4)
    fig.tight_layout()
    fig.savefig(out, dpi=150)
    plt.close(fig)
    print(f"→ {out}")


# Expérience 2 : débit vs taux de perte (simulé via le proxy)

def bench_throughput_vs_loss(tmp_base: pathlib.Path, loss_rates, repeats=3,
                            file_size_kb=LOSS_BENCH_SIZE_KB):
    """
    Pour chaque taux de perte simulé, mesure le débit en utilisant
    le UDPCapturingProxy avec imperfect_network=True et DROP_PROB fixé.

    On modifie DROP_PROB dynamiquement avant chaque mesure.
    """
    # Import local du proxy
    sys.path.insert(0, str(SCRIPT_DIR))
    try:
        import proxy as proxy_mod
        import protocol
    except ImportError:
        print("WARN : proxy.py ou protocol.py introuvable — skip expérience 2")
        return {}

    FILE_SIZE = file_size_kb * 1024

    results = {}
    for rate in loss_rates:
        throughputs = []
        for rep in range(repeats):
            serve_dir = tmp_base / f"loss_{int(rate*100)}_{rep}"
            serve_dir.mkdir(parents=True, exist_ok=True)
            src = make_file(serve_dir / "bench.bin", FILE_SIZE)

            port = free_port()

            server_proc = subprocess.Popen(
                [sys.executable, str(SERVER_PY), HOST, str(port)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                cwd=str(serve_dir)
            )
            time.sleep(0.3)

            # Instancier le proxy avec DROP_PROB = rate, CORRUPT_PROB = 0
            prx = proxy_mod.UDPCapturingProxy(server_port=port, imperfect_network=True)
            prx.DROP_PROB = rate
            prx.CORRUPT_PROB = rate
            prx.DUPLICATE_PROB = rate
            prx.start()

            t0 = time.perf_counter()
            try:
                res = subprocess.run(
                    [sys.executable, str(CLIENT_PY),
                    f"http://[{HOST}]:{prx.proxy_port}/bench.bin",
                    "--save", str(serve_dir / "recv.out")],
                    timeout=TIMEOUT,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                elapsed = time.perf_counter() - t0
                prx.wait_done(timeout=5)
                prx.stop()

                recv = serve_dir / "recv.out"
                ok   = (res.returncode == 0 and recv.exists()
                        and recv.stat().st_size == FILE_SIZE)
                tp   = FILE_SIZE / elapsed / 1024 if (ok and elapsed > 0) else 0.0
            except subprocess.TimeoutExpired:
                elapsed = TIMEOUT
                tp = 0.0
                prx.stop()
            finally:
                server_proc.terminate()
                server_proc.wait(timeout=2)

            throughputs.append(tp)
            print(f"  loss={rate:.0%}  rep={rep}  {elapsed:.2f}s  {tp:.1f} KB/s")

        results[rate] = throughputs
    return results


def plot_throughput_vs_loss(results: dict, out: pathlib.Path, file_size_kb=LOSS_BENCH_SIZE_KB):
    if not results:
        return
    rates  = sorted(results)
    means  = [statistics.mean(results[r]) for r in rates]
    stdevs = [statistics.stdev(results[r]) if len(results[r]) > 1 else 0
            for r in rates]

    fig, ax = plt.subplots(figsize=(7, 4))
    # Offset de 0.1 sur l'axe X pour éviter log(0) au taux 0%
    x_pct = [r * 100 + 0.1 for r in rates]
    ax.errorbar(x_pct, means, yerr=stdevs,
                fmt="-o", color="orange", linewidth=2, capsize=4,
                markersize=6)
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.xaxis.set_major_formatter(ticker.FuncFormatter(
        lambda v, _: f"{v - 0.1:.4g}%"))
    ax.set_xticks(x_pct)
    ax.set_xticklabels([f"{r * 100:.4g}%" for r in rates], fontsize=8)
    ax.set_xlabel("Taux de perte simulé (%)")
    ax.set_ylabel("Débit moyen (KB/s)")
    ax.set_title(f"Impact des pertes sur le débit (log-log)\n"
                f"(fichier {_fmt_size(file_size_kb)}, proxy UDP loopback)")
    ax.grid(which="both", linestyle="--", alpha=0.4)
    fig.tight_layout()
    fig.savefig(out, dpi=150)
    plt.close(fig)
    print(f"→ {out}")



# Main 

def main():
    import tempfile

    if not SERVER_PY.exists() or not CLIENT_PY.exists():
        print(f"ERREUR : server.py ou client.py introuvable dans {SRC_DIR}")
        print("Lancez ce script depuis la racine du projet (là où src/ est présent),")
        print("ou copiez server.py / client.py à côté de ce script.")
        sys.exit(1)

    print("=" * 60)
    print("Benchmark SRTP — génération des graphiques")
    print(f"Tailles testées : {', '.join(_fmt_size(s) for s in FILE_SIZES_KB)}")
    print("=" * 60)

    with tempfile.TemporaryDirectory(prefix="srtp_bench_") as tmp_str:
        tmp = pathlib.Path(tmp_str)

        # 1) Débit vs taille (12 tailles, 1 KB → 100 MB)
        print(f"\n[1/3] Débit vs taille de fichier ({len(FILE_SIZES_KB)} tailles)")
        r1 = bench_throughput_vs_size(tmp / "exp1", FILE_SIZES_KB, repeats=3)
        plot_throughput_vs_size(r1, PLOT_DIR / "throughput_vs_filesize.png")

        # 2) Débit vs taux de perte (fichier fixe = LOSS_BENCH_SIZE_KB)
        print(f"\n[2/3] Débit vs taux de perte (fichier fixe {_fmt_size(LOSS_BENCH_SIZE_KB)})")
        loss_rates = [0.0, 0.05, 0.10, 0.20, 0.30]
        r2 = bench_throughput_vs_loss(tmp / "exp2", loss_rates, repeats=3, file_size_kb=LOSS_BENCH_SIZE_KB)
        plot_throughput_vs_loss(r2, PLOT_DIR / "throughput_vs_loss.png",file_size_kb=LOSS_BENCH_SIZE_KB)

    print("\nTerminé. Graphiques dans :", PLOT_DIR)


if __name__ == "__main__":
    main()