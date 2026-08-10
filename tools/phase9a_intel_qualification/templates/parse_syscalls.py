#!/usr/bin/env python3
from __future__ import annotations
import argparse, json, xml.etree.ElementTree as ET
from pathlib import Path

NETWORK_CALLS = {
    'connect', 'connect_nocancel', 'connectx', 'sendto', 'sendto_nocancel',
    'sendmsg', 'sendmsg_nocancel',
}
NETWORK_DOMAINS = {'0x2', '0x1e'}

def parse_rows(path: Path) -> dict:
    root=ET.parse(path).getroot()
    refs={e.attrib['id']:e.attrib.get('fmt',e.text or '') for e in root.iter() if 'id' in e.attrib}
    def value(e): return e.attrib.get('fmt') or refs.get(e.attrib.get('ref',''),'')
    attempts=[]; total=0
    for row in root.iter('row'):
        call=next((e for e in row if e.tag=='syscall'),None)
        if call is None: continue
        total+=1; name=value(call)
        args=[value(e) for e in row if e.tag=='syscall-arg']
        is_network_socket=name=='socket' and bool(args) and args[0] in NETWORK_DOMAINS
        if name in NETWORK_CALLS or is_network_socket:
            returns=[value(e) for e in row if e.tag=='syscall-return']
            attempts.append({'call':name,'arguments':args[:4],'errno':returns[-1:]})
    return {'schema':'agentic.memory.xctrace-network-syscalls.v1','syscall_count':total,'network_attempts':attempts}

def validate(result: dict, expectation: str) -> None:
    calls=[row['call'] for row in result['network_attempts']]
    if expectation in {'direct-canary','child-canary'}:
        if not {'socket','connect','sendto'}.issubset(calls):
            raise RuntimeError(f'{expectation} coverage incomplete: {calls}')
        denied=[row for row in result['network_attempts'] if row['call'] in {'connect','sendto'}]
        if any(row['errno'] not in (['0x1'],['1']) for row in denied):
            raise RuntimeError(f'{expectation} was not denied with EPERM: {denied}')
    elif result['network_attempts']:
        raise RuntimeError(f'qualification observed network syscalls: {result["network_attempts"]}')
    if result['syscall_count'] <= 0:
        raise RuntimeError('empty syscall export cannot qualify coverage')

def main() -> int:
    ap=argparse.ArgumentParser(); ap.add_argument('--xml',type=Path,required=True); ap.add_argument('--expect',choices=('direct-canary','child-canary','zero'),required=True); ap.add_argument('--output',type=Path,required=True); args=ap.parse_args()
    result=parse_rows(args.xml); validate(result,args.expect); args.output.write_text(json.dumps(result,indent=2,sort_keys=True)+'\n'); return 0
if __name__=='__main__': raise SystemExit(main())
