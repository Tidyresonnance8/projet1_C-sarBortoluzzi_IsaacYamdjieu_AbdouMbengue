import socket
import sys
import os
import time
from SRTP import SRTPPackets, PTYPE_ACK, PTYPE_DATA, PTYPE_SACK, MAX_PAYLOAD_LENGTH
import argparse
import threading
import queue

MAX_PACKET_SIZE = 4 + 4 + 4 + MAX_PAYLOAD_LENGTH + 4
TIMEOUT = 2

MAX_SEQ = 2048

client_queues: dict = {}
client_queues_lock = threading.Lock()


def window_size(base, next_seq):
    if next_seq >= base:
        return next_seq - base
    return MAX_SEQ - base + next_seq


def handle_ack(ack, last_ack, sent_packets):
    acknum = ack.seqnum

    # Ignore ACKs older than last_ack
    if (acknum - last_ack) % MAX_SEQ > MAX_SEQ // 2:
        return last_ack

    while last_ack != acknum:
        sent_packets.pop(last_ack, None)
        last_ack = (last_ack + 1) % MAX_SEQ

    return last_ack


def send_file(sock, addr, path, window, ack_queue):
    try:
        with open(path, "rb") as f:

            last_ack = 0
            next_seq = 0
            dest_window = window
            sent_packets = {}
            end_of_file = False

            estimated_rtt = TIMEOUT
            alpha = 0.125

            while True:

                # SEND DATA UP TO WINDOW
                while not end_of_file and window_size(last_ack, next_seq) < dest_window:
                    data = f.read(1024)

                    if not data:
                        end_of_file = True
                        print(f"[{addr}] end of file")
                        break
                    
                    packet = SRTPPackets(PTYPE_DATA, 1, next_seq, int(time.time()), data)
                    sock.sendto(packet.encode(), addr)
                    print(f"[{addr[1]}] sending len(payload)={len(data)} seq={next_seq}")

                    sent_packets[next_seq] = packet
                    next_seq = (next_seq + 1) % MAX_SEQ

                if end_of_file and not sent_packets:
                    break

                # WAIT FOR ACK from this client's queue
                try:
                    print(f"[{addr[1]}] Waiting for ACK...")
                    dynamic_timeout = max(0.5, 2 * estimated_rtt)
                    ack = ack_queue.get(timeout=dynamic_timeout)
                    print(f"[{addr[1]}] Received ACK: {ack.seqnum}")

                    if ack.ptype == PTYPE_ACK or ack.ptype == PTYPE_SACK:
                        now = time.time()
                        sample_rtt = now - ack.timestamp
                        estimated_rtt = (1 - alpha) * estimated_rtt + alpha * sample_rtt
                        dest_window = ack.window
                        last_ack = handle_ack(ack, last_ack, sent_packets)

                    # Drain any additional ACKs that arrived in the meantime
                    while True:
                        try:
                            ack = ack_queue.get_nowait()
                            if ack.ptype == PTYPE_ACK or ack.ptype == PTYPE_SACK:
                                now = time.time()
                                sample_rtt = now - ack.timestamp
                                estimated_rtt = (1 - alpha) * estimated_rtt + alpha * sample_rtt
                                dest_window = ack.window
                                last_ack = handle_ack(ack, last_ack, sent_packets)
                        except queue.Empty:
                            break

                except queue.Empty:
                    pass  # Timeout expired, fall through to retransmit check

                # RETRANSMIT LOST PACKETS
                now = time.time()
                for seq, packet in list(sent_packets.items()):
                    if now - packet.timestamp > TIMEOUT:
                        print(f"[{addr[1]}] Retransmitting seq={seq}")
                        sock.sendto(packet.encode(), addr)
                        sent_packets[seq] = packet

            # END OF TRANSFER — send empty packet several times for reliability
            eof_packet = SRTPPackets(PTYPE_DATA, 1, next_seq, int(time.time()), b"")
            for _ in range(5):
                sock.sendto(eof_packet.encode(), addr)
                time.sleep(0.1)

    except FileNotFoundError:
        print(f"[{addr}] File not found: {path}")
        vide = SRTPPackets(PTYPE_DATA, 1, 0, int(time.time()), b"")
        sock.sendto(vide.encode(), addr)


def handle_client(sock, addr, first_packet, root):
    ack_queue = queue.Queue()
    with client_queues_lock:
        client_queues[addr] = ack_queue

    try:
        try:
            request = first_packet.payload.decode("ascii").strip()
        except Exception:
            return

        if not request.startswith("GET "):
            return

        print(f"[{addr}] Request: {request}")

        path = request[4:].lstrip("/")
        final_path = os.path.join(root, path)
        print(f"[{addr}] Serving: {final_path}")

        send_file(sock, addr, final_path, first_packet.window, ack_queue)

    finally:
        with client_queues_lock:
            client_queues.pop(addr, None)
        print(f"[{addr}] Session closed")


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("hostname")
    parser.add_argument("port", type=int)
    parser.add_argument("--root", default=".")
    args = parser.parse_args(argv)

    try:
        sock = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
        sock.bind((args.hostname, args.port))
    except OSError as e:
        raise SystemExit(f"Error: could not create socket: {e}\n")

    print(f"Server listening on {args.hostname}:{args.port}, root={args.root}")

    # SERVER LOOP — receives all packets and dispatches to the right thread
    while True:
        try:
            raw_packet, addr = sock.recvfrom(MAX_PACKET_SIZE)
        except OSError as e:
            sys.stderr.write(f"Error receiving packet: {e}\n")
            continue

        try:
            packet = SRTPPackets.decode(raw_packet)
        except Exception:
            continue

        if packet is None:
            continue

        if packet.ptype == PTYPE_DATA:
            with client_queues_lock:
                known = addr in client_queues

            if not known:
                # New client — spin up a handler thread
                print(f"[{addr}] New client")
                t = threading.Thread(
                    target=handle_client,
                    args=(sock, addr, packet, args.root),
                    daemon=True,
                )
                t.start()
            # Re-sent GETs from an already-active client are ignored

        elif packet.ptype == PTYPE_ACK or packet.ptype == PTYPE_SACK:
            with client_queues_lock:
                ack_queue = client_queues.get(addr)
            if ack_queue is not None:
                ack_queue.put(packet)
            else:
                print(f"[{addr}] ACK for unknown client, discarding")

if __name__ == "__main__":
    main()
