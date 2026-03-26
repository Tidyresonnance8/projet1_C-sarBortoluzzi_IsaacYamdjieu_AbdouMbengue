# SRTP — Simple Reliable Transfer Protocol

Un protocole de transfert de fichiers fiable sur UDP avec fenêtre glissante, CRC et retransmissions adaptatives.

---

## Vue d'ensemble

SRTP implémente un transfert de fichiers fiable par-dessus UDP en combinant :

- **Fenêtre glissante** (window size configurable, max 63 paquets en vol)
- **Numérotation de séquence** sur 11 bits (0–2047, wrap-around automatique)
- **Acquittements cumulatifs (ACK)** et **acquittements sélectifs (SACK)**
- **Double CRC-32** : un sur l'en-tête, un sur le payload
- **Retransmission adaptative** basée sur le RTT mesuré dynamiquement
- **Serveur multi-clients** : plusieurs transferts simultanés sur le même port UDP

---

## Structure du projet

```
.
├── src/
│   ├── protocol.py         # Encodage/décodage des paquets SRTP
│   ├── server.py           # Serveur UDP multi-clients
│   └── client.py           # Client UDP (requête HTTP 0.9 simplifiée)
├── tests/
│   ├── test_srtp.py        # Tests unitaires du protocole
│   ├── test_integration.py # Tests d'intégration
│   ├── test_interop.py     # Tests d'interopérabilité
│   ├── proxy.py            # Proxy UDP capturant (simulation réseau imparfait)
│   ├── Helpers.py          # Utilitaires partagés par les tests
│   └── Benchmark.py        # Benchmark de performances (débit, pertes, CDF)
├── tools/                  # Outils pour tester l'interopérabilité (optionnels)
│   ├── server              # Serveur de référence (fourni sur Moodle)
│   ├── client              # Client de référence (fourni sur Moodle)
│   ├── groupe_20/          # Codes du groupe 20
│   │   ├── server.py       # Server du groupe 20
│   │   └── client.py       # Client du groupe 20
│   ├── groupe_42/          # Codes du groupe 42
│   │   ├── server.py       # Server du groupe 42
│   │   └── client.py       # Client du groupe 42
│   ├── groupe_61/          # Codes du groupe 61
│   │   ├── server.py       # Server du groupe 61
│   │   └── client.py       # Client du groupe 61
├── test_outputs/           # Fichiers .txt reçus persistés après les tests
├── plots/                  # Graphiques générés par le benchmark
├── Makefile                
├── rapport.pdf
└── README.md
```

---

## Format des paquets

Chaque paquet SRTP a la structure suivante :

```
 0                   1                   2                   3
 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|type(2)|  window (6) |    longueur (13)   |    seqnum (11)    |  ← 4 octets
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|                        timestamp (32 bits)                    |  ← 4 octets
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|                      CRC-32 header (32 bits)                  |  ← 4 octets
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|                       payload (0–1024 octets)                 |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|                      CRC-32 payload (32 bits)  [si payload>0] |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
```

**Types de paquets :**

| Valeur | Type   | Description                               |
|--------|--------|-------------------------------------------|
| 1      | DATA   | Données (requête client ou chunk serveur) |
| 2      | ACK    | Acquittement cumulatif                    |
| 3      | SACK   | Acquittement sélectif                     |

**Fin de transfert :** un paquet DATA avec payload vide (`b""`) signale la fin du fichier.

---

## Installation

Python 3.10+ requis. Aucune dépendance externe pour `protocol.py`, `server.py` et `client.py`.

```bash
# Dépendances de test et benchmark
pip install pytest matplotlib numpy
```

---

## Utilisation

### Démarrer le serveur

```bash
python3 src/server.py <hostname> <port> [--root <dossier>]

# Exemple : écouter sur IPv6 loopback, port 5000, servir le dossier courant
python3 src/server.py ::1 5000 --root ./fichiers
```

### Télécharger un fichier (client)

