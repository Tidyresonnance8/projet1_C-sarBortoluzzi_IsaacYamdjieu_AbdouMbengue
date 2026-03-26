"""
Tests d'intégration pour SRTP.

Deux catégories :

5. TestClientServeur — notre server.py + notre client.py sur loopback,
   dans des conditions réseau parfait.
   Inclut des transferts de fichiers .txt sauvegardés dans OUTPUT_DIR.

6. TestClientServeur — notre server.py + notre client.py sur loopback,
   dans des conditions réseau imparfaits (pertes, corruptions, dédoublements).
   Inclut des transferts de fichiers .txt sauvegardés dans OUTPUT_DIR.

Lancement :
    make test (tous les tests)
    make test-v (verbeux)
    make test-integration (uniquement ce fichier)
    pytest tests/test_integration.py -v 
"""

import os
import sys
import time
import socket
import hashlib
import subprocess
import threading
import pathlib
import pytest
from proxy import *
from Helpers import *

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

#  Markers de skip
ref_server_present = pytest.mark.skipif(
    not os.path.isfile(SERVER_REF),
    reason=f"Binaire server absent ({SERVER_REF}). "
            "Télécharge-le depuis Moodle et place-le dans tools/."
)
ref_client_present = pytest.mark.skipif(
    not os.path.isfile(CLIENT_REF),
    reason=f"Binaire client absent ({CLIENT_REF}). "
            "Télécharge-le depuis Moodle et place-le dans tools/."
)


#  5. TESTS CLIENT <=> SERVEUR (notre implémentation)

