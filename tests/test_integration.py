"""
Tests d'intégration et d'interopérabilité pour SRTP.

Deux catégories :

5. TestClientServeur  — ton server.py + ton client.py sur loopback,
   dans des conditions réseau variées (parfait, pertes, corruption).
   Inclut des transferts de fichiers .txt sauvegardés dans OUTPUT_DIR.

6. TestInteroperabilite — ton implémentation contre les binaires de
   référence fournis sur Moodle (tools/server / tools/client).
   Ces tests sont skippés automatiquement si les binaires sont absents.
   Pour les activer : place server et client dans tools/ à la racine
   du projet, puis relance pytest.

Notes sur les interfaces des binaires de référence (spec §2.4) :
    server_ref: tools/server <hostname> <port> [--root <dir>]
                Par défaut root = dossier courant du processus.
                On lance avec cwd=serve_dir SANS --root.

    client_ref: tools/client <url> [--save <path>]
                --save peut être ignoré ou non supporté selon la version.
                On utilise un proxy UDP capturant pour reconstruire le
                fichier reçu côté test, indépendamment du comportement
                de fichier du client_ref.

Lancement :
    make test (tous les tests)
    make test-v (verbeux)
    pytest tests/test_integration.py -v (uniquement ce fichier)
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


#  5. TESTS CLIENT ↔ SERVEUR (notre implémentation)

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

#  6. TESTS D'INTEROPÉRABILITÉ avec les binaires de référence

class TestInteroperabilite:
    """
    Tests d'interopérabilité entre notre implémentation et les binaires
    de référence fournis sur Moodle (tools/server et tools/client).

    Stratégie pour client_ref :
    client_ref peut ne pas écrire de fichier local (--save ignoré ou
    comportement non garanti). On intercale donc un proxy UDP capturant
    (UDPCapturingProxy) entre client_ref et le serveur. Le proxy relaie
    tous les paquets UDP dans les deux sens et reconstruit le fichier
    reçu par le client en capturant les paquets DATA côté serveur->client.
    On compare ensuite le MD5 du fichier source avec le MD5 du contenu
    capturé — sans dépendre d'aucune écriture disque de client_ref.

    Quatre combinaisons :
    1) server_ref + client_ref vs sanity check des binaires
    2) notre server.py + client_ref vs notre serveur est conforme
    3) server_ref + notre client.py vs notre client est conforme
    4) notre server.py + N clients_ref vs serveur multi-clients conforme
    """

    #  Helper : lancer server_ref + proxy + client_ref

    def trasfer_via_proxy(self, serve_dir, filename, server_cmd,server_cwd, client_dir, server_port, filesize=None, is_text=False, lines=None):
        """
        Lance server_cmd, puis client_ref via UDPCapturingProxy.

        Retourne (src_path, received_bytes, rc_client).
        """
        ready = threading.Event()
        proc_server = run_server(server_cmd, ready, cwd=server_cwd)
        ready.wait(timeout=2)

        try:
            src, received, rc = run_client_ref_via_proxy(
                serve_dir=serve_dir,
                filename=filename,
                server_port=server_port,
                client_cwd=client_dir,
                is_text=is_text,
                lines=lines,
                filesize=filesize,
            )
        finally:
            proc_server.terminate()
            proc_server.wait(timeout=2)

        return src, received, rc

    #  Helper : plusieurs client_ref simultanés via proxies

    def _run_multi_client_ref(self, server_port, jobs, timeout=TIMEOUT_TRANSFER):
        """
        Lance plusieurs client_ref simultanément, chacun via son propre proxy.

        jobs = liste de dicts : {filename, filesize?, is_text?, lines?, client_dir}
        Retourne liste de (src_path, received_bytes, rc).
        """
        proxies = []
        procs   = []
        srcs    = []

        for job in jobs:
            proxy = UDPCapturingProxy(server_port)
            proxy.start()
            proxies.append(proxy)

            # Créer le fichier source
            if job.get('is_text'):
                src = make_text_file(job['serve_dir'], job['filename'], lines=job['lines'])
            else:
                src = make_test_file(job['serve_dir'], job['filename'], job['filesize'])
            srcs.append(src)

            save_path = job['client_dir'] / "received_by_ref.bin"
            cmd = [CLIENT_REF,
                    f'http://[{HOST}]:{proxy.proxy_port}/{job["filename"]}',
                    '--save', str(save_path)]
            p = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(job['client_dir']),
            )
            procs.append(p)

        # Attendre tous les clients
        rcs = []
        deadline = time.time() + timeout
        for p in procs:
            remaining = max(0, deadline - time.time())
            try:
                p.wait(timeout=remaining)
                rcs.append(p.returncode)
            except subprocess.TimeoutExpired:
                p.kill()
                rcs.append(-1)

        # Attendre fin des proxies et récupérer les données
        results = []
        for proxy, src, rc in zip(proxies, srcs, rcs):
            proxy.wait_done(timeout=5)
            proxy.stop()
            received = proxy.get_received_data()
            results.append((src, received, rc))

        return results

    # 1) Sanity check : server_ref + client_ref 

    @ref_server_present
    @ref_client_present
    def test_1_sanity_ref_vs_ref(self, tmp_path):
        """
        Vérifie que les deux binaires de référence fonctionnent ensemble.
        Si ce test échoue, le problème vient des binaires, pas du code.
        Le proxy capture ce que client_ref reçoit réellement du serveur.
        """
        serve_dir  = tmp_path / "serve"
        client_dir = tmp_path / "client"
        serve_dir.mkdir(); client_dir.mkdir()

        port = free_port()
        server_cmd = [SERVER_REF, HOST, str(port)]

        src, received, rc = self.trasfer_via_proxy(
            serve_dir, "test.bin", server_cmd,
            server_cwd=serve_dir, client_dir=client_dir,
            server_port=port, filesize=5_000,
        )

        assert rc >= 0, f"client_ref s'est terminé avec le code {rc}"
        assert len(received) > 0, "Le proxy n'a rien capturé — client_ref n'a pas envoyé de requête ?"
        assert md5_bytes(received) == md5(src), \
            f"Intégrité compromise (sanity ref vs ref) : reçu {len(received)} o, attendu {src.stat().st_size} o"

    # 2) Notre serveur + client de référence 

    @ref_client_present
    def test_2_notre_serveur_client_ref_petit(self, tmp_path):
        """client_ref télécharge un petit fichier (.bin) depuis notre server.py."""
        serve_dir  = tmp_path / "serve"
        client_dir = tmp_path / "client"
        serve_dir.mkdir(); client_dir.mkdir()

        port = free_port()
        server_cmd = ['python3', SERVER_PY, HOST, str(port)]

        src, received, rc = self.trasfer_via_proxy(
            serve_dir, "small.bin", server_cmd,
            server_cwd=serve_dir, client_dir=client_dir,
            server_port=port, filesize=500,
        )

        assert rc == 0, f"client_ref s'est terminé avec le code {rc}"
        assert md5_bytes(received) == md5(src), \
            "Intégrité compromise (2, petit .bin)"

    @ref_client_present
    def test_2_notre_serveur_client_ref_grand(self, tmp_path):
        """client_ref télécharge un grand fichier (.bin) depuis notre server.py."""
        serve_dir  = tmp_path / "serve"
        client_dir = tmp_path / "client"
        serve_dir.mkdir(); client_dir.mkdir()

        port = free_port()
        server_cmd = ['python3', SERVER_PY, HOST, str(port)]

        src, received, rc = self.trasfer_via_proxy(
            serve_dir, "large.bin", server_cmd,
            server_cwd=serve_dir, client_dir=client_dir,
            server_port=port, filesize=50_000,
        )

        assert rc == 0, f"client_ref s'est terminé avec le code {rc}"
        assert md5_bytes(received) == md5(src), \
            "Intégrité compromise (2, grand .bin)"

    @ref_client_present
    def test_2_notre_serveur_client_ref_txt(self, tmp_path):
        """client_ref télécharge un fichier .txt depuis notre server.py."""
        serve_dir  = tmp_path / "serve"
        client_dir = tmp_path / "client"
        serve_dir.mkdir(); client_dir.mkdir()

        port = free_port()
        server_cmd = ['python3', SERVER_PY, HOST, str(port)]

        src, received, rc = self.trasfer_via_proxy(
            serve_dir, "readme.txt", server_cmd,
            server_cwd=serve_dir, client_dir=client_dir,
            server_port=port, is_text=True, lines=200
        )

        assert rc == 0, f"client_ref s'est terminé avec le code {rc}"
        assert md5_bytes(received) == md5(src), \
            "Intégrité compromise (2, .txt)"

        # Sauvegarder dans OUTPUT_DIR pour inspection manuelle
        out = OUTPUT_DIR / "interop_client_ref_readme_recu.txt"
        out.write_bytes(received)

    @ref_client_present
    def test_2_notre_serveur_client_ref_fichier_inexistant(self, tmp_path):
        """client_ref demande un fichier inexistant — notre serveur répond proprement."""
        serve_dir  = tmp_path / "serve"
        client_dir = tmp_path / "client"
        serve_dir.mkdir(); client_dir.mkdir()

        port = free_port()
        server_cmd = ['python3', SERVER_PY, HOST, str(port)]
        ready = threading.Event()
        proc_server = run_server(server_cmd, ready, cwd=serve_dir)
        ready.wait(timeout=2)

        proxy = UDPCapturingProxy(port)
        proxy.start()
        try:
            result = subprocess.run(
                [CLIENT_REF,
                f'http://[{HOST}]:{proxy.proxy_port}/nexiste_pas.bin',
                '--save', str(client_dir / "vide.bin")],
                timeout=TIMEOUT_TRANSFER,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=str(client_dir)
            )
            rc = result.returncode
        except subprocess.TimeoutExpired:
            rc = -1
        finally:
            proxy.wait_done(timeout=5)
            proxy.stop()
            proc_server.terminate()
            proc_server.wait(timeout=2)

        assert rc == 0, "client_ref ne doit pas crasher sur fichier inexistant"
        # Le proxy ne doit avoir capturé aucune donnée (fichier vide = paquet vide)
        received = proxy.get_received_data()
        assert len(received) == 0, \
            f"Le serveur a envoyé des données pour un fichier inexistant ({len(received)} octets)"

    # 3) Serveur de référence + notre client 

    @ref_server_present
    def test_3_server_ref_notre_client_petit(self, tmp_path):
        """Notre client.py télécharge un petit fichier depuis server_ref."""
        serve_dir = tmp_path / "serve"
        serve_dir.mkdir()
        src = make_test_file(serve_dir, "small.bin", 500)
        dst = tmp_path / "llm.model"
        port = free_port()

        server_cmd = [SERVER_REF, HOST, str(port)]
        client_cmd = ['python3', CLIENT_PY,
                        f'http://[{HOST}]:{port}/small.bin',
                        '--save', str(dst)]

        rc, _ = transfer_simple(server_cmd, client_cmd, server_cwd=serve_dir)
        assert rc == 0, "Notre client a échoué contre server_ref (petit)"
        assert dst.exists()
        assert md5(src) == md5(dst), "Intégrité compromise (3, petit .bin)"

    @ref_server_present
    def test_3_server_ref_notre_client_grand(self, tmp_path):
        """Notre client.py télécharge un grand fichier depuis server_ref."""
        serve_dir = tmp_path / "serve"
        serve_dir.mkdir()
        src = make_test_file(serve_dir, "large.bin", 50_000)
        dst = tmp_path / "llm.model"
        port = free_port()

        server_cmd = [SERVER_REF, HOST, str(port)]
        client_cmd = ['python3', CLIENT_PY,
                        f'http://[{HOST}]:{port}/large.bin',
                        '--save', str(dst)]

        rc, _ = transfer_simple(server_cmd, client_cmd, server_cwd=serve_dir)
        assert rc == 0, "Notre client a échoué sur grand fichier contre server_ref"
        assert md5(src) == md5(dst), "Intégrité compromise (3, grand .bin)"

    @ref_server_present
    def test_3_server_ref_notre_client_txt(self, tmp_path):
        """Notre client.py télécharge un fichier .txt depuis server_ref.
        Résultat sauvegardé dans test_outputs/ pour inspection."""
        serve_dir = tmp_path / "serve"
        serve_dir.mkdir()
        src  = make_text_file(serve_dir, "notes.txt", lines=300)
        dst  = OUTPUT_DIR / "interop_server_ref_notes_recu.txt"
        port = free_port()

        server_cmd = [SERVER_REF, HOST, str(port)]
        client_cmd = ['python3', CLIENT_PY,
                        f'http://[{HOST}]:{port}/notes.txt',
                        '--save', str(dst)]

        rc, _ = transfer_simple(server_cmd, client_cmd, server_cwd=serve_dir)
        assert rc == 0, "Notre client a échoué sur .txt contre server_ref"
        assert dst.exists()
        assert md5(src) == md5(dst), "Intégrité compromise (3, .txt)"
        contenu = dst.read_text(encoding='utf-8')
        assert "Ligne 0000" in contenu
        assert "Ligne 0299" in contenu

    @ref_server_present
    def test_3_server_ref_notre_client_fichier_inexistant(self, tmp_path):
        """Notre client demande un fichier inexistant à server_ref."""
        serve_dir = tmp_path / "serve"
        serve_dir.mkdir()
        dst = tmp_path / "llm.model"
        port = free_port()

        server_cmd = [SERVER_REF, HOST, str(port)]
        client_cmd = ['python3', CLIENT_PY,
                        f'http://[{HOST}]:{port}/nexiste_pas.bin',
                        '--save', str(dst)]

        rc, _ = transfer_simple(server_cmd, client_cmd, server_cwd=serve_dir)
        assert rc == 0, "Notre client ne doit pas crasher sur fichier inexistant"

    # 4) Notre serveur + plusieurs clients_ref simultanés 

    @ref_client_present
    def test_4_deux_clients_ref_simultanes(self, tmp_path):
        """
        Deux client_ref lancés simultanément contre notre server.py.
        Chacun passe par son propre UDPCapturingProxy.
        """
        serve_dir    = tmp_path / "serve"
        client_dir_a = tmp_path / "client_a"
        client_dir_b = tmp_path / "client_b"
        serve_dir.mkdir(); client_dir_a.mkdir(); client_dir_b.mkdir()

        port = free_port()
        server_cmd = ['python3', SERVER_PY, HOST, str(port)]
        ready = threading.Event()
        proc_server = run_server(server_cmd, ready, cwd=serve_dir)
        ready.wait(timeout=2)

        jobs = [
            {'serve_dir': serve_dir, 'filename': 'file_a.bin', 'filesize': 10_000, 'client_dir': client_dir_a},
            {'serve_dir': serve_dir, 'filename': 'file_b.bin', 'filesize': 8_000, 'client_dir': client_dir_b},
        ]

        try:
            results = self._run_multi_client_ref(port, jobs)
        finally:
            proc_server.terminate()
            proc_server.wait(timeout=2)

        for (src, received, rc), job in zip(results, jobs):
            name = job['filename']
            assert rc == 0, f"client_ref pour {name} s'est terminé avec le code {rc}"
            assert len(received) > 0, f"Proxy n'a rien capturé pour {name}"
            assert md5_bytes(received) == md5(src), \
                f"Intégrité compromise pour client_ref sur {name}"

    @ref_client_present
    def test_4_trois_clients_ref_simultanes(self, tmp_path):
        """
        Trois client_ref lancés simultanément contre notre server.py.
        Stress-test du serveur multi-clients.
        """
        serve_dir = tmp_path / "serve"
        serve_dir.mkdir()

        fichiers = [
            ("alpha.bin", 15_000),
            ("beta.bin",  12_000),
            ("gamma.bin",  9_000),
        ]
        client_dirs = []
        for name, _ in fichiers:
            d = tmp_path / f"client_{name}"
            d.mkdir()
            client_dirs.append(d)

        port = free_port()
        server_cmd = ['python3', SERVER_PY, HOST, str(port)]
        ready = threading.Event()
        proc_server = run_server(server_cmd, ready, cwd=serve_dir)
        ready.wait(timeout=2)

        jobs = [
            {'serve_dir': serve_dir, 'filename': name, 'filesize': size, 'client_dir': cdir}
            for (name, size), cdir in zip(fichiers, client_dirs)
        ]

        try:
            results = self._run_multi_client_ref(port, jobs)
        finally:
            proc_server.terminate()
            proc_server.wait(timeout=2)

        for (src, received, rc), job in zip(results, jobs):
            name = job['filename']
            assert rc == 0, f"client_ref pour {name} s'est terminé avec le code {rc}"
            assert len(received) > 0, f"Proxy n'a rien capturé pour {name}"
            assert md5_bytes(received) == md5(src), \
                f"Intégrité compromise pour client_ref sur {name}"

    @ref_client_present
    def test_4_clients_ref_fichiers_txt_simultanes(self, tmp_path):
        """
        Deux client_ref téléchargent des .txt simultanément depuis notre server.py.
        Résultats capturés par le proxy et sauvegardés dans test_outputs/.
        """
        serve_dir    = tmp_path / "serve"
        client_dir_a = tmp_path / "client_a"
        client_dir_b = tmp_path / "client_b"
        serve_dir.mkdir(); client_dir_a.mkdir(); client_dir_b.mkdir()

        port = free_port()
        server_cmd = ['python3', SERVER_PY, HOST, str(port)]
        ready = threading.Event()
        proc_server = run_server(server_cmd, ready, cwd=serve_dir)
        ready.wait(timeout=2)

        jobs = [
            {'serve_dir': serve_dir, 'filename': 'doc_a.txt', 'is_text': True, 'lines': 400, 'client_dir': client_dir_a},
            {'serve_dir': serve_dir, 'filename': 'doc_b.txt','is_text': True, 'lines': 300, 'client_dir': client_dir_b},
        ]

        try:
            results = self._run_multi_client_ref(port, jobs)
        finally:
            proc_server.terminate()
            proc_server.wait(timeout=2)

        output_names = ["interop_multi_doc_a_recu.txt", "interop_multi_doc_b_recu.txt"]
        for (src, received, rc), job, out_name in zip(results, jobs, output_names):
            name = job['filename']
            assert rc == 0, f"client_ref pour {name} s'est terminé avec le code {rc}"
            assert len(received) > 0, f"Proxy n'a rien capturé pour {name}"
            assert md5_bytes(received) == md5(src), \
                f"Intégrité compromise pour {name}"
            # Sauvegarder pour inspection manuelle
            (OUTPUT_DIR / out_name).write_bytes(received)