```bash
python3 src/client.py <url> [--save <chemin>]

# Exemple
python3 src/client.py http://[::1]:5000/rapport.pdf --save ./rapport.pdf
```

L'URL utilise le format `http://[<IPv6>]:<port>/<chemin>`. Le protocole de transport reste UDP — le préfixe `http://` sert uniquement à transporter l'hôte, le port et le chemin.

---

## Tests

### Tests unitaires (protocol.py)

```bash
make test-protocol
```

Couvre : `empackage`, `depackage`, `encode_sack`, `decode_sack`, intégration encode→paquet→décode, valeurs limites, corruptions CRC.

### Tests d'intégration

```bash
make test-integration
```
Teste notre server contre notre client sur loopback, avedc des fichiers bianires et texte en condition parfaite et en réseau imparfait avec pertes, corruption et dédoublement de paquets via proxy UDP.

### Tests d'interopérabilité

```bash
make test-interoperabilite
```

Quatre catégories :

| Catégorie | Description |
|-----------|-------------|
| `TestInteroperabilite` (1) | Sanity check : server_interop + client_interop   |
| `TestInteroperabilite` (2) | Notre server.py + client_interop                 |
| `TestInteroperabilite` (3) | server_interop + notre client.py                 |
| `TestInteroperabilite` (4) | Notre server.py + N clients_interop simultanés   |

Les tests d'interopérabilité sont **skippés automatiquement** si les clients ou server nécessaires sont absents. Les tests d'interopérabilité sont effectuer en conditions parfaites et imparfaites (corruptions, pertes et dédoublements).

### Lancer tous les tests

```bash
make test        # tous les tests
make test-v      # verbeux
```

---

## Interopérabilité

Pour activer les tests contre les binaires de référence Moodle :

1. Télécharger `server` et `client` depuis Moodle
2. Les placer dans `tools/` à la racine du projet
3. Les rendre exécutables : `chmod +x tools/server tools/client`
4. Relancer `pytest tests/test_integration.py -v`

Le proxy UDP capturant (`proxy.py`) est utilisé pour intercepter ce que `client_ref` reçoit réellement, indépendamment du fait qu'il écrive ou non un fichier local.

---

## Benchmark

```bash
make plots
```

Génère deux graphiques dans `plots/` :

| Fichier | Contenu |
|---------|---------|
| `throughput_vs_filesize.png` | Débit (KB/s) en fonction de la taille du fichier (1 KB → 100 MB) |
| `throughput_vs_loss.png` | Débit en fonction du taux de perte simulé (0 % → 30 %) pour un transfert d'un fichier de 512 KB|

---

## Simulation de réseau imparfait

`UDPCapturingProxy` (dans `proxy.py`) peut introduire des perturbations réseau :

```python
proxy = UDPCapturingProxy(server_port, imperfect_network=True)
proxy.DROP_PROB    = 0.10   # 10 % de paquets perdus
proxy.CORRUPT_PROB = 0.10   # 10 % de paquets corrompus (CRC invalide)
proxy.DUPLICATE_PROB = 0.10  # 10 % de chance de duplication d'un paquet
proxy.start()
```

Les paquets corrompus sont détectés et rejetés par le récepteur via le CRC ; le protocole les retransmet automatiquement.

---

## Notes de conception

- Le **serveur** ne termine pas un transfert client avant que tous les ACK soient reçus et que 3 secondes se soient écoulées depuis le dernier paquet de fin envoyé.
- Le **client** retransmet sa requête GET si aucune réponse n'arrive dans les 5 premières tentatives (2 s chacune), puis abandonne proprement.
- Le **seqnum** se fait sur 11 bits (modulo 2048) ; la fenêtre maximale est de 63 paquets, ce qui garantit l'absence d'ambiguïté lors du wrap-around.
- Les **SACK** encodent les numéros de séquence en champs de 11 bits compactés, alignés sur 32 bits.
- Le champ **Timestamp** a été utilisé pour implémenter un timeout adaptatif au RTT (max de 0.5 et 2.5 fois le RTT estimé avec fenêtre glissante).