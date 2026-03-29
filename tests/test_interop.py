"""

7. TestInteroperabilite — notre implémentation contre les binaires de
    référence fournis sur Moodle et les client et server d'autres groupes (tools/server / tools/client).
    Ces tests sont skippés automatiquement si les binaires sont absents.
    Pour les activer : place server et client dans tools/ à la racine
    du projet, puis relance pytest.

    Stratégie pour client_ref :
    client_ref peut ne pas écrire de fichier local (--save ignoré ou
    comportement non garanti). On intercale donc un proxy UDP capturant
    (UDPCapturingProxy) entre client_ref et le serveur. Le proxy relaie
    tous les paquets UDP dans les deux sens et reconstruit le fichier
    reçu par le client en capturant les paquets DATA côté serveur->client.
    On compare ensuite le MD5 du fichier source avec le MD5 du contenu
    capturé — sans dépendre d'aucune écriture disque de client_ref.

    Quatre combinaisons :
    1) server_interop + client_interop vs sanity check des binaires
    2) notre server.py + client_interop vs notre serveur est conforme
    3) server_interop + notre client.py vs notre client est conforme
    4) notre server.py + N clients_interop vs serveur multi-clients conforme

Lancement :
    make test (tous les tests)
    make test-v (verbeux)
    make test-interoperabilite (uniquement ce fichier)
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
HOST = "::1"
SRC_DIR = os.path.join(os.path.dirname(__file__), '..', 'src')
TOOLS_DIR = os.path.join(os.path.dirname(__file__), '..', 'tools')
SERVER_PY = os.path.join(SRC_DIR, 'server.py')
CLIENT_PY = os.path.join(SRC_DIR, 'client.py')

# Binaires de référence dans tools/ à la racine du projet
SERVER_REF = os.path.join(TOOLS_DIR, 'server')
CLIENT_REF = os.path.join(TOOLS_DIR, 'client')

TIMEOUT_TRANSFER = 120   # secondes max pour un transfert complet

# Dossier de sortie persistant pour les fichiers .txt reçus
OUTPUT_DIR = pathlib.Path(os.path.join(os.path.dirname(__file__), '..', 'test_outputs'))
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

#  Markers de skip
ref_server_present = pytest.mark.skipif(not os.path.isfile(SERVER_REF), reason=f"Binaire server absent ({SERVER_REF}). Télécharge-le depuis Moodle et place-le dans tools/.")
ref_client_present = pytest.mark.skipif(not os.path.isfile(CLIENT_REF), reason=f"Binaire client absent ({CLIENT_REF}). Télécharge-le depuis Moodle et place-le dans tools/.")

#  7. TESTS D'INTEROPÉRABILITÉ avec les binaires de référence

class TestInteroperabilite:

    #  Helper : lancer server_ref + proxy + client_ref

    def trasfer_via_proxy(self, serve_dir, filename, server_cmd,server_cwd, client_dir, server_port, filesize=None, is_text=False, lines=None):
        """
        Lance server_cmd, puis client_ref via UDPCapturingProxy.

        Retourne (src_path, received_bytes, rc_client).
        """
        ready = threading.Event()
        proc_server = run_server(server_cmd, ready, cwd=server_cwd)
        ready.wait(timeout=10)

        try:
            src, received, rc = run_client_ref_via_proxy(
                serve_dir=serve_dir,
                filename=filename,
                server_port=server_port,
                client_cwd=client_dir,
                is_text=is_text,
                lines=lines,
                filesize=filesize
            )
        finally:
            proc_server.terminate()
            proc_server.wait(timeout=10)

        return src, received, rc

    #  Helper : plusieurs client_ref simultanés via proxies

    def run_multi_client_ref(self, server_port, jobs, timeout=TIMEOUT_TRANSFER):
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
            proxy.wait_done(timeout=10)
            proxy.stop()
            received = proxy.get_received_data()
            results.append((src, received, rc))

        return results

    # 1) Sanity check : server_ref + client_ref 

    @ref_server_present
    @ref_client_present
    @pytest.mark.dependency(name="ref_sanity")
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
    @pytest.mark.dependency(name="ref_notre_server_client_ref_petit", depends=["ref_sanity"])
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
            server_port=port, filesize=500
        )

        assert rc == 0, f"client_ref s'est terminé avec le code {rc}"
        assert md5_bytes(received) == md5(src), \
            "Intégrité compromise (2, petit .bin)"

    @ref_client_present
    @pytest.mark.dependency(name="ref_notre_server_client_ref_grand", depends=["ref_notre_server_client_ref_petit"])
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
            server_port=port, filesize=50_000
        )

        assert rc == 0, f"client_ref s'est terminé avec le code {rc}"
        assert md5_bytes(received) == md5(src), \
            "Intégrité compromise (2, grand .bin)"

    @ref_client_present
    @pytest.mark.dependency(name="ref_notre_server_client_ref_txt", depends=["ref_sanity"])
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
    @pytest.mark.dependency(depends=["ref_sanity"])
    def test_2_notre_serveur_client_ref_fichier_inexistant(self, tmp_path):
        """client_ref demande un fichier inexistant — notre serveur répond proprement."""
        serve_dir  = tmp_path / "serve"
        client_dir = tmp_path / "client"
        serve_dir.mkdir(); client_dir.mkdir()

        port = free_port()
        server_cmd = ['python3', SERVER_PY, HOST, str(port)]
        ready = threading.Event()
        proc_server = run_server(server_cmd, ready, cwd=serve_dir)
        ready.wait(timeout=10)

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
            proxy.wait_done(timeout=10)
            proxy.stop()
            proc_server.terminate()
            proc_server.wait(timeout=10)

        assert rc == 0, "client_ref ne doit pas crasher sur fichier inexistant"
        # Le proxy ne doit avoir capturé aucune donnée (fichier vide = paquet vide)
        received = proxy.get_received_data()
        assert len(received) == 0, \
            f"Le serveur a envoyé des données pour un fichier inexistant ({len(received)} octets)"

    # 3) Serveur de référence + notre client 

    @ref_server_present
    @pytest.mark.dependency(name="ref_server_ref_notre_client_petit", depends=["ref_sanity"])
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
    @pytest.mark.dependency(name="ref_server_ref_notre_client_grand", depends=["ref_server_ref_notre_client_petit"])
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
    @pytest.mark.dependency(name="ref_server_ref_notre_client_txt", depends=["ref_sanity"])
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
    @pytest.mark.dependency(depends=["ref_sanity"])
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
    @pytest.mark.dependency(name="ref_multi_2", depends=["ref_notre_server_client_ref_petit", "ref_notre_server_client_ref_grand"])
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
        ready.wait(timeout=10)

        jobs = [
            {'serve_dir': serve_dir, 'filename': 'file_a.bin', 'filesize': 10_000, 'client_dir': client_dir_a},
            {'serve_dir': serve_dir, 'filename': 'file_b.bin', 'filesize': 8_000, 'client_dir': client_dir_b}
        ]

        try:
            results = self.run_multi_client_ref(port, jobs)
        finally:
            proc_server.terminate()
            proc_server.wait(timeout=10)

        for (src, received, rc), job in zip(results, jobs):
            name = job['filename']
            assert rc == 0, f"client_ref pour {name} s'est terminé avec le code {rc}"
            assert len(received) > 0, f"Proxy n'a rien capturé pour {name}"
            assert md5_bytes(received) == md5(src), \
                f"Intégrité compromise pour client_ref sur {name}"

    @ref_client_present
    @pytest.mark.dependency(depends=["ref_multi_2"])
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
        ready.wait(timeout=10)

        jobs = [
            {'serve_dir': serve_dir, 'filename': name, 'filesize': size, 'client_dir': cdir}
            for (name, size), cdir in zip(fichiers, client_dirs)
        ]

        try:
            results = self.run_multi_client_ref(port, jobs)
        finally:
            proc_server.terminate()
            proc_server.wait(timeout=10)

        for (src, received, rc), job in zip(results, jobs):
            name = job['filename']
            assert rc == 0, f"client_ref pour {name} s'est terminé avec le code {rc}"
            assert len(received) > 0, f"Proxy n'a rien capturé pour {name}"
            assert md5_bytes(received) == md5(src), \
                f"Intégrité compromise pour client_ref sur {name}"

    @ref_client_present
    @pytest.mark.dependency(depends=["ref_notre_server_client_ref_txt"])
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
        ready.wait(timeout=10)

        jobs = [
            {'serve_dir': serve_dir, 'filename': 'doc_a.txt', 'is_text': True, 'lines': 400, 'client_dir': client_dir_a},
            {'serve_dir': serve_dir, 'filename': 'doc_b.txt','is_text': True, 'lines': 300, 'client_dir': client_dir_b}
        ]

        try:
            results = self.run_multi_client_ref(port, jobs)
        finally:
            proc_server.terminate()
            proc_server.wait(timeout=10)

        output_names = ["interop_multi_doc_a_recu.txt", "interop_multi_doc_b_recu.txt"]
        for (src, received, rc), job, out_name in zip(results, jobs, output_names):
            name = job['filename']
            assert rc == 0, f"client_ref pour {name} s'est terminé avec le code {rc}"
            assert len(received) > 0, f"Proxy n'a rien capturé pour {name}"
            assert md5_bytes(received) == md5(src), \
                f"Intégrité compromise pour {name}"
            # Sauvegarder pour inspection manuelle
            (OUTPUT_DIR / out_name).write_bytes(received)


#  8. INTEROPÉRABILITÉ AVEC LES AUTRES GROUPES
#
#  Structure commune à chaque classe TestInteropGroupeXX :
#
#  Markers de skip : skippé automatiquement si server.py / client.py du groupe
#  sont absents dans tools/groupe_XX/.
#
#  Combinaisons testées par groupe :
#    0) server du groupe  + client du groupe — sanity check
#    1) notre server.py  + client du groupe — conditions parfaites
#    2) notre server.py  + client du groupe — réseau imparfait
#    3) server du groupe + notre client.py — conditions parfaites
#    4) server du groupe + notre client.py — réseau imparfait
#    5) notre server.py  + N clients du groupe simultanés — conditions parfaites
#    6) notre server.py  + N clients du groupe simultanés — réseau imparfait
#
#  Convention de nommage des helpers internes :
#    transfer_our_server_their_client(...)  -> proxy normal
#    transfer_their_server_our_client(...)  -> transfer_simple
#
#  Les tests imparfaits utilisent UDPCapturingProxy avec :
#    DROP_PROB = 0.10, CORRUPT_PROB = 0.10, DUPLICATE_PROB = 0.10

# Paramètres réseau imparfait communs à tous les groupes
IMPERFECT = dict(drop=0.10, corrupt=0.10, duplicate=0.10)


def make_imperfect_proxy(server_port, **kw):
    """Crée et configure un UDPCapturingProxy en mode réseau imparfait."""
    proxy = UDPCapturingProxy(server_port, imperfect_network=True)
    proxy.DROP_PROB = kw.get('drop',      IMPERFECT['drop'])
    proxy.CORRUPT_PROB = kw.get('corrupt',   IMPERFECT['corrupt'])
    proxy.DUPLICATE_PROB = kw.get('duplicate', IMPERFECT['duplicate'])
    return proxy


# Groupe 20 

G20_DIR = os.path.join(TOOLS_DIR, 'groupe_20')
G20_SERVER_PY = os.path.join(G20_DIR, 'server.py')
G20_CLIENT_PY = os.path.join(G20_DIR, 'client.py')

g20_server_present = pytest.mark.skipif(not os.path.isfile(G20_SERVER_PY), reason=f"server.py du groupe 20 absent ({G20_SERVER_PY}).")
g20_client_present = pytest.mark.skipif(not os.path.isfile(G20_CLIENT_PY), reason=f"client.py du groupe 20 absent ({G20_CLIENT_PY}).")


class TestInteropGroupe20:
    """
    Interopérabilité avec le groupe 20.
    """

    # Helpers privés

    def our_server_their_client(self, serve_dir, client_dir, filename, server_port, imperfect=False, **file_kw):
        """
        Lance notre server.py, puis client_g20.
        En mode parfait : connexion directe, le client écrit lui-même le fichier.
        En mode imparfait : proxy capturant, données écrites dans save_path.
        Retourne (src_path, dst_path, returncode).
        """
        ready = threading.Event()
        proc_server = run_server(
            ['python3', SERVER_PY, HOST, str(server_port)],
            ready, cwd=serve_dir
        )
        ready.wait(timeout=10)

        # Créer le fichier source
        if file_kw.get('is_text'):
            src = make_text_file(serve_dir, filename, lines=file_kw.get('lines', 200))
        else:
            src = make_test_file(serve_dir, filename, file_kw.get('filesize', 5_000))

        save_path = client_dir / "received_g20.bin"

        if imperfect:
            proxy = make_imperfect_proxy(server_port)
            proxy.start()
            target_port = proxy.proxy_port
        else:
            proxy = None
            target_port = server_port

        try:
            result = subprocess.run(
                ['python3', G20_CLIENT_PY,
                f'http://[{HOST}]:{target_port}/{filename}',
                '--save', str(save_path)],
                timeout=TIMEOUT_TRANSFER,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=str(client_dir)
            )
            rc = result.returncode
        except subprocess.TimeoutExpired:
            rc = -1
        finally:
            if proxy:
                proxy.wait_done(timeout=10)
                proxy.stop()
                save_path.write_bytes(proxy.get_received_data())
            proc_server.terminate()
            proc_server.wait(timeout=10)

        return src, save_path, rc

    def their_server_our_client(self, serve_dir, filename, dst, server_port, imperfect=False, **file_kw):
        """
        Lance server_g20, puis notre client.py (direct ou via proxy imparfait).
        Retourne (src_path, returncode).
        """
        if file_kw.get('is_text'):
            src = make_text_file(serve_dir, filename, lines=file_kw.get('lines', 200))
        else:
            src = make_test_file(serve_dir, filename, file_kw.get('filesize', 5_000))

        ready = threading.Event()
        proc_server = run_server(
            ['python3', G20_SERVER_PY, HOST, str(server_port)],
            ready, cwd=serve_dir
        )
        ready.wait(timeout=10)

        if imperfect:
            proxy = make_imperfect_proxy(server_port)
            proxy.start()
            target_port = proxy.proxy_port
        else:
            proxy = None
            target_port = server_port

        client_cmd = ['python3', CLIENT_PY,
                        f'http://[{HOST}]:{target_port}/{filename}',
                        '--save', str(dst)]
        try:
            result = subprocess.run(
                client_cmd,
                timeout=TIMEOUT_TRANSFER,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            rc = result.returncode
        except subprocess.TimeoutExpired:
            rc = -1
        finally:
            if proxy:
                proxy.wait_done(timeout=10)
                proxy.stop()
            proc_server.terminate()
            proc_server.wait(timeout=10)

        return src, rc

    # 0) Sanity check : server_g20 + client_g20 

    @g20_server_present
    @g20_client_present
    @pytest.mark.dependency(name="g20_sanity")
    def test_0_sanity_g20_vs_g20(self, tmp_path):
        """
        Vérifie que le server et le client du groupe 20 fonctionnent ensemble.
        Si ce test échoue, le problème vient de leur implémentation.
        """
        serve_dir  = tmp_path / "serve";  serve_dir.mkdir()
        client_dir = tmp_path / "client"; client_dir.mkdir()

        port = free_port()
        src = make_test_file(serve_dir, "test.bin", 5_000)
        ready = threading.Event()
        proc_server = run_server(
            ['python3', G20_SERVER_PY, HOST, str(port)], ready, cwd=serve_dir
        )
        ready.wait(timeout=10)

        save_path = client_dir / "received_sanity_g20.bin"
        try:
            result = subprocess.run(
                ['python3', G20_CLIENT_PY,
                f'http://[{HOST}]:{port}/test.bin',
                '--save', str(save_path)],
                timeout=TIMEOUT_TRANSFER,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=str(client_dir)
            )
            rc = result.returncode
        except subprocess.TimeoutExpired:
            rc = -1
        finally:
            proc_server.terminate()
            proc_server.wait(timeout=10)

        assert rc >= 0, f"client_g20 s'est terminé avec le code {rc}"
        assert save_path.exists(), "client_g20 n'a pas écrit le fichier (sanity G20 vs G20)"
        assert md5(src) == md5(save_path), f"Intégrité compromise (sanity G20 vs G20) : reçu {save_path.stat().st_size} o, attendu {src.stat().st_size} o"

    # 1) Notre server + client_g20 — parfait 

    @g20_client_present
    @pytest.mark.dependency(name="g20_notre_server_client_petit", depends=["g20_sanity"])
    def test_1_notre_server_client_g20_petit_parfait(self, tmp_path):
        """client_g20 télécharge un petit fichier depuis notre server.py (parfait)."""
        serve_dir  = tmp_path / "serve";  serve_dir.mkdir()
        client_dir = tmp_path / "client"; client_dir.mkdir()

        src, dst, rc = self.our_server_their_client(serve_dir, client_dir, "small.bin", free_port(), filesize=500)
        assert rc == 0, f"client_g20 a échoué (petit, parfait) — code {rc}"
        assert dst.exists(), "client_g20 n'a pas écrit le fichier (G20, 1, petit, parfait)"
        assert md5(src) == md5(dst), "Intégrité compromise (G20, 1, petit, parfait)"

    @g20_client_present
    @pytest.mark.dependency(name="g20_notre_server_client_grand", depends=["g20_notre_server_client_petit"])
    def test_1_notre_server_client_g20_grand_parfait(self, tmp_path):
        """client_g20 télécharge un grand fichier depuis notre server.py (parfait)."""
        serve_dir  = tmp_path / "serve";  serve_dir.mkdir()
        client_dir = tmp_path / "client"; client_dir.mkdir()

        src, dst, rc = self.our_server_their_client(serve_dir, client_dir, "large.bin", free_port(), filesize=50_000)
        assert rc == 0, f"client_g20 a échoué (grand, parfait) — code {rc}"
        assert dst.exists(), "client_g20 n'a pas écrit le fichier (G20, 1, grand, parfait)"
        assert md5(src) == md5(dst), "Intégrité compromise (G20, 1, grand, parfait)"

    @g20_client_present
    @pytest.mark.dependency(name="g20_notre_server_client_txt", depends=["g20_sanity"])
    def test_1_notre_server_client_g20_txt_parfait(self, tmp_path):
        """client_g20 télécharge un fichier .txt depuis notre server.py (parfait)."""
        serve_dir  = tmp_path / "serve";  serve_dir.mkdir()
        client_dir = tmp_path / "client"; client_dir.mkdir()

        src, dst, rc = self.our_server_their_client(serve_dir, client_dir, "notes.txt", free_port(), is_text=True, lines=300)
        assert rc == 0, f"client_g20 a échoué (.txt, parfait) — code {rc}"
        assert dst.exists(), "client_g20 n'a pas écrit le fichier (G20, 1, .txt, parfait)"
        assert md5(src) == md5(dst), "Intégrité compromise (G20, 1, .txt, parfait)"
        import shutil; shutil.copy(dst, OUTPUT_DIR / "g20_notre_server_notes_recu.txt")

    # 2) Notre server + client_g20 — imparfait

    @g20_client_present
    @pytest.mark.dependency(depends=["g20_notre_server_client_petit"])
    def test_2_notre_server_client_g20_petit_imparfait(self, tmp_path):
        """client_g20 télécharge un petit fichier depuis notre server.py (réseau imparfait)."""
        serve_dir  = tmp_path / "serve";  serve_dir.mkdir()
        client_dir = tmp_path / "client"; client_dir.mkdir()

        src, dst, rc = self.our_server_their_client(serve_dir, client_dir, "small.bin", free_port(), imperfect=True, filesize=500)
        assert rc == 0, f"client_g20 a échoué (petit, imparfait) — code {rc}"
        assert dst.exists() and dst.stat().st_size > 0, "Proxy n'a rien capturé (G20, 2, petit, imparfait)"
        assert md5(src) == md5(dst), "Intégrité compromise (G20, 2, petit, imparfait)"

    @g20_client_present
    @pytest.mark.dependency(depends=["g20_notre_server_client_grand"])
    def test_2_notre_server_client_g20_grand_imparfait(self, tmp_path):
        """client_g20 télécharge un grand fichier depuis notre server.py (réseau imparfait)."""
        serve_dir  = tmp_path / "serve";  serve_dir.mkdir()
        client_dir = tmp_path / "client"; client_dir.mkdir()

        src, dst, rc = self.our_server_their_client(serve_dir, client_dir, "large.bin", free_port(), imperfect=True, filesize=50_000)
        assert rc == 0, f"client_g20 a échoué (grand, imparfait) — code {rc}"
        assert md5(src) == md5(dst), "Intégrité compromise (G20, 2, grand, imparfait)"

    @g20_client_present
    @pytest.mark.dependency(depends=["g20_notre_server_client_txt"])
    def test_2_notre_server_client_g20_txt_imparfait(self, tmp_path):
        """client_g20 télécharge un .txt depuis notre server.py (réseau imparfait)."""
        serve_dir  = tmp_path / "serve";  serve_dir.mkdir()
        client_dir = tmp_path / "client"; client_dir.mkdir()

        src, dst, rc = self.our_server_their_client(serve_dir, client_dir, "notes.txt", free_port(), imperfect=True, is_text=True, lines=300)
        assert rc == 0, f"client_g20 a échoué (.txt, imparfait) — code {rc}"
        assert md5(src) == md5(dst), "Intégrité compromise (G20, 2, .txt, imparfait)"
        import shutil; shutil.copy(dst, OUTPUT_DIR / "g20_notre_server_notes_imparfait_recu.txt")

    # 3) server_g20 + notre client — parfait 

    @g20_server_present
    @pytest.mark.dependency(name="g20_server_notre_client_petit", depends=["g20_sanity"])
    def test_3_server_g20_notre_client_petit_parfait(self, tmp_path):
        """Notre client.py télécharge un petit fichier depuis server_g20 (parfait)."""
        serve_dir = tmp_path / "serve"; serve_dir.mkdir()
        dst = tmp_path / "received.bin"
        src, rc = self.their_server_our_client(serve_dir, "small.bin", dst, free_port(), filesize=500)
        assert rc == 0, f"Notre client a échoué contre server_g20 (petit, parfait) — code {rc}"
        assert dst.exists()
        assert md5(src) == md5(dst), "Intégrité compromise (G20, 3, petit, parfait)"

    @g20_server_present
    @pytest.mark.dependency(name="g20_server_notre_client_grand", depends=["g20_server_notre_client_petit"])
    def test_3_server_g20_notre_client_grand_parfait(self, tmp_path):
        """Notre client.py télécharge un grand fichier depuis server_g20 (parfait)."""
        serve_dir = tmp_path / "serve"; serve_dir.mkdir()
        dst = tmp_path / "received.bin"
        src, rc = self.their_server_our_client(serve_dir, "large.bin", dst, free_port(), filesize=50_000)
        assert rc == 0, f"Notre client a échoué contre server_g20 (grand, parfait) — code {rc}"
        assert md5(src) == md5(dst), "Intégrité compromise (G20, 3, grand, parfait)"

    @g20_server_present
    @pytest.mark.dependency(name="g20_server_notre_client_txt", depends=["g20_sanity"])
    def test_3_server_g20_notre_client_txt_parfait(self, tmp_path):
        """Notre client.py télécharge un .txt depuis server_g20 (parfait)."""
        serve_dir = tmp_path / "serve"; serve_dir.mkdir()
        dst = OUTPUT_DIR / "g20_server_g20_notes_recu.txt"
        src, rc = self.their_server_our_client(serve_dir, "notes.txt", dst, free_port(), is_text=True, lines=300)
        assert rc == 0, f"Notre client a échoué contre server_g20 (.txt, parfait) — code {rc}"
        assert dst.exists()
        assert md5(src) == md5(dst), "Intégrité compromise (G20, 3, .txt, parfait)"

    # 4) server_g20 + notre client — imparfait

    @g20_server_present
    @pytest.mark.dependency(depends=["g20_server_notre_client_petit"])
    def test_4_server_g20_notre_client_petit_imparfait(self, tmp_path):
        """Notre client.py télécharge un petit fichier depuis server_g20 (imparfait)."""
        serve_dir = tmp_path / "serve"; serve_dir.mkdir()
        dst = tmp_path / "received.bin"
        src, rc = self.their_server_our_client(serve_dir, "small.bin", dst, free_port(), imperfect=True, filesize=500)
        assert rc == 0, f"Notre client a échoué contre server_g20 (petit, imparfait) — code {rc}"
        assert dst.exists()
        assert md5(src) == md5(dst), "Intégrité compromise (G20, 4, petit, imparfait)"

    @g20_server_present
    @pytest.mark.dependency(depends=["g20_server_notre_client_grand"])
    def test_4_server_g20_notre_client_grand_imparfait(self, tmp_path):
        """Notre client.py télécharge un grand fichier depuis server_g20 (imparfait)."""
        serve_dir = tmp_path / "serve"; serve_dir.mkdir()
        dst = tmp_path / "received.bin"
        src, rc = self.their_server_our_client(serve_dir, "large.bin", dst, free_port(), imperfect=True, filesize=50_000)
        assert rc == 0, f"Notre client a échoué contre server_g20 (grand, imparfait) — code {rc}"
        assert md5(src) == md5(dst), "Intégrité compromise (G20, 4, grand, imparfait)"

    @g20_server_present
    @pytest.mark.dependency(depends=["g20_server_notre_client_txt"])
    def test_4_server_g20_notre_client_txt_imparfait(self, tmp_path):
        """Notre client.py télécharge un .txt depuis server_g20 (imparfait)."""
        serve_dir = tmp_path / "serve"; serve_dir.mkdir()
        dst = OUTPUT_DIR / "g20_server_g20_notes_imparfait_recu.txt"
        src, rc = self.their_server_our_client(serve_dir, "notes.txt", dst, free_port(), imperfect=True, is_text=True, lines=300)
        assert rc == 0, f"Notre client a échoué contre server_g20 (.txt, imparfait) — code {rc}"
        assert dst.exists()
        assert md5(src) == md5(dst), "Intégrité compromise (G20, 4, .txt, imparfait)"

    # 5) Notre server + N clients_g20 simultanés — parfait

    @g20_client_present
    @pytest.mark.dependency(name="g20_multi_parfait", depends=["g20_notre_server_client_petit", "g20_notre_server_client_grand", "g20_notre_server_client_txt"])
    def test_5_multi_clients_g20_simultanes_parfait(self, tmp_path):
        """
        Deux clients_g20 lancés simultanément contre notre server.py
        en conditions réseau parfaites.
        """
        serve_dir    = tmp_path / "serve";    serve_dir.mkdir()
        client_dir_a = tmp_path / "client_a"; client_dir_a.mkdir()
        client_dir_b = tmp_path / "client_b"; client_dir_b.mkdir()

        port = free_port()
        ready = threading.Event()
        proc_server = run_server(
            ['python3', SERVER_PY, HOST, str(port)], ready, cwd=serve_dir
        )
        ready.wait(timeout=10)

        jobs = [
            {'serve_dir': serve_dir, 'filename': 'file_a.bin', 'filesize': 10_000, 'client_dir': client_dir_a},
            {'serve_dir': serve_dir, 'filename': 'file_b.bin', 'filesize':  8_000, 'client_dir': client_dir_b}
        ]

        procs, srcs = [], []
        try:
            for job in jobs:
                src = make_test_file(job['serve_dir'], job['filename'], job['filesize'])
                srcs.append(src)

                save_path = job['client_dir'] / "received_g20.bin"
                p = subprocess.Popen(
                    ['python3', G20_CLIENT_PY,
                    f'http://[{HOST}]:{port}/{job["filename"]}',
                    '--save', str(save_path)],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    cwd=str(job['client_dir'])
                )
                procs.append(p)

            rcs = []
            deadline = time.time() + TIMEOUT_TRANSFER
            for p in procs:
                remaining = max(0, deadline - time.time())
                try:
                    p.wait(timeout=remaining); rcs.append(p.returncode)
                except subprocess.TimeoutExpired:
                    p.kill(); rcs.append(-1)
        finally:
            proc_server.terminate()
            proc_server.wait(timeout=10)

        for src, rc, job in zip(srcs, rcs, jobs):
            name = job['filename']
            save_path = job['client_dir'] / "received_g20.bin"
            assert rc == 0, f"client_g20 pour {name} a échoué (multi, parfait) — code {rc}"
            assert save_path.exists(), f"client_g20 n'a pas écrit le fichier pour {name} (G20, 5, multi, parfait)"
            assert md5(src) == md5(save_path), f"Intégrité compromise pour {name} (G20, 5, multi, parfait)"

    # 6) Notre server + N clients_g20 simultanés — imparfait

    @g20_client_present
    @pytest.mark.dependency(depends=["g20_multi_parfait"])
    def test_6_multi_clients_g20_simultanes_imparfait(self, tmp_path):
        """
        Deux clients_g20 lancés simultanément contre notre server.py
        en conditions réseau imparfaites.
        """
        serve_dir    = tmp_path / "serve";    serve_dir.mkdir()
        client_dir_a = tmp_path / "client_a"; client_dir_a.mkdir()
        client_dir_b = tmp_path / "client_b"; client_dir_b.mkdir()

        port = free_port()
        ready = threading.Event()
        proc_server = run_server(
            ['python3', SERVER_PY, HOST, str(port)], ready, cwd=serve_dir
        )
        ready.wait(timeout=10)

        jobs = [
            {'serve_dir': serve_dir, 'filename': 'file_a.bin', 'filesize': 10_000, 'client_dir': client_dir_a},
            {'serve_dir': serve_dir, 'filename': 'file_b.bin', 'filesize':  8_000, 'client_dir': client_dir_b}
        ]

        proxies, procs, srcs = [], [], []
        try:
            for job in jobs:
                proxy = make_imperfect_proxy(port)
                proxy.start()
                proxies.append(proxy)

                src = make_test_file(job['serve_dir'], job['filename'], job['filesize'])
                srcs.append(src)

                save_path = job['client_dir'] / "received_g20.bin"
                p = subprocess.Popen(
                    ['python3', G20_CLIENT_PY,
                    f'http://[{HOST}]:{proxy.proxy_port}/{job["filename"]}',
                    '--save', str(save_path)],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    cwd=str(job['client_dir'])
                )
                procs.append(p)

            rcs = []
            deadline = time.time() + TIMEOUT_TRANSFER
            for p in procs:
                remaining = max(0, deadline - time.time())
                try:
                    p.wait(timeout=remaining); rcs.append(p.returncode)
                except subprocess.TimeoutExpired:
                    p.kill(); rcs.append(-1)
        finally:
            proc_server.terminate()
            proc_server.wait(timeout=10)

        for proxy, src, rc, job in zip(proxies, srcs, rcs, jobs):
            proxy.wait_done(timeout=10)
            proxy.stop()
            received = proxy.get_received_data()
            name = job['filename']
            assert rc == 0, f"client_g20 pour {name} a échoué (multi, imparfait) — code {rc}"
            assert len(received) > 0, f"Proxy n'a rien capturé pour {name} (G20, multi)"
            assert md5_bytes(received) == md5(src), f"Intégrité compromise pour {name} (G20, 6, multi, imparfait)"


# Groupe 42 

G42_DIR = os.path.join(TOOLS_DIR, 'groupe_42')
G42_SERVER_PY = os.path.join(G42_DIR, 'server.py')
G42_CLIENT_PY = os.path.join(G42_DIR, 'client.py')

g42_server_present = pytest.mark.skipif(not os.path.isfile(G42_SERVER_PY), reason=f"server.py du groupe 42 absent ({G42_SERVER_PY}).")
g42_client_present = pytest.mark.skipif(not os.path.isfile(G42_CLIENT_PY), reason=f"client.py du groupe 42 absent ({G42_CLIENT_PY}).")


class TestInteropGroupe42:
    """
    Interopérabilité avec le groupe 42.
    """

    # Helpers privés 

    def our_server_their_client(self, serve_dir, client_dir, filename, server_port, imperfect=False, **file_kw):
        """
        Lance notre server.py, puis client_g42.
        En mode parfait : connexion directe, le client écrit lui-même le fichier.
        En mode imparfait : proxy capturant, données écrites dans save_path.
        Retourne (src_path, dst_path, returncode).
        """
        ready = threading.Event()
        proc_server = run_server(
            ['python3', SERVER_PY, HOST, str(server_port)],
            ready, cwd=serve_dir
        )
        ready.wait(timeout=10)

        if file_kw.get('is_text'):
            src = make_text_file(serve_dir, filename, lines=file_kw.get('lines', 200))
        else:
            src = make_test_file(serve_dir, filename, file_kw.get('filesize', 5_000))

        save_path = client_dir / "received_g42.bin"

        if imperfect:
            proxy = make_imperfect_proxy(server_port)
            proxy.start()
            target_port = proxy.proxy_port
        else:
            proxy = None
            target_port = server_port

        try:
            result = subprocess.run(
                ['python3', G42_CLIENT_PY,
                f'http://[{HOST}]:{target_port}/{filename}',
                '--save', str(save_path)],
                timeout=TIMEOUT_TRANSFER,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=str(client_dir)
            )
            rc = result.returncode
        except subprocess.TimeoutExpired:
            rc = -1
        finally:
            if proxy:
                proxy.wait_done(timeout=10)
                proxy.stop()
                save_path.write_bytes(proxy.get_received_data())
            proc_server.terminate()
            proc_server.wait(timeout=10)

        return src, save_path, rc

    def their_server_our_client(self, serve_dir, filename, dst, server_port, imperfect=False, **file_kw):
        if file_kw.get('is_text'):
            src = make_text_file(serve_dir, filename, lines=file_kw.get('lines', 200))
        else:
            src = make_test_file(serve_dir, filename, file_kw.get('filesize', 5_000))

        ready = threading.Event()
        proc_server = run_server(
            ['python3', G42_SERVER_PY, HOST, str(server_port)],
            ready, cwd=serve_dir
        )
        ready.wait(timeout=10)

        if imperfect:
            proxy = make_imperfect_proxy(server_port)
            proxy.start()
            target_port = proxy.proxy_port
        else:
            proxy = None
            target_port = server_port

        try:
            result = subprocess.run(
                ['python3', CLIENT_PY,
                f'http://[{HOST}]:{target_port}/{filename}',
                '--save', str(dst)],
                timeout=TIMEOUT_TRANSFER,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            rc = result.returncode
        except subprocess.TimeoutExpired:
            rc = -1
        finally:
            if proxy:
                proxy.wait_done(timeout=10)
                proxy.stop()
            proc_server.terminate()
            proc_server.wait(timeout=10)

        return src, rc

    # 0) Sanity check : server_g42 + client_g42 

    @g42_server_present
    @g42_client_present
    @pytest.mark.dependency(name="g42_sanity")
    def test_0_sanity_g42_vs_g42(self, tmp_path):
        """
        Vérifie que le server et le client du groupe 42 fonctionnent ensemble.
        Si ce test échoue, le problème vient de leur implémentation.
        """
        serve_dir  = tmp_path / "serve";  serve_dir.mkdir()
        client_dir = tmp_path / "client"; client_dir.mkdir()

        port = free_port()
        src = make_test_file(serve_dir, "test.bin", 5_000)
        ready = threading.Event()
        proc_server = run_server(
            ['python3', G42_SERVER_PY, HOST, str(port)], ready, cwd=serve_dir
        )
        ready.wait(timeout=10)

        save_path = client_dir / "received_sanity_g42.bin"
        try:
            result = subprocess.run(
                ['python3', G42_CLIENT_PY,
                f'http://[{HOST}]:{port}/test.bin',
                '--save', str(save_path)],
                timeout=TIMEOUT_TRANSFER,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=str(client_dir)
            )
            rc = result.returncode
        except subprocess.TimeoutExpired:
            rc = -1
        finally:
            proc_server.terminate()
            proc_server.wait(timeout=10)

        assert rc >= 0, f"client_g42 s'est terminé avec le code {rc}"
        assert save_path.exists(), "client_g42 n'a pas écrit le fichier (sanity G42 vs G42)"
        assert md5(src) == md5(save_path), f"Intégrité compromise (sanity G42 vs G42) : reçu {save_path.stat().st_size} o, attendu {src.stat().st_size} o"

    # 1) Notre server + client_g42 — parfait 

    @g42_client_present
    @pytest.mark.dependency(name="g42_notre_server_client_petit", depends=["g42_sanity"])
    def test_1_notre_server_client_g42_petit_parfait(self, tmp_path):
        """client_g42 télécharge un petit fichier depuis notre server.py (parfait)."""
        serve_dir  = tmp_path / "serve";  serve_dir.mkdir()
        client_dir = tmp_path / "client"; client_dir.mkdir()
        src, dst, rc = self.our_server_their_client(serve_dir, client_dir, "small.bin", free_port(), filesize=500)
        assert rc == 0, f"client_g42 a échoué (petit, parfait) — code {rc}"
        assert dst.exists(), "client_g42 n'a pas écrit le fichier (G42, 1, petit, parfait)"
        assert md5(src) == md5(dst), "Intégrité compromise (G42, 1, petit, parfait)"

    @g42_client_present
    @pytest.mark.dependency(name="g42_notre_server_client_grand", depends=["g42_notre_server_client_petit"])
    def test_1_notre_server_client_g42_grand_parfait(self, tmp_path):
        """client_g42 télécharge un grand fichier depuis notre server.py (parfait)."""
        serve_dir  = tmp_path / "serve";  serve_dir.mkdir()
        client_dir = tmp_path / "client"; client_dir.mkdir()
        src, dst, rc = self.our_server_their_client(serve_dir, client_dir, "large.bin", free_port(), filesize=50_000)
        assert rc == 0, f"client_g42 a échoué (grand, parfait) — code {rc}"
        assert dst.exists(), "client_g42 n'a pas écrit le fichier (G42, 1, grand, parfait)"
        assert md5(src) == md5(dst), "Intégrité compromise (G42, 1, grand, parfait)"

    @g42_client_present
    @pytest.mark.dependency(name="g42_notre_server_client_txt", depends=["g42_sanity"])
    def test_1_notre_server_client_g42_txt_parfait(self, tmp_path):
        """client_g42 télécharge un .txt depuis notre server.py (parfait)."""
        serve_dir  = tmp_path / "serve";  serve_dir.mkdir()
        client_dir = tmp_path / "client"; client_dir.mkdir()
        src, dst, rc = self.our_server_their_client(serve_dir, client_dir, "notes.txt", free_port(), is_text=True, lines=300)
        assert rc == 0, f"client_g42 a échoué (.txt, parfait) — code {rc}"
        assert dst.exists(), "client_g42 n'a pas écrit le fichier (G42, 1, .txt, parfait)"
        assert md5(src) == md5(dst), "Intégrité compromise (G42, 1, .txt, parfait)"
        import shutil; shutil.copy(dst, OUTPUT_DIR / "g42_notre_server_notes_recu.txt")

    # 2) Notre server + client_g42 — imparfait 

    @g42_client_present
    @pytest.mark.dependency(depends=["g42_notre_server_client_petit"])
    def test_2_notre_server_client_g42_petit_imparfait(self, tmp_path):
        """client_g42 télécharge un petit fichier depuis notre server.py (imparfait)."""
        serve_dir  = tmp_path / "serve";  serve_dir.mkdir()
        client_dir = tmp_path / "client"; client_dir.mkdir()
        src, dst, rc = self.our_server_their_client(serve_dir, client_dir, "small.bin", free_port(), imperfect=True, filesize=500)
        assert rc == 0, f"client_g42 a échoué (petit, imparfait) — code {rc}"
        assert dst.exists() and dst.stat().st_size > 0, "Proxy n'a rien capturé (G42, 2, petit, imparfait)"
        assert md5(src) == md5(dst), "Intégrité compromise (G42, 2, petit, imparfait)"

    @g42_client_present
    @pytest.mark.dependency(depends=["g42_notre_server_client_grand"])
    def test_2_notre_server_client_g42_grand_imparfait(self, tmp_path):
        """client_g42 télécharge un grand fichier depuis notre server.py (imparfait)."""
        serve_dir  = tmp_path / "serve";  serve_dir.mkdir()
        client_dir = tmp_path / "client"; client_dir.mkdir()
        src, dst, rc = self.our_server_their_client(serve_dir, client_dir, "large.bin", free_port(), imperfect=True, filesize=50_000)
        assert rc == 0, f"client_g42 a échoué (grand, imparfait) — code {rc}"
        assert md5(src) == md5(dst), "Intégrité compromise (G42, 2, grand, imparfait)"

    @g42_client_present
    @pytest.mark.dependency(depends=["g42_notre_server_client_txt"])
    def test_2_notre_server_client_g42_txt_imparfait(self, tmp_path):
        """client_g42 télécharge un .txt depuis notre server.py (imparfait)."""
        serve_dir  = tmp_path / "serve";  serve_dir.mkdir()
        client_dir = tmp_path / "client"; client_dir.mkdir()
        src, dst, rc = self.our_server_their_client(serve_dir, client_dir, "notes.txt", free_port(), imperfect=True, is_text=True, lines=300)
        assert rc == 0, f"client_g42 a échoué (.txt, imparfait) — code {rc}"
        assert md5(src) == md5(dst), "Intégrité compromise (G42, 2, .txt, imparfait)"
        import shutil; shutil.copy(dst, OUTPUT_DIR / "g42_notre_server_notes_imparfait_recu.txt")

    # 3) server_g42 + notre client — parfait 

    @g42_server_present
    @pytest.mark.dependency(name="g42_server_notre_client_petit", depends=["g42_sanity"])
    def test_3_server_g42_notre_client_petit_parfait(self, tmp_path):
        """Notre client.py télécharge un petit fichier depuis server_g42 (parfait)."""
        serve_dir = tmp_path / "serve"; serve_dir.mkdir()
        dst = tmp_path / "received.bin"
        src, rc = self.their_server_our_client(serve_dir, "small.bin", dst, free_port(), filesize=500)
        assert rc == 0, f"Notre client a échoué contre server_g42 (petit, parfait) — code {rc}"
        assert dst.exists()
        assert md5(src) == md5(dst), "Intégrité compromise (G42, 3, petit, parfait)"

    @g42_server_present
    @pytest.mark.dependency(name="g42_server_notre_client_grand", depends=["g42_server_notre_client_petit"])
    def test_3_server_g42_notre_client_grand_parfait(self, tmp_path):
        """Notre client.py télécharge un grand fichier depuis server_g42 (parfait)."""
        serve_dir = tmp_path / "serve"; serve_dir.mkdir()
        dst = tmp_path / "received.bin"
        src, rc = self.their_server_our_client(serve_dir, "large.bin", dst, free_port(), filesize=50_000)
        assert rc == 0, f"Notre client a échoué contre server_g42 (grand, parfait) — code {rc}"
        assert md5(src) == md5(dst), "Intégrité compromise (G42, 3, grand, parfait)"

    @g42_server_present
    @pytest.mark.dependency(name="g42_server_notre_client_txt", depends=["g42_sanity"])
    def test_3_server_g42_notre_client_txt_parfait(self, tmp_path):
        """Notre client.py télécharge un .txt depuis server_g42 (parfait)."""
        serve_dir = tmp_path / "serve"; serve_dir.mkdir()
        dst = OUTPUT_DIR / "g42_server_g42_notes_recu.txt"
        src, rc = self.their_server_our_client(serve_dir, "notes.txt", dst, free_port(), is_text=True, lines=300)
        assert rc == 0, f"Notre client a échoué contre server_g42 (.txt, parfait) — code {rc}"
        assert dst.exists()
        assert md5(src) == md5(dst), "Intégrité compromise (G42, 3, .txt, parfait)"

    # 4) server_g42 + notre client — imparfait 

    @g42_server_present
    @pytest.mark.dependency(depends=["g42_server_notre_client_petit"])
    def test_4_server_g42_notre_client_petit_imparfait(self, tmp_path):
        """Notre client.py télécharge un petit fichier depuis server_g42 (imparfait)."""
        serve_dir = tmp_path / "serve"; serve_dir.mkdir()
        dst = tmp_path / "received.bin"
        src, rc = self.their_server_our_client(serve_dir, "small.bin", dst, free_port(), imperfect=True, filesize=500)
        assert rc == 0, f"Notre client a échoué contre server_g42 (petit, imparfait) — code {rc}"
        assert dst.exists()
        assert md5(src) == md5(dst), "Intégrité compromise (G42, 4, petit, imparfait)"

    @g42_server_present
    @pytest.mark.dependency(depends=["g42_server_notre_client_grand"])
    def test_4_server_g42_notre_client_grand_imparfait(self, tmp_path):
        """Notre client.py télécharge un grand fichier depuis server_g42 (imparfait)."""
        serve_dir = tmp_path / "serve"; serve_dir.mkdir()
        dst = tmp_path / "received.bin"
        src, rc = self.their_server_our_client(serve_dir, "large.bin", dst, free_port(), imperfect=True, filesize=50_000)
        assert rc == 0, f"Notre client a échoué contre server_g42 (grand, imparfait) — code {rc}"
        assert md5(src) == md5(dst), "Intégrité compromise (G42, 4, grand, imparfait)"

    @g42_server_present
    @pytest.mark.dependency(depends=["g42_server_notre_client_txt"])
    def test_4_server_g42_notre_client_txt_imparfait(self, tmp_path):
        """Notre client.py télécharge un .txt depuis server_g42 (imparfait)."""
        serve_dir = tmp_path / "serve"; serve_dir.mkdir()
        dst = OUTPUT_DIR / "g42_server_g42_notes_imparfait_recu.txt"
        src, rc = self.their_server_our_client(serve_dir, "notes.txt", dst, free_port(), imperfect=True, is_text=True, lines=300)
        assert rc == 0, f"Notre client a échoué contre server_g42 (.txt, imparfait) — code {rc}"
        assert dst.exists()
        assert md5(src) == md5(dst), "Intégrité compromise (G42, 4, .txt, imparfait)"

    # 5) Notre server + N clients_g42 simultanés — parfait

    @g42_client_present
    @pytest.mark.dependency(name="g42_multi_parfait", depends=["g42_notre_server_client_petit", "g42_notre_server_client_grand", "g42_notre_server_client_txt"])
    def test_5_multi_clients_g42_simultanes_parfait(self, tmp_path):
        """
        Deux clients_g42 lancés simultanément contre notre server.py
        en conditions réseau parfaites.
        """
        serve_dir    = tmp_path / "serve";    serve_dir.mkdir()
        client_dir_a = tmp_path / "client_a"; client_dir_a.mkdir()
        client_dir_b = tmp_path / "client_b"; client_dir_b.mkdir()

        port = free_port()
        ready = threading.Event()
        proc_server = run_server(
            ['python3', SERVER_PY, HOST, str(port)], ready, cwd=serve_dir
        )
        ready.wait(timeout=10)

        jobs = [
            {'serve_dir': serve_dir, 'filename': 'file_a.bin', 'filesize': 10_000, 'client_dir': client_dir_a},
            {'serve_dir': serve_dir, 'filename': 'file_b.bin', 'filesize':  8_000, 'client_dir': client_dir_b}
        ]

        procs, srcs = [], []
        try:
            for job in jobs:
                src = make_test_file(job['serve_dir'], job['filename'], job['filesize'])
                srcs.append(src)

                save_path = job['client_dir'] / "received_g42.bin"
                p = subprocess.Popen(
                    ['python3', G42_CLIENT_PY,
                    f'http://[{HOST}]:{port}/{job["filename"]}',
                    '--save', str(save_path)],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    cwd=str(job['client_dir'])
                )
                procs.append(p)

            rcs = []
            deadline = time.time() + TIMEOUT_TRANSFER
            for p in procs:
                remaining = max(0, deadline - time.time())
                try:
                    p.wait(timeout=remaining); rcs.append(p.returncode)
                except subprocess.TimeoutExpired:
                    p.kill(); rcs.append(-1)
        finally:
            proc_server.terminate()
            proc_server.wait(timeout=10)

        for src, rc, job in zip(srcs, rcs, jobs):
            name = job['filename']
            save_path = job['client_dir'] / "received_g42.bin"
            assert rc == 0, f"client_g42 pour {name} a échoué (multi, parfait) — code {rc}"
            assert save_path.exists(), f"client_g42 n'a pas écrit le fichier pour {name} (G42, 5, multi, parfait)"
            assert md5(src) == md5(save_path), f"Intégrité compromise pour {name} (G42, 5, multi, parfait)"

    # 6) Notre server + N clients_g42 simultanés — imparfait 

    @g42_client_present
    @pytest.mark.dependency(depends=["g42_multi_parfait"])
    def test_6_multi_clients_g42_simultanes_imparfait(self, tmp_path):
        """
        Deux clients_g42 lancés simultanément contre notre server.py
        en conditions réseau imparfaites.
        """
        serve_dir    = tmp_path / "serve";    serve_dir.mkdir()
        client_dir_a = tmp_path / "client_a"; client_dir_a.mkdir()
        client_dir_b = tmp_path / "client_b"; client_dir_b.mkdir()

        port = free_port()
        ready = threading.Event()
        proc_server = run_server(
            ['python3', SERVER_PY, HOST, str(port)], ready, cwd=serve_dir
        )
        ready.wait(timeout=10)

        jobs = [
            {'serve_dir': serve_dir, 'filename': 'file_a.bin', 'filesize': 10_000, 'client_dir': client_dir_a},
            {'serve_dir': serve_dir, 'filename': 'file_b.bin', 'filesize':  8_000, 'client_dir': client_dir_b}
        ]

        proxies, procs, srcs = [], [], []
        try:
            for job in jobs:
                proxy = make_imperfect_proxy(port)
                proxy.start()
                proxies.append(proxy)

                src = make_test_file(job['serve_dir'], job['filename'], job['filesize'])
                srcs.append(src)

                save_path = job['client_dir'] / "received_g42.bin"
                p = subprocess.Popen(
                    ['python3', G42_CLIENT_PY,
                    f'http://[{HOST}]:{proxy.proxy_port}/{job["filename"]}',
                    '--save', str(save_path)],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    cwd=str(job['client_dir'])
                )
                procs.append(p)

            rcs = []
            deadline = time.time() + TIMEOUT_TRANSFER
            for p in procs:
                remaining = max(0, deadline - time.time())
                try:
                    p.wait(timeout=remaining); rcs.append(p.returncode)
                except subprocess.TimeoutExpired:
                    p.kill(); rcs.append(-1)
        finally:
            proc_server.terminate()
            proc_server.wait(timeout=10)

        for proxy, src, rc, job in zip(proxies, srcs, rcs, jobs):
            proxy.wait_done(timeout=10)
            proxy.stop()
            received = proxy.get_received_data()
            name = job['filename']
            assert rc == 0, f"client_g42 pour {name} a échoué (multi, imparfait) — code {rc}"
            assert len(received) > 0, f"Proxy n'a rien capturé pour {name} (G42, multi)"
            assert md5_bytes(received) == md5(src), f"Intégrité compromise pour {name} (G42, 6, multi, imparfait)"


# Groupe 61 

G61_DIR = os.path.join(TOOLS_DIR, 'groupe_61')
G61_SERVER_PY = os.path.join(G61_DIR, 'server.py')
G61_CLIENT_PY = os.path.join(G61_DIR, 'client.py')

g61_server_present = pytest.mark.skipif(not os.path.isfile(G61_SERVER_PY), reason=f"server.py du groupe 61 absent ({G61_SERVER_PY}).")
g61_client_present = pytest.mark.skipif(not os.path.isfile(G61_CLIENT_PY), reason=f"client.py du groupe 61 absent ({G61_CLIENT_PY}).")


class TestInteropGroupe61:
    """
    Interopérabilité avec le groupe 61.
    """

    # Helpers privés 

    def our_server_their_client(self, serve_dir, client_dir, filename, server_port, imperfect=False, **file_kw):
        """
        Lance notre server.py, puis client_g61.
        En mode parfait : connexion directe, le client écrit lui-même le fichier.
        En mode imparfait : proxy capturant, données écrites dans save_path.
        Retourne (src_path, dst_path, returncode).
        """
        ready = threading.Event()
        proc_server = run_server(
            ['python3', SERVER_PY, HOST, str(server_port)],
            ready, cwd=serve_dir
        )
        ready.wait(timeout=10)

        if file_kw.get('is_text'):
            src = make_text_file(serve_dir, filename, lines=file_kw.get('lines', 200))
        else:
            src = make_test_file(serve_dir, filename, file_kw.get('filesize', 5_000))

        save_path = client_dir / "received_g61.bin"

        if imperfect:
            proxy = make_imperfect_proxy(server_port)
            proxy.start()
            target_port = proxy.proxy_port
        else:
            proxy = None
            target_port = server_port

        try:
            result = subprocess.run(
                ['python3', G61_CLIENT_PY,
                f'http://[{HOST}]:{target_port}/{filename}',
                '--save', str(save_path)],
                timeout=TIMEOUT_TRANSFER,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=str(client_dir)
            )
            rc = result.returncode
        except subprocess.TimeoutExpired:
            rc = -1
        finally:
            if proxy:
                proxy.wait_done(timeout=10)
                proxy.stop()
                save_path.write_bytes(proxy.get_received_data())
            proc_server.terminate()
            proc_server.wait(timeout=10)

        return src, save_path, rc

    def their_server_our_client(self, serve_dir, filename, dst, server_port, imperfect=False, **file_kw):
        if file_kw.get('is_text'):
            src = make_text_file(serve_dir, filename, lines=file_kw.get('lines', 200))
        else:
            src = make_test_file(serve_dir, filename, file_kw.get('filesize', 5_000))

        ready = threading.Event()
        proc_server = run_server(
            ['python3', G61_SERVER_PY, HOST, str(server_port)],
            ready, cwd=serve_dir
        )
        ready.wait(timeout=10)

        if imperfect:
            proxy = make_imperfect_proxy(server_port)
            proxy.start()
            target_port = proxy.proxy_port
        else:
            proxy = None
            target_port = server_port

        try:
            result = subprocess.run(
                ['python3', CLIENT_PY,
                f'http://[{HOST}]:{target_port}/{filename}',
                '--save', str(dst)],
                timeout=TIMEOUT_TRANSFER,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            rc = result.returncode
        except subprocess.TimeoutExpired:
            rc = -1
        finally:
            if proxy:
                proxy.wait_done(timeout=10)
                proxy.stop()
            proc_server.terminate()
            proc_server.wait(timeout=10)

        return src, rc

    # 0) Sanity check : server_g61 + client_g61 

    @g61_server_present
    @g61_client_present
    @pytest.mark.dependency(name="g61_sanity")
    def test_0_sanity_g61_vs_g61(self, tmp_path):
        """
        Vérifie que le server et le client du groupe 61 fonctionnent ensemble.
        Si ce test échoue, le problème vient de leur implémentation.
        """
        serve_dir  = tmp_path / "serve";  serve_dir.mkdir()
        client_dir = tmp_path / "client"; client_dir.mkdir()

        port = free_port()
        src = make_test_file(serve_dir, "test.bin", 5_000)
        ready = threading.Event()
        proc_server = run_server(
            ['python3', G61_SERVER_PY, HOST, str(port)], ready, cwd=serve_dir
        )
        ready.wait(timeout=10)

        save_path = client_dir / "received_sanity_g61.bin"
        try:
            result = subprocess.run(
                ['python3', G61_CLIENT_PY,
                f'http://[{HOST}]:{port}/test.bin',
                '--save', str(save_path)],
                timeout=TIMEOUT_TRANSFER,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=str(client_dir)
            )
            rc = result.returncode
        except subprocess.TimeoutExpired:
            rc = -1
        finally:
            proc_server.terminate()
            proc_server.wait(timeout=10)

        assert rc >= 0, f"client_g61 s'est terminé avec le code {rc}"
        assert save_path.exists(), "client_g61 n'a pas écrit le fichier (sanity G61 vs G61)"
        assert md5(src) == md5(save_path), f"Intégrité compromise (sanity G61 vs G61) : reçu {save_path.stat().st_size} o, attendu {src.stat().st_size} o"

    # 1) Notre server + client_g61 — parfait 

    @g61_client_present
    @pytest.mark.dependency(name="g61_notre_server_client_petit", depends=["g61_sanity"])
    def test_1_notre_server_client_g61_petit_parfait(self, tmp_path):
        """client_g61 télécharge un petit fichier depuis notre server.py (parfait)."""
        serve_dir  = tmp_path / "serve";  serve_dir.mkdir()
        client_dir = tmp_path / "client"; client_dir.mkdir()
        src, dst, rc = self.our_server_their_client(serve_dir, client_dir, "small.bin", free_port(), filesize=500)
        assert rc == 0, f"client_g61 a échoué (petit, parfait) — code {rc}"
        assert dst.exists(), "client_g61 n'a pas écrit le fichier (G61, 1, petit, parfait)"
        assert md5(src) == md5(dst), "Intégrité compromise (G61, 1, petit, parfait)"

    @g61_client_present
    @pytest.mark.dependency(name="g61_notre_server_client_grand", depends=["g61_notre_server_client_petit"])
    def test_1_notre_server_client_g61_grand_parfait(self, tmp_path):
        """client_g61 télécharge un grand fichier depuis notre server.py (parfait)."""
        serve_dir  = tmp_path / "serve";  serve_dir.mkdir()
        client_dir = tmp_path / "client"; client_dir.mkdir()
        src, dst, rc = self.our_server_their_client(serve_dir, client_dir, "large.bin", free_port(), filesize=50_000)
        assert rc == 0, f"client_g61 a échoué (grand, parfait) — code {rc}"
        assert dst.exists(), "client_g61 n'a pas écrit le fichier (G61, 1, grand, parfait)"
        assert md5(src) == md5(dst), "Intégrité compromise (G61, 1, grand, parfait)"

    @g61_client_present
    @pytest.mark.dependency(name="g61_notre_server_client_txt", depends=["g61_sanity"])
    def test_1_notre_server_client_g61_txt_parfait(self, tmp_path):
        """client_g61 télécharge un .txt depuis notre server.py (parfait)."""
        serve_dir  = tmp_path / "serve";  serve_dir.mkdir()
        client_dir = tmp_path / "client"; client_dir.mkdir()
        src, dst, rc = self.our_server_their_client(serve_dir, client_dir, "notes.txt", free_port(), is_text=True, lines=300)
        assert rc == 0, f"client_g61 a échoué (.txt, parfait) — code {rc}"
        assert dst.exists(), "client_g61 n'a pas écrit le fichier (G61, 1, .txt, parfait)"
        assert md5(src) == md5(dst), "Intégrité compromise (G61, 1, .txt, parfait)"
        import shutil; shutil.copy(dst, OUTPUT_DIR / "g61_notre_server_notes_recu.txt")

    # 2) Notre server + client_g61 — imparfait 

    @g61_client_present
    @pytest.mark.dependency(depends=["g61_notre_server_client_petit"])
    def test_2_notre_server_client_g61_petit_imparfait(self, tmp_path):
        """client_g61 télécharge un petit fichier depuis notre server.py (imparfait)."""
        serve_dir  = tmp_path / "serve";  serve_dir.mkdir()
        client_dir = tmp_path / "client"; client_dir.mkdir()
        src, dst, rc = self.our_server_their_client(serve_dir, client_dir, "small.bin", free_port(), imperfect=True, filesize=500)
        assert rc == 0, f"client_g61 a échoué (petit, imparfait) — code {rc}"
        assert dst.exists() and dst.stat().st_size > 0, "Proxy n'a rien capturé (G61, 2, petit, imparfait)"
        assert md5(src) == md5(dst), "Intégrité compromise (G61, 2, petit, imparfait)"

    @g61_client_present
    @pytest.mark.dependency(depends=["g61_notre_server_client_grand"])
    def test_2_notre_server_client_g61_grand_imparfait(self, tmp_path):
        """client_g61 télécharge un grand fichier depuis notre server.py (imparfait)."""
        serve_dir  = tmp_path / "serve";  serve_dir.mkdir()
        client_dir = tmp_path / "client"; client_dir.mkdir()
        src, dst, rc = self.our_server_their_client(serve_dir, client_dir, "large.bin", free_port(), imperfect=True, filesize=50_000)
        assert rc == 0, f"client_g61 a échoué (grand, imparfait) — code {rc}"
        assert md5(src) == md5(dst), "Intégrité compromise (G61, 2, grand, imparfait)"

    @g61_client_present
    @pytest.mark.dependency(depends=["g61_notre_server_client_txt"])
    def test_2_notre_server_client_g61_txt_imparfait(self, tmp_path):
        """client_g61 télécharge un .txt depuis notre server.py (imparfait)."""
        serve_dir  = tmp_path / "serve";  serve_dir.mkdir()
        client_dir = tmp_path / "client"; client_dir.mkdir()
        src, dst, rc = self.our_server_their_client(serve_dir, client_dir, "notes.txt", free_port(), imperfect=True, is_text=True, lines=300)
        assert rc == 0, f"client_g61 a échoué (.txt, imparfait) — code {rc}"
        assert md5(src) == md5(dst), "Intégrité compromise (G61, 2, .txt, imparfait)"
        import shutil; shutil.copy(dst, OUTPUT_DIR / "g61_notre_server_notes_imparfait_recu.txt")

    # 3) server_g61 + notre client — parfait 

    @g61_server_present
    @pytest.mark.dependency(name="g61_server_notre_client_petit", depends=["g61_sanity"])
    def test_3_server_g61_notre_client_petit_parfait(self, tmp_path):
        """Notre client.py télécharge un petit fichier depuis server_g61 (parfait)."""
        serve_dir = tmp_path / "serve"; serve_dir.mkdir()
        dst = tmp_path / "received.bin"
        src, rc = self.their_server_our_client(serve_dir, "small.bin", dst, free_port(), filesize=500)
        assert rc == 0, f"Notre client a échoué contre server_g61 (petit, parfait) — code {rc}"
        assert dst.exists()
        assert md5(src) == md5(dst), "Intégrité compromise (G61, 3, petit, parfait)"

    @g61_server_present
    @pytest.mark.dependency(name="g61_server_notre_client_grand", depends=["g61_server_notre_client_petit"])
    def test_3_server_g61_notre_client_grand_parfait(self, tmp_path):
        """Notre client.py télécharge un grand fichier depuis server_g61 (parfait)."""
        serve_dir = tmp_path / "serve"; serve_dir.mkdir()
        dst = tmp_path / "received.bin"
        src, rc = self.their_server_our_client(serve_dir, "large.bin", dst, free_port(), filesize=50_000)
        assert rc == 0, f"Notre client a échoué contre server_g61 (grand, parfait) — code {rc}"
        assert md5(src) == md5(dst), "Intégrité compromise (G61, 3, grand, parfait)"

    @g61_server_present
    @pytest.mark.dependency(name="g61_server_notre_client_txt", depends=["g61_sanity"])
    def test_3_server_g61_notre_client_txt_parfait(self, tmp_path):
        """Notre client.py télécharge un .txt depuis server_g61 (parfait)."""
        serve_dir = tmp_path / "serve"; serve_dir.mkdir()
        dst = OUTPUT_DIR / "g61_server_g61_notes_recu.txt"
        src, rc = self.their_server_our_client(serve_dir, "notes.txt", dst, free_port(), is_text=True, lines=300)
        assert rc == 0, f"Notre client a échoué contre server_g61 (.txt, parfait) — code {rc}"
        assert dst.exists()
        assert md5(src) == md5(dst), "Intégrité compromise (G61, 3, .txt, parfait)"

    # 4) server_g61 + notre client — imparfait 

    @g61_server_present
    @pytest.mark.dependency(depends=["g61_server_notre_client_petit"])
    def test_4_server_g61_notre_client_petit_imparfait(self, tmp_path):
        """Notre client.py télécharge un petit fichier depuis server_g61 (imparfait)."""
        serve_dir = tmp_path / "serve"; serve_dir.mkdir()
        dst = tmp_path / "received.bin"
        src, rc = self.their_server_our_client(serve_dir, "small.bin", dst, free_port(), imperfect=True, filesize=500)
        assert rc == 0, f"Notre client a échoué contre server_g61 (petit, imparfait) — code {rc}"
        assert dst.exists()
        assert md5(src) == md5(dst), "Intégrité compromise (G61, 4, petit, imparfait)"

    @g61_server_present
    @pytest.mark.dependency(depends=["g61_server_notre_client_grand", "g61_server_notre_petit_imparfait"])
    def test_4_server_g61_notre_client_grand_imparfait(self, tmp_path):
        """Notre client.py télécharge un grand fichier depuis server_g61 (imparfait)."""
        serve_dir = tmp_path / "serve"; serve_dir.mkdir()
        dst = tmp_path / "received.bin"
        src, rc = self.their_server_our_client(serve_dir, "large.bin", dst, free_port(), imperfect=True, filesize=50_000)
        assert rc == 0, f"Notre client a échoué contre server_g61 (grand, imparfait) — code {rc}"
        assert md5(src) == md5(dst), "Intégrité compromise (G61, 4, grand, imparfait)"

    @g61_server_present
    @pytest.mark.dependency(depends=["g61_server_notre_client_txt"])
    def test_4_server_g61_notre_client_txt_imparfait(self, tmp_path):
        """Notre client.py télécharge un .txt depuis server_g61 (imparfait)."""
        serve_dir = tmp_path / "serve"; serve_dir.mkdir()
        dst = OUTPUT_DIR / "g61_server_g61_notes_imparfait_recu.txt"
        src, rc = self.their_server_our_client(serve_dir, "notes.txt", dst, free_port(), imperfect=True, is_text=True, lines=300)
        assert rc == 0, f"Notre client a échoué contre server_g61 (.txt, imparfait) — code {rc}"
        assert dst.exists()
        assert md5(src) == md5(dst), "Intégrité compromise (G61, 4, .txt, imparfait)"

    # 5) Notre server + N clients_g61 simultanés — parfait

    @g61_client_present
    @pytest.mark.dependency(name="g61_multi_parfait", depends=["g61_notre_server_client_grand", "g61_notre_server_client_txt"])
    def test_5_multi_clients_g61_simultanes_parfait(self, tmp_path):
        """
        Deux clients_g61 lancés simultanément contre notre server.py
        en conditions réseau parfaites.
        """
        serve_dir    = tmp_path / "serve";    serve_dir.mkdir()
        client_dir_a = tmp_path / "client_a"; client_dir_a.mkdir()
        client_dir_b = tmp_path / "client_b"; client_dir_b.mkdir()

        port = free_port()
        ready = threading.Event()
        proc_server = run_server(
            ['python3', SERVER_PY, HOST, str(port)], ready, cwd=serve_dir
        )
        ready.wait(timeout=10)

        jobs = [
            {'serve_dir': serve_dir, 'filename': 'file_a.bin', 'filesize': 10_000, 'client_dir': client_dir_a},
            {'serve_dir': serve_dir, 'filename': 'file_b.bin', 'filesize':  8_000, 'client_dir': client_dir_b}
        ]

        procs, srcs = [], []
        try:
            for job in jobs:
                src = make_test_file(job['serve_dir'], job['filename'], job['filesize'])
                srcs.append(src)

                save_path = job['client_dir'] / "received_g61.bin"
                p = subprocess.Popen(
                    ['python3', G61_CLIENT_PY,
                    f'http://[{HOST}]:{port}/{job["filename"]}',
                    '--save', str(save_path)],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    cwd=str(job['client_dir'])
                )
                procs.append(p)

            rcs = []
            deadline = time.time() + TIMEOUT_TRANSFER
            for p in procs:
                remaining = max(0, deadline - time.time())
                try:
                    p.wait(timeout=remaining); rcs.append(p.returncode)
                except subprocess.TimeoutExpired:
                    p.kill(); rcs.append(-1)
        finally:
            proc_server.terminate()
            proc_server.wait(timeout=10)

        for src, rc, job in zip(srcs, rcs, jobs):
            name = job['filename']
            save_path = job['client_dir'] / "received_g61.bin"
            assert rc == 0, f"client_g61 pour {name} a échoué (multi, parfait) — code {rc}"
            assert save_path.exists(), f"client_g61 n'a pas écrit le fichier pour {name} (G61, 5, multi, parfait)"
            assert md5(src) == md5(save_path), f"Intégrité compromise pour {name} (G61, 5, multi, parfait)"

    # 6) Notre server + N clients_g61 simultanés — imparfait 

    @g61_client_present
    @pytest.mark.dependency(depends=["g61_multi_parfait"])
    def test_6_multi_clients_g61_simultanes_imparfait(self, tmp_path):
        """
        Deux clients_g61 lancés simultanément contre notre server.py
        en conditions réseau imparfaites.
        """
        serve_dir    = tmp_path / "serve";    serve_dir.mkdir()
        client_dir_a = tmp_path / "client_a"; client_dir_a.mkdir()
        client_dir_b = tmp_path / "client_b"; client_dir_b.mkdir()

        port = free_port()
        ready = threading.Event()
        proc_server = run_server(
            ['python3', SERVER_PY, HOST, str(port)], ready, cwd=serve_dir
        )
        ready.wait(timeout=10)

        jobs = [
            {'serve_dir': serve_dir, 'filename': 'file_a.bin', 'filesize': 10_000, 'client_dir': client_dir_a},
            {'serve_dir': serve_dir, 'filename': 'file_b.bin', 'filesize':  8_000, 'client_dir': client_dir_b}
        ]

        proxies, procs, srcs = [], [], []
        try:
            for job in jobs:
                proxy = make_imperfect_proxy(port)
                proxy.start()
                proxies.append(proxy)

                src = make_test_file(job['serve_dir'], job['filename'], job['filesize'])
                srcs.append(src)

                save_path = job['client_dir'] / "received_g61.bin"
                p = subprocess.Popen(
                    ['python3', G61_CLIENT_PY,
                    f'http://[{HOST}]:{proxy.proxy_port}/{job["filename"]}',
                    '--save', str(save_path)],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    cwd=str(job['client_dir'])
                )
                procs.append(p)

            rcs = []
            deadline = time.time() + TIMEOUT_TRANSFER
            for p in procs:
                remaining = max(0, deadline - time.time())
                try:
                    p.wait(timeout=remaining); rcs.append(p.returncode)
                except subprocess.TimeoutExpired:
                    p.kill(); rcs.append(-1)
        finally:
            proc_server.terminate()
            proc_server.wait(timeout=10)

        for proxy, src, rc, job in zip(proxies, srcs, rcs, jobs):
            proxy.wait_done(timeout=10)
            proxy.stop()
            received = proxy.get_received_data()
            name = job['filename']
            assert rc == 0, f"client_g61 pour {name} a échoué (multi, imparfait) — code {rc}"
            assert len(received) > 0, f"Proxy n'a rien capturé pour {name} (G61, multi)"
            assert md5_bytes(received) == md5(src), f"Intégrité compromise pour {name} (G61, 6, multi, imparfait)"