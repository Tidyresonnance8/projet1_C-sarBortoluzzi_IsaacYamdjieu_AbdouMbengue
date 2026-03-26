import socket 
import zlib
import struct

MAX_PAYLOAD_LENGTH = 1024
MAX_PACKET_SIZE = 4 + 4 + 4 + MAX_PAYLOAD_LENGTH + 4

PTYPE_DATA = 1
PTYPE_ACK  = 2
PTYPE_SACK = 3

class SRTPPackets:

    def __init__(self, ptype, window, seqnum, timestamp, payload):
        self.ptype = ptype
        self.window = window
        self.seqnum = seqnum
        self.payload = payload
        self.length = len(payload)
        self.timestamp = timestamp

    @staticmethod 
    def fill_header(ptype, window, length, seqnum):
        """
        fill_header fills a 32 bit integer with all the header fields
        
        *32-bit layout: | ptype | window | length | seqnum |*

        Parameters:
            ptype  (int) : type of packet (data : 1, ACK : 2, selectif ACK : 3) --- 0 is ignored
            window (int) : number of empty slots in receiver buffer (0 - 63)    --- 0 if receiver does not have a buffer
            length (int) : number of bytes in the payload                       --- cannot be superior to MAX_PAYLOAD_LENGTH
            seqnum (int) : depending on packet type 
                                - PTYPE_DATA : sequence number of sent data
                                - PTYPE_ACK  : sequence number of ACK
        Returns:
            out (int, 32 bit integer) :  | ptype (2 bits) | window (6 bits) | length (13 bits) | seqnum (11 bits) |

        """ 

        # Pas encore sure comment on va gérer les erreur, return None is a placeholder
        if ptype != 1 and ptype != 2 and ptype != 3 :
            return None
        if window < 0 or window > 63 :
            return None
        if length < 0 or length > MAX_PAYLOAD_LENGTH :
            return None
        if seqnum < 0 :
            return None
        if seqnum > 2047 :
            seqnum = 0
        # -----------------------------------------------------

        ptype_h  = (ptype  & 0b11) << 30
        window_h = (window & 0b111111) << 24
        seqnum_h = (seqnum & 0b11111111111)
        length_h = (length & 0b1111111111111) << 11

        header_32bit = (ptype_h | window_h | length_h | seqnum_h)

        return header_32bit



    @staticmethod
    def extract_header(header):
        """
        extract_header takes a 32 bit integer and extract the various header fields

        Parameters:
            header (32-bit int) : 32 bit integer containing the header fields

        Returns:
            ptype  (int) : type of the recieved packet
            window (int) : number of empty slots in reciever buffer (0 - 63) 
            seqnum (int) : sequence number of the data or ACK depening on ptype
            length (int) : length of data in bytes
        """
        if header is None : return None, None, None, None
        ptype  = (header & (0b11 << 30)) >> 30
        window = (header & (0b111111 << 24)) >> 24
        seqnum = (header & (0b11111111111))
        length = (header & (0b1111111111111 << 11)) >> 11

        return ptype, window, seqnum, length
    
    @staticmethod
    def _crc32(stuff):
        """ 
        applies the CRC32 function to the data in `stuff`, and returns an unsigned 32-bit integer
        """
        return zlib.crc32(stuff) & 0xffffffff
    
    def encode(self):
        """
        builds the SRTP packet as a bytes object, to be sent over UDP later.

        *Packet layout:*
            *| Header (4B) | Timestamp (4B) | CRC1 (4B) | Payload (0-1024B) | CRC2 (4B, optional) |*
        
        Parameters:
            Header (int) : the 32-bit integer returned by fill_header(ptype, window, length, seqnum)
            Timestamp (int) : 32-bit value chosen by the sender
            CRC1 (int) : CRC32 of Header + Timestamp, stored as a 32-bit integer
            Payload (int) :  contains `length` bytes
            CRC2 (int) : CRC32 of Payload, exists only if `length` > 0
        
        Returns:
            out (bytes) : the full encoded SRTP packet (header + timestamp + crc1 + payload + optional crc2)
        """
        # utilise fill_header, qui te rendera un 32-bit integer avec le header
        # utilise struct pour joindre header et timestamp
        # puis tu répète avec struct pour joindre CRC1
        # ensuite payload
        # ensuite CRC2

        ## HEADER
        if self.length > MAX_PAYLOAD_LENGTH: raise ValueError("Payload is too large")
        header = self.fill_header(self.ptype, self.window, self.length, self.seqnum)
        if header is None: raise ValueError("Header specs are invalid")
        timed_header_asBytes = struct.pack("!2I", header, self.timestamp) # 2 * 32-bit numbers 

        ## CRC1
        CRC1 = self._crc32(timed_header_asBytes)
        CRC1_asBytes = struct.pack("!I", CRC1)

        ## CRC2
        payload_asBytes = self.payload
        if self.length > 0:
            CRC2 = self._crc32(payload_asBytes)
            CRC2_asBytes = struct.pack("!I", CRC2)
            return timed_header_asBytes + CRC1_asBytes + payload_asBytes + CRC2_asBytes

        return timed_header_asBytes + CRC1_asBytes
    
    @staticmethod
    def decode(packed:bytes):
        """
        decodes the SRTP packet: unwraps the bytes object after receiving it through UDP

        Parameters:
            Header (int) : the 32-bit integer returned by fill_header(ptype, window, length, seqnum)
            Timestamp (int) : 32-bit value chosen by the sender
            CRC1 (int) : CRC32 of Header + Timestamp, stored as a 32-bit integer
            Payload (int) : contains `length` bytes
            CRC2 (int) : CRC32 of Payload, exists only if `length` > 0
        
        Returns:
            out (SRTPPacket) : the decoded SRTP packet
        """
        # HEADER + TIMESTAMP + CRC1
        if packed is None or len(packed) < 12: return None
        header_asInt = struct.unpack("!I", packed[0:4])[0]
        timestamp_asInt = struct.unpack("!I", packed[4:8])[0]
        CRC1_asInt = struct.unpack("!I", packed[8:12])[0]
            # un unsigned int32 prend 4 bytes en stockage, donc on coupe packed en blocs de 4;
            # struct.unpack() retourne toujours un tuple, donc on prend l'élément 0;
        CRC1_computed = SRTPPackets._crc32(packed[0:8])
        if CRC1_computed != CRC1_asInt: 
            return None # header invalide
        this_ptype, this_window, this_seqnum, this_length = SRTPPackets.extract_header(header_asInt)
            # got ptype, window, seqnum, length

        # VALIDATION
        if this_ptype not in (PTYPE_ACK, PTYPE_DATA, PTYPE_SACK) or\
        not (0 <= this_window <= 63) or\
        not (0 <= this_seqnum <= 2047) or\
        not (0 <= this_length <= MAX_PAYLOAD_LENGTH): 
            return None # attributs invalides 
        
        # "NO PAYLOAD" CASE
        if this_length == 0:
            if len(packed) != 12: return None
            return SRTPPackets(this_ptype, this_window, this_seqnum, timestamp_asInt, b"")
            # pas de payload => crc2 ne doit pas exister pour un packet valide
            # on ne doit accepter le packet que s'il fait exactement 12 bytes, sinon malformé
        expected_size = 12 + this_length + 4 
        if len(packed) < expected_size: 
            return None # packet tronqué

        # PAYLOAD + CRC2
        part = 12 + this_length 
        this_payload = packed[12:part] # still in bytes
        CRC2_asInt = struct.unpack("!I", packed[part : part+4])[0]
        CRC2_computed = SRTPPackets._crc32(this_payload) # verification value
        if CRC2_computed != CRC2_asInt: 
            return None # packet corrompu
        return SRTPPackets(this_ptype, this_window, this_seqnum, timestamp_asInt, this_payload) 