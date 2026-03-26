import argparse
import sys
import socket
from SRTPClient import SRTPClient


def run_client(args):
    with socket.socket(socket.AF_INET6, socket.SOCK_DGRAM) as sock:
        client = SRTPClient(sock, args.servername)
        if client.is_connected():
            with open(args.save, "wb") as f:
                client.client_receive(f)
                if f.tell() == 0:
                    # No data has been written
                    sys.stderr.write(f"File not found : {f}\n")
                    # Should we delete the file we have created ? -> pour moi non, je vois pas le problème d'avoir un fichier vide
                return
        else:
            error_msg = "No packet received from the server, stop trying\n"
            sys.stderr.write(error_msg)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='SRTP client')
    parser.add_argument("servername", help="Arg like http://hostname:port/path/to/file", type=str)
    parser.add_argument("--save", help="Path to save file", type=str, default="llm.model")
    args = parser.parse_args()
    run_client(args)
