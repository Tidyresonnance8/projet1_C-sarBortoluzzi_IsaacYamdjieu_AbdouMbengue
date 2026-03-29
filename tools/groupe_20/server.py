import argparse
import sys
import os
import socket
from SRTP import server_send, RECV_BUFFER_SIZE, SRTPPacket, server_end_connection, server_send_ack_request
import threading
import queue


class Client:
    def __init__(self, sock, addr, segment, directory):
        self.sock = sock
        self.addr = addr
        self.queue = queue.LifoQueue()
        request = SRTPPacket.from_segment(segment)
        self.filename = os.path.join(directory, request.payload.decode()[5:])
        threading.Thread(target=serve_client, args=(self,)).start()


def serve_client(client):
    try:
        with open(client.filename, "rb") as f:
            server_send_ack_request(client.sock, client.addr)                     # Ack the HTTP request
            server_send(client, f)
    except FileNotFoundError:
        sys.stderr.write(f"File not found: {client.filename}\n")
        # Send empty packet to signal file not found
        server_end_connection(client.sock, client.addr, 0)


def run_server(args):
    clients = []
    directory = args.root
    with socket.socket(socket.AF_INET6, socket.SOCK_DGRAM) as sock:
        sock.bind((args.hostname, args.port))
        while True:
            data, addr = sock.recvfrom(RECV_BUFFER_SIZE)
            client_match = [c for c in clients if c.addr == addr]
            if len(client_match) == 0:
                sys.stderr.write(f"New connection from: {addr}\n")
                try:
                    client = Client(sock, addr, data, directory)
                    clients.append(client)
                except ValueError as e:             # Fails when the http request fails (cannot be decoded in Client)
                    sys.stderr.write(f"Nouvelle connexion depuis {addr} ignoree en raison de : {e}\n")
            else:
                # case if client closed connection and then same client requested new connection
                client = client_match[0]

                # dispatch packet to corresponding thread
                client.queue.put(data)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='SRTP client')
    parser.add_argument("hostname", help="Hostname", type=str)
    parser.add_argument("port", help="Path to save file", type=int,)
    parser.add_argument("--root", help="Directory where the file is located on the server", type=str, default=".")
    args = parser.parse_args()

    run_server(args)