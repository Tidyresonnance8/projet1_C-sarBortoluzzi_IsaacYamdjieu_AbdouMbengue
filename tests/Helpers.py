import os
import sys
import time
import socket
import hashlib
import subprocess
import threading
import pathlib
import proxy as UDP_proxy

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import protocol

#  Constantes
HOST        = "::1"
SRC_DIR     = os.path.join(os.path.dirname(__file__), '..', 'src')
TOOLS_DIR   = os.path.join(os.path.dirname(__file__), '..', 'tools')
SERVER_PY   = os.path.join(SRC_DIR, 'server.py')
CLIENT_PY   = os.path.join(SRC_DIR, 'client.py')

# Binaires de référence dans tools/ à la racine du projet
SERVER_REF  = os.path.join(TOOLS_DIR, 'server')
CLIENT_REF  = os.path.join(TOOLS_DIR, 'client')

TIMEOUT_TRANSFER = 60   # secondes max pour un transfert complet

# Dossier de sortie persistant pour les fichiers .txt reçus
OUTPUT_DIR = pathlib.Path(os.path.join(os.path.dirname(__file__), '..', 'test_outputs'))
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

#  Helpers de base

def free_port():
    """Retourne un port UDP libre sur ::1."""
    with socket.socket(socket.AF_INET6, socket.SOCK_DGRAM) as s:
        s.bind((HOST, 0))
        return s.getsockname()[1]

def md5(path):
    h = hashlib.md5()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(8192), b''):
            h.update(chunk)
    return h.hexdigest()

def md5_bytes(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def make_test_file(tmp_path, name, size):
    """Crée un fichier binaire de test avec contenu déterministe."""
    path = tmp_path / name
    data = bytes((i * 37 + 13) % 256 for i in range(size))
    path.write_bytes(data)
    return path

def make_text_file(tmp_path, name, lines=50):
    """Crée un fichier texte .txt avec contenu déterministe."""
    path = tmp_path / name
    content = "\n".join(
        f"Ligne {i:04d} — test SRTP, pour l'instant ça va"
        for i in range(lines)
    )
    path.write_text(content, encoding='utf-8')
    return path

def run_server(cmd, ready_event, cwd=None):
    """Lance un serveur en subprocess et signale quand il est prêt."""
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(cwd) if cwd is not None else None,
    )
    time.sleep(0.4)
    ready_event.set()
    return proc


def transfer_simple(server_cmd, client_cmd, timeout=TIMEOUT_TRANSFER,server_cwd=None, client_cwd=None):
    """
    Lance server_cmd puis client_cmd, attend la fin du client.
    Retourne (returncode_client, proc_server).
    """
    ready = threading.Event()
    proc_server = run_server(server_cmd, ready, cwd=server_cwd)
    ready.wait(timeout=2)
    try:
        proc_client = subprocess.run(
            client_cmd,
            timeout=timeout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(client_cwd) if client_cwd is not None else None,
        )
        return proc_client.returncode, proc_server
    except subprocess.TimeoutExpired:
        return -1, proc_server
    finally:
        proc_server.terminate()
        proc_server.wait(timeout=2)

#  Helper interop : lancer client_ref via le proxy

def run_client_ref_via_proxy(serve_dir, filename, server_port, client_cwd, is_text=False, lines=None, filesize=None):
    """
    Lance client_ref en le faisant pointer vers un proxy UDP capturant.

    Le proxy relaie tout vers notre serveur (server_port) et reconstitue
    le fichier reçu côté test, indépendamment du fait que client_ref
    écrive ou non un fichier local.

    Retourne (src_path, received_bytes, rc_client).
    - src_path : Path du fichier original servi
    - received_bytes : contenu reconstitué par le proxy
    - rc_client : code de retour du client_ref (0 = succès)
    """

    # Créer le fichier source si nécessaire
    if is_text:
        src = make_text_file(serve_dir, filename, lines=lines)
    else:
        src = make_test_file(serve_dir, filename, filesize)

    # Démarrer le proxy
    proxy = UDP_proxy.UDPCapturingProxy(server_port)
    proxy.start()

    # Lancer client_ref vers le port du proxy
    # On passe quand même --save pour les clients qui le supportent
    save_path = client_cwd / "received_by_ref.bin"
    cmd = [CLIENT_REF,
            f'http://[{HOST}]:{proxy.proxy_port}/{filename}',
            '--save', str(save_path)]

    try:
        result = subprocess.run(
            cmd,
            timeout=TIMEOUT_TRANSFER,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(client_cwd)
        )
        rc = result.returncode
    except subprocess.TimeoutExpired:
        rc = -1

    # Attendre la fin du transfert côté proxy (signal paquet vide)
    proxy.wait_done(timeout=5)
    proxy.stop()

    received = proxy.get_received_data()

    # Bonus : si client_ref a quand même écrit un fichier, on le signale
    wrote_file = save_path.exists() or (client_cwd / "llm.model").exists()
    if wrote_file:
        print(f"  [proxy] client_ref a aussi écrit un fichier local", flush=True)

    return src, received, rc