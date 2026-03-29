import socket
from SRTP import *
import time

def now32():
    """Return a 32-bit timestamp in milliseconds (loops at 2^32)"""
    return int(time.monotonic() * 1000) & 0xffffffff

def send_ack(sock, seqnum: int, window: int, tmsp: int):
    """ builds and sends an ACK signal (SRTPPackets) through a connected socket """
    ack = SRTPPackets(
        ptype=PTYPE_ACK,
        window=window,
        seqnum=seqnum,
        timestamp=tmsp,
        payload=b""
    )
    sock.send(ack.encode())
    
def send_data(sock, seqnum: int, window: int, tmsp: int, payload: bytes):
    """ builds and sends a DATA packet (SRTPPackets) through a connected socket """
    data = SRTPPackets(
        ptype=PTYPE_DATA,
        window=window,
        seqnum=seqnum,
        timestamp=tmsp,
        payload=payload
    )
    sock.send(data.encode())

def send_get(sock, path, window):
    """ builds and sends a GET request (HTTP/0.9) to the server """
    req = f"GET {path}".encode("ascii")
    get = SRTPPackets(
        ptype=PTYPE_DATA,
        window=window,
        seqnum=0,
        timestamp=now32(),
        payload=req
    )
    sock.send(get.encode())

def recv_data(sock):
    """ receives and unpacks a DATA packet (SRTPPackets) through a connected socket """
    try:
        buffer = sock.recv(MAX_PACKET_SIZE)
    except socket.timeout:
        return "timeout", None
    print("CLIENT raw recv len =", len(buffer))
    data = SRTPPackets.decode(buffer)
    print("CLIENT decoded =", data)
    if data is None or data.ptype != PTYPE_DATA:
        return "invalid", None
    return "ok", data


def recv_file(sock, save_file: str, window_size: int = 0, packet1=None):
    """
    receives data (SRTPPackets) from a server, unpacks it and stores it in a save file
    Parameters:
        sock (socket): the connected socket used to listen to a response
        save_file (str): the name of the file in which to store the response
        window_size (int): the size of the reception buffer/window
    ## Result:
        data is stored in the chosen file if specified, otherwise in a default file
    """
    expected_seqnum = 0
    last_timestamp = 0 

    # TO BUFFER OR NOT TO BUFFER
    useBuff = window_size > 0
    W = max(1, min(63, window_size)) if useBuff else 0
    window_buffer = {}

    chances = 10 # TBD, placeholder for now
    retries = 0
    pending = packet1
    with open(save_file, "wb") as outfile:
        while True:
            if pending is not None:
                data = pending
                status = "ok"
                pending = None
            else:
                status, data = recv_data(sock)
            

            if status == "timeout": # plusieurs essais avant erreur
                retries += 1
                if retries >= chances: 
                    raise SystemExit("Error: server has timed out") 
                continue
            if status == "invalid": # data est invalide et on ignore
                continue
            retries = 0 # for next data block !


            last_timestamp = data.timestamp # keep track of it
            if data.length == 0:
                if data.seqnum == expected_seqnum: # FIN
                    free = max(0, min(63, W - len(window_buffer)))
                    send_ack(sock, expected_seqnum, free, last_timestamp)
                    return 
                else:       # FIN trop tôt, ignoré
                    free = max(0, min(63, W - len(window_buffer))) 
                    send_ack(sock, expected_seqnum, free, last_timestamp)
                    continue 

            #################################################
            ################# MODE NOBUFFER #################       # when window_size == 0 (consignes)
            if not useBuff:
                if data.seqnum == expected_seqnum:
                    outfile.write(data.payload)
                    expected_seqnum = (expected_seqnum + 1) % 2048
                send_ack(sock, expected_seqnum, 0, last_timestamp) 
                continue

            #################################################
            ################## MODE BUFFER ##################
            if not in_window(data.seqnum, expected_seqnum, W):
                free = max(0, min(63, W - len(window_buffer)))
                send_ack(sock, expected_seqnum, free, last_timestamp)
                continue
            if data.seqnum == expected_seqnum:
                outfile.write(data.payload)
                expected_seqnum = (expected_seqnum + 1) % 2048
                while expected_seqnum in window_buffer: # ACK additif/cumulatif
                        # flush les paquets pour avoir un expected correct qui n'attend pa sdans le vide
                    outfile.write(window_buffer.pop(expected_seqnum))
                    expected_seqnum = (expected_seqnum + 1) % 2048
            else:
                if data.seqnum not in window_buffer and len(window_buffer) < W:
                    window_buffer[data.seqnum] = data.payload

                    
            free = max(0, min(63, W - len(window_buffer)))
            send_ack(sock, expected_seqnum, free, last_timestamp) 

def in_window(seqnum: int, base: int, window_size: int):
    """True if `seqnum` is in [`base`, `base` + `window_size`) mod 2048."""
    return ((seqnum - base) % 2048) < window_size