import socket
from SRTP import *
import argparse
from urllib.parse import urlparse
from srtp_io import recv_file, now32, send_get, recv_data


def main(argv=None):

    # ARGS : SAVE
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("--save", default="llm.model")
    args = parser.parse_args(argv)
    save_file = args.save
    print("Saving to:", save_file)

    # ARGS : URL
    url = args.url
    u = urlparse(url)
    if u.scheme != "http" or u.hostname is None or u.port is None or u.path is None:
        raise SystemExit("Error: URL must look like http://host:port/path/to/file")
    host = u.hostname
    port = u.port
    path = u.path
    print("From URL:", url)

    # SOCKET
    try:
        sock = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
        sock.connect((host, port, 0, 0))
        sock.settimeout(2.0) # > 2s timer for avoiding infinite wait
    except OSError as e:
        raise SystemExit(f"Error: could not create socket: {e}")
    
    # GET REQUEST

    window_size = 32 # placeholder value for now
    max_tries = 10
    packet1 = None

    for _ in range(max_tries):
        send_get(sock, path, window_size)
        status, pkt = recv_data(sock)
        if status == "ok":
            packet1 = pkt
            break
    if packet1 is None:
        raise SystemExit(f"ERROR: server did not answer after {max_tries} GET attempts")
    
    # RESPONSE
    recv_file(sock, save_file, window_size, packet1)
    sock.close()

if __name__ == "__main__":
    main()