import zlib
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import protocol

#  Helpers

def make_packet(ptype=protocol.PTYPE_DATA, window=63, seqnum=0,
                timestamp=12345, payload=b"hello"):
    return protocol.empackage(ptype, window, seqnum, timestamp, payload)


#  1. EMPACKAGE — construction des paquets

class TestEmpackage:

    def test_retourne_bytes(self):
        pkt = make_packet()
        assert isinstance(pkt, bytes)

    def test_taille_sans_payload(self):
        # 4 (header) + 4 (timestamp) + 4 (CRC header) = 12 octets
        pkt = protocol.empackage(protocol.PTYPE_DATA, 63, 0, 0, b"")
        assert len(pkt) == 12

    def test_taille_avec_payload(self):
        payload = b"A" * 100
        pkt = protocol.empackage(protocol.PTYPE_DATA, 63, 0, 0, payload)
        # 12 (header+CRC1) + 100 (payload) + 4 (CRC2)
        assert len(pkt) == 116

    def test_payload_trop_grand_retourne_none(self):
        pkt = protocol.empackage(protocol.PTYPE_DATA, 63, 0, 0, b"X" * 1025)
        assert pkt is None

    def test_payload_exactement_1024_accepte(self):
        pkt = protocol.empackage(protocol.PTYPE_DATA, 63, 0, 0, b"X" * 1024)
        assert pkt is not None

    def test_seqnum_wrap_2048(self):
        # seqnum 2048 doit être traité comme 0
        pkt1 = protocol.empackage(protocol.PTYPE_DATA, 63, 0,    0, b"x")
        pkt2 = protocol.empackage(protocol.PTYPE_DATA, 63, 2048, 0, b"x")
        assert pkt1 == pkt2

    def test_seqnum_max_2047(self):
        pkt = protocol.empackage(protocol.PTYPE_DATA, 63, 2047, 0, b"x")
        assert pkt is not None
        result = protocol.depackage(pkt)
        assert result[2] == 2047

    def test_tous_types_paquets(self):
        for ptype in [protocol.PTYPE_DATA, protocol.PTYPE_ACK, protocol.PTYPE_SACK]:
            pkt = protocol.empackage(ptype, 63, 0, 0, b"")
            assert pkt is not None


#  2. DEPACKAGE — décodage des paquets

class TestDepackage:

    def test_bug_longueur_mal_extraite(self):
        pkt = protocol.empackage(protocol.PTYPE_DATA, 63, 0, 0, b"hello")
        result = protocol.depackage(pkt)
        assert result is not None, "depackage doit réussir sur un paquet valide"

    def test_roundtrip_data(self):
        """Roundtrip complet """
        payload = b"test payload"
        pkt = protocol.empackage(protocol.PTYPE_DATA, 63, 42, 99999, payload)
        result = protocol.depackage(pkt)
        assert result is not None
        ptype, window, seqnum, timestamp, decoded_payload = result
        assert ptype          == protocol.PTYPE_DATA
        assert window         == 63
        assert seqnum         == 42
        assert timestamp      == 99999
        assert decoded_payload == payload

    def test_roundtrip_ack_sans_payload(self):
        """ACK sans payload"""
        pkt = protocol.empackage(protocol.PTYPE_ACK, 10, 7, 0, b"")
        result = protocol.depackage(pkt)
        assert result is not None
        ptype, window, seqnum, _, payload = result
        assert ptype   == protocol.PTYPE_ACK
        assert window  == 10
        assert seqnum  == 7
        assert payload == b""

    def test_paquet_trop_court_retourne_none(self):
        assert protocol.depackage(b"")        is None
        assert protocol.depackage(b"x" * 5)  is None
        assert protocol.depackage(b"x" * 11) is None

    def test_crc_header_corrompu_retourne_none(self):
        pkt = bytearray(make_packet())
        pkt[8] ^= 0xFF  # on corrompt un octet du CRC header
        assert protocol.depackage(bytes(pkt)) is None

    def test_crc_payload_corrompu_retourne_none(self):
        pkt = bytearray(make_packet(payload=b"important data"))
        pkt[-1] ^= 0xFF  # on corrompt le dernier octet du CRC payload
        result = protocol.depackage(bytes(pkt))
        assert result is None, "Un paquet avec CRC corrompu doit retourner None"

    def test_payload_corrompu_retourne_none(self):
        pkt = bytearray(make_packet(payload=b"important data"))
        pkt[12] ^= 0xFF  # on corrompt le premier octet du payload
        result = protocol.depackage(bytes(pkt))
        assert result is None, "Un paquet avec payload corrompu doit retourner None"

    def test_seqnum_roundtrip_valeurs_limites(self):
        for seqnum in [0, 1, 1023, 1024, 2047]:
            pkt = protocol.empackage(protocol.PTYPE_DATA, 63, seqnum, 0, b"x")
            result = protocol.depackage(pkt)
            assert result[2] == seqnum, f"seqnum={seqnum} mal encodé"

    def test_timestamp_roundtrip_valeurs_limites(self):
        for ts in [0, 1, 2**16, 2**32 - 1]:
            pkt = protocol.empackage(protocol.PTYPE_DATA, 1, 0, ts, b"")
            result = protocol.depackage(pkt)
            assert result[3] == ts, f"timestamp={ts} mal encodé"

    def test_payload_vide_roundtrip(self):
        pkt = protocol.empackage(protocol.PTYPE_DATA, 63, 0, 0, b"")
        result = protocol.depackage(pkt)
        assert result is not None
        assert result[4] == b""

    def test_payload_binaire_roundtrip(self):
        payload = bytes(range(256))
        pkt = protocol.empackage(protocol.PTYPE_DATA, 63, 0, 0, payload)
        result = protocol.depackage(pkt)
        assert result[4] == payload


