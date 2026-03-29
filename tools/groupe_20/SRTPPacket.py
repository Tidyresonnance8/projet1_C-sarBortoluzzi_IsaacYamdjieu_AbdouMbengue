import zlib

MAX_PAYLOAD = 1024

PTYPE_DATA = 1
PTYPE_ACK = 2
PTYPE_SACK = 3

SACK_SEQNUM_BITS = 11
SACK_PAYLOAD_ALIGNMENT = 4
MAX_SACK_SEQNUMS = (1024 * 8) // SACK_SEQNUM_BITS

#header0 -> type(2) +  window(6) + length(13) + seqnum(11)


class SRTPPacket:
    def __init__(self, ptype, window, seqnum, timestamp, payload=b""):
        """
            Construit un objet SRTPPacket (correspondant à un paquet SRTP)
            Lève une ValueError si un champ est incohérent avec les spécifications du protocole
        """
        if len(timestamp) != 4:
            raise ValueError("Timestamp doit faire exactement 4 octets")

        if payload is None:
            payload = b""

        length = len(payload)

        if not (0 <= ptype <= 3):
            raise ValueError("Le type (ptype) doit être entre 0 et 3")
        if not (0 <= window <= 63):
            raise ValueError("La fenêtre (window) doit être entre 0 et 63")
        if not (0 <= seqnum <= 2047):
            raise ValueError("Le numéro de séquence (seqnum) doit être entre 0 et 2047")

        if length > MAX_PAYLOAD:
            raise ValueError("Le payload est trop grand (max 1024 octets)")

        if ptype == PTYPE_ACK and length != 0:
            raise ValueError("Les segments ACK doivent avoir un payload vide")

        if ptype == PTYPE_SACK and (length % 4 != 0):
            raise ValueError("Le payload SACK doit avoir une taille multiple de 4 octets")

        self.ptype = ptype
        self.window = window
        self.length = length
        self.seqnum = seqnum
        self.timestamp = timestamp
        self.payload = payload

    def to_segment(self):
        """
            Encode un segment SRTP et retourne les bytes correspondants.
            header0(4) + timestamp(4) + crc1(4) [+ payload + crc2(4)]
        """
        # Construire le header0 en combinant les champs
        header0 = (self.ptype << 30) | (self.window << 24) | (self.length << 11) | self.seqnum
        header0_bytes = header0.to_bytes(4, "big")  # Sur 4 octets et big-endian pour correspondre à l'encodage

        header_without_crc1 = header0_bytes + self.timestamp
        crc1 = crc32_zlib(header_without_crc1)
        crc1_bytes = crc1.to_bytes(4, "big")

        header = header_without_crc1 + crc1_bytes

        if self.length == 0:
            return header

        crc2 = crc32_zlib(self.payload)
        crc2_bytes = crc2.to_bytes(4, "big")

        return header + self.payload + crc2_bytes

    @classmethod
    def from_segment(cls, segment):
        """
        Décode un segment SRTP et renvoie un packet avec les champs:
          {"ptype","window","length","seqnum","timestamp","payload"}

        Lève ValueError si paquet invalide, corrompu ou incohérent.
        """
        if segment is None or len(segment) < 12:  # Header de 12 octets ((type+window+length+seqnum) + timestamp + crc1)
            raise ValueError("Header trop court (min 12 octets)")

        header0 = int.from_bytes(segment[0:4], "big")  # Big-endian pour correspondre à l'encodage
        timestamp = segment[4:8]
        crc1_segment = int.from_bytes(segment[8:12], "big")

        ptype = (header0 >> 30) & ((1 << 2) - 1)
        window = (header0 >> 24) & ((1 << 6) - 1)
        length = (header0 >> 11) & ((1 << 13) - 1)
        seqnum = header0 & ((1 << 11) - 1)

        # Check la validité du type attendu
        if ptype not in (PTYPE_DATA, PTYPE_ACK, PTYPE_SACK):
            raise ValueError("Type inconnu")

        # Check la validité de la longueur du payload
        if length > MAX_PAYLOAD:
            raise ValueError("Payload trop long (max 1024 octets)")

        if ptype == PTYPE_ACK and length != 0:
            raise ValueError("Payload présent dans ACK")

        if ptype == PTYPE_SACK and (length % 4 != 0):
            raise ValueError("Payload SACK invalide")

        # Check CRC1 sur le header (header0 + timestamp)
        crc1_calc = crc32_zlib(segment[0:8])
        if crc1_calc != crc1_segment:
            raise ValueError("CRC1 incorrect")

        # Check la cohérence entre la longueur du payload et la taille du segment
        if length == 0:
            if len(segment) != 12:
                raise ValueError("Payload vide mais octets en trop")
            payload = b""  # Payload vide
        else:
            expected = 12 + length + 4
            if len(segment) != expected:
                raise ValueError(f"Longueur du payload ({length}) incohérente avec la taille du segment ({len(segment)})")

            # Extraire le payload et vérifier le CRC2
            payload = segment[12:12 + length]
            crc2_segment = int.from_bytes(segment[12 + length: 12 + length + 4], "big")

            crc2_calc = crc32_zlib(payload)
            if crc2_calc != crc2_segment:
                raise ValueError("CRC2 incorrect")

        return SRTPPacket(ptype, window, seqnum, timestamp, payload)

    def __str__(self):
        ptype_map = {1: "PTYPE_DATA",
                     2: "PTYPE_ACK",
                     3: "PTYPE_SACK"}
        return f"{ptype_map[self.ptype]} win:{self.window} len:{self.length} seq:{self.seqnum} {self.payload[0:20]}..."

    # Encodage du payload d'un segment SACK à partir des numéros de séquence SACK
    @classmethod
    def encode_sack_payload(cls, seqnums):
        ordered_seqnums = sorted(set(seqnums))
        if len(ordered_seqnums) > MAX_SACK_SEQNUMS:
            raise ValueError("Trop de numéros de séquence SACK")

        bitstream = 0
        bit_length = 0
        for seqnum in ordered_seqnums:
            if not (0 <= seqnum <= 2047):
                raise ValueError("Numéro de séquence SACK invalide")
            bitstream = (bitstream << SACK_SEQNUM_BITS) | seqnum
            bit_length += SACK_SEQNUM_BITS

        if bit_length == 0:
            return b""

        padding_bits = (-bit_length) % (SACK_PAYLOAD_ALIGNMENT * 8)
        bitstream <<= padding_bits
        bit_length += padding_bits
        return bitstream.to_bytes(bit_length // 8, "big")

    # Décodage du payload d'un segment SACK pour extraire les numéros de séquence SACK
    @classmethod
    def decode_sack_payload(cls, payload):
        if len(payload) == 0:
            return ()

        if len(payload) % SACK_PAYLOAD_ALIGNMENT != 0:
            raise ValueError("Payload SACK invalide")

        total_bits = len(payload) * 8
        bitstream = int.from_bytes(payload, "big")
        trailing_padding_bits = total_bits % SACK_SEQNUM_BITS
        if trailing_padding_bits != 0 and (bitstream & ((1 << trailing_padding_bits) - 1)) != 0:
            raise ValueError("Padding SACK invalide")

        seqnum_count = total_bits // SACK_SEQNUM_BITS
        seqnums = []
        for index in range(seqnum_count):
            shift = total_bits - ((index + 1) * SACK_SEQNUM_BITS)
            seqnum = (bitstream >> shift) & ((1 << SACK_SEQNUM_BITS) - 1)
            seqnums.append(seqnum)

        while len(seqnums) > 1 and seqnums[-1] == 0:
            seqnums.pop()

        return tuple(seqnums)


def crc32_zlib(data):
    return zlib.crc32(data) & 0xFFFFFFFF  #& 0xFFFFFFFF pour forcer à 32 bits non signés