class TestClientServeur:
    """
    Teste notre server.py contre notre client.py sur loopback IPv6.
    """

    def _run(self, tmp_path, filename, filesize):
        serve_dir = tmp_path / "serve"
        save_dir  = tmp_path / "save"
        serve_dir.mkdir()
        save_dir.mkdir()

        src = make_test_file(serve_dir, filename, filesize)
        dst = save_dir / "llm.model"
        port = free_port()

        server_cmd = ['python3', SERVER_PY, HOST, str(port)]
        client_cmd = ['python3', CLIENT_PY,
                        f'http://[{HOST}]:{port}/{filename}',
                        '--save', str(dst)]

        rc, _ = transfer_simple(server_cmd, client_cmd, server_cwd=serve_dir)
        assert rc == 0, f"Le client s'est terminé avec le code {rc}"
        assert dst.exists(), "Le fichier de destination n'a pas été créé"
        return md5(src), md5(dst)

    def _run_txt(self, tmp_path, filename, lines, output_name):
        serve_dir = tmp_path / "serve"
        serve_dir.mkdir()

        src  = make_text_file(serve_dir, filename, lines=lines)
        dst  = OUTPUT_DIR / output_name
        port = free_port()

        server_cmd = ['python3', SERVER_PY, HOST, str(port)]
        client_cmd = ['python3', CLIENT_PY,
                        f'http://[{HOST}]:{port}/{filename}',
                        '--save', str(dst)]

        rc, _ = transfer_simple(server_cmd, client_cmd, server_cwd=serve_dir)
        assert rc == 0, f"Le client s'est terminé avec le code {rc}"
        assert dst.exists(), "Le fichier .txt de destination n'a pas été créé"
        return src, dst

    # Transferts binaires

    def test_transfert_petit_fichier(self, tmp_path):
        """Fichier tenant dans un seul paquet (< 1024 octets)."""
        src_md5, dst_md5 = self._run(tmp_path, "small.bin", 500)
        assert src_md5 == dst_md5, "Intégrité compromise sur petit fichier"

    def test_transfert_exactement_1024_octets(self, tmp_path):
        """Fichier tenant exactement dans un paquet."""
        src_md5, dst_md5 = self._run(tmp_path, "exact.bin", 1024)
        assert src_md5 == dst_md5

    def test_transfert_multi_paquets(self, tmp_path):
        """Fichier nécessitant plusieurs paquets (5 Ko)."""
        src_md5, dst_md5 = self._run(tmp_path, "medium.bin", 5_000)
        assert src_md5 == dst_md5, "Intégrité compromise sur fichier moyen"

    def test_transfert_grand_fichier(self, tmp_path):
        """Fichier de 50 Ko — teste la fenêtre glissante."""
        src_md5, dst_md5 = self._run(tmp_path, "large.bin", 50_000)
        assert src_md5 == dst_md5, "Intégrité compromise sur grand fichier"

    # Transferts de fichiers texte (.txt) 

    def test_transfert_txt_petit(self, tmp_path):
        """Fichier .txt court (50 lignes) — tenant en un seul paquet."""
        src, dst = self._run_txt(tmp_path, "court.txt", lines=50, output_name="court_recu.txt")
        assert md5(src) == md5(dst), "Intégrité compromise sur .txt court"
        contenu = dst.read_text(encoding='utf-8')
        assert "Ligne 0000" in contenu, "Le début du fichier texte est manquant"
        assert "Ligne 0049" in contenu, "La fin du fichier texte est manquante"

    def test_transfert_txt_multi_paquets(self, tmp_path):
        """Fichier .txt de taille moyenne (500 lignes) — plusieurs paquets."""
        src, dst = self._run_txt(tmp_path, "moyen.txt", lines=500,output_name="moyen_recu.txt")
        assert md5(src) == md5(dst), "Intégrité compromise sur .txt moyen"
        contenu = dst.read_text(encoding='utf-8')
        assert "Ligne 0499" in contenu, "La dernière ligne est absente"

    def test_transfert_txt_grand(self, tmp_path):
        """Fichier .txt volumineux (1000001 lignes) — fenêtre glissante."""
        src, dst = self._run_txt(tmp_path, "grand.txt", lines=1000001, output_name="grand_recu.txt")
        assert md5(src) == md5(dst), "Intégrité compromise sur .txt grand"
        contenu = dst.read_text(encoding='utf-8')
        assert "Ligne 4999" in contenu, "La dernière ligne est absente"

    def test_transfert_txt_contenu_identique(self, tmp_path):
        """Vérifie que le contenu texte reçu est identique octet par octet."""
        src, dst = self._run_txt(tmp_path, "identique.txt", lines=100, output_name="identique_recu.txt")
        assert src.read_bytes() == dst.read_bytes(), \
            "Le contenu reçu diffère de la source"

    # Autres cas 

    def test_fichier_inexistant(self, tmp_path):
        """Le serveur doit répondre par un paquet vide si le fichier n'existe pas."""
        serve_dir = tmp_path / "serve"
        save_dir  = tmp_path / "save"
        serve_dir.mkdir()
        save_dir.mkdir()

        port = free_port()
        dst  = save_dir / "llm.model"

        server_cmd = ['python3', SERVER_PY, HOST, str(port)]
        client_cmd = ['python3', CLIENT_PY,
                        f'http://[{HOST}]:{port}/fichier_inexistant.bin',
                        '--save', str(dst)]

        rc, _ = transfer_simple(server_cmd, client_cmd, server_cwd=serve_dir)
        assert rc == 0, "Le client ne doit pas crasher sur fichier inexistant"
        if dst.exists():
            assert dst.stat().st_size == 0, "Le fichier doit être vide"

    def test_deux_clients_simultanes(self, tmp_path):
        """Deux clients — teste server_multitache."""
        serve_dir = tmp_path / "serve"
        serve_dir.mkdir()

        src_a = make_test_file(serve_dir, "file_a.bin", 10_000)
        src_b = make_test_file(serve_dir, "file_b.bin", 8_000)
        dst_a = tmp_path / "a.model"
        dst_b = tmp_path / "b.model"
        port  = free_port()

        server_cmd = ['python3', SERVER_PY, HOST, str(port)]
        ready = threading.Event()
        proc_server = run_server(server_cmd, ready, cwd=serve_dir)
        ready.wait(timeout=2)

        try:
            proc_a = subprocess.Popen(
                ['python3', CLIENT_PY,
                f'http://[{HOST}]:{port}/file_a.bin',
                '--save', str(dst_a)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            proc_b = subprocess.Popen(
                ['python3', CLIENT_PY,
                f'http://[{HOST}]:{port}/file_b.bin',
                '--save', str(dst_b)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            try:
                proc_a.wait(timeout=TIMEOUT_TRANSFER)
                proc_b.wait(timeout=TIMEOUT_TRANSFER)
            except subprocess.TimeoutExpired:
                proc_a.kill()
                proc_b.kill()
                pytest.fail("Timeout : les deux clients n'ont pas terminé à temps")
        finally:
            proc_server.terminate()
            proc_server.wait(timeout=2)

        assert dst_a.exists() and dst_b.exists(), "Un fichier de destination manque"
        assert md5(src_a) == md5(dst_a), "Intégrité compromise pour client A"
        assert md5(src_b) == md5(dst_b), "Intégrité compromise pour client B"

    def test_paquet_corrompu_ignore(self, tmp_path):
        """Transfert réussi même après un paquet corrompu envoyé au serveur."""
        serve_dir = tmp_path / "serve"
        serve_dir.mkdir()
        src = make_test_file(serve_dir, "test.bin", 2_000)
        dst = tmp_path / "llm.model"
        port = free_port()

        server_cmd = ['python3', SERVER_PY, HOST, str(port)]
        ready = threading.Event()
        proc_server = run_server(server_cmd, ready, cwd=serve_dir)
        ready.wait(timeout=2)

        try:
            with socket.socket(socket.AF_INET6, socket.SOCK_DGRAM) as s:
                s.sendto(b'\x00' * 20, (HOST, port))
            time.sleep(0.05)

            proc_client = subprocess.run(
                ['python3', CLIENT_PY,
                f'http://[{HOST}]:{port}/test.bin',
                '--save', str(dst)],
                timeout=TIMEOUT_TRANSFER,
                capture_output=True
            )
        finally:
            proc_server.terminate()
            proc_server.wait(timeout=2)

        assert proc_client.returncode == 0
        assert dst.exists()
        assert md5(src) == md5(dst), "Intégrité compromise après paquet corrompu"

#  6. TESTS CLIENT <=> SERVEUR (notre implémentation) sutr réseau avec corruption, pertes et dédoublement
class TestReseauImparfait:
    """
    Tests avec pertes et corruption via UDPCapturingProxy.
    """

    def test_transfert_avec_pertes_et_corruption(self, tmp_path):
        serve_dir = tmp_path / "serve"
        client_dir = tmp_path / "client"
        serve_dir.mkdir()
        client_dir.mkdir()

        filename = "imperfect.bin"
        filesize = 20_000

        src = make_test_file(serve_dir, filename, filesize)
        port = free_port()

        server_cmd = ['python3', SERVER_PY, HOST, str(port)]
        ready = threading.Event()
        proc_server = run_server(server_cmd, ready, cwd=serve_dir)
        ready.wait(timeout=2)

        proxy = UDPCapturingProxy(port, imperfect_network=True)
        proxy.start()

        try:
            proc = subprocess.run(
                ['python3', CLIENT_PY,
                f'http://[{HOST}]:{proxy.proxy_port}/{filename}',
                '--save', str(client_dir / "out.bin")],
                timeout=TIMEOUT_TRANSFER,
                capture_output=True
            )
        finally:
            proxy.wait_done(timeout=5)
            proxy.stop()
            proc_server.terminate()
            proc_server.wait(timeout=2)

        received = proxy.get_received_data()

        assert proc.returncode == 0, "Le client a échoué en réseau imparfait"
        assert md5_bytes(received) == md5(src), \
            "Intégrité compromise avec pertes/corruption"

    def test_transfert_avec_pertes_et_corruption_grand(self, tmp_path):
        serve_dir = tmp_path / "serve"
        client_dir = tmp_path / "client"
        serve_dir.mkdir()
        client_dir.mkdir()

        filename = "imperfect.bin"
        filesize = 200_000

        src = make_test_file(serve_dir, filename, filesize)
        port = free_port()

        server_cmd = ['python3', SERVER_PY, HOST, str(port)]
        ready = threading.Event()
        proc_server = run_server(server_cmd, ready, cwd=serve_dir)
        ready.wait(timeout=2)

        proxy = UDPCapturingProxy(port, imperfect_network=True)
        proxy.start()

        try:
            proc = subprocess.run(
                ['python3', CLIENT_PY,
                f'http://[{HOST}]:{proxy.proxy_port}/{filename}',
                '--save', str(client_dir / "out.bin")],
                timeout=TIMEOUT_TRANSFER,
                capture_output=True
            )
        finally:
            proxy.wait_done(timeout=5)
            proxy.stop()
            proc_server.terminate()
            proc_server.wait(timeout=2)

        received = proxy.get_received_data()

        assert proc.returncode == 0, "Le client a échoué en réseau imparfait"
        assert md5_bytes(received) == md5(src), \
            "Intégrité compromise avec pertes/corruption"
        
    def test_transfert_txt_reseau_imparfait(self, tmp_path):
        """
        Transfert d'un fichier texte avec pertes et corruption réseau.
        Vérifie le fichier sauvegardé par le client (comme les autres tests).
        """
        serve_dir = tmp_path / "serve"
        serve_dir.mkdir()

        filename = "imperfect.txt"
        output_file = OUTPUT_DIR / "imperfect_recu.txt"

        # Générer fichier texte source
        src = make_text_file(serve_dir, filename, lines=1001)

        port = free_port()

        server_cmd = ['python3', SERVER_PY, HOST, str(port)]
        ready = threading.Event()
        proc_server = run_server(server_cmd, ready, cwd=serve_dir)
        ready.wait(timeout=2)

        # Proxy avec réseau imparfait
        proxy = UDPCapturingProxy(port, imperfect_network=True)
        proxy.start()

        try:
            proc = subprocess.run(
                ['python3', CLIENT_PY,
                f'http://[{HOST}]:{proxy.proxy_port}/{filename}',
                '--save', str(output_file)],
                timeout=TIMEOUT_TRANSFER,
                capture_output=True
            )
        finally:
            proxy.wait_done(timeout=5)
            proxy.stop()
            proc_server.terminate()
            proc_server.wait(timeout=2)

        # 🔍 Assertions (comme les autres tests .txt)
        assert proc.returncode == 0, "Le client a échoué en réseau imparfait"
        assert output_file.exists(), "Le fichier texte n'a pas été créé"

        # Intégrité
        assert md5(src) == md5(output_file), \
            "Intégrité compromise sur fichier texte"

        # Vérification contenu
        contenu = output_file.read_text(encoding='utf-8')

        assert "Ligne 0000" in contenu, "Début du fichier manquant"
        assert "Ligne 1000" in contenu, "Fin du fichier manquante"

        # Vérification stricte (optionnelle mais recommandée)
        assert src.read_text(encoding='utf-8') == contenu, \
            "Le contenu texte diffère exactement de la source"
