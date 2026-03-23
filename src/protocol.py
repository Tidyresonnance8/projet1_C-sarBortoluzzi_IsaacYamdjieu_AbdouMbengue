import struct
import zlib
import socket

PTYPE_DATA = 1
PTYPE_ACK = 2
PTYPE_SACK = 3

def empackage(packet_type, window, seqnum,timestamp, payload = b""):
    try:
        longueur_0 = len(payload)
        if longueur_0 > 1024:
            raise ValueError("Payload trop grand")
        seqnum = seqnum % 2048 # Pour garantir 11 bits maximum
        header_byte = (packet_type << 6) | window
        header_byte_2 = (header_byte << 13) | longueur_0
        header_byte_3 = (header_byte_2 << 11) | seqnum

        header_sans_crc = struct.pack("!II",header_byte_3,timestamp)

        crc1 = zlib.crc32(header_sans_crc)
        entete_finale = header_sans_crc + struct.pack("!I", crc1)

        if len(payload) > 0:
            crc2 = zlib.crc32(payload)
            return entete_finale + payload + struct.pack("!I", crc2)
        else:
            return entete_finale

    except Exception:
        return None
    
def depackage(segment):
    #try:
    if len(segment) < 12:
        return None
    header_bytes = segment[:12]
    bloc_32, timestamp, crc1_recu = struct.unpack("!III", header_bytes)
    seqnum = bloc_32 % 2048
    longueur  = (bloc_32 >> 11) & 0x1FFF # 13 bits
    window    = (bloc_32 >> 24) & 0x3F # 6 bits
    pack_type = (bloc_32 >> 30) 

    crc1_calcule = zlib.crc32(segment[:8])
    if crc1_calcule != crc1_recu:
        return None
    payload = b""
    if longueur > 0:
        payload = segment[12:12+longueur]
        crc2_bytes = segment[12 + longueur : 12 + longueur + 4]
        crc2_recu = struct.unpack("!I",crc2_bytes)[0]
        crc2_calcule = zlib.crc32(payload)
        if crc2_recu != crc2_calcule:
            return None
    return (pack_type,window,seqnum,timestamp,payload)
    #except Exception:
        #return None


def encode_sack(liste_seqnums):
    if not liste_seqnums:
        return b""
    bit_string = "".join(format(s,'011b') for s in liste_seqnums)
    reste = len(bit_string) % 32
    if reste != 0:
        bit_string += '0' * (32 - reste)
    return int(bit_string,2).to_bytes(len(bit_string) // 8, byteorder = 'big')

def decode_sack(payload):
    if not payload:
        return []
    bit_string = bin(int.from_bytes(payload, byteorder='big'))[2:].zfill(len(payload) * 8)
    seqnums = []
    for i in range(0, len(bit_string) - 10, 11):
        morceau = bit_string[i:i+11]
        if len(morceau) == 11:
            seqnums.append(int(morceau,2))
    # Supprimer les zéros de padding en fin de liste
    while seqnums and seqnums[-1] == 0:
        seqnums.pop()
    return seqnums

