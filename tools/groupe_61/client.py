import argparse
import socket
from code_pack import *
import time
#constantes
WINDOW_SIZE = 63
MAX_PACKET_SIZE = 1040
MAX_SEQNUM = 2048

def parse_args():
    """parse les arguments précisés à l'exécution du client"""
    parser = argparse.ArgumentParser()
    #http://hostname:port/path
    parser.add_argument("url")
    parser.add_argument("--save")

    args = parser.parse_args()

    #hostname:port/path
    url = args.url[len("http://"):]

    #hostname:port , path
    hostname_port, path = url.split("/", 1)
    #/path
    path = "/" + path

    hostname, port = hostname_port.rsplit(":", 1)
    port = int(port)
    #des fois le hostname peut être entouré de [], on les enlève
    hostname = hostname.strip("[").strip("]")
    args.save = args.save if args.save is not None else "llm.model"
    return hostname,port,path,args.save

def gettimestamp():
    """retourne le timestamp actuel en ms sur 32 bits"""
    return int(time.time()*1000) % 4294967296

def send_request(sock, hostname, port, path):
    """envoie la requete au serveur et retourne le packetinfo de la requete envoyée"""
    
    request = f"GET {path}".encode("ascii")
    timestamp = gettimestamp()
    packet = encode_pack(
        ptype=1,
        window=WINDOW_SIZE,
        seqnum=0,
        payload=request,
        timestamp=timestamp
    )
    sock.sendto(packet, (hostname, port))
    
    print("client send : \n"\
            "ptype : 1\n" \
            f"window : {WINDOW_SIZE}\n"
            f"seqnum : 0\n"
            f"payload {request}\n"
            f"timestamp : {timestamp}\n"
                  )
    
    return Packetinfo(ptype=1,window = WINDOW_SIZE,seqnum=0,payload=request,timestamp=timestamp)




def client(hostname,port,path,savefile):
    """fonction principale"""

    #buffer des requetes,ici cela ne sert pas à grand chose vu que il y en a qu'une seule
    requests = []
    #objet qui gere l'encodage des paquets dans le fichier
    window = Window(WINDOW_SIZE,savefile)

    #socket
    with socket.socket(socket.AF_INET6, socket.SOCK_DGRAM) as sock:
        request = send_request(sock, hostname, port, path)
        requests.append(request)
        #on attend un pacquet pendant max 1sec
        sock.settimeout(1)

        while True:
            #plutot que d'attendre l'arrivée d'un paquet indéfiniment,le programme peut réenvoyer la requete
            try:
                packet_server,addr_server = sock.recvfrom(MAX_PACKET_SIZE)
            except socket.timeout:
                packet_server = None

            for i in requests:
                timestamp = gettimestamp()
                if(timestamp - i.timestamp > 2000): #si le packet a été envoyé il y a plus de 2 secondes, on le renvoie
                    pack = encode_pack(
                        ptype=i.ptype,         
                        window=i.window,
                        seqnum=i.seqnum,
                        payload=i.payload,
                        timestamp=timestamp
                    )
                    sock.sendto(pack, (hostname, port))
                    
                    print("\nclient send : \n"\
                    f"ptype : {i.ptype}\n" \
                    f"window : {i.window}\n"
                    f"seqnum : {i.seqnum}\n"
                    f"payload : {i.payload}\n"
                    f"timestamp : {timestamp}\n"
                    )
                    
                    i.timestamp = timestamp 

            if packet_server is not None:
                packet = decode_pack(packet_server)
                if (packet == None):
                    #le pequet est corrompu
                    print("invalid packet")
                    continue
            
                print("\nclient recieve : \n"
                    f"ptype : {packet.ptype}\n" 
                    f"window : {packet.window}\n" 
                    f"seqnum : {packet.seqnum}\n" 
                    f"payload_len : {packet.length}\n" 
                    f"payload : {packet.payload}\n" 
                    f"timestamp : {packet.timestamp}\n" 
                    )
                
                # ack
                if(packet.ptype == 2 or packet.ptype == 3):
                        for i in requests:
                            if(packet.seqnum == i.seqnum+1):
                                requests.remove(i)
                # data
                if(packet.ptype == 1):
                    # l'objet window gère le paquet
                    if(window.add_packet(packet)):
                        timestamp = gettimestamp()
                        #on envoie un ack pour valider la reception du paquet
                        ack = encode_pack(
                            ptype=2,          # ACK
                            window=window.remaining_window_size(),
                            seqnum=window.base_seqnum, #le seqnum de l'ack doit etre celui du packet recu + 1
                            payload=b'',
                            timestamp=packet.timestamp
                        )
                        sock.sendto(ack, (hostname, port))
                        
                        print("\nclient send : \n"\
                        f"ptype : {2}\n" \
                        f"window : {window.remaining_window_size()}\n"
                        f"seqnum : {window.base_seqnum}\n"
                        f"payload : {b''}\n"
                        f"timestamp : {timestamp}\n"
                        )
                        
                        #si l'écriture du fichier est fini
                        #variable interne de l'objet window
                        if (window.file_written):
                            break

             


class Window:
    #objet qui va s'occuper de mettre en ordre les paquets pour les écrire dans le fichier de destination
    def __init__(self,window_size,path):
        self.window_size = window_size
        self.packets = {}
        self.base_seqnum = 0
        self.path = path # chemin du fichier
        self.file_written = False # écriture du fichier fini ? 
        self.file = open(self.path, "wb")

    def in_window(self, seqnum):
        """Vérifie si un seqnum est dans la fenêtre de réception [base, base+window_size["""
        if self.base_seqnum + self.window_size < MAX_SEQNUM:
            return self.base_seqnum <= seqnum < self.base_seqnum + self.window_size
        else:
            #cas limite
            return   self.base_seqnum <= seqnum < MAX_SEQNUM  or  0 <= seqnum < (self.base_seqnum + self.window_size) % MAX_SEQNUM

    def is_already_received(self, seqnum):
        """Vérifie si un seqnum est déjà traité (avant base_seqnum)"""
        for i in range(1, self.window_size + 1):
            if seqnum == (self.base_seqnum - i) % MAX_SEQNUM:
                return True
        return False

    def add_packet(self, packet):
        """
        Retourne:
          - True si un ACK doit être envoyé
          - False sinon
        """
        seqnum = packet.seqnum

        # Paquet déjà reçu et traité
        if self.is_already_received(seqnum):
            return True

        # Paquet hors fenêtre
        if not self.in_window(seqnum):
            return False

        # Paquet déjà dans le buffer
        if seqnum in self.packets:
            return False

        # Stocker le paquet
        self.packets[seqnum] = packet

        # Si c'est prochain paquet à inscrire dans le fichier
        if seqnum == self.base_seqnum:
            self.write_packets()

        return True

    def write_packets(self):
        """écrit tout les paquets conséqutifs à partir de base_seqnum dans le fichier"""
        while self.base_seqnum in self.packets.keys():
            packet = self.packets.pop(self.base_seqnum)
            self.write_in_file(packet.payload)

            self.base_seqnum = (self.base_seqnum + 1) % MAX_SEQNUM


            if self.file_written:  # paquet de fin détecté
                break



    def write_in_file(self, payload):
        """écrit dans le fichier"""
        if len(payload) == 0:
            self.file.close()
            self.file_written = True # l'écriture du fichier fini
        else:
            self.file.write(payload)


    def remaining_window_size(self):
        """nombre d'emplacement libre dans le buffer"""
        return self.window_size - len(self.packets)
    

if __name__ == "__main__":
    hostname,port,path,savefile = parse_args()
    client(hostname,port,path,savefile)