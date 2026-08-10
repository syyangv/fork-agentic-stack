import json, socket, subprocess, sys
if '--spawn-child' in sys.argv:
    completed=subprocess.run([sys.executable,__file__,'--child'],text=True,capture_output=True,check=True)
    print(json.dumps({'parent':True,'child':json.loads(completed.stdout)}))
    raise SystemExit(0)
results=[]
for kind, socktype in [('connect', socket.SOCK_STREAM), ('sendto', socket.SOCK_DGRAM)]:
    s=socket.socket(socket.AF_INET,socktype)
    try:
        if kind=='connect': s.connect(('203.0.113.1', 9))
        else: s.sendto(b'CANARY',('203.0.113.1',9))
        results.append({'kind':kind,'denied':False})
    except OSError as e:
        results.append({'kind':kind,'denied':True,'errno':e.errno})
    finally:s.close()
print(json.dumps(results,sort_keys=True))
assert all(x['denied'] for x in results)
