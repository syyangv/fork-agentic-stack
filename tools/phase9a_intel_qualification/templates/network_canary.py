#!/usr/bin/env python3
from __future__ import annotations
import argparse,json,socket,subprocess,sys

def direct() -> list[dict]:
    rows=[]
    for kind,socktype in [('connect',socket.SOCK_STREAM),('sendto',socket.SOCK_DGRAM)]:
        sock=socket.socket(socket.AF_INET,socktype)
        try:
            if kind=='connect': sock.connect(('203.0.113.1',9))
            else: sock.sendto(b'PHASE9A-CANARY',('203.0.113.1',9))
            rows.append({'kind':kind,'denied':False})
        except OSError as exc: rows.append({'kind':kind,'denied':exc.errno==1,'errno':exc.errno})
        finally: sock.close()
    if not all(row['denied'] for row in rows): raise RuntimeError(f'network deny failed: {rows}')
    return rows

def main() -> int:
    ap=argparse.ArgumentParser(); ap.add_argument('--mode',choices=('direct','parent','child'),required=True); args=ap.parse_args()
    if args.mode=='parent':
        subprocess.run([sys.executable,str(__file__),'--mode','child'],check=True)
        print(json.dumps({'parent':True},sort_keys=True)); return 0
    print(json.dumps(direct(),sort_keys=True)); return 0
if __name__=='__main__': raise SystemExit(main())