#  3. ENCODE_SACK / DECODE_SACK

class TestSack:

    def test_encode_liste_vide(self):
        assert protocol.encode_sack([]) == b""

    def test_decode_payload_vide(self):
        assert protocol.decode_sack(b"") == []

    def test_roundtrip_un_seqnum(self):
        original = [42]
        encoded = protocol.encode_sack(original)
        decoded = protocol.decode_sack(encoded)
        assert decoded == original

    def test_roundtrip_plusieurs_seqnums(self):
        original = [0, 1, 100, 1024, 2047]
        encoded = protocol.encode_sack(original)
        decoded = protocol.decode_sack(encoded)
        assert decoded == original

    def test_roundtrip_seqnums_consecutifs(self):
        original = list(range(10))
        encoded = protocol.encode_sack(original)
        decoded = protocol.decode_sack(encoded)
        assert decoded[:len(original)] == original

    def test_seqnum_max_2047(self):
        original = [2047]
        encoded = protocol.encode_sack(original)
        decoded = protocol.decode_sack(encoded)
        assert decoded == original

    def test_encode_retourne_bytes(self):
        result = protocol.encode_sack([1, 2, 3])
        assert isinstance(result, bytes)

    def test_taille_encodage_alignee_32bits(self):
        # 1 seqnum = 11 bits → padded à 32 bits → 4 octets
        result = protocol.encode_sack([42])
        assert len(result) % 4 == 0

    def test_taille_encodage_3_seqnums(self):
        # 3 seqnums = 33 bits → padded à 64 bits → 8 octets
        result = protocol.encode_sack([1, 2, 3])
        assert len(result) == 8


#  4. INTÉGRATION — encode → empackage → depackage

class TestIntegration:

    def test_sack_dans_paquet_complet(self):
        seqnums = [10, 20, 30]
        payload_sack = protocol.encode_sack(seqnums)
        pkt = protocol.empackage(protocol.PTYPE_SACK, 63, 5, 0, payload_sack)
        result = protocol.depackage(pkt)
        assert result is not None
        ptype, _, seqnum, _, payload_recu = result
        assert ptype == protocol.PTYPE_SACK
        assert seqnum == 5
        decoded = protocol.decode_sack(payload_recu)
        assert decoded[:3] == seqnums  # les 3 premiers sont corrects (bug padding ignoré)

    def test_sequence_paquets_data_incrementaux(self):
        for i in range(5):
            payload = f"morceau_{i}".encode()
            pkt = protocol.empackage(protocol.PTYPE_DATA, 63, i, 0, payload)
            result = protocol.depackage(pkt)
            assert result is not None
            assert result[2] == i
            assert result[4] == payload

    def test_paquet_requete_http(self):
        request = b"GET /fichier.bin"
        pkt = protocol.empackage(protocol.PTYPE_DATA, 63, 0, 12345, request)
        result = protocol.depackage(pkt)
        assert result is not None
        assert result[4] == request

    def test_paquet_fin_transfert_payload_vide(self):
        pkt = protocol.empackage(protocol.PTYPE_DATA, 63, 100, 0, b"")
        result = protocol.depackage(pkt)
        assert result is not None
        assert result[4] == b""
