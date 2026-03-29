import struct
import zlib #checksum CRC32

def encode_pack(ptype: int, window: int, seqnum:int, payload = b'', timestamp = 0):
    if (ptype not in [1,2,3]): return None
    if (seqnum < 0 or seqnum > 2047): return None
    
    length = len(payload) #calcul de len du payload (en bytes)
    if (window > 63 or window < 0): return None
    if (length > 1024 or length < 0): return None #ignorer le packet
    
    #construction "header" sur 32 bits
    header = (
        (ptype << 30)| #bit shift de 30 vers la gauche pour avoir les 2 MSB a ptype 
        (window << 24) | #bit shift de (32 - 2 - 6 = 24) 
        (length << 11) | #bit shift de (32 - 2 - 6 - 12 = 13)
        (seqnum)) #dernier 11 bits du header
    
    header_pack: bytes = struct.pack("!I",header) #!I pour network order unsigned int
    timestamp_pack: bytes = struct.pack("!I",timestamp)
    
    CRC1 = zlib.crc32(header_pack + timestamp_pack) #checksum du header 
    crc1_pack: bytes = struct.pack("!I",CRC1)
    
    packet: bytes = header_pack + timestamp_pack + crc1_pack
    
    if payload: #payload doit etre packed par utilisateur!
        CRC2 = zlib.crc32(payload)
        crc2_pack: bytes = struct.pack("!I",CRC2) #checksum du payload 
        packet += payload + crc2_pack
    
    return packet

def decode_pack(data: bytes):
    if len(data) < 12: return None  # Minimum packet size: header(4) + ts(4) + crc1(4)
    
    #calcul du CRC1 
    crc1_check = zlib.crc32(data[0:8])
    crc1: int = struct.unpack("!I", data[8:12])[0]
    
    if (crc1 != crc1_check): return None #ignorer car crc1 different 
    
    #recuperer donnes du header
    header: int = struct.unpack("!I",data[0:4])[0] # on prend les 4 premier bytes (header)
    
    ptype: int = (header >> 30) & 0b11 #bit shift vers la droite de 30, et on prend just les 2 derniers bit (ptype)

    if (ptype not in [1,2,3]): return None 
    
    window: int = (header >> 24) & 0b111111 #bit shift de 24, on prend les 6 derniers bit
    length:  int = (header >> 11) & 0b1111111111111 #bit shift de 13, on prend les 13 derniers bit 
    seqnum: int = header & 0b11111111111 #on prend les 11 derniers bit 
    
    timestamp: int = struct.unpack("!I", data[4:8])[0]
    
    if (window > 63 or window < 0): return None#ignorer le packet
    if (length > 1024 or length < 0): return None 
    if (seqnum > 2047 or seqnum < 0): return None 
    if (length != 0): #le packet contient un payload non vide
        if len(data) < 16 + length:
            return None
        payload = data[12:12+length]  # payload --> bytes (donc unpack avec struct.unpack("!I",payload)[0])
        
        crc2_check = zlib.crc32(payload)
        crc2 = struct.unpack("!I", data[12+length:16+length])[0]
        
        if (crc2 != crc2_check): return None
    else:
        payload = b''
        
    return Packetinfo(ptype,window,seqnum,timestamp=timestamp,payload=payload,length=length)
        
class Packetinfo:
    def __init__(self,ptype = None,window = None, seqnum = None,  length = 0,payload = b'', timestamp = 0, rto = 4000, data = None, retransmitted = False):
        if (data == None):
            self.ptype = ptype
            self.window = window
            self.seqnum = seqnum
            self.payload = payload
            self.timestamp = timestamp
            self.length = length
            self.rto = rto #in ms
            self.retransmitted = retransmitted 
        else:
            p = decode_pack(data)
            if (p == None): 
                raise ValueError
            self.ptype = p.ptype
            self.window = p.window
            self.seqnum = p.seqnum
            self.payload = p.payload
            self.timestamp = p.timestamp
            self.length = p.length
            self.rto = p.rto #in ms
            self.retransmitted = retransmitted
 
        
    def encode_pack(self):
        return encode_pack(self.ptype, self.window, self.seqnum, self.payload, self.timestamp)
        
    def decremente_rto(self,decrement):
        self.rto -= decrement #decrement in ms 
        if (self.rto <= 0): self.rto = 0
        
    def retransmit_pack(self):
        if (self.rto == 0): return True
        return False 

    def print_info(self, is_received):
        #print packet_info 
        print("*------------------*\n")
        if is_received:
            print("server received : \n"\
                f"ptype : {self.ptype}\n" \
                f"window : {self.window}\n"
                f"seqnum : {self.seqnum}\n"
                f"payload_len : {self.length}\n"
                f"payload : {self.payload}\n"
                f"timestamp : {self.timestamp}\n"
                )
        else:
            print("server sent : \n"\
                f"ptype : {self.ptype}\n" \
                f"window : {self.window}\n"
                f"seqnum : {self.seqnum}\n"
                f"payload_len : {self.length}\n"
                f"payload : {self.payload}\n"
                f"timestamp : {self.timestamp}\n"
                )